"""Overnight mode — standing-consent invariants, consumption ledger, morning brief.

The substrate for ``docs/design/notebook-audit.md`` **item 8 (overnight mode)**
and its two amendments (the watch rule / notification leg; the wake leg). A
STANDING CONSENT is the human's own typed utterance accepting the fallout of
letting named boundaries auto-advance while they sleep. It is NOT a new store:
the consent record IS an ``append-decision`` record under the distinct block
:data:`OVERNIGHT_CONSENT_BLOCK` (gated in ``ops/decision/journal.py`` beside the
scope-unlock / notebook-sign-off authorship gates), so it rides the same
utterance-authorship locks. Four invariants ride the record and are enforced
here (:func:`assert_consent_hard_caps`, :func:`assert_wake_armed`) and at
consumption (:func:`standing_consent_status`):

* **hard caps** — an ``expires_at`` (the morning boundary) plus at least one
  resource cap (``budget_cap`` / ``walltime_cap``); a consent with no ceiling is
  refused. Item 8 pin (c).
* **spec-identity binding** — ``cmd_sha`` (the block-drive §4 input-spec identity,
  the SAME token a pre-y is carried under). Consumption recomputes the current
  identity and REFUSES on a mismatch — "consent dies on spec change", item 8 pin
  (b) — reusing the exact mechanism that made run #11's gate refuse to carry a
  pre-y across a regenerated grid.
* **the wake** — recording a consent verifies a harness-TRACKED ``status-watch``
  is armed for the same scope (:func:`status_watch_armed`), else the consent is
  refused-with-remedy: a pre-y with no armed watch is "consent nobody can
  consume" (the wake-leg amendment — "or the whole thing is theater").
* **disclosure** — every boundary auto-advanced under the consent is written to a
  per-scope LEDGER (:func:`record_consumption`) carrying ``failed_at`` (when the
  event happened overnight) so the :func:`overnight_morning_brief` can surface
  ``failed_at`` vs ``surfaced_at`` — the overnight canary death sat undetected;
  that latency MUST be visible (amendment (b)).

The consumption ledger is a SEPARATE jsonl (``<scope>.overnight.jsonl``, the
canonical :func:`hpc_agent.infra.io.append_jsonl_line` seam the look-ledger /
harvest marker / tick log already share) rather than more lines in the y/nudge
decision journal: a code-authored audit event must never flip
``is_latest_committed_greenlight`` or shadow a real greenlight in the block-gate
scan. The CONSENT itself stays in the decision journal (the pin); only the audit
trail lives beside it.

This file lives at the ``ops/`` role root (sibling to ``harness_capabilities.py``
/ ``export_dossier.py``) because it reads across subjects — the detached-watch
lease (``_kernel.lifecycle``), the decision journal (``state``), the harness
capability probe (``ops.harness_capabilities``). The subject-imports lint
short-circuits for role-root files, so the cross-subject reads are allowed by
construction (the ``harness_capabilities.py`` precedent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.state.decision_journal import read_decisions

__all__ = [
    "OVERNIGHT_CONSENT_BLOCK",
    "CONSENT_SCOPE_KINDS",
    "OVERNIGHT_CONSUMABLE_BLOCKS",
    "WAKE_KIND",
    "ConsentDecision",
    "ConsumptionOutcome",
    "status_watch_armed",
    "assert_wake_armed",
    "assert_consent_hard_caps",
    "compose_consent_defaults",
    "arm_consent_wake",
    "compose_overnight_consent",
    "latest_standing_consent",
    "standing_consent_status",
    "assert_standing_consent",
    "is_consumable_boundary",
    "consume_boundary_under_consent",
    "overnight_ledger_path",
    "record_consumption",
    "read_consumption_ledger",
    "consumed_spend",
    "consent_heal_classes",
    "consent_authorizes_class",
    "sever_recurrence_count",
    "notification_plan",
    "overnight_morning_brief",
    "morning_brief_if_any",
    "HEAL_ATTEMPT_KIND",
    "HEAL_FAILED_KIND",
    "ChainStatus",
    "HealOutcome",
    "campaign_chain_last_tick",
    "campaign_chain_status",
    "consent_marked_dead",
    "self_heal_campaign",
    "self_heal_scan",
]

# The block-terminator convention for a STANDING CONSENT. Mirrors the
# ``scope-unlock`` / ``notebook-sign-off`` block conventions: there is NO consent
# verb or chain — an ``append-decision`` under this block is the only write path,
# so the authorship gate (ops/decision/journal.py) is the single choke point that
# a consent cannot be laundered around.
OVERNIGHT_CONSENT_BLOCK = "overnight-consent"

# The block a per-scope consumption-ledger line records (its OWN jsonl, never the
# decision journal — see the module docstring). Code-authored audit events.
OVERNIGHT_CONSUMED_BLOCK = "overnight-consumed"

# A standing consent names a run boundary or a campaign boundary — the two
# "named boundaries while the human sleeps" of item 8. (Notebook/scope/etc.
# touchpoints are synchronous human work, never overnight-consumable.)
CONSENT_SCOPE_KINDS = frozenset({"run", "campaign"})

# The block boundaries a standing consent of each scope kind may auto-advance
# overnight (item 8 seam 1). A boundary NOT named here is NEVER consumed under a
# consent, no matter how live — the two designated overnight boundaries are the
# run's main-array launch (``submit-s3``, after the S2 canary verified) and the
# campaign's anomaly halt (``campaign-watch``'s loud-fail terminator). Every
# other gated boundary (``submit-s2`` stage/canary, ``submit-s4`` harvest,
# ``aggregate-run`` reduce) always parks for a live human — a pre-consent cannot
# launch a canary the human never saw or reduce results they never reviewed.
OVERNIGHT_CONSUMABLE_BLOCKS: dict[str, frozenset[str]] = {
    "run": frozenset({"submit-s3"}),
    "campaign": frozenset({"campaign-watch"}),
}

# The ONLY sanctioned cluster watch (the watch-rule amendment): a harness-tracked
# ``status-watch`` whose terminal re-invokes the driver. A hand-rolled local log
# tail on cluster state is the improvisation class the amendment names.
WAKE_KIND = "status-watch"


@dataclass(frozen=True)
class ConsentDecision:
    """The verdict of consulting a standing consent at a consumption boundary.

    ``live`` is True only when a consent exists, has not expired, its caps are
    not exceeded, AND its spec identity still matches the current spec. ``reason``
    names the failing leg for the refusal message / the morning brief; ``consent``
    is the raw record (or ``None`` when none was recorded).
    """

    live: bool
    reason: str
    consent: dict[str, Any] | None


# ── the wake leg (item 8, second amendment) ───────────────────────────────────


def _watch_lease_path(run_id: str) -> Path:
    """The detached ``status-watch`` lease path for *run_id* (the wake marker).

    The SAME construction ``ops/status_blocks._live_watch_handle`` reads: the
    journal-home ``_detached/<status-watch-key>-<run_id>.lease.json`` a
    backgrounded watch stamps its pid into. Keying off the canonical
    ``terminal_block_key`` derivation keeps this reader from ever drifting from
    the launcher's lease key.
    """
    from hpc_agent.state.block_terminal import terminal_block_key
    from hpc_agent.state.run_record import _current_homedir

    watch_key = terminal_block_key("status-watch")
    return _current_homedir() / "_detached" / f"{watch_key}-{run_id}.lease.json"


def status_watch_armed(run_id: str) -> bool:
    """True when a harness-tracked ``status-watch`` is armed and alive for *run_id*.

    Reads the detached-watch lease (the same store ``wait-detached`` /
    ``_live_watch_handle`` read) and checks the recorded pid is alive. A
    dead / absent / torn lease reads False — no armed wake. This is the CODE
    seat the wake-leg amendment demands: consuming a pre-consent is theater
    unless a harness-tracked wait is armed in the same breath to tick the driver
    on the watch's terminal.

    Pure local read (fail-safe on any lease surprise): a broken lease must read
    "not armed", never raise into the consent-record path.
    """
    import json

    from hpc_agent._kernel.lifecycle.detached import _pid_alive

    try:
        lease = json.loads(_watch_lease_path(run_id).read_text(encoding="utf-8"))
        pid = int(lease.get("pid", -1))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return pid > 0 and _pid_alive(pid)


def assert_wake_armed(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    resolved: dict[str, Any] | None,
) -> None:
    """Refuse a standing consent whose wake is not armed (the wake-leg gate).

    The ``resolved`` block must name the wake — ``resolved["wake"] ==
    {"kind": "status-watch", ...}`` — the ONLY sanctioned cluster watch. For a
    RUN scope the gate additionally verifies the watch is actually armed and
    alive (:func:`status_watch_armed` against ``scope_id`` = the run id): the
    skill arms ``status-watch`` (detach) FIRST, then journals the consent, so
    the lease exists at record time. A campaign scope's wake is its own
    reconcile self-chain (a per-run watch key does not apply), so the token
    presence + kind is required but the per-run liveness probe is skipped (a
    seam documented in the design doc's item-8 SHIPPED note).

    Raises :class:`errors.SpecInvalid` naming ``status-watch`` as the remedy — a
    STRUCTURAL refusal (a fresh human utterance cannot fix a missing watch), so
    it is deliberately NOT the E2 authorship-missing marker.
    """
    wake = resolved.get("wake") if isinstance(resolved, dict) else None
    kind = wake.get("kind") if isinstance(wake, dict) else None
    if kind != WAKE_KIND:
        raise errors.SpecInvalid(
            f"overnight-consent wake gate: a standing consent must name its wake — "
            f"resolved.wake = {{'kind': '{WAKE_KIND}', ...}} — the ONLY sanctioned "
            "cluster watch (a hand-rolled local log tail on cluster state is the "
            "improvisation class). A pre-consent with no armed watch is consent "
            "nobody can consume. Arm the wake in the SAME breath: background "
            f"`status-watch` for this scope, then journal the consent naming it."
        )
    if scope_kind == "run" and not status_watch_armed(scope_id):
        raise errors.SpecInvalid(
            f"overnight-consent wake gate: resolved.wake names '{WAKE_KIND}' but no "
            f"armed, live status-watch lease exists for run {scope_id!r}. Arm the "
            "wake FIRST (background `status-watch` with detach for this run — the "
            "harness-tracked wait whose terminal re-invokes the driver), THEN "
            "record the consent. A pre-y without an armed watch cannot be consumed "
            "(the overnight canary death sat undetected for exactly this reason)."
        )


# ── hard caps (item 8 pin c) + spec-identity binding (pin b) ──────────────────


def assert_consent_hard_caps(resolved: dict[str, Any] | None) -> None:
    """Refuse a standing consent that lacks a ceiling or a spec-identity binding.

    Three requirements ride the record (item 8 pins b + c):

    * ``expires_at`` — a parseable ISO-8601 morning boundary in the FUTURE (an
      already-expired consent is nonsense; a consent with no expiry is unbounded).
    * at least one of ``budget_cap`` / ``walltime_cap`` — a resource ceiling; a
      consent that caps neither spend nor walltime accepts unbounded fallout.
    * ``cmd_sha`` — the block-drive §4 input-spec identity the consent binds to,
      so consumption can refuse on a spec change (:func:`standing_consent_status`).
      (``cmd_sha`` is exempt from the code-derived-field refusal — it is a
      journal-sanctioned identity echo — so it rides ``resolved`` legitimately.)

    Raises :class:`errors.SpecInvalid` naming the missing leg. STRUCTURAL (never
    the authorship-missing marker) — a fresh utterance cannot supply a cap.
    """
    if not isinstance(resolved, dict):
        resolved = {}
    expires_raw = resolved.get("expires_at")
    expires = parse_iso_utc_or_none(expires_raw if isinstance(expires_raw, str) else None)
    if expires is None:
        raise errors.SpecInvalid(
            "overnight-consent caps gate: a standing consent MUST carry "
            "resolved.expires_at — a parseable ISO-8601 morning boundary (the "
            "expires-at-morning hard cap). A consent with no expiry is unbounded."
        )
    from hpc_agent.infra.time import utcnow

    if expires <= utcnow():
        raise errors.SpecInvalid(
            f"overnight-consent caps gate: resolved.expires_at ({expires_raw!r}) is "
            "not in the future — an already-expired consent grants nothing. Set the "
            "morning boundary ahead of now."
        )
    has_budget = _is_positive_number(resolved.get("budget_cap"))
    has_walltime = _is_positive_number(resolved.get("walltime_cap"))
    if not (has_budget or has_walltime):
        raise errors.SpecInvalid(
            "overnight-consent caps gate: a standing consent MUST carry at least one "
            "resource ceiling — resolved.budget_cap and/or resolved.walltime_cap "
            "(a positive number). A consent that caps neither spend nor walltime "
            "accepts unbounded overnight fallout."
        )
    if not (isinstance(resolved.get("cmd_sha"), str) and resolved["cmd_sha"]):
        raise errors.SpecInvalid(
            "overnight-consent identity gate: a standing consent MUST bind to a spec "
            "identity — resolved.cmd_sha (the block-drive §4 input-spec fingerprint). "
            "Consent dies on a spec change; without the binding there is nothing to "
            "compare the morning's spec against (run #11 correctly refused to carry a "
            "pre-y across a regenerated grid)."
        )


def _is_positive_number(val: Any) -> bool:
    """True when *val* is a real, positive, finite number (bool excluded)."""
    import math

    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return math.isfinite(val) and val > 0


def _as_dict(val: Any) -> dict[str, Any]:
    """*val* as a dict when it is one, else ``{}`` (the tolerant-read idiom)."""
    return val if isinstance(val, dict) else {}


# ── the poka-yoke compose seat (item-8 conversions, 2026-07-10) ───────────────
#
# The consent WRITE path composes what code can — the wake (arm the sanctioned
# ``status-watch`` detach) and the cap defaults (a morning-boundary expiry + a
# walltime ceiling) — so the human is handed a complete, editable ``resolved``
# block instead of a refusal. The two gates behind this
# (:func:`assert_wake_armed` / :func:`assert_consent_hard_caps`) stay as
# never-fires ASSERTIONS: a composed block satisfies them, but they still FIRE on
# a directly-constructed block that skips composition (the tests pin both fire
# paths). ``cmd_sha`` is NEVER composed — it is the identity binding, the one
# trust boundary a default cannot stand in for, so its absence stays a refusal.

#: The local morning boundary a composed consent expires at by default: 08:00
#: local time — the "human is presumed awake" line item 8 pin (c) names.
_DEFAULT_MORNING_HOUR = 8


def _next_morning_boundary_utc(now_utc: datetime) -> datetime:
    """The next local ``08:00`` as a UTC datetime (the composed morning boundary).

    Computed in the machine's LOCAL timezone (the human's wall clock is what
    "morning" means), then converted back to UTC for the canonical record. When
    the current local time is already past today's 08:00, the boundary rolls to
    tomorrow — a composed expiry is always in the future (the caps gate refuses a
    past one, so composition must never hand it a stale value).
    """
    local_now = now_utc.astimezone()
    boundary = local_now.replace(hour=_DEFAULT_MORNING_HOUR, minute=0, second=0, microsecond=0)
    if boundary <= local_now:
        boundary += timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def _mark_composed(resolved: dict[str, Any], field: str) -> None:
    """Append *field* to ``resolved["composed_defaults"]`` (idempotent, ordered).

    A composed default MUST be visible in the journal AS composed — the disclosure
    leg of the conversion (a value the human never typed must never masquerade as
    their own). The list is the audit surface the human / a morning brief reads to
    see which fields code filled.
    """
    existing = resolved.get("composed_defaults")
    merged = [str(f) for f in existing] if isinstance(existing, list) else []
    if field not in merged:
        merged.append(field)
    resolved["composed_defaults"] = merged


def compose_consent_defaults(
    resolved: dict[str, Any] | None, *, now_utc: datetime | None = None
) -> dict[str, Any]:
    """Compose the cap defaults a standing consent omits (``expires_at`` + a cap).

    The poka-yoke for item 8 pin (c): rather than REFUSE a capless spec, the write
    path fills a default morning-boundary ``expires_at`` (next local 08:00) and,
    when NEITHER a ``budget_cap`` nor a ``walltime_cap`` is present, a
    ``walltime_cap`` sized to the overnight window (seconds from now to the
    composed expiry — a job must never outlive the morning the consent expires at).
    Every field code fills is disclosed in ``composed_defaults``
    (:func:`_mark_composed`).

    NEVER composes ``cmd_sha`` — the spec-identity binding is a trust boundary a
    default cannot stand in for (item 8 pin b), so its absence stays a refusal at
    :func:`assert_consent_hard_caps`. A field the human already supplied is left
    untouched; only genuine omissions are filled, and an already-PAST ``expires_at``
    is NOT overridden (a real, bad value the caps gate must still catch).
    """
    out: dict[str, Any] = dict(resolved) if isinstance(resolved, dict) else {}
    now = now_utc or _now()

    expires_raw = out.get("expires_at")
    if parse_iso_utc_or_none(expires_raw if isinstance(expires_raw, str) else None) is None:
        boundary = _next_morning_boundary_utc(now)
        out["expires_at"] = boundary.isoformat(timespec="seconds")
        _mark_composed(out, "expires_at")

    has_budget = _is_positive_number(out.get("budget_cap"))
    has_walltime = _is_positive_number(out.get("walltime_cap"))
    if not (has_budget or has_walltime):
        expires = parse_iso_utc_or_none(out.get("expires_at"))
        window = int((expires - now).total_seconds()) if expires is not None else 0
        out["walltime_cap"] = window if window > 0 else _DEFAULT_MORNING_HOUR * 3600
        _mark_composed(out, "walltime_cap")

    return out


def _arm_status_watch(experiment_dir: Path, run_id: str) -> bool:
    """Arm a detached ``status-watch`` for *run_id* (the sanctioned wake), best-effort.

    Reuses the EXACT machinery :func:`self_heal_campaign` re-arms a watcher with —
    ``launch_submit_block_detached`` over a status pipeline spec — so the child
    owns the one cold dial and the single-lease guard dedups a racing/live worker
    (``DetachedLeaseHeld`` reads as "already armed"). Returns True when a live watch
    is (now) armed, False on a spawn failure — in which case the wake gate behind
    the compose seat fires as before (composition is a convenience, never a bypass).
    """
    from hpc_agent._kernel.lifecycle.detached import (
        DetachedLeaseHeld,
        DriveModeError,
        build_status_pipeline_spec,
        launch_submit_block_detached,
    )

    try:
        launch_submit_block_detached(
            verb="status-watch",
            experiment_dir=str(experiment_dir),
            spec=build_status_pipeline_spec({"run_id": run_id}),
        )
    except DetachedLeaseHeld:
        return True  # a live watcher already owns the lease — armed
    except (DriveModeError, OSError):
        return False
    return True


def arm_consent_wake(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    resolved: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compose (and, for a run, ARM) the wake a standing consent needs.

    The poka-yoke for the wake leg: rather than REFUSE a consent whose watch is
    absent, the write path names the wake token and — for a RUN scope — arms the
    detached ``status-watch`` in the same breath (:func:`_arm_status_watch`, the
    ``launch_submit_block_detached`` path the self-heal already uses; the
    single-lease guard dedups). A CAMPAIGN scope keeps its documented seam (its
    wake is the reconcile self-chain — no per-run probe), so only the token is
    composed. A composed wake token is disclosed in ``composed_defaults``.

    :func:`assert_wake_armed` stays behind this as the never-fires assertion (a
    composed+armed block satisfies it; a directly-constructed block that skips this
    seat still fires it — the fire path the tests pin).
    """
    out: dict[str, Any] = dict(resolved) if isinstance(resolved, dict) else {}
    wake = out.get("wake")
    if not (isinstance(wake, dict) and wake.get("kind") == WAKE_KIND):
        key = "run_id" if scope_kind == "run" else "campaign_id"
        out["wake"] = {"kind": WAKE_KIND, key: scope_id}
        _mark_composed(out, "wake")
    if scope_kind == "run" and not status_watch_armed(scope_id):
        _arm_status_watch(experiment_dir, scope_id)
    return out


