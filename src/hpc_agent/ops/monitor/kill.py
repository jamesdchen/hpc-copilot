"""``kill`` — first-class run cancellation (§5 kill semantics).

A ``mutate`` primitive. Given a ``run_id``, it: (1) journals the kill INTENT
before any scheduler mutation (durable even if the process dies mid-kill), (2)
attempts scheduler cancellation *through the backend seam*
(:mod:`hpc_agent.infra.backends`) if a cancel affordance exists, (3) verifies
against the scheduler which requested job IDs are still alive (reusing
:func:`hpc_agent.ops.monitor.reconcile._ssh_alive_job_ids`), (4) journals the
subset verified gone, and (5) reports the honest "N requested, N confirmed gone".

Request → journaled → verified → surfaced. The count never claims more than the
scheduler confirms.

BACKEND-CANCEL GAP: the backend seam does not today expose a cancel-command
builder — no ``build_cancel_cmd`` staticmethod exists on the backend base class.
This primitive therefore implements the journaled-intent + verify-gone + honest
half; :func:`_attempt_backend_cancel` detects the *absence* of the affordance and
reports it (``backend_cancel_available=False``) rather than fabricating a
``scancel``/``qdel`` string or importing a concrete backend. When a backend later
grows ``build_cancel_cmd(job_ids) -> str``, the cancel path lights up
automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.kill import KillResult, KillSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.monitor.reconcile import _ssh_alive_job_ids, reconcile
from hpc_agent.state.journal import load_run, record_kill_confirmed, record_kill_request
from hpc_agent.state.run_record import TERMINAL_STATUSES, RunRecord


def _attempt_backend_cancel(
    *, scheduler: str, ssh_target: str, job_ids: list[str]
) -> tuple[bool, bool]:
    """Attempt scheduler cancellation THROUGH the backend seam, if one exists.

    Returns ``(attempted, available)``. The seam does not today expose a
    cancel-command builder, so this reports ``(False, False)`` — the honest
    no-op half — without fabricating a cancel string or importing a concrete
    backend. When a backend grows ``build_cancel_cmd(job_ids) -> str``, this
    builds it off the *class* (never a concrete backend) and dispatches it over
    the shared SSH transport, lighting the cancel path up with no other change.
    """
    if not job_ids:
        return (False, False)
    from hpc_agent.infra.backends import get_backend_class

    backend_cls = get_backend_class(scheduler)
    builder = getattr(backend_cls, "build_cancel_cmd", None)
    if not callable(builder):
        return (False, False)  # no cancel affordance on the seam yet
    cmd = builder(job_ids)
    from hpc_agent.infra import remote

    remote.ssh_run(cmd, ssh_target=ssh_target)
    return (True, True)


@primitive(
    name="kill",
    verb="mutate",
    side_effects=[
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)",
        ),
        SideEffect("ssh", "<cluster>"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.RemoteCommandFailed],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Kill a run's scheduler jobs: journal the intent, attempt "
            "cancellation through the backend seam (if one exists), verify "
            "against the scheduler, journal the verified-gone subset, and report "
            "'N requested, N confirmed gone'. Request -> journaled -> verified."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        requires_ssh=True,
        spec_model=KillSpec,
        schema_ref=SchemaRef(input="kill"),
    ),
    agent_facing=True,
)
def kill(*, experiment_dir: Path, spec: KillSpec) -> dict[str, Any]:
    """Kill *spec.run_id*'s scheduler jobs and report the honest confirmed count.

    Journals the kill intent BEFORE any scheduler mutation, attempts cancellation
    through the backend seam, verifies which requested job IDs remain alive, and
    journals the subset confirmed gone. When verification cannot run (SSH /
    transport failure) NOTHING is counted as gone — the count never overstates.

    Raises :class:`errors.SpecInvalid` if no journal record exists for the run.
    """
    experiment_dir = Path(experiment_dir)
    record = load_run(experiment_dir, spec.run_id)
    if record is None:
        raise errors.SpecInvalid(f"kill: no journal record for run_id {spec.run_id!r}")
    job_ids = list(record.job_ids)

    # 1. Journal the INTENT first — durable even if we die mid-kill (§5).
    requested_at = utcnow_iso()
    record_kill_request(
        spec.run_id,
        requested_at=requested_at,
        job_ids=job_ids,
        experiment_dir=experiment_dir,
    )

    # 2. Attempt cancellation through the backend seam (no-op today; flagged).
    cancel_attempted, cancel_available = _attempt_backend_cancel(
        scheduler=spec.scheduler, ssh_target=record.ssh_target, job_ids=job_ids
    )

    # 3. Verify against the scheduler: which requested ids are still alive?
    if job_ids:
        try:
            alive = _ssh_alive_job_ids(
                ssh_target=record.ssh_target, job_ids=job_ids, scheduler=spec.scheduler
            )
        except errors.RemoteCommandFailed:
            # Cannot verify — count NOTHING as gone rather than assume success.
            alive = set(job_ids)
    else:
        alive = set()
    confirmed_gone = [j for j in job_ids if j not in alive]
    still_alive = [j for j in job_ids if j in alive]

    # 4. Journal the verified-gone subset.
    confirmed_at = utcnow_iso()
    record_kill_confirmed(
        spec.run_id,
        confirmed_at=confirmed_at,
        job_ids=confirmed_gone,
        experiment_dir=experiment_dir,
    )

    # 5. Settle a FULL kill through reconcile — the single settle definition.
    #    A FULL kill (everything confirmed gone, nothing still alive) is a
    #    terminal transition, so route it through the ``reconcile`` primitive
    #    rather than harvesting here: reconcile decides the verdict ONCE
    #    (classify.settle), marks the journal terminal — no lingering
    #    ``in_flight`` that would make ``doctor`` emit a spurious "driver
    #    stalled — re-arm?" brief for a deliberately-killed run — and fires the
    #    terminal harvest EXACTLY once (its settle-arm harvest), so kill no
    #    longer double-harvests with reconcile. A PARTIAL kill leaves the run
    #    live and its status untouched: it is still running, and the eventual
    #    real terminal harvests it.
    #
    #    Best-effort: a reconcile failure must NOT mask the kill result just
    #    journaled — log a warning and carry ``settled=False`` on.
    #
    #    ``settled`` reports what reconcile actually DID, never that it merely
    #    returned: reconcile's unable_to_verify path (e.g. an SSH blip on its
    #    OWN alive probe) returns WITHOUT raising while leaving the journal
    #    in_flight, and the envelope contract (KillResult.settled: "journal
    #    marked terminal and the terminal harvest fired") must not claim a
    #    settle that didn't happen — callers would skip the re-reconcile the
    #    run still needs. So derive it from the reconciled record's status.
    settled = False
    if confirmed_gone and not still_alive:
        try:
            settled_record = reconcile(experiment_dir, spec.run_id, scheduler=spec.scheduler)
        except Exception as exc:  # noqa: BLE001 — reconcile is best-effort; never mask the kill
            logging.getLogger(__name__).warning(
                "kill: reconcile settle failed for run %s after a full kill "
                "(the kill result stands): %s",
                spec.run_id,
                exc,
            )
        else:
            settled = (
                isinstance(settled_record, RunRecord) and settled_record.status in TERMINAL_STATUSES
            )

    result = KillResult(
        run_id=spec.run_id,
        requested_job_ids=job_ids,
        confirmed_gone_job_ids=confirmed_gone,
        still_alive_job_ids=still_alive,
        requested_count=len(job_ids),
        confirmed_count=len(confirmed_gone),
        backend_cancel_attempted=cancel_attempted,
        backend_cancel_available=cancel_available,
        summary=f"{len(job_ids)} requested, {len(confirmed_gone)} confirmed gone",
        requested_at=requested_at,
        confirmed_at=confirmed_at,
        settled=settled,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped
