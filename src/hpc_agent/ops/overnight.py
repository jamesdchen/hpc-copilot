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
from typing import TYPE_CHECKING, Any, cast

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from pathlib import Path

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
    "latest_standing_consent",
    "standing_consent_status",
    "assert_standing_consent",
    "is_consumable_boundary",
    "consume_boundary_under_consent",
    "overnight_ledger_path",
    "record_consumption",
    "read_consumption_ledger",
    "notification_plan",
    "overnight_morning_brief",
    "morning_brief_if_any",
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
    spent_budget: float = 0.0,
    spent_walltime: float = 0.0,
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
    derivation. ``spent_budget`` / ``spent_walltime`` default to 0.0 — a spend meter is
    a future seam; the expiry (morning boundary) + spec-identity legs are enforced now.
    """
    if not is_consumable_boundary(scope_kind, boundary_block):
        return ConsumptionOutcome(
            False, ConsentDecision(False, "boundary-not-consumable", None), None
        )
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

    Pure read — never mutates the ledger or the consent. Returns an opaque dict
    the caller relays verbatim (the code-digested-evidence posture).
    """
    surfaced_at = now_iso or utcnow_iso()
    surfaced_dt = parse_iso_utc_or_none(surfaced_at)
    consent = latest_standing_consent(experiment_dir, scope_kind, scope_id)
    consumed: list[dict[str, Any]] = []
    for line in read_consumption_ledger(experiment_dir, scope_kind, scope_id):
        failed_at = line.get("failed_at") if isinstance(line.get("failed_at"), str) else None
        failed_dt = parse_iso_utc_or_none(failed_at)
        latency = (
            (surfaced_dt - failed_dt).total_seconds()
            if (surfaced_dt is not None and failed_dt is not None)
            else None
        )
        notification = _as_dict(line.get("notification"))
        consumed.append(
            {
                "consumed_block": line.get("consumed_block"),
                "event_kind": line.get("event_kind"),
                "failed_at": failed_at,
                "surfaced_at": surfaced_at,
                "latency_seconds": latency,
                "push_available": bool(notification.get("push_available")),
                "disclosure_gap": notification.get("gap"),
                "detail": _as_dict(line.get("detail")),
            }
        )
    consent_resolved = _as_dict(consent.get("resolved")) if consent is not None else {}
    return {
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
    if brief.get("has_consent") or int(brief.get("consumed_count") or 0) > 0:
        return brief
    return None


def _now() -> Any:
    from hpc_agent.infra.time import utcnow

    return utcnow()