def compose_overnight_consent(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    resolved: dict[str, Any] | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Compose everything code can for a standing consent, in front of the gates.

    The single entry the consent write path (``ops/decision/journal.py``
    ``append_decision``) calls BEFORE the authorship + caps + wake gates: fill the
    cap defaults (:func:`compose_consent_defaults`) and compose/arm the wake
    (:func:`arm_consent_wake`). Everything composed is disclosed in
    ``composed_defaults``. ``cmd_sha`` is untouched — the one field a default cannot
    stand in for, so a consent that omits it still refuses at the caps gate.
    """
    out = compose_consent_defaults(resolved, now_utc=now_utc)
    out = arm_consent_wake(experiment_dir, scope_kind=scope_kind, scope_id=scope_id, resolved=out)
    return out


# ── consumption: consult the consent, then ledger the auto-advance ────────────


def latest_standing_consent(
    experiment_dir: Path, scope_kind: str, scope_id: str
) -> dict[str, Any] | None:
    """The most recent ``overnight-consent`` record for a scope, or ``None``.

    Scans the scope's decision journal newest-to-oldest for the latest record
    under :data:`OVERNIGHT_CONSENT_BLOCK` (a later unrelated touchpoint must not
    hide a still-live consent — the block-gate scanning idiom). ``None`` when the
    scope has recorded no consent.
    """
    for rec in reversed(read_decisions(experiment_dir, scope_kind, scope_id)):
        if str(rec.get("block") or "") == OVERNIGHT_CONSENT_BLOCK:
            return rec
    return None


def standing_consent_status(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    current_cmd_sha: str,
    now_iso: str | None = None,
    spent_budget: float = 0.0,
    spent_walltime: float = 0.0,
) -> ConsentDecision:
    """Consult a scope's standing consent at a consumption boundary.

    Returns a :class:`ConsentDecision`. ``live`` is True only when EVERY leg
    holds; the first failing leg names ``reason`` (so the caller refuses the
    auto-advance and falls back to a human decision — never a silent proceed):

    * **no consent** — nothing recorded → not live (``reason="no-consent"``).
    * **expired** — ``now >= expires_at`` → not live (``reason="expired"``); the
      morning boundary has passed, so the human is presumed awake.
    * **spec changed** — ``current_cmd_sha`` != the consent's bound ``cmd_sha`` →
      not live (``reason="spec-changed"``). THE reuse of the pre-y cmd_sha gate:
      consent dies on spec change (item 8 pin b).
    * **over cap** — ``spent_budget`` / ``spent_walltime`` exceeds a declared cap
      → not live (``reason="over-budget-cap"`` / ``"over-walltime-cap"``).

    The caller supplies ``current_cmd_sha`` (from the block's input-spec identity
    — ``block_drive._spec_sha`` — or the run sidecar), mirroring how
    ``block_gate.assert_greenlit_target`` takes the ``verb`` it checks: the gate
    stays pure and the caller owns identity derivation.
    """
    consent = latest_standing_consent(experiment_dir, scope_kind, scope_id)
    if consent is None:
        return ConsentDecision(False, "no-consent", None)

    # Overnight self-heal (item 8, 2026-07-09): once a campaign's reconcile chain
    # has been declared unrecoverable (the self-heal cap exhausted / heal was
    # structurally impossible), the standing consent is DEAD — it refuses every
    # further auto-advance so the campaign cannot keep self-chaining on a chain
    # nobody is watching. The flip lives in the consumption ledger, so it survives
    # the consent's own expiry. Run scope is unaffected (no reconcile chain).
    if scope_kind == "campaign" and consent_marked_dead(experiment_dir, scope_kind, scope_id):
        return ConsentDecision(False, "heal-exhausted", consent)

    resolved = _as_dict(consent.get("resolved"))

    now = parse_iso_utc_or_none(now_iso) or _now()
    expires_raw = resolved.get("expires_at")
    expires = parse_iso_utc_or_none(expires_raw if isinstance(expires_raw, str) else None)
    if expires is None or now >= expires:
        return ConsentDecision(False, "expired", consent)

    bound_sha = resolved.get("cmd_sha")
    if not (isinstance(bound_sha, str) and bound_sha) or current_cmd_sha != bound_sha:
        return ConsentDecision(False, "spec-changed", consent)

    budget_cap = resolved.get("budget_cap")
    if _is_positive_number(budget_cap) and spent_budget > float(cast("float", budget_cap)):
        return ConsentDecision(False, "over-budget-cap", consent)
    walltime_cap = resolved.get("walltime_cap")
    if _is_positive_number(walltime_cap) and spent_walltime > float(cast("float", walltime_cap)):
        return ConsentDecision(False, "over-walltime-cap", consent)

    return ConsentDecision(True, "live", consent)


def assert_standing_consent(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    current_cmd_sha: str,
    now_iso: str | None = None,
    spent_budget: float = 0.0,
    spent_walltime: float = 0.0,
) -> dict[str, Any]:
    """Return the live consent for a boundary, or raise if none applies.

    The imperative wrapper over :func:`standing_consent_status` for callers that
    want the boundary to REFUSE (rather than branch) when no live consent covers
    it. Raises :class:`errors.SpecInvalid` naming the failing leg. A caller that
    prefers to fall back to a human decision consults the status directly.
    """
    decision = standing_consent_status(
        experiment_dir,
        scope_kind=scope_kind,
        scope_id=scope_id,
        current_cmd_sha=current_cmd_sha,
        now_iso=now_iso,
        spent_budget=spent_budget,
        spent_walltime=spent_walltime,
    )
    if not decision.live:
        raise errors.SpecInvalid(
            f"no live standing consent for {scope_kind} {scope_id!r} "
            f"({decision.reason}) — surface the boundary for a human decision."
        )
    return decision.consent or {}


def overnight_ledger_path(experiment_dir: Path, scope_kind: str, scope_id: str) -> Path:
    """The per-scope overnight consumption-ledger jsonl path.

    Sits BESIDE the decision journal (``<run_id>.overnight.jsonl`` in the run
    sidecar tree; ``overnight.jsonl`` in the campaign dir) — its own file so a
    code-authored audit line never pollutes the y/nudge journal the block-gate /
    Stop-guard scan. Parent dirs are created idempotently (the ``decisions_path``
    posture).
    """
    if scope_kind not in CONSENT_SCOPE_KINDS:
        raise errors.SpecInvalid(
            f"overnight scope_kind must be one of {sorted(CONSENT_SCOPE_KINDS)}; got {scope_kind!r}"
        )
    if not scope_id or "/" in scope_id or "\\" in scope_id or scope_id in (".", ".."):
        raise errors.SpecInvalid(f"scope_id must be filesystem-safe; got {scope_id!r}")
    if scope_kind == "run":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).runs / f"{scope_id}.overnight.jsonl"
    from hpc_agent.meta.campaign.dirs import campaign_dir

    return campaign_dir(experiment_dir, scope_id) / "overnight.jsonl"


def record_consumption(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    consumed_block: str,
    event_kind: str,
    failed_at: str,
    detail: dict[str, Any] | None = None,
    notification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ledger one boundary auto-advanced under a standing consent (the audit trail).

    Appends one line to :func:`overnight_ledger_path` recording *consumed_block*
    (the boundary that advanced), *event_kind* (``terminal`` / ``anomaly`` — the
    disclosure-latency classes the morning brief separates), and **failed_at**
    (when the event happened overnight — the timestamp whose gap from
    ``surfaced_at`` the morning brief makes visible). *notification* records the
    push decision at consumption time (:func:`notification_plan`) so a
    disclosure GAP is itself part of the consented, journaled fallout.

    ``recorded_at`` is auto-stamped. Returns the written line.
    """
    line: dict[str, Any] = {
        "block": OVERNIGHT_CONSUMED_BLOCK,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "consumed_block": consumed_block,
        "event_kind": event_kind,
        "failed_at": failed_at,
        "recorded_at": utcnow_iso(),
        "detail": dict(detail) if detail else {},
        "notification": dict(notification) if notification else {},
    }
    append_jsonl_line(overnight_ledger_path(experiment_dir, scope_kind, scope_id), line)
    return line


def read_consumption_ledger(
    experiment_dir: Path, scope_kind: str, scope_id: str
) -> list[dict[str, Any]]:
    """Every consumption-ledger line for a scope, in append order (``[]`` if none)."""
    import json
    import logging

    path = overnight_ledger_path(experiment_dir, scope_kind, scope_id)
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    except (OSError, UnicodeDecodeError) as exc:
        logging.getLogger(__name__).warning("overnight ledger unreadable %s (%s)", path, exc)
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ── the spend meter (sequencing item 1: meter before the first B heal) ────────


def consumed_spend(experiment_dir: Path, scope_kind: str, scope_id: str) -> tuple[float, float]:
    """Sum the ``(budget, walltime)`` a scope has ALREADY consumed under its consent.

    The spend meter the overnight-repair sequencing puts FIRST (§8 item 1): every
    consumption-ledger line MAY carry ``detail.spent_budget`` / ``detail.spent_walltime``
    (a boundary that launched a real array, a B heal that resubmitted tasks). This
    sums them so :func:`standing_consent_status` can be consulted with the REAL
    running total instead of the ``0.0`` placeholder the callers passed before a B
    heal made the meter load-bearing. Non-numeric / missing costs count as zero (a
    disclosed no-op line spends nothing); fail-safe by construction — a torn ledger
    line contributes nothing rather than raising.

    Returns ``(spent_budget, spent_walltime)``. HEAL_FAILED lines are excluded (a
    fail-loud flip is bookkeeping, not spend); HEAL_ATTEMPT lines ARE counted when
    they carry a cost (a respawn that burned walltime is real fallout).
    """
    spent_budget = 0.0
    spent_walltime = 0.0
    for line in read_consumption_ledger(experiment_dir, scope_kind, scope_id):
        if str(line.get("event_kind") or "") == HEAL_FAILED_KIND:
            continue
        detail = _as_dict(line.get("detail"))
        b = detail.get("spent_budget")
        w = detail.get("spent_walltime")
        if _is_positive_number(b):
            spent_budget += float(cast("float", b))
        if _is_positive_number(w):
            spent_walltime += float(cast("float", w))
    return spent_budget, spent_walltime


# ── declared heal-class cap (§7.2 rule 4) ─────────────────────────────────────


def consent_heal_classes(consent: dict[str, Any] | None) -> set[str]:
    """The repair classes a standing consent DECLARES it authorizes (``resolved.heal_classes``).

    A consent that names no classes authorizes NOTHING beyond the shipped watcher
    re-arm (the Class-A ``self_heal_campaign`` reference posture, which predates the
    taxonomy and rides the consent's mere existence). The set is drawn verbatim from
    the bound record's declared classes; the class-authorization gate
    (:func:`consent_authorizes_class`) reads it, and the bound-capture gate already
    verified the human's typed consent COVERS at least these classes.
    """
    resolved = _as_dict(consent.get("resolved")) if isinstance(consent, dict) else {}
    raw = resolved.get("heal_classes")
    if not isinstance(raw, list):
        return set()
    return {str(c) for c in raw if isinstance(c, str)}


def consent_authorizes_class(consent: dict[str, Any] | None, heal_class: str) -> bool:
    """True when *consent* declares it authorizes healing *heal_class* (§7.2 rule 4).

    The consumption-side companion to the write-time bound-capture gate: a heal of
    class X may proceed under a consent only when X is in ``resolved.heal_classes``.
    Class C is NEVER authorized here regardless of the declared set (the §7.3 hard
    "never heal a Class C" — C1 elicits, C2 reports; neither is an autonomous heal),
    so ``C1`` / ``C2`` always return False even if a malformed consent lists them.
    """
    if heal_class in ("C1", "C2"):
        return False
    return heal_class in consent_heal_classes(consent)


# ── recurrence escalation (§9 RULED 2026-07-12: a sever recurring escalates) ──


def sever_recurrence_count(
    experiment_dir: Path, scope_kind: str, scope_id: str, *, heal_class: str = "A"
) -> int:
    """How many times a heal of *heal_class* has been RE-ARMED for this scope.

    Counts the ``respawned`` heal-attempt lines carrying ``detail.heal_class ==
    heal_class`` (default ``A`` — the severed-connection / watcher-re-arm class).
    The escalation ruling (§9): with correct keepalives a SINGLE sever is Class A
    (re-arm), but a sever that RECURS under correct keepalives is no longer a
    mechanical fault to re-heal — it is a cluster-social signal that routes to a
    C-style finding. A caller that sees this counter exceed its escalation
    threshold stops re-arming and reports (the taxonomy module's
    ``escalate_if_recurring`` owns the threshold + the routing).
    """
    n = 0
    for line in read_consumption_ledger(experiment_dir, scope_kind, scope_id):
        if str(line.get("event_kind") or "") != HEAL_ATTEMPT_KIND:
            continue
        detail = _as_dict(line.get("detail"))
        if str(detail.get("outcome") or "") != "respawned":
            continue
        if str(detail.get("heal_class") or "A") == heal_class:
            n += 1
    return n


# ── the wiring seam: consult-and-consume a named boundary (item 8 seam 1) ─────


@dataclass(frozen=True)
class ConsumptionOutcome:
    """The result of consulting a standing consent AT an auto-advanceable boundary.

    ``consumed`` is True only when the boundary is an :data:`OVERNIGHT_CONSUMABLE_BLOCKS`
    member for the scope AND a live consent covers it — in which case the auto-advance
    was recorded to the consumption ledger in the SAME breath (``line`` is the written
    record, or ``None`` when an equal consumption was already ledgered — the idempotent
    re-tick / gate-replay case). ``decision`` is the underlying :class:`ConsentDecision`;
    ``decision.reason`` names the refusal leg for the park brief when ``consumed`` is
    False.
    """

    consumed: bool
    decision: ConsentDecision
    line: dict[str, Any] | None


def is_consumable_boundary(scope_kind: str, boundary_block: str) -> bool:
    """True when *boundary_block* is a consent-auto-advanceable boundary for the scope.

    The SoT for "NEVER auto-advance a boundary whose block isn't named in the
    consent's scope" (item 8 seam 1): the two designated overnight boundaries are
    :data:`OVERNIGHT_CONSUMABLE_BLOCKS`. Every other boundary parks for a live human
    regardless of any consent.
    """
    return boundary_block in OVERNIGHT_CONSUMABLE_BLOCKS.get(scope_kind, frozenset())


def _already_consumed(
    experiment_dir: Path,
    scope_kind: str,
    scope_id: str,
    boundary_block: str,
    bound_cmd_sha: str,
) -> bool:
    """True when this boundary was already ledgered under the SAME consent identity.

    The idempotency key is ``(consumed_block, detail.cmd_sha)``: a boundary is
    consumed exactly once per spec identity. This keeps re-consulting the consent
    safe — the driver's park site and the gated block's own gate both funnel here,
    and a later re-tick / a detached-block replay re-enters the same boundary — so a
    duplicate audit line (which would double-count in the morning brief) is never
    written for one real auto-advance.
    """
    for line in read_consumption_ledger(experiment_dir, scope_kind, scope_id):
        if str(line.get("consumed_block") or "") != boundary_block:
            continue
        if str(_as_dict(line.get("detail")).get("cmd_sha") or "") == bound_cmd_sha:
            return True
    return False


def consume_boundary_under_consent(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    boundary_block: str,
    current_cmd_sha: str,
    event_kind: str = "auto-advance",
    failed_at: str | None = None,
    detail: dict[str, Any] | None = None,
    now_iso: str | None = None,
    spent_budget: float | None = None,
    spent_walltime: float | None = None,
) -> ConsumptionOutcome:
    """Consult a scope's standing consent at *boundary_block*; ledger the auto-advance.

    The ONE place a boundary is consumed under a standing consent (item 8 seam 1),
    reusing the substrate's own checks — :func:`standing_consent_status` for liveness
    (never re-derived inline, the one-definition rule) and :func:`record_consumption`
    for the ledger. Returns a :class:`ConsumptionOutcome`:

    * boundary NOT named for the scope → ``consumed=False``,
      ``reason="boundary-not-consumable"`` — the caller PARKS (a live consent for a
      DIFFERENT boundary never launches this one).
    * no live consent (expired / over-cap / spec-changed / no consent) →
      ``consumed=False`` carrying the failing leg — the caller PARKS with the reason
      in the brief.
    * a live consent covers the boundary → ``consumed=True``; the auto-advance is
      recorded (``failed_at`` = when it happened, defaulting to now) with the current
      push :func:`notification_plan` so a disclosure GAP is itself journaled. Recording
      is idempotent per spec identity (:func:`_already_consumed`).

    The caller supplies ``current_cmd_sha`` (the run sidecar fingerprint / the campaign
    spec identity), mirroring how :func:`standing_consent_status` delegates identity
    derivation. ``spent_budget`` / ``spent_walltime`` default to ``None`` → the SPEND
    METER (:func:`consumed_spend`) supplies the running total from the ledger (§8 item
    1), so the budget/walltime caps are enforced against real consumption; a caller
    that already knows the total passes it explicitly.
    """
    if not is_consumable_boundary(scope_kind, boundary_block):
        return ConsumptionOutcome(
            False, ConsentDecision(False, "boundary-not-consumable", None), None
        )
    if spent_budget is None or spent_walltime is None:
        metered_budget, metered_walltime = consumed_spend(experiment_dir, scope_kind, scope_id)
        spent_budget = metered_budget if spent_budget is None else spent_budget
        spent_walltime = metered_walltime if spent_walltime is None else spent_walltime
    decision = standing_consent_status(
        experiment_dir,
        scope_kind=scope_kind,
        scope_id=scope_id,
        current_cmd_sha=current_cmd_sha,
        now_iso=now_iso,
        spent_budget=spent_budget,
        spent_walltime=spent_walltime,
    )
    if not decision.live:
        return ConsumptionOutcome(False, decision, None)
    stamped_detail: dict[str, Any] = {"cmd_sha": current_cmd_sha, **(detail or {})}
    if _already_consumed(experiment_dir, scope_kind, scope_id, boundary_block, current_cmd_sha):
        # Already ledgered for this identity — a live consent still authorizes the
        # advance, but a second audit line would double-count in the morning brief.
        return ConsumptionOutcome(True, decision, None)
    line = record_consumption(
        experiment_dir,
        scope_kind=scope_kind,
        scope_id=scope_id,
        consumed_block=boundary_block,
        event_kind=event_kind,
        failed_at=failed_at or now_iso or utcnow_iso(),
        detail=stamped_detail,
        notification=notification_plan(experiment_dir),
    )
    return ConsumptionOutcome(True, decision, line)


# ── the notification leg (item 8, first amendment) ────────────────────────────


def notification_plan(experiment_dir: Path) -> dict[str, Any]:
    """Whether a push channel can disclose an overnight event, and the gap if not.

    Consults ``harness-capabilities`` — the SAME capability negotiation the
    authorship tier reads — for the watchdog alert-DELIVERY hook (the push seat:
    "detection without delivery is silence"). When present, terminal/anomaly under
    standing consent can be PUSHED overnight; when absent, disclosure defers to the
    next turn / the morning brief, and that latency is the recorded ``gap`` — part
    of the consented fallout, never a silent surprise.

    Fail-open: a capability-probe failure degrades to "no push, gap recorded",
    never an exception into the consumption path.
    """
    push_available = False
    try:
        from hpc_agent.ops.harness_capabilities import harness_capabilities

        result = harness_capabilities(experiment_dir=experiment_dir)
        backgrounding = result.capabilities.get("backgrounding")
        evidence = getattr(backgrounding, "evidence", {}) or {}
        push_available = bool(evidence.get("watchdog_alert"))
    except Exception:  # noqa: BLE001 — a probe failure must not wedge consumption
        push_available = False
    if push_available:
        return {
            "push_available": True,
            "channel": "watchdog alert-delivery hook",
            "gap": None,
        }
    return {
        "push_available": False,
        "channel": None,
        "gap": (
            "no push channel (watchdog alert-delivery hook absent) — an overnight "
            "terminal/anomaly is not disclosed until the next turn / the morning "
            "brief; this disclosure latency is part of the consented fallout."
        ),
    }


# ── campaign self-heal (item 8 overnight self-heal ruling, 2026-07-09) ─────────
#
# SUPERSEDES the "defer the reconcile-tick-recency liveness marker to run #12"
# note recorded in docs/design/notebook-audit.md item 8. USER RULING: when the
# human is asleep overnight they cannot give consent, so a live standing consent
# must SELF-HEAL a dead reconcile chain with a BOUNDED number of trusted
# robustness attempts, then FAIL LOUD so the human is notified on waking.
#
# Two hard rules bound this substrate:
#   * ZERO unattended cold-SSH — the heal check reads only LOCAL state (the
#     cursor / consumption ledger / decision journal / detached-worker leases);
#     it NEVER opens SSH. A heal RESTORES the watcher by spawning a detached
#     `status-watch` worker (the sanctioned wake — the child owns the one cold
#     dial, exactly as the detach-by-contract path does), so the healer process
#     itself never dials.
#   * OBSERVE / JUDGE / ROUTE, never ACTUATE — the heal re-arms the WATCHER only.
#     It spawns `status-watch` (a pure monitor poll whose terminal re-invokes the
#     driver), NEVER `campaign-run` (which could re-submit) or any qdel/qsub. No
#     cluster action of any kind rides the heal path.

#: The reconcile-chain tick cadence the liveness marker measures recency against
#: when the consent names none — the driver default tick cadence (drive.py
#: `_DEFAULT_DRIVER_TICK_CADENCE_SECONDS`), duplicated as a plain literal to avoid
#: importing the lifecycle layer into this ops role-root at module load.
_DEFAULT_CHAIN_TICK_SECONDS = 900.0

#: N — a chain is DEAD when its last tick is older than N × the expected tick
#: interval. Sane default: three missed ticks (a single slow iteration gets slack;
#: three in a row means the self-chain stopped). Overridable per consent.
_DEFAULT_DEAD_CHAIN_MULTIPLE = 3

#: How many trusted respawn attempts a standing consent authorises before the
#: chain is declared unrecoverable and the consent flips DEAD. Overridable per
#: consent via ``resolved.heal_attempts_cap``.
_DEFAULT_HEAL_ATTEMPTS_CAP = 3

#: Ledger ``event_kind`` for one journaled heal attempt (the audit trail — an
#: unrecorded heal is the laundering class). ``detail.outcome`` is one of
#: ``respawned`` / ``noop-lease-held`` / ``spawn-failed`` / ``structurally-impossible``.
HEAL_ATTEMPT_KIND = "heal-attempt"

#: Ledger ``event_kind`` for the terminal FAIL-LOUD flip: the chain could not be
#: revived within the cap (or heal was structurally impossible). Its presence is
#: what makes :func:`standing_consent_status` refuse the campaign consent from
#: then on. Survives consent expiry (it lives in the ledger, not the consent).
HEAL_FAILED_KIND = "heal-failed"


@dataclass(frozen=True)
class ChainStatus:
    """The liveness verdict for a campaign's reconcile self-chain (local read).

    ``live`` is True when a detached reconcile worker is alive OR the last tick is
    recent enough. ``reason`` is one of ``live-worker`` / ``recent-tick`` /
    ``dead-chain`` / ``no-tick-record``. ``last_tick_at`` is the freshest local
    recency signal (or ``None``); ``age_seconds`` is now − that (or ``None`` when
    no tick was ever recorded); ``threshold_seconds`` is N × the expected tick
    interval — the age past which the chain reads dead.
    """

    live: bool
    reason: str
    last_tick_at: str | None
    age_seconds: float | None
    threshold_seconds: float


@dataclass(frozen=True)
class HealOutcome:
    """The result of one self-heal consultation for a campaign scope.

    ``status`` names what happened: ``respawned`` (a watcher was re-armed),
    ``chain-live-noop`` (the chain read live — a disclosed no-op, no spawn),
    ``exhausted`` / ``structurally-impossible`` (the FAIL-LOUD flip fired),
    ``already-dead`` (the consent was already flipped — idempotent no-op),
    ``no-consent`` / ``consent-not-live`` (nothing to heal under). ``chain`` is
    the :class:`ChainStatus` consulted; ``attempt_line`` is the journaled ledger
    line (or ``None``); ``notification`` is the delivery record when a FAIL-LOUD
    push fired.
    """

    status: str
    reason: str
    chain: ChainStatus | None = None
    attempt_line: dict[str, Any] | None = None
    notification: dict[str, Any] | None = None


def _argv_flag_value(argv: Any, flag: str) -> str | None:
    """The value following *flag* in a detached lease's stamped ``argv``, or ``None``."""
    if not isinstance(argv, list):
        return None
    for name, value in zip(argv, argv[1:], strict=False):
        if name == flag and isinstance(value, str) and value:
            return value
    return None


def _same_path(a: Path, b: Path) -> bool:
    """Symlink/relative-tolerant path equality (``resolve()`` both sides, fail-safe)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _iter_campaign_run_leases(
    experiment_dir: Path, campaign_id: str
) -> list[tuple[dict[str, Any], str, float]]:
    """``(lease, run_id, mtime)`` for THIS campaign+experiment's ``campaign-run`` leases.

    Purely local, fail-open per lease: the detached ``campaign-run`` worker (the
    reconcile iteration worker) stamps ``_detached/campaign-run-<run_id>.lease.json``
    with ``run_id``/``pid``/``argv``; the argv carries ``--experiment-dir`` (scope)
    and ``--spec <path>`` (whose CampaignRunSpec carries ``campaign_id``). A lease
    is included only when both match — the ``_detached/`` dir is GLOBAL across
    experiments and campaigns, so an unscoped read would cross wires. Any unreadable
    / torn / foreign lease is skipped, never raised.
    """
    from hpc_agent.state.run_record import _current_homedir

    detached_dir = _current_homedir() / "_detached"
    out: list[tuple[dict[str, Any], str, float]] = []
    if not detached_dir.is_dir():
        return out
    import json as _json

    for lease_path in sorted(detached_dir.glob("campaign-run-*.lease.json")):
        try:
            lease = _json.loads(lease_path.read_text(encoding="utf-8"))
            run_id = lease["run_id"]
            argv = lease.get("argv")
        except (OSError, ValueError, TypeError, KeyError, _json.JSONDecodeError):
            continue
        if not (isinstance(run_id, str) and run_id):
            continue
        exp = _argv_flag_value(argv, "--experiment-dir")
        if exp is None or not _same_path(Path(exp), experiment_dir):
            continue
        spec_path = _argv_flag_value(argv, "--spec")
        if spec_path is None:
            continue
        try:
            spec = _json.loads(Path(spec_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, _json.JSONDecodeError):
            continue
        if str((spec or {}).get("campaign_id") or "") != campaign_id:
            continue
        try:
            mtime = lease_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append((lease, run_id, mtime))
    return out


def _campaign_live_detached_worker(experiment_dir: Path, campaign_id: str) -> bool:
    """True when a live detached ``campaign-run`` worker owns THIS campaign's chain.

    The "ticking right now" signal: a long single iteration bumps no cursor, so a
    live worker must count as a live chain (else the recency read would falsely
    declare a slow-but-healthy iteration dead). Pure local: reads the lease pid and
    checks it is alive (the SAME probe the single-lease guard uses).
    """
    from hpc_agent._kernel.lifecycle.detached import _pid_alive

    for lease, _run_id, _mtime in _iter_campaign_run_leases(experiment_dir, campaign_id):
        try:
            pid = int(lease.get("pid", -1))
        except (TypeError, ValueError):
            continue
        if pid > 0 and _pid_alive(pid):
            return True
    return False


def _campaign_inflight_run_id(experiment_dir: Path, campaign_id: str) -> str | None:
    """The run id of the campaign's most recent iteration (freshest lease), or ``None``.

    The watcher target for a heal: re-arm ``status-watch`` on the current
    iteration's run. Read purely from local leases (no cluster probe). ``None``
    when the chain has spawned no detached iteration worker yet — heal is then
    structurally impossible (there is no in-flight run to watch).
    """
    best: str | None = None
    best_mtime = float("-inf")
    for _lease, run_id, mtime in _iter_campaign_run_leases(experiment_dir, campaign_id):
        if mtime > best_mtime:
            best_mtime = mtime
            best = run_id
    return best


def campaign_chain_last_tick(experiment_dir: Path, campaign_id: str) -> str | None:
    """The freshest LOCAL recency signal for a campaign's reconcile chain, or ``None``.

    Reads only local state — never the cluster — and returns the newest ISO-8601
    timestamp among the signals a healthy self-chain refreshes as it WORKS:

    * the campaign cursor's ``updated_at`` (bumped on each iteration advance);
    * the newest consumption-ledger ``recorded_at`` for a real BOUNDARY advance
      (an overnight anomaly auto-advance is chain activity).

    Deliberately EXCLUDED: the standing-consent grant record (that is the human
    granting consent at bedtime, NOT the chain ticking — counting it would make a
    dead chain read live all night) and the heal-attempt / heal-failed ledger
    lines (those are the healer acting, not the chain — counting them would make
    each heal falsely revive the recency and starve the cap).

    ``None`` only when NO real tick exists — a campaign that never advanced at all.
    """
    candidates: list[Any] = []

    try:
        from hpc_agent.meta.campaign.cursor import read_cursor

        cursor = read_cursor(experiment_dir, campaign_id)
        if isinstance(cursor, dict):
            candidates.append(cursor.get("updated_at"))
    except Exception:  # noqa: BLE001 — a bad cursor must not blind the liveness read
        pass

    for line in read_consumption_ledger(experiment_dir, "campaign", campaign_id):
        if str(line.get("event_kind") or "") in {HEAL_ATTEMPT_KIND, HEAL_FAILED_KIND}:
            continue  # healer activity is not a chain tick
        candidates.append(line.get("recorded_at"))

    best_raw: str | None = None
    best_dt = None
    for raw in candidates:
        if not isinstance(raw, str) or not raw:
            continue
        dt = parse_iso_utc_or_none(raw)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_raw = raw
    return best_raw


def campaign_chain_status(
    experiment_dir: Path,
    *,
    campaign_id: str,
    now_iso: str | None = None,
    expected_tick_seconds: float | None = None,
    dead_after_multiple: int | None = None,
) -> ChainStatus:
    """Liveness of a campaign's reconcile self-chain, read from LOCAL state only.

    The item-8 self-heal liveness marker (this ruling closes the gap where the
    campaign wake probe was SKIPPED). A live detached iteration worker reads live
    unconditionally (``live-worker``). Otherwise the freshest local tick
    (:func:`campaign_chain_last_tick`) is aged against ``threshold =
    expected_tick_seconds × dead_after_multiple``: newer ⇒ ``recent-tick`` (live),
    older ⇒ ``dead-chain`` (dead). No tick ever recorded ⇒ ``no-tick-record``
    (dead — the chain never started). Never opens SSH.
    """
    tick = expected_tick_seconds if (expected_tick_seconds and expected_tick_seconds > 0) else None
    tick_s = tick if tick is not None else _DEFAULT_CHAIN_TICK_SECONDS
    mult = dead_after_multiple if (dead_after_multiple and dead_after_multiple > 0) else None
    mult_n = mult if mult is not None else _DEFAULT_DEAD_CHAIN_MULTIPLE
    threshold = tick_s * mult_n

    if _campaign_live_detached_worker(experiment_dir, campaign_id):
        return ChainStatus(True, "live-worker", None, 0.0, threshold)

    last = campaign_chain_last_tick(experiment_dir, campaign_id)
    if last is None:
        return ChainStatus(False, "no-tick-record", None, None, threshold)

    now = parse_iso_utc_or_none(now_iso) or _now()
    last_dt = parse_iso_utc_or_none(last)
    if last_dt is None:
        return ChainStatus(False, "no-tick-record", last, None, threshold)
    age = (now - last_dt).total_seconds()
    if age > threshold:
        return ChainStatus(False, "dead-chain", last, age, threshold)
    return ChainStatus(True, "recent-tick", last, age, threshold)


def consent_marked_dead(experiment_dir: Path, scope_kind: str, scope_id: str) -> bool:
    """True when a scope's consent has been flipped DEAD by an exhausted self-heal.

    Reads the consumption ledger for a terminal :data:`HEAL_FAILED_KIND` line. Its
    presence makes :func:`standing_consent_status` refuse the consent from then on,
    and it OUTLIVES the consent (the ledger is separate from the decision journal),
    so the fail-loud disclosure never evaporates when the grant expires.
    """
    for line in read_consumption_ledger(experiment_dir, scope_kind, scope_id):
        if str(line.get("event_kind") or "") == HEAL_FAILED_KIND:
            return True
    return False


def _heal_respawn_count(experiment_dir: Path, campaign_id: str) -> int:
    """How many REAL heal attempts have been journaled against the cap.

    An ``outcome=respawned`` (a watcher was launched) OR an
    ``outcome=spawn-failed`` (the launch itself failed) heal-attempt line counts
    toward the cap: a deterministically-failing spawn (persistent OSError /
    DriveModeError) must exhaust the bounded budget and flip the consent DEAD,
    not retry forever. Only a disclosed no-op (``outcome=noop-lease-held``, a
    live-lease-held attempt) is exempt — it is not a spent attempt.
    """
    n = 0
    for line in read_consumption_ledger(experiment_dir, "campaign", campaign_id):
        if str(line.get("event_kind") or "") != HEAL_ATTEMPT_KIND:
            continue
        if str(_as_dict(line.get("detail")).get("outcome") or "") in ("respawned", "spawn-failed"):
            n += 1
    return n


def _record_heal_attempt(
    experiment_dir: Path,
    *,
    campaign_id: str,
    outcome: str,
    now_iso: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Journal one heal attempt to the consumption ledger (the audit trail)."""
    return record_consumption(
        experiment_dir,
        scope_kind="campaign",
        scope_id=campaign_id,
        consumed_block="campaign-watch",
        event_kind=HEAL_ATTEMPT_KIND,
        failed_at=now_iso,
        detail={"outcome": outcome, **(detail or {})},
        notification=notification_plan(experiment_dir),
    )


def _mark_consent_dead(
    experiment_dir: Path,
    *,
    campaign_id: str,
    failed_at: str,
    reason: str,
    attempts: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flip a campaign consent DEAD and FIRE the fail-loud notification.

    Journals the terminal :data:`HEAL_FAILED_KIND` line (so the consent refuses
    from then on and the morning brief leads with the failure) and delivers a push
    where ``harness-capabilities`` declares the alert-delivery hook
    (:func:`notification_plan`); when no hook is present the disclosure GAP is
    recorded on the line and surfaces in the morning brief instead. Returns
    ``(ledger_line, delivery_record)``.
    """
    plan = notification_plan(experiment_dir)
    text = (
        f"hpc-agent overnight self-heal FAILED for campaign {campaign_id!r}: the "
        f"reconcile chain could not be revived ({reason}) after {attempts} attempt(s); "
        "the standing consent is now DEAD — no further auto-advance. Review the "
        "morning brief."
    )
    delivery: dict[str, Any] = {"push_available": bool(plan.get("push_available")), "fired": False}
    if plan.get("push_available"):
        try:
            from hpc_agent.ops.recover.notify import raise_alert_notification

            record = raise_alert_notification(text, experiment_dir=experiment_dir)
            delivery = {"push_available": True, "fired": True, **record}
        except Exception:  # noqa: BLE001 — a delivery failure must not wedge the flip
            delivery = {"push_available": True, "fired": False}
    line = record_consumption(
        experiment_dir,
        scope_kind="campaign",
        scope_id=campaign_id,
        consumed_block="campaign-watch",
        event_kind=HEAL_FAILED_KIND,
        failed_at=failed_at,
        detail={"reason": reason, "attempts": attempts, "text": text},
        notification={**plan, "delivery": delivery},
    )
    return line, delivery


def self_heal_campaign(
    experiment_dir: Path,
    *,
    campaign_id: str,
    now_iso: str | None = None,
) -> HealOutcome:
    """Consult a campaign's standing consent and self-heal a dead reconcile chain.

    The item-8 overnight self-heal (2026-07-09). Under a LIVE campaign standing
    consent, a dead chain earns a BOUNDED, journaled respawn of the sanctioned
    WATCHER; the cap exhausted (or heal structurally impossible) flips the consent
    DEAD and fails loud. The full decision, in order:

    * no consent / consent already expired / spec changed → nothing to heal under
      (``no-consent`` / ``consent-not-live``): the human is presumed reachable, or
      the spec moved (consent already dies on that leg);
    * consent already flipped DEAD → ``already-dead`` (idempotent no-op);
    * chain reads LIVE → ``chain-live-noop`` (a disclosed no-op — NO spawn, so a
      healthy or slow-but-live iteration is never disturbed and never
      double-spawned);
    * chain DEAD, respawn cap already reached → FAIL LOUD (``exhausted``): flip the
      consent dead, record ``failed_at``, fire the push / record the gap;
    * chain DEAD, no in-flight run to watch → FAIL LOUD
      (``structurally-impossible``);
    * chain DEAD, under cap → respawn a detached ``status-watch`` on the current
      iteration's run (reusing ``launch_submit_block_detached`` — the SAME machinery
      that started the chain's watchers; the single-lease guard makes a respawn
      against an actually-live worker a disclosed ``noop-lease-held``, never a
      duplicate) and journal the attempt (``respawned``).

    Zero SSH in this process: the healer only reads local state, journals, and
    SPAWNS the detached watcher (whose child owns the one cold dial). It re-arms the
    watcher only — never ``campaign-run``, never any scheduler action.
    """
    now = now_iso or utcnow_iso()

    consent = latest_standing_consent(experiment_dir, "campaign", campaign_id)
    if consent is None:
        return HealOutcome("no-consent", "no standing consent for this campaign")

    if consent_marked_dead(experiment_dir, "campaign", campaign_id):
        return HealOutcome("already-dead", "consent already flipped dead by a prior heal")

    resolved = _as_dict(consent.get("resolved"))

    # Consent liveness (expiry / spec-identity) — reuse the substrate's own check
    # against the campaign's greenlit-spec identity. An expired / spec-changed
    # consent is NOT healed: the morning boundary passed (human reachable) or the
    # spec moved (the consent already died on that leg).
    identity = ""
    try:
        from hpc_agent.meta.campaign.blocks import _campaign_spec_identity
        from hpc_agent.meta.campaign.manifest import read_manifest

        identity = _campaign_spec_identity(read_manifest(experiment_dir, campaign_id=campaign_id))
    except Exception:  # noqa: BLE001 — a manifest read failure must not crash the heal
        identity = ""
    metered_budget, metered_walltime = consumed_spend(experiment_dir, "campaign", campaign_id)
    decision = standing_consent_status(
        experiment_dir,
        scope_kind="campaign",
        scope_id=campaign_id,
        current_cmd_sha=identity,
        now_iso=now,
        spent_budget=metered_budget,
        spent_walltime=metered_walltime,
    )
    if not decision.live:
        return HealOutcome("consent-not-live", decision.reason)

    tick_s = resolved.get("chain_tick_seconds")
    mult = resolved.get("dead_chain_multiple")
    chain = campaign_chain_status(
        experiment_dir,
        campaign_id=campaign_id,
        now_iso=now,
        expected_tick_seconds=tick_s if isinstance(tick_s, (int, float)) else None,
        dead_after_multiple=mult if isinstance(mult, int) and not isinstance(mult, bool) else None,
    )
    if chain.live:
        return HealOutcome("chain-live-noop", chain.reason, chain=chain)

    cap_raw = resolved.get("heal_attempts_cap")
    cap = (
        cap_raw
        if isinstance(cap_raw, int) and not isinstance(cap_raw, bool) and cap_raw > 0
        else _DEFAULT_HEAL_ATTEMPTS_CAP
    )
    attempts = _heal_respawn_count(experiment_dir, campaign_id)
    if attempts >= cap:
        line, delivery = _mark_consent_dead(
            experiment_dir,
            campaign_id=campaign_id,
            failed_at=now,
            reason=f"heal cap reached ({attempts}/{cap} heal attempts did not revive the chain)",
            attempts=attempts,
        )
        return HealOutcome(
            "exhausted", chain.reason, chain=chain, attempt_line=line, notification=delivery
        )

    run_id = _campaign_inflight_run_id(experiment_dir, campaign_id)
    if run_id is None:
        _record_heal_attempt(
            experiment_dir,
            campaign_id=campaign_id,
            outcome="structurally-impossible",
            now_iso=now,
            detail={"reason": "no in-flight iteration run to re-arm a watcher on"},
        )
        line, delivery = _mark_consent_dead(
            experiment_dir,
            campaign_id=campaign_id,
            failed_at=now,
            reason="no in-flight iteration run to watch (heal structurally impossible)",
            attempts=attempts,
        )
        return HealOutcome(
            "structurally-impossible",
            chain.reason,
            chain=chain,
            attempt_line=line,
            notification=delivery,
        )

    # Re-arm the WATCHER — a detached `status-watch` on the current iteration's run.
    # Pure observe: monitor-flow polls the scheduler and its terminal re-invokes the
    # driver; it NEVER submits or kills. Reuses the exact machinery that started the
    # chain's watchers, so the single-lease guard dedups a racing/live worker.
    from hpc_agent._kernel.lifecycle.detached import (
        DetachedLeaseHeld,
        DriveModeError,
        build_status_pipeline_spec,
        launch_submit_block_detached,
    )

    try:
        launch = launch_submit_block_detached(
            verb="status-watch",
            experiment_dir=str(experiment_dir),
            spec=build_status_pipeline_spec({"run_id": run_id}),
        )
    except DetachedLeaseHeld:
        # A live watcher already owns the lease — the chain was live after all.
        # A disclosed no-op (does NOT count against the cap), never a duplicate.
        line = _record_heal_attempt(
            experiment_dir,
            campaign_id=campaign_id,
            outcome="noop-lease-held",
            now_iso=now,
            detail={"run_id": run_id},
        )
        return HealOutcome(
            "chain-live-noop",
            "status-watch lease held by a live worker",
            chain=chain,
            attempt_line=line,
        )
    except (DriveModeError, OSError) as exc:
        line = _record_heal_attempt(
            experiment_dir,
            campaign_id=campaign_id,
            outcome="spawn-failed",
            now_iso=now,
            detail={"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return HealOutcome(
            "spawn-failed", f"{type(exc).__name__}: {exc}", chain=chain, attempt_line=line
        )

    line = _record_heal_attempt(
        experiment_dir,
        campaign_id=campaign_id,
        outcome="respawned",
        now_iso=now,
        detail={
            "run_id": run_id,
            "watcher": "status-watch",
            "pid": launch.pid,
            # Class A (invariant-by-construction): the re-arm restores a watcher, no
            # result value can change. Tagged so ``sever_recurrence_count`` /
            # ``escalate_if_recurring`` can see a sever RECURRING under correct
            # keepalives and route it to a C-style finding (§9 escalation ruling).
            "heal_class": "A",
        },
    )
    return HealOutcome(
        "respawned", f"re-armed status-watch on {run_id}", chain=chain, attempt_line=line
    )


def self_heal_scan(
    experiment_dir: Path,
    *,
    now_iso: str | None = None,
) -> list[HealOutcome]:
    """Run :func:`self_heal_campaign` for every campaign in *experiment_dir*.

    The seat's fan-out (see the doctor wiring): enumerate campaigns with at least
    one iteration and self-heal each under its own standing consent. Fail-open per
    campaign — one campaign's bad state never blocks another's heal. Returns only
    the outcomes that DID something (a spawn, a fail-loud flip, or a disclosed
    no-op) — a campaign with no consent is silently skipped.
    """
    outcomes: list[HealOutcome] = []
    try:
        from hpc_agent.meta.campaign.atoms.list_campaigns import campaign_list

        listed = campaign_list(experiment_dir=experiment_dir)
    except Exception:  # noqa: BLE001 — enumeration must never crash the watchdog scan
        return outcomes
    for entry in listed.get("campaigns", []):
        cid = entry.get("campaign_id") if isinstance(entry, dict) else None
        if not (isinstance(cid, str) and cid):
            continue
        try:
            outcome = self_heal_campaign(experiment_dir, campaign_id=cid, now_iso=now_iso)
        except Exception:  # noqa: BLE001 — one campaign's failure must not block the rest
            continue
        if outcome.status not in {"no-consent", "consent-not-live"}:
            outcomes.append(outcome)
    return outcomes


# ── the morning brief (item 8 pin d + amendment b) ────────────────────────────


def overnight_morning_brief(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Disclose everything consumed under a scope's standing consent.

    Reads the consent (the human's typed utterance + its caps) and the
    consumption ledger, and for each consumed boundary surfaces ``failed_at``
    (when it happened overnight) vs ``surfaced_at`` (now — first disclosure) plus
    the ``latency_seconds`` between them. The overnight canary death sat
    undetected until the human woke and asked; this brief makes that gap visible
    (amendment b). A ``push_available=False`` note on any consumed item flags an
    item whose latency was BAKED IN by the missing push channel.

    When the scope's reconcile chain died overnight and the self-heal exhausted
    its attempts (a :data:`HEAL_FAILED_KIND` line), the brief LEADS with a
    ``heal_failure`` section (item 8 self-heal ruling, 2026-07-09): what died,
    when, each respawn attempt + its outcome, and the ``failed_at`` vs
    ``surfaced_at`` latency — the human wakes to a loud, structured failure. That
    section survives consent expiry exactly as the consumption disclosure does.

    Pure read — never mutates the ledger or the consent. Returns an opaque dict
    the caller relays verbatim (the code-digested-evidence posture).
    """
    surfaced_at = now_iso or utcnow_iso()
    surfaced_dt = parse_iso_utc_or_none(surfaced_at)
    consent = latest_standing_consent(experiment_dir, scope_kind, scope_id)

    def _latency(failed_at: str | None) -> float | None:
        failed_dt = parse_iso_utc_or_none(failed_at)
        if surfaced_dt is None or failed_dt is None:
            return None
        return (surfaced_dt - failed_dt).total_seconds()

    consumed: list[dict[str, Any]] = []
    heal_attempts: list[dict[str, Any]] = []
    heal_failed: dict[str, Any] | None = None
    for line in read_consumption_ledger(experiment_dir, scope_kind, scope_id):
        event_kind = str(line.get("event_kind") or "")
        failed_at = line.get("failed_at") if isinstance(line.get("failed_at"), str) else None
        detail = _as_dict(line.get("detail"))
        notification = _as_dict(line.get("notification"))
        if event_kind == HEAL_ATTEMPT_KIND:
            heal_attempts.append(
                {
                    "outcome": detail.get("outcome"),
                    "at": failed_at,
                    "detail": detail,
                }
            )
            continue
        if event_kind == HEAL_FAILED_KIND:
            heal_failed = {
                "reason": detail.get("reason"),
                "attempts": detail.get("attempts"),
                "failed_at": failed_at,
                "surfaced_at": surfaced_at,
                "latency_seconds": _latency(failed_at),
                "push_available": bool(notification.get("push_available")),
                "disclosure_gap": notification.get("gap"),
                "text": detail.get("text"),
            }
            continue
        consumed.append(
            {
                "consumed_block": line.get("consumed_block"),
                "event_kind": event_kind,
                "failed_at": failed_at,
                "surfaced_at": surfaced_at,
                "latency_seconds": _latency(failed_at),
                "push_available": bool(notification.get("push_available")),
                "disclosure_gap": notification.get("gap"),
                "detail": detail,
            }
        )
    if heal_failed is not None:
        heal_failed["attempts_detail"] = heal_attempts
    consent_resolved = _as_dict(consent.get("resolved")) if consent is not None else {}

    # Per-class sections (§7.4): A/B heals, C1 parked elicitations, C2 findings. Lazy
    # import — the taxonomy module imports THIS module, so a top-level import would
    # cycle. Fail-open: a taxonomy read error must never blank the brief.
    try:
        from hpc_agent.ops.recover.heal_taxonomy import class_morning_sections

        class_sections = class_morning_sections(experiment_dir, scope_kind, scope_id)
    except Exception:  # noqa: BLE001 — a class-section read must not wedge the brief
        class_sections = {}

    return {
        # LEADS with the fail-loud disclosure when the chain died unrecoverably.
        "heal_failure": heal_failed,
        # Per-class breakdown (§7.4): the human reads A/B heals, C1 parked
        # elicitations ready-to-sign at wake, and C2 findings routed to the run story.
        "class_sections": class_sections,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "surfaced_at": surfaced_at,
        "has_consent": consent is not None,
        "consent": {
            "response": consent.get("response"),
            "expires_at": consent_resolved.get("expires_at"),
            "budget_cap": consent_resolved.get("budget_cap"),
            "walltime_cap": consent_resolved.get("walltime_cap"),
            "cmd_sha": consent_resolved.get("cmd_sha"),
        }
        if consent is not None
        else None,
        "consumed": consumed,
        "consumed_count": len(consumed),
    }


def morning_brief_if_any(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    now_iso: str | None = None,
) -> dict[str, Any] | None:
    """The scope's morning brief IFF a consent OR any consumption exists, else ``None``.

    The status-snapshot fold (item 8 seams 2+3): a scope earns an overnight brief
    section when the human recorded a standing consent for it OR when anything was
    auto-advanced under one. The consumption disclosure deliberately OUTLIVES the
    consent — a consent that expired overnight still surfaces what it consumed
    (``consumed_count > 0``), so the disclosure never evaporates with the grant.
    Returns ``None`` when there is nothing overnight to disclose (the byte-unchanged
    case for a scope that never went overnight).
    """
    brief = overnight_morning_brief(
        experiment_dir, scope_kind=scope_kind, scope_id=scope_id, now_iso=now_iso
    )
    if (
        brief.get("has_consent")
        or int(brief.get("consumed_count") or 0) > 0
        or brief.get("heal_failure") is not None
    ):
        return brief
    return None


def _now() -> Any:
    from hpc_agent.infra.time import utcnow

    return utcnow()
