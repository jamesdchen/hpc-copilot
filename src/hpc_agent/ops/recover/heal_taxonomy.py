"""Overnight-repair A/B/C1/C2 taxonomy — the classifier front-end + the class arms.

The heal machinery for ``docs/design/overnight-repair.md``. The overnight SUBSTRATE
(``ops/overnight.py``) already carries the standing consent, the caps + wake gates,
the consumption ledger, and the ONE shipped Class-A heal (``self_heal_campaign`` —
the watcher re-arm). This module builds the taxonomy ON TOP of it:

* the **classifier front-end** (:func:`classify_crash_cause`) — crash-cause → class
  ROUTING, the run-#12 worked examples pinned in :data:`_ROUTING_TABLE`. Detection
  and routing ONLY: it opens no SSH, actuates nothing, and returns a
  :class:`HealRouting` naming the class + the arm the enactment layer would spawn;
* the **Class-A stray reaper** (:func:`spawn_stray_reap_detached`) — folds
  ``stray-sweep --reap`` into the overnight seat as a SPAWNED detached child that
  owns the one cold dial (§4.1 enactment rule), never dialed from the doctor process;
* the **Class-B env-pin heal** (:func:`restore_env_to_pin` + :func:`verify_env_restored`)
  + the boundary-index canary sampling (:func:`boundary_index_sample`, §7.1), wired
  into the live S2 canary machinery by :func:`reverify_boundary_canaries` (the
  re-verify fires one fresh canary per sampled edge index);
* the **Class-C1 elicit-then-heal** (:func:`compose_env_pin_elicitation` +
  :func:`mint_env_pin_anchor`) — the env-drift finding parks a bound elicitation; the
  human's ``y`` mints an env-pin anchor into the C1 anchor ledger, so the SAME drift
  next episode classifies Class B (:func:`env_drift_class`);
* the **Class-C2 report-only** routing (:func:`report_c2_finding`) into the morning
  brief / run story;
* the **recurrence escalation** (:func:`escalate_if_recurring`, §9 ruling) — a sever
  recurring under correct keepalives routes to a C-style finding, not a re-heal loop.

The hard boundaries the substrate already enforces bind here too: **observe / judge /
route, never actuate** (the doctor seat routes; a spawned detached child enacts), and
**never heal a Class C** (C1 elicits, C2 reports — neither is an autonomous heal).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent.infra.env_flags import HEALABLE_TRANSPORT_ENV_VARS
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.overnight import (
    overnight_ledger_path,
    read_consumption_ledger,
    sever_recurrence_count,
)

__all__ = [
    "HEAL_CLASSES",
    "HealRouting",
    "classify_crash_cause",
    "routing_table",
    "ENV_PIN_ANCHOR_KIND",
    "anchor_ledger_path",
    "read_anchors",
    "read_env_pin_anchor",
    "has_env_pin_anchor",
    "mint_env_pin_anchor",
    "env_drift",
    "env_drift_class",
    "compose_env_pin_elicitation",
    "restore_env_to_pin",
    "verify_env_restored",
    "boundary_index_sample",
    "reverify_boundary_canaries",
    "spawn_stray_reap_detached",
    "report_c2_finding",
    "escalate_if_recurring",
    "class_morning_sections",
]

#: The four ruled classes (overnight-repair.md §2). C1/C2 are DISTINCT classes.
HEAL_CLASSES = ("A", "B", "C1", "C2")


@dataclass(frozen=True)
class HealRouting:
    """The verdict of the classifier front-end for one crash cause (routing only).

    ``heal_class`` is one of :data:`HEAL_CLASSES` (or ``""`` when the cause is
    unclassified — NEVER healed, §5). ``arm`` names the enactment the class would
    spawn (``stray-reap`` / ``watcher-rearm`` / ``env-pin-restore`` / ``elicit`` /
    ``report``), or ``"none"`` for an unclassified cause. ``reverify`` is True only
    for Class B (the §7.1 verify-then-canary obligation). ``reason`` is the
    human-facing one-liner; ``detail`` carries per-cause context (never actuated).
    """

    cause: str
    heal_class: str
    arm: str
    reverify: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


# ── the classifier front-end: crash-cause → class routing (§3, §6 worked cases) ──
#
# Detection + ROUTING only. Every row is a run-#12 worked example (§6) pinned to the
# ruled class. The router NEVER opens SSH and NEVER actuates — it returns the class
# + the arm the enactment layer (a spawned detached child) would own.

# canonical cause kinds (the ``cluster_env_init`` classification vocabulary, §3.2)
_CAUSE_FORK_EXHAUSTION = "fork-exhaustion"
_CAUSE_SEVERED_CONNECTION = "severed-connection"
_CAUSE_STALE_WHEEL = "stale-wheel"
_CAUSE_ENV_DRIFT = "env-drift"
_CAUSE_RESOURCE_EXHAUSTION = "resource-exhaustion"
_CAUSE_RESULT_ANOMALY = "result-anomaly"


def _route_fork_exhaustion(_context: dict[str, Any]) -> HealRouting:
    return HealRouting(
        cause=_CAUSE_FORK_EXHAUSTION,
        heal_class="A",
        arm="stray-reap",
        reverify=False,
        reason=(
            "login-node fork exhaustion (finding 20): the target set is "
            "invariant-by-construction — marked, over-age framework strays — so reaping "
            "exactly those PIDs cannot touch a result. Class A: spawn stray-sweep --reap "
            "as a detached child (the doctor process never dials)."
        ),
    )


def _route_severed_connection(context: dict[str, Any]) -> HealRouting:
    return HealRouting(
        cause=_CAUSE_SEVERED_CONNECTION,
        heal_class="A",
        arm="watcher-rearm",
        reverify=False,
        reason=(
            "severed remote leg (finding 24): re-arm the watcher inside a fresh "
            "detached child whose dial carries keepalives by construction. Class A. "
            "(A sever that RECURS under correct keepalives escalates to a C-finding — "
            "see escalate_if_recurring.)"
        ),
        detail={"recurrence_hint": context.get("recurrence_count", 0)},
    )


def _route_stale_wheel(_context: dict[str, Any]) -> HealRouting:
    return HealRouting(
        cause=_CAUSE_STALE_WHEEL,
        heal_class="C2",
        arm="report",
        reverify=False,
        reason=(
            "stale-wheel reinstall (finding 24, §6c): a version change makes the "
            "repaired path a different experiment and no predicate distinguishes "
            "'fixed' from 'silently reshaped'. Class C2 — REPORT-ONLY for the "
            "autonomous healer; a human reinstall with disclosure is the C choreography."
        ),
    )


def _route_env_drift(context: dict[str, Any]) -> HealRouting:
    # C1 by default (no anchor → the env-pin elicit); B once an env-pin anchor exists
    # (§6d ruling). The caller passes ``anchored=True`` when :func:`has_env_pin_anchor`
    # already holds for this scope — then the same drift is auto-restore + verify.
    anchored = bool(context.get("anchored"))
    if anchored:
        return HealRouting(
            cause=_CAUSE_ENV_DRIFT,
            heal_class="B",
            arm="env-pin-restore",
            reverify=True,
            reason=(
                "transport env drift with a MINTED env-pin anchor (§6d): auto-restore "
                "the transport env to the pinned set, then re-verify against the anchor "
                "and re-canary (boundary-index sampling). Class B."
            ),
        )
    return HealRouting(
        cause=_CAUSE_ENV_DRIFT,
        heal_class="C1",
        arm="elicit",
        reverify=False,
        reason=(
            "transport env drift, no journaled anchor yet (§6d, finding 24d): stateable "
            "but unanchored — the anchor is in the human's head. Class C1 — ELICIT "
            "'unset the drifted transport var?'; the human's `y` mints an env-pin anchor "
            "so the same drift next episode is Class B."
        ),
    )


def _route_resource_exhaustion(_context: dict[str, Any]) -> HealRouting:
    return HealRouting(
        cause=_CAUSE_RESOURCE_EXHAUSTION,
        heal_class="C1",
        arm="elicit",
        reverify=False,
        reason=(
            "resource exhaustion — OOM / walltime kill (§4.4): no predicate "
            "distinguishes a transient neighbor-job node OOM from a parameter regime "
            "that GENUINELY exceeds memory/walltime, and the latter IS the finding. "
            "C-adjacent — ELICIT the resubmit proposal (C1) or REPORT; an auto-resubmit "
            "is the 'stopped crashing = removed evidence' failure §5 forbids."
        ),
    )


def _route_result_anomaly(_context: dict[str, Any]) -> HealRouting:
    return HealRouting(
        cause=_CAUSE_RESULT_ANOMALY,
        heal_class="C2",
        arm="report",
        reverify=False,
        reason=(
            "result anomaly (§2 standing rule): an anomalous number is NEVER evidence "
            "of a mechanical fault to repair — it is a finding. Always Class C2, routed "
            "into the run story / attention queue as an observation."
        ),
    )


# The routing table — the classifier front-end's whole knowledge (§6 worked cases).
_ROUTING_TABLE = {
    _CAUSE_FORK_EXHAUSTION: _route_fork_exhaustion,
    _CAUSE_SEVERED_CONNECTION: _route_severed_connection,
    _CAUSE_STALE_WHEEL: _route_stale_wheel,
    _CAUSE_ENV_DRIFT: _route_env_drift,
    _CAUSE_RESOURCE_EXHAUSTION: _route_resource_exhaustion,
    _CAUSE_RESULT_ANOMALY: _route_result_anomaly,
}


def classify_crash_cause(cause_kind: str, *, context: dict[str, Any] | None = None) -> HealRouting:
    """Route a CLASSIFIED crash cause to its ruled heal class (detection + routing only).

    The classifier front-end (§3.3 discriminators, §6 worked examples). *cause_kind*
    is the ``cluster_env_init``-style classification of the crash. An UNCLASSIFIED
    cause (unknown kind) routes to ``heal_class=""`` / ``arm="none"`` — the §5
    standing rule: a crash may only be healed once its CAUSE is classified, and
    "stopped crashing" is removed evidence, not success. Never opens SSH, never
    actuates — the enactment layer spawns a detached child for a heal.

    *context* carries per-cause routing input (e.g. ``anchored=True`` for an env
    drift with a minted env-pin anchor → Class B instead of C1).
    """
    ctx = context or {}
    router = _ROUTING_TABLE.get(cause_kind)
    if router is None:
        return HealRouting(
            cause=cause_kind,
            heal_class="",
            arm="none",
            reverify=False,
            reason=(
                f"unclassified crash cause {cause_kind!r}: NEVER healed (§5). A retry "
                "that succeeds without a classified cause destroys the reproduction — "
                "classify the cause first, then re-consult the router."
            ),
        )
    return router(ctx)


def routing_table() -> dict[str, HealRouting]:
    """The classifier's full routing table as ``{cause: HealRouting}`` (for disclosure/tests)."""
    return {cause: router({}) for cause, router in _ROUTING_TABLE.items()}


