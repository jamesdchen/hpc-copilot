"""Supersession conduct — a new run_id must never make the journal forget.

Proving run #4 (2026-07-05, findings e/g/h): after a spec-changing nudge, the
agent "fixed" gate friction by minting a NEW run_id for the SAME experiment
code (``pi-estimation-h2-e2cddfb7`` vs ``pi-estimation-e2cddfb7`` — same
cmd_sha) without closing the old attempt. The old worker + canary (and a prior
discovery-cluster canary) were orphaned: because every rule-9 / single-lease
gate keys on run_id, a fresh run_id = a fresh lease, and scope-hopping became
an escape hatch from the whole conduct system.

This module makes creating/submitting under a new run_id while a SIBLING prior
run_id has live state an explicit, closure-triggering act:

* **Detection** (:func:`find_live_siblings`): a sibling is another run in the
  same experiment's journal home whose record is ``in_flight`` AND whose
  EFFECTIVE code identity (``node_sha`` when parented, else ``cmd_sha`` — the
  one definition in :func:`hpc_agent.state.runs.sidecar_effective_identity`)
  equals the about-to-submit run's. Journal-home + sidecar reads only — no SSH.
* **Refusal** (:func:`apply_supersession_gate`): submitting the new run_id
  while such a sibling is live raises :class:`hpc_agent.errors.SiblingRunLive`
  naming the sibling, its state, and the two sanctioned exits — close it first
  (``hpc-agent kill --run-id <old>`` / reconcile to terminal), or pass an
  explicit ``supersedes: "<old_run_id>"`` field on the submit spec.
* **Supersession** (:func:`supersede_run`): with ``supersedes`` present the
  gate journals the old→new link (``superseded_by`` on the old record — the
  durable evidence — and ``supersedes`` on the new one, stamped post-submit by
  :func:`stamp_supersedes_on_new`), TRIGGERS closure of the old attempt (and
  its ``-canary`` pairing) through the existing kill/reconcile machinery where
  reachable, marks any record the scheduler could not confirm gone as
  ``abandoned`` via the centralized ``mark_run`` transition with the reason in
  ``last_status.verdict_reason`` (the same idiom reconcile's settle arms use),
  records a ``pending_closure`` marker for unverified jobs, and proceeds.

Never trips on: the run's own ``-canary`` pairing (#258 — the two-phase canary
gate legitimately submits main while its own canary record is live), terminal
siblings, different-identity runs, the very first submit, or a run whose
identity is unknown (we cannot prove sameness — never a false trip).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.index import find_in_flight_runs
from hpc_agent.state.journal import load_run, mark_run, update_run_record
from hpc_agent.state.run_record import TERMINAL_STATUSES, RunRecord
from hpc_agent.state.runs import read_run_sidecar, sidecar_effective_identity

__all__ = [
    "apply_supersession_gate",
    "find_live_siblings",
    "stamp_supersedes_on_new",
    "supersede_run",
]

_log = logging.getLogger(__name__)


def _spec_identity(experiment_dir: Path, spec: Any) -> str | None:
    """The about-to-submit run's effective code identity, or None if unknowable.

    ``cmd_sha`` comes from ``job_env['HPC_CMD_SHA']`` (the caller-stamped
    parameter identity that ships to the scheduler). A parented spec composes
    ``node_sha`` via :func:`hpc_agent.state.runs.resolve_node_sha` — the same
    effective-identity rule the dedup lookup uses — so a parented run never
    false-trips against an unparented sibling with the same params. A failure
    to compose (missing parent sidecar — the flow refuses that later anyway)
    disables the gate for this spec rather than inventing a new failure mode.
    """
    cmd_sha = (getattr(spec, "job_env", None) or {}).get("HPC_CMD_SHA")
    if not cmd_sha:
        return None
    parents = getattr(spec, "parents", None)
    if parents:
        from hpc_agent.state.runs import resolve_node_sha

        try:
            return resolve_node_sha(experiment_dir, cmd_sha=cmd_sha, parent_run_ids=list(parents))
        except errors.HpcError:
            return None
    return str(cmd_sha)


def _record_identity(experiment_dir: Path, record: RunRecord) -> str | None:
    """A journal record's effective code identity, or None if unknowable.

    The per-run sidecar is the canonical identity store (it carries both
    ``cmd_sha`` and, for parented runs, ``node_sha``); read it through the ONE
    effective-identity definition. A ``-canary`` record's mirrored sidecar
    carries the main run's identity, so an orphaned live canary of a PRIOR run
    is detected too (proving run #4, finding e). Falls back to the journal
    record's own ``job_env['HPC_CMD_SHA']`` copy (#299) when the sidecar was
    pruned; ``None`` (identity unknown) when neither exists.
    """
    try:
        sidecar = read_run_sidecar(experiment_dir, record.run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError, errors.HpcError):
        sidecar = None
    if sidecar is not None:
        identity = sidecar_effective_identity(sidecar)
        if identity:
            return identity
    env_sha = (record.job_env or {}).get("HPC_CMD_SHA")
    return str(env_sha) if env_sha else None


def find_live_siblings(
    experiment_dir: Path, *, run_id: str, identity: str | None
) -> list[RunRecord]:
    """Live (``in_flight``) journal records sharing *identity* under another run_id.

    The supersession sibling predicate — cheap journal-home + sidecar reads,
    no SSH. Excludes, by design (each is a documented no-false-trip case):

    * *run_id* itself (same-run_id replay is layer-1 dedup's job, not ours);
    * the SAME run's ``-canary`` pairing in either direction (#258): the
      two-phase canary gate submits main ``X`` while ``X-canary`` is live;
    * terminal siblings (``find_in_flight_runs`` only returns live records);
    * records whose identity is unknown or differs.
    """
    if not identity:
        return []
    from hpc_agent.ops.monitor.reconcile import canary_parent_of

    siblings: list[RunRecord] = []
    for record in find_in_flight_runs(experiment_dir):
        if record.run_id == run_id:
            continue
        # Same-run canary pairing never trips, in either direction.
        if canary_parent_of(record.run_id) == run_id or canary_parent_of(run_id) == record.run_id:
            continue
        if _record_identity(experiment_dir, record) != identity:
            continue
        siblings.append(record)
    return siblings


def _live_lease(run_id: str) -> dict[str, Any] | None:
    """The live detached-worker lease for *run_id*, if any (refusal context).

    Reads the journal home's ``_detached/<block>-<run_id>.lease.json`` files
    (the idempotent-single lease :mod:`hpc_agent._kernel.lifecycle.detached`
    stamps) and reports the first one whose recorded pid is still alive.
    Local reads only; best-effort — an unreadable lease contributes nothing.
    """
    from hpc_agent._kernel.lifecycle.detached import _pid_alive
    from hpc_agent.state.run_record import _current_homedir

    detached_dir = _current_homedir() / "_detached"
    if not detached_dir.is_dir():
        return None
    for path in sorted(detached_dir.glob(f"*-{run_id}.lease.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data.get("pid", -1))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if data.get("run_id") == run_id and pid > 0 and _pid_alive(pid):
            return {"pid": pid, "block": data.get("block"), "lease_path": str(path)}
    return None


def _sibling_line(experiment_dir: Path, record: RunRecord) -> str:
    """One human-readable evidence line for a live sibling (refusal message)."""
    lease = _live_lease(record.run_id)
    lease_part = (
        f", live {lease['block']!r} worker lease pid {lease['pid']}" if lease else ", no live lease"
    )
    return (
        f"{record.run_id!r} ({record.status} since {record.submitted_at}, "
        f"cluster {record.cluster!r}, job_ids {record.job_ids}{lease_part})"
    )


def _refuse(experiment_dir: Path, *, new_run_id: str, siblings: list[RunRecord]) -> None:
    """Raise the structured supersession refusal naming siblings + the two exits."""
    lines = "; ".join(_sibling_line(experiment_dir, s) for s in siblings)
    first = siblings[0].run_id
    raise errors.SiblingRunLive(
        f"refusing to submit new run_id {new_run_id!r}: {len(siblings)} sibling prior "
        f"run(s) with the SAME code identity (cmd_sha) still have live state — {lines}. "
        "A fresh run_id is not an exit from the single-lease / provenance gates "
        "(proving run #4, finding g/h). Two sanctioned exits: (a) close the prior "
        f"attempt first — `hpc-agent kill --run-id {first}` (or reconcile it to a "
        "terminal verdict) — then re-submit; or (b) declare the supersession "
        f'explicitly by adding `"supersedes": "{first}"` to this submit spec, which '
        "journals the old→new link, closes the old attempt (kill + verify where "
        "reachable, else a pending-closure marker the watchdog surfaces), and proceeds.",
        remediation=(
            f"Either `hpc-agent kill --run-id {first}` and reconcile it to terminal, "
            f'or re-submit with `"supersedes": "{first}"` in the spec.'
        ),
    )


def supersede_run(
    experiment_dir: Path,
    *,
    old_run_id: str,
    new_run_id: str,
    scheduler: str,
) -> dict[str, Any]:
    """Journal + close *old_run_id* (and its ``-canary`` pairing) as superseded.

    Per target record, in order (evidence first, so a crash mid-closure still
    leaves the WHY on disk):

    1. Stamp ``superseded_by=new_run_id`` / ``superseded_at`` (durable evidence;
       the backward half of the two-direction audit link — the forward half is
       :func:`stamp_supersedes_on_new` after the new record lands).
    2. If the record is live and has job_ids, request closure through the
       existing kill machinery (:func:`hpc_agent.ops.monitor.kill.kill`):
       journaled intent → backend cancel where the seam has one → verify →
       reconcile settle on a full kill. An unreachable cluster (SSH failure /
       open circuit) records a ``pending_closure`` marker instead of blocking.
    3. If the record is STILL ``in_flight`` afterwards (partial kill, no cancel
       affordance, or unreachable), record ``pending_closure`` for any
       unconfirmed job_ids and settle it ``abandoned`` through the centralized
       ``mark_run`` transition with ``last_status.verdict_reason =
       "superseded_by=<new>"`` — the same reason-recording idiom reconcile's
       settle arms use — so the §5 watchdog stops re-flagging it and a later
       reconcile can still revise the verdict (evidence is durable).

    Returns a summary ``{superseded_run_id, closed, pending_closure,
    superseded_at}`` for the caller's envelope/logging.
    """
    from hpc_agent.ops.monitor.reconcile import _sibling_run_ids

    now = utcnow_iso()
    closed: list[str] = []
    pending: list[dict[str, Any]] = []

    # The old run and its #258 canary pairing (both directions covered by
    # _sibling_run_ids: given either id it returns the paired one).
    targets = [old_run_id, *_sibling_run_ids(old_run_id)]
    for target in targets:
        record = load_run(experiment_dir, target)
        if record is None:
            continue

        def _stamp(r: RunRecord) -> None:
            r.superseded_by = new_run_id
            r.superseded_at = now

        update_run_record(experiment_dir, target, _stamp)
        if record.status in TERMINAL_STATUSES:
            closed.append(target)  # already settled — link recorded, nothing to close
            continue

        kill_result: dict[str, Any] | None = None
        unreachable_reason: str | None = None
        if record.job_ids:
            try:
                kill_result = _invoke_kill(experiment_dir, run_id=target, scheduler=scheduler)
            except errors.HpcError as exc:
                # Circuit open / SSH down / remote failure: closure is
                # unreachable right now. Record it and move on — the new
                # submit must not block on the old cluster.
                unreachable_reason = f"{type(exc).__name__}: {exc}"
                _log.warning(
                    "supersede: kill of %s unreachable (%s); recording pending_closure",
                    target,
                    unreachable_reason,
                )

        reloaded = load_run(experiment_dir, target)
        if reloaded is not None and reloaded.status not in TERMINAL_STATUSES:
            # Not settled by kill→reconcile — mark it superseded/abandoned so
            # the watchdog stops re-flagging it, with the reason recorded via
            # the centralized machinery (never an ad-hoc status write).
            unconfirmed = (
                list(kill_result.get("still_alive_job_ids") or [])
                if kill_result is not None
                else list(record.job_ids)
            )
            reason = f"superseded_by={new_run_id}"
            marker: dict[str, Any] = {}
            if unconfirmed:
                marker = {
                    "job_ids": unconfirmed,
                    "reason": unreachable_reason or "scheduler jobs not confirmed gone at kill",
                    "recorded_at": now,
                }
                pending.append({"run_id": target, **marker})

            def _settle_evidence(
                r: RunRecord,
                marker: dict[str, Any] = marker,
                reason: str = reason,
            ) -> None:
                r.last_status = {**(r.last_status or {}), "verdict_reason": reason}
                if marker:
                    r.pending_closure = dict(marker)

            update_run_record(experiment_dir, target, _settle_evidence)
            mark_run(experiment_dir, target, status="abandoned")
        closed.append(target)

    return {
        "superseded_run_id": old_run_id,
        "closed": closed,
        "pending_closure": pending,
        "superseded_at": now,
    }


def _invoke_kill(experiment_dir: Path, *, run_id: str, scheduler: str) -> dict[str, Any]:
    """Dispatch the existing ``kill`` primitive (module seam for test doubles)."""
    from hpc_agent._wire.actions.kill import KillSpec
    from hpc_agent.ops.monitor.kill import kill as _kill

    return _kill(experiment_dir=experiment_dir, spec=KillSpec(run_id=run_id, scheduler=scheduler))


def _supersede_missing_main(
    experiment_dir: Path,
    *,
    spec: Any,
    supersedes: str,
    siblings: list[RunRecord],
) -> dict[str, Any]:
    """Resolve a ``supersedes`` whose named run has NO main journal record.

    An attempt that dies at the canary stage leaves only a ``<id>-canary``
    sub-record — the main journal entry never lands. The old blanket refusal
    ("no journal record exists … nothing to supersede") was *unsatisfiable* for
    the honest caller here: the ONLY way to proceed was to DROP the ``supersedes``
    honesty marker, which trains agents to route around the gate (proving run
    #5). Split known-but-clean from unknown instead:

    * **Unknown** — no ``<id>-canary`` sub-record AND no live detached lease for
      the id or its canary: keep the typo-protecting refusal (a real bad
      run_id, same message as before).
    * **Clean** — a *terminal* canary sub-record and no live lease: no-op PASS.
      Stamp the backward ``superseded_by`` link on the canary record (the only
      record that exists to stamp) and return a ``noop_already_clean`` summary;
      the forward link on the new record is stamped post-submit as usual.
    * **Live** — a *non-terminal* canary sub-record OR a live lease: a REAL
      supersession target. Route the existing kill/settle machinery at the
      canary id via :func:`supersede_run` (it does not structurally require a
      main record — it stamps + closes whichever paired ids exist), never
      "nothing to supersede".
    """
    from hpc_agent.ops.monitor.reconcile import _sibling_run_ids, canary_parent_of

    # The paired ``<id>-canary`` entry via the one #258 suffix definition.
    (canary_id,) = _sibling_run_ids(supersedes)
    canary = load_run(experiment_dir, canary_id)
    lease = _live_lease(supersedes) or _live_lease(canary_id)

    if canary is None and lease is None:
        raise errors.SpecInvalid(
            f"supersedes names run {supersedes!r} but no journal record exists for it "
            f"in this experiment's journal home — nothing to supersede. Check the "
            "run_id (hpc-agent status-snapshot lists known runs), or drop the field."
        )

    canary_live = (
        canary is not None and canary.status not in TERMINAL_STATUSES
    ) or lease is not None

    if canary_live:
        # A live canary attempt IS a real target. Any OTHER uncovered live
        # sibling still refuses (the canary itself is covered by `supersedes`).
        uncovered = [
            s
            for s in siblings
            if s.run_id != canary_id and canary_parent_of(s.run_id) != supersedes
        ]
        if uncovered:
            _refuse(experiment_dir, new_run_id=spec.run_id, siblings=uncovered)
        summary = supersede_run(
            experiment_dir,
            old_run_id=canary_id,
            new_run_id=spec.run_id,
            scheduler=spec.backend,
        )
        summary["superseded_run_id"] = supersedes
        summary["action"] = "superseded_live_canary"
        summary["note"] = (
            f"{supersedes!r} has no main journal record; its canary attempt "
            f"{canary_id!r} was still live and was closed as the supersession target."
        )
        return summary

    # Clean: a terminal canary attempt, nothing live to close. Stamp the
    # backward link on the canary record (the honest audit trail) and pass —
    # the caller must NOT have to drop `supersedes` to proceed.
    now = utcnow_iso()
    closed: list[str] = []
    if canary is not None:

        def _stamp(r: RunRecord) -> None:
            r.superseded_by = spec.run_id
            r.superseded_at = now

        update_run_record(experiment_dir, canary_id, _stamp)
        closed.append(canary_id)

    return {
        "superseded_run_id": supersedes,
        "closed": closed,
        "pending_closure": [],
        "superseded_at": now,
        "action": "noop_already_clean",
        "note": (
            f"{supersedes!r} has no main journal record; its only attempt "
            f"{canary_id!r} is terminal"
            + (f" ({canary.status})" if canary is not None else "")
            + " with no live lease — supersession link stamped where a record "
            "exists, nothing to close."
        ),
    }


def apply_supersession_gate(experiment_dir: Path, spec: Any) -> dict[str, Any] | None:
    """Pre-submit supersession gate for one fresh submit-flow spec.

    Runs in the submit-flow batch prelude — before any sidecar write, rsync,
    or scheduler traffic — alongside the other pre-submit guards (#191 /
    provenance drift). Journal-home reads only unless a ``supersedes`` closure
    actually fires (which reuses the kill machinery's SSH).

    * No live same-identity sibling and no ``supersedes`` → ``None`` (no-op).
    * Live sibling(s), no ``supersedes`` → :class:`errors.SiblingRunLive`.
    * ``supersedes`` named, main record present → validates it (a live sibling
      NOT covered by it still refuses), then :func:`supersede_run` and returns
      its closure summary.
    * ``supersedes`` named, NO main record → :func:`_supersede_missing_main`
      splits known-but-clean from unknown: a genuinely unknown id still refuses
      (typo protection), a terminal ``-canary``-only attempt is a no-op PASS
      (link stamped, ``action=noop_already_clean``), and a LIVE ``-canary``-only
      attempt is closed as a real target — never "nothing to supersede".

    The forward link on the NEW record is stamped after the submit lands
    (:func:`stamp_supersedes_on_new`).
    """
    experiment_dir = Path(experiment_dir)
    supersedes = getattr(spec, "supersedes", None)
    identity = _spec_identity(experiment_dir, spec)
    siblings = find_live_siblings(experiment_dir, run_id=spec.run_id, identity=identity)

    if not supersedes:
        if siblings:
            _refuse(experiment_dir, new_run_id=spec.run_id, siblings=siblings)
        return None

    old = load_run(experiment_dir, supersedes)
    if old is None:
        return _supersede_missing_main(
            experiment_dir, spec=spec, supersedes=supersedes, siblings=siblings
        )
    from hpc_agent.ops.monitor.reconcile import canary_parent_of

    uncovered = [
        s for s in siblings if s.run_id != supersedes and canary_parent_of(s.run_id) != supersedes
    ]
    if uncovered:
        # Superseding one sibling is not a blanket amnesty: any OTHER live
        # same-identity run still refuses (each needs its own explicit close).
        _refuse(experiment_dir, new_run_id=spec.run_id, siblings=uncovered)

    return supersede_run(
        experiment_dir,
        old_run_id=supersedes,
        new_run_id=spec.run_id,
        scheduler=spec.backend,
    )


def stamp_supersedes_on_new(experiment_dir: Path, *, new_run_id: str, old_run_id: str) -> None:
    """Stamp the forward ``supersedes`` link on the NEW record, post-submit.

    Best-effort: the backward link (``superseded_by`` on the old record,
    stamped BEFORE the submit) is the durable half of the audit chain; this
    forward half makes the new→old direction readable without a scan. A
    missing new record (e.g. a canary-only phase that has not landed the main
    journal entry yet) logs and moves on — it must never fail a submit that
    already succeeded.
    """

    def _mutate(r: RunRecord) -> None:
        r.supersedes = old_run_id

    try:
        update_run_record(Path(experiment_dir), new_run_id, _mutate)
    except FileNotFoundError:
        _log.warning(
            "supersede: no journal record yet for new run %s — forward link not stamped "
            "(the backward superseded_by link on %s is already durable)",
            new_run_id,
            old_run_id,
        )