# ── the C1 anchor ledger (§4.3 the ratchet) ───────────────────────────────────
#
# Each signed C1 anchor joins the scope's PERMANENT anchor ledger — its own jsonl
# beside the consumption ledger (never the decision journal, so a code-authored
# anchor line never shadows a real greenlight). The env-pin anchor is the first
# member: it pins the transport env the human consented to restore to.

ENV_PIN_ANCHOR_KIND = "env-pin"


def anchor_ledger_path(experiment_dir: Path, scope_kind: str, scope_id: str) -> Path:
    """The per-scope C1 anchor-ledger jsonl path (beside the consumption ledger)."""
    consumption = overnight_ledger_path(experiment_dir, scope_kind, scope_id)
    # ``<scope>.overnight.jsonl`` → ``<scope>.anchors.jsonl`` (run sidecar tree) /
    # ``overnight.jsonl`` → ``anchors.jsonl`` (campaign dir).
    name = consumption.name.replace("overnight.jsonl", "anchors.jsonl")
    if name == consumption.name:  # defensive — never share a file with consumption
        name = consumption.name + ".anchors.jsonl"
    return consumption.with_name(name)


def read_anchors(experiment_dir: Path, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Every minted anchor for a scope, in append order (``[]`` if none). Tolerant read."""
    import json
    import logging

    path = anchor_ledger_path(experiment_dir, scope_kind, scope_id)
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    except (OSError, UnicodeDecodeError) as exc:
        logging.getLogger(__name__).warning("anchor ledger unreadable %s (%s)", path, exc)
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


def read_env_pin_anchor(
    experiment_dir: Path, scope_kind: str, scope_id: str
) -> dict[str, Any] | None:
    """The most recent env-pin anchor for a scope, or ``None`` (newest-wins)."""
    for anchor in reversed(read_anchors(experiment_dir, scope_kind, scope_id)):
        if str(anchor.get("kind") or "") == ENV_PIN_ANCHOR_KIND:
            return anchor
    return None


def has_env_pin_anchor(experiment_dir: Path, scope_kind: str, scope_id: str) -> bool:
    """True when a scope has a minted env-pin anchor (→ env drift is Class B, not C1)."""
    return read_env_pin_anchor(experiment_dir, scope_kind, scope_id) is not None


def mint_env_pin_anchor(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    pinned_env: dict[str, str | None],
    consent_ref: str | None = None,
) -> dict[str, Any]:
    """MINT an env-pin anchor into the scope's C1 anchor ledger (the ratchet, §4.3).

    Called after the human's `y` authorizes the C1 env-pin heal (the mint is the
    second thing the `y` does — see :func:`compose_env_pin_elicitation`). *pinned_env*
    is the pinned transport env: ``{var: value_or_None}`` where ``None`` means "this
    transport var MUST be unset" (the finding-24d case — unset ``HPC_SSH_ENGINE``).
    Only :data:`HEALABLE_TRANSPORT_ENV_VARS` may be pinned (a non-transport var is a
    spec-identity var whose drift kills the consent, never an anchor). *consent_ref*
    is the ``append-decision`` record identity the mint rides (the anchor IS that
    record; this ledger line is the ratchet index). Returns the written anchor line.
    """
    bad = sorted(k for k in pinned_env if k not in HEALABLE_TRANSPORT_ENV_VARS)
    if bad:
        raise errors.SpecInvalid(
            f"env-pin anchor may pin ONLY transport-selection vars "
            f"{sorted(HEALABLE_TRANSPORT_ENV_VARS)}; refusing to pin {bad} — a "
            "non-transport var can reach the job env, so its drift kills the standing "
            "consent (spec-changed), it is never an env-pin anchor."
        )
    anchor: dict[str, Any] = {
        "kind": ENV_PIN_ANCHOR_KIND,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "pinned_env": dict(pinned_env),
        "consent_ref": consent_ref,
        "minted_at": utcnow_iso(),
    }
    append_jsonl_line(anchor_ledger_path(experiment_dir, scope_kind, scope_id), anchor)
    return anchor


# ── env-drift detection + classification (§6d) ────────────────────────────────


def env_drift(
    live_transport_overrides: dict[str, str],
    *,
    pinned_env: dict[str, str | None] | None = None,
) -> dict[str, dict[str, Any]]:
    """The transport-env DRIFT — live overrides vs the pinned set (pure comparison).

    *live_transport_overrides* is :func:`infra.env_flags.active_transport_overrides`
    output (the live healable transport vars). When *pinned_env* is ``None`` (no
    anchor yet — the C1 case) EVERY live override is drift (the anchor's absence means
    none is expected). When an anchor exists (the B case), a var drifts when its live
    value differs from the pinned value, and a var pinned-to-``None`` (must-be-unset)
    that is live is drift. Returns ``{var: {"live": v, "pinned": p}}`` for each
    drifted var. Never judges beyond equality — an entry IS the finding.
    """
    pinned = pinned_env or {}
    out: dict[str, dict[str, Any]] = {}
    if pinned_env is None:
        for var, val in live_transport_overrides.items():
            out[var] = {"live": val, "pinned": None}
        return out
    # anchored: compare each pinned var + surface any un-pinned live override too.
    for var in set(pinned) | set(live_transport_overrides):
        live = live_transport_overrides.get(var)
        want = pinned.get(var)  # None → must be unset
        if live != want:
            out[var] = {"live": live, "pinned": want}
    return out


def env_drift_class(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    live_transport_overrides: dict[str, str],
) -> HealRouting:
    """Classify a live transport-env drift for a scope (§6d): C1 (no anchor) or B (anchored).

    Reads whether an env-pin anchor already exists for the scope and, if so, whether
    the live env actually drifts from it. No drift at all → ``heal_class=""`` /
    ``arm="none"`` (nothing to heal). Anchored + drift → Class B (auto-restore +
    verify). Unanchored + any live override → Class C1 (elicit-then-mint). Routes
    through the shared classifier so the reason strings stay one definition.
    """
    anchor = read_env_pin_anchor(experiment_dir, scope_kind, scope_id)
    if anchor is None:
        if not live_transport_overrides:
            return HealRouting(
                _CAUSE_ENV_DRIFT, "", "none", False, "no transport env override live"
            )
        routing = classify_crash_cause(_CAUSE_ENV_DRIFT, context={"anchored": False})
        drift = env_drift(live_transport_overrides, pinned_env=None)
        return HealRouting(
            routing.cause, "C1", routing.arm, False, routing.reason, {"drift": drift}
        )
    pinned = anchor.get("pinned_env")
    pinned = pinned if isinstance(pinned, dict) else {}
    drift = env_drift(live_transport_overrides, pinned_env=pinned)
    if not drift:
        return HealRouting(_CAUSE_ENV_DRIFT, "", "none", False, "env matches the pinned anchor")
    routing = classify_crash_cause(_CAUSE_ENV_DRIFT, context={"anchored": True})
    return HealRouting(routing.cause, "B", routing.arm, True, routing.reason, {"drift": drift})


def compose_env_pin_elicitation(
    *,
    scope_kind: str,
    scope_id: str,
    cmd_sha: str,
    live_transport_overrides: dict[str, str],
) -> dict[str, Any]:
    """Compose the PARKED C1 elicitation for a transport-env drift (the bound firing site).

    The C1 choreography (§4.3, §6d): the agent does NOT heal — it composes the
    candidate anchor as a typed elicitation the human signs at the ``append-decision``
    boundary (the SAME bound firing site the overnight standing consent uses;
    ``mcp_server._overnight_consent_binding``). Overnight the finding PARKS with this
    composed anchor ready in the morning brief (declared-but-dark). On the human's `y`
    the caller mints the anchor (:func:`mint_env_pin_anchor`) with ``pinned_env`` set
    to unset every drifted transport var. Returns a ``resolved``-shaped dict naming
    the predicate + the proposed pin (all code-selected identifiers, never model prose).
    """
    proposed_pin = {var: None for var in sorted(live_transport_overrides)}
    return {
        "heal_class": "C1",
        "anchor_kind": ENV_PIN_ANCHOR_KIND,
        "cmd_sha": cmd_sha,
        "predicate": (
            "the client-side transport env matches the pinned set (each of "
            f"{sorted(live_transport_overrides)} unset unless you pin a value)"
        ),
        "drifted": dict(live_transport_overrides),
        "proposed_pinned_env": proposed_pin,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
    }


# ── Class B env-pin heal: restore + verify (§7.1) ─────────────────────────────


def restore_env_to_pin(
    anchor: dict[str, Any], live_transport_overrides: dict[str, str]
) -> dict[str, str | None]:
    """The corrected transport env a Class-B env-pin heal spawns its child WITH.

    Given a minted env-pin *anchor* and the live overrides, returns the corrective
    env map: ``{var: value_or_None}`` — a pinned value restores it, a var pinned to
    ``None`` (must-be-unset) that is currently live is marked ``None`` (unset). The
    healer never mutates its OWN environment; the map is applied to the SPAWNED
    detached child's env (transport vars are read by the client at dial time), so the
    child re-dials with the pinned transport selection.
    """
    pinned = anchor.get("pinned_env")
    pinned = pinned if isinstance(pinned, dict) else {}
    correction: dict[str, str | None] = {}
    for var in set(pinned) | set(live_transport_overrides):
        want = pinned.get(var)  # None → unset
        if live_transport_overrides.get(var) != want:
            correction[var] = want
    return correction


def verify_env_restored(
    anchor: dict[str, Any], transport_overrides_after: dict[str, str]
) -> dict[str, Any]:
    """Re-verify a Class-B env-pin heal against the anchor (the §7.1 invariance re-check).

    After the corrected child is spawned, the transport env it dials with must match
    the anchor: every pinned-to-a-value var carries that value, every pinned-to-``None``
    (must-be-unset) var is absent. Returns ``{"verified": bool, "mismatches": {...}}``
    — the ``verify_result`` the caller journals on the HEAL_ATTEMPT line. A FAILED
    verify flips the heal to fail-loud (§7.1); it never "tries harder".
    """
    pinned = anchor.get("pinned_env")
    pinned = pinned if isinstance(pinned, dict) else {}
    mismatches: dict[str, dict[str, Any]] = {}
    for var, want in pinned.items():
        got = transport_overrides_after.get(var)
        if got != want:
            mismatches[var] = {"want": want, "got": got}
    return {
        "verified": not mismatches,
        "mismatches": mismatches,
        "anchor_kind": ENV_PIN_ANCHOR_KIND,
    }


def boundary_index_sample(first_index: int, last_index: int) -> list[int]:
    """The canary sample indices for a repaired range: the BOUNDARIES, not just 0 (§7.1).

    Run-#10's harvest-gap class showed edge indices are where repairs go wrong; a heal
    that re-canaries only index 0 re-earns a greenlight the boundary would have denied.
    Returns ``[first, last]`` de-duplicated + sorted (a single-index range → ``[i]``);
    a caller threads this as the canary path's ``boundary_indices`` so the fresh canary
    samples the first AND last affected index.
    """
    lo, hi = (first_index, last_index) if first_index <= last_index else (last_index, first_index)
    return [lo] if lo == hi else [lo, hi]


def reverify_boundary_canaries(
    experiment_dir: Path,
    *,
    spec: Any,
    first_index: int,
    last_index: int,
    fire: Callable[..., list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Fire a fresh Class-B re-verify canary at EACH boundary of a repaired range (§7.1).

    This is the heal-arm call site that threads the boundary-index sampling
    parameter INTO the live S2 canary machinery. Given the repaired range's
    first/last affected task, it samples the boundaries (:func:`boundary_index_sample`)
    and fires ONE fresh canary per edge index through the existing canary submit leg
    (``ops.submit_flow.fire_second_canary``), each under its own
    ``<run_id>-canary-b<idx>`` id so the probes never collide with each other or with
    the ordinary ``<run_id>-canary``. The canary at index *i* dispatches the main
    run's task-*i* frozen kwargs — the repaired path re-earns its greenlight AT the
    edge, not just at index 0 (run-#10's harvest-gap class: edge indices are where
    repairs go wrong).

    The **enactment rule** (§4.1) still binds: the healer process never opens SSH
    itself. In production the overnight/doctor seat passes ``fire`` = a wrapper that
    spawns the canary submission as a detached child owning the one cold dial; the
    default (``fire_second_canary``) is the in-process leg the two-phase gate and the
    determinism double-canary already use, and is the seam this function threads the
    ``boundary_index`` through. Returns one record per boundary
    (``{"boundary_index", "canary_run_id", "job_ids"}``) for the ``verify_result`` /
    morning-brief canary-outcome disclosure.
    """
    fire_fn = fire if fire is not None else _default_canary_fire()
    results: list[dict[str, Any]] = []
    for idx in boundary_index_sample(first_index, last_index):
        canary_run_id = f"{spec.run_id}-canary-b{idx}"
        job_ids = fire_fn(
            experiment_dir,
            spec=spec,
            canary_run_id=canary_run_id,
            boundary_index=idx,
        )
        results.append(
            {
                "boundary_index": idx,
                "canary_run_id": canary_run_id,
                "job_ids": list(job_ids),
            }
        )
    return results


def _default_canary_fire() -> Callable[..., list[str]]:
    """The default boundary-canary firing leg — the live S2 canary machinery.

    Lazily imported so the ``ops.submit_flow`` dependency (and its transitive
    transport imports) is paid only when a Class-B re-verify actually fires, never
    at module load (heal_taxonomy is imported by the no-SSH doctor seat).
    """
    from hpc_agent.ops.submit_flow import fire_second_canary

    return fire_second_canary


# ── Class A enactment: fold stray-sweep --reap into the overnight seat (§6a) ───


def spawn_stray_reap_detached(
    experiment_dir: Path,
    *,
    ssh_target: str,
    max_age_sec: int = 3900,
    hpc_agent_bin: str | None = None,
) -> Any:
    """Spawn ``stray-sweep --reap`` as a DETACHED child (the Class-A enactment, §6a).

    ``stray-sweep`` opens its OWN ssh (one fork-minimal ``ps``; a ``kill`` of exactly
    the marked over-age strays when reaping), so under the overnight seat it is
    SPAWNED as a detached child that owns the cold dial — NOT dialed from the doctor
    process (whose contract is no-SSH). Reuses the kernel's platform-correct detached
    spawn (:func:`_kernel.lifecycle.detached._spawn_detached`) so the child survives
    session death exactly as the watcher re-arm's child does. The target set is
    invariant-by-construction (marked + over-age), so no re-verification is owed
    (Class A). Returns the ``DetachedLaunch`` handle; raises the spawn's own errors.

    The doctor/overnight seat that calls this NEVER opens SSH itself — it only routes
    (classify) and spawns; the child enacts. That is the never-actuate boundary.
    """
    from hpc_agent._kernel.lifecycle.detached import _agent_launch_prefix, _spawn_detached
    from hpc_agent.state.run_record import _current_homedir

    detached_dir = _current_homedir() / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    # Not a run-keyed block, so it does not ride ``launch_submit_block_detached`` (which
    # keys on run_id + SUPPORTED_DETACHED_BLOCK_VERBS). A synthetic filesystem-safe key
    # scopes the lease/log to this login node.
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in ssh_target)
    key = f"stray-sweep-{safe}"
    log_path = detached_dir / f"{key}.log"
    argv = [
        *_agent_launch_prefix(hpc_agent_bin),
        "stray-sweep",
        "--ssh-target",
        ssh_target,
        "--reap",
        "--max-age-sec",
        str(int(max_age_sec)),
    ]
    return _spawn_detached(
        run_id=key,
        block="stray-sweep",
        argv=argv,
        log_path=log_path,
        cwd=str(experiment_dir),
    )


# ── Class C2 report-only routing (§4.4, §7.4) ─────────────────────────────────


def report_c2_finding(
    *,
    cause: str,
    scope_kind: str,
    scope_id: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose a Class-C2 REPORT-ONLY finding (§4.4): an observation, never a heal.

    A C2 finding (version upgrade / winsorize / dtype / retry-selection / result
    anomaly / a crash that only reproduces under one library version) is routed OUT
    of the infra brief and INTO the run story / attention queue as an observation
    ABOUT the experiment. This composes that observation (the morning brief's C2
    section surfaces it, tagged ``becomes-science``). It ACTUATES nothing — the whole
    point of C2 is that no autonomous change is ever made.
    """
    return {
        "heal_class": "C2",
        "cause": cause,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "routing": "run-story / attention-queue (observation about the experiment)",
        "becomes_science": True,
        "detail": dict(detail) if detail else {},
        "surfaced_at": utcnow_iso(),
    }


# ── recurrence escalation (§9 RULED 2026-07-12) ───────────────────────────────


def escalate_if_recurring(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    threshold: int = 2,
) -> HealRouting | None:
    """Escalate a RECURRING sever to a C-style finding instead of re-arming (§9 ruling).

    With correct keepalives a SINGLE sever is Class A (re-arm). But a sever that
    RECURS under correct keepalives is no longer a mechanical fault to re-heal — it is
    a cluster-social signal for the human. Reads the Class-A re-arm count
    (:func:`ops.overnight.sever_recurrence_count`) and, once it reaches *threshold*,
    returns a Class-C2 routing (report — escalate to a finding, do NOT re-arm again);
    below threshold returns ``None`` (a single sever heals normally as Class A). This
    is the counter on the heal audit trail the ruling names — it turns a re-heal LOOP
    into a finding.
    """
    count = sever_recurrence_count(experiment_dir, scope_kind, scope_id, heal_class="A")
    if count < threshold:
        return None
    return HealRouting(
        cause=_CAUSE_SEVERED_CONNECTION,
        heal_class="C2",
        arm="report",
        reverify=False,
        reason=(
            f"connection sever RECURRED {count}× under correct keepalives (≥ threshold "
            f"{threshold}) — no longer a mechanical fault to re-heal (§9 escalation "
            "ruling). Escalate to a C-style finding: a cluster-social signal for the "
            "human, not another watcher re-arm."
        ),
        detail={"recurrence_count": count, "threshold": threshold},
    )


# ── per-class morning-brief sections (§7.4) ───────────────────────────────────


def class_morning_sections(experiment_dir: Path, scope_kind: str, scope_id: str) -> dict[str, Any]:
    """The per-class breakdown the morning brief layers on (§7.4).

    Reads the consumption ledger + anchor ledger and splits the overnight heal
    activity by class: Class A/B heals (each carrying ``heal_class`` and, for B, the
    ``anchor_ref`` + ``verify_result``), Class C1 parked elicitations (composed anchors
    waiting for a `y`, rendered ready-to-sign at wake — declared-but-dark), and Class
    C2 findings (routed OUT to the run story). Pure read — actuates nothing. The
    overnight morning brief folds this in beside its shipped ``failed_at`` vs
    ``surfaced_at`` latency disclosure.
    """
    heals_ab: list[dict[str, Any]] = []
    parked_c1: list[dict[str, Any]] = []
    findings_c2: list[dict[str, Any]] = []
    for line in read_consumption_ledger(experiment_dir, scope_kind, scope_id):
        detail = line.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        cls = str(detail.get("heal_class") or "")
        if cls in ("A", "B"):
            heals_ab.append(
                {
                    "heal_class": cls,
                    "outcome": detail.get("outcome"),
                    "anchor_ref": detail.get("anchor_ref"),
                    "verify_result": detail.get("verify_result"),
                    "at": line.get("failed_at"),
                }
            )
        elif cls == "C1":
            parked_c1.append({"anchor": detail.get("elicitation"), "at": line.get("failed_at")})
        elif cls == "C2":
            findings_c2.append({"finding": detail, "at": line.get("failed_at")})
    return {
        "class_a_b_heals": heals_ab,
        "class_c1_parked_elicitations": parked_c1,
        "class_c2_findings": findings_c2,
        "minted_anchors": read_anchors(experiment_dir, scope_kind, scope_id),
    }
