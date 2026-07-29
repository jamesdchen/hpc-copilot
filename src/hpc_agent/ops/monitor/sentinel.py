"""The run-terminal SENTINEL job — crash-only-monitoring W1 (submit-side leg).

``docs/design/crash-only-monitoring.md`` inversion #1: at submit time a tiny
dependent job rides along behind the run's array jobs (SLURM
``--dependency=afterany:<ids>``; SGE ``-hold_jid <ids>``; PBS
``-W depend=afterany:<ids>``) whose SOLE act is writing the run-terminal WAKE
marker — ``<remote_path>/.hpc/announce/<run_id>/.run_terminal``, the SAME
shipped filename the census waiter already watches
(:data:`hpc_agent.ops.monitor.announce.ANNOUNCE_RUN_TERMINAL`; the design doc's
older ``.hpc_TERMINAL`` sketch predates the shipped announce machinery, so the
shipped vocabulary wins). Run-end detection becomes "stat one file": the
scheduler's own epilogue knowledge is captured AT the source, so the marker
appears even when the dispatcher process died mid-task — the crash case the
dispatcher's own per-task marker writes miss.

Doctrine (rows 11-12, ``docs/internals/principles/lifecycle-verdicts.md``):

* **A wake is a HINT, never a settle.** The sentinel's marker only WAKES the
  census; the control plane always re-reads the per-task markers (the truth)
  via ``read_announcements``. That is exactly why ``afterany``/``-hold_jid``
  (completion, not success) is correct — a killed or partially failed run still
  fires the sentinel, the marker wakes the poller, and the census (not the
  marker) decides the lifecycle. A "premature" wake on SGE (where ``-hold_jid``
  releases when the held-on jobs are DELETED too) is therefore harmless by
  construction.
* **The existing polling census remains the authority.** The sentinel is
  OPPORTUNISTIC: a staging or submit failure is disclosed (WARN at degrade
  time, the fallback-inventory bar) and the run proceeds exactly as today —
  never fatal, never retried inline.

Gating: OFF by default behind ``HPC_SENTINEL_JOB`` (:func:`sentinel_enabled`),
the same posture as ``HPC_ANNOUNCE_WAIT`` (``monitor_flow._announce_wait_enabled``,
Wave-2 telemetry-gate precedent): the win — one fewer polling window on the
crash path — is only provable at live-cluster scale, so flipping the default is
the maintainer's call after telemetry, not this module's.

Accounting: the sentinel's job id is recorded on the run sidecar as a SEPARATE
``sentinel_job_id`` field (:func:`hpc_agent.state.runs.stamp_sentinel_job`),
NEVER appended to ``job_ids`` — every status rollup (reconcile alive-checks,
monitor batch status, occupancy) reads ``job_ids``, and the run's compute-job
accounting must stay byte-identical whether or not a sentinel rode along.
"""

from __future__ import annotations

import logging
import posixpath
import shlex
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra import remote
from hpc_agent.infra.env_flags import env_flag
from hpc_agent.ops.monitor.announce import ANNOUNCE_RUN_TERMINAL, ANNOUNCE_SUBPATH

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.infra.backends import HPCBackend

__all__ = [
    "SENTINEL_JOB_ENV",
    "maybe_submit_run_terminal_sentinel",
    "render_sentinel_script",
    "sentinel_enabled",
    "sentinel_job_name",
    "sentinel_script_relpath",
    "stage_sentinel_script",
]

logger = logging.getLogger(__name__)

# Opt-in flag (default OFF). Rationale in the module docstring: the
# HPC_ANNOUNCE_WAIT / Wave-2 telemetry-gate precedent — build it wired, flip it
# only on live-cluster evidence.
SENTINEL_JOB_ENV = "HPC_SENTINEL_JOB"

# Job-name suffix so a human reading squeue/qstat (or the doctor's scheduler
# listings) can tell the sentinel from the compute array at a glance. Nothing
# machine-reads job NAMES for run accounting — reconcile/monitor/kill key on the
# recorded job_ids, which never include the sentinel's — so the suffix is a
# human affordance, not a contract.
_SENTINEL_NAME_SUFFIX = "_sentinel"


def sentinel_enabled() -> bool:
    """Is the W1 sentinel-job leg opted in for this process? (default OFF)."""
    return env_flag(SENTINEL_JOB_ENV, default=False)


def sentinel_job_name(job_name: str) -> str:
    """The sentinel's scheduler job name for a run submitted as *job_name*."""
    return f"{job_name}{_SENTINEL_NAME_SUFFIX}"


def sentinel_script_relpath(run_id: str) -> str:
    """The staged sentinel script's path RELATIVE to the remote repo root.

    Lives under the announce root (``.hpc/announce/<run_id>.sentinel.sh``) as a
    SIBLING of the per-run announce dirs — never inside one, so the census
    reads (``ls task_*`` / ``test -f .run_terminal``) can never see it.
    """
    return f"{ANNOUNCE_SUBPATH}/{run_id}.sentinel.sh"


def render_sentinel_script(*, remote_path: str, run_id: str) -> str:
    """Render the sentinel job's body — plain POSIX ``sh``, no core imports.

    The body follows the standalone dispatcher's marker-write shape
    (``execution/mapreduce/dispatch._touch_run_terminal_marker``; the MIRROR
    annotation at the render site names the pinning test): ``mkdir -p``
    the announce dir, write a tmp file, ``mv`` it onto ``.run_terminal`` — an
    atomic same-directory rename, idempotent alongside the dispatcher's own
    writes (last writer wins; the marker is DATA-only either way). Every step
    is ``|| exit 0`` best-effort: the sentinel must never surface as a failed
    job in the queue — its absence of effect simply means the poller keeps
    polling, exactly today's behaviour.

    Generated shell only — the standalone-files-don't-import-core rule: the
    marker filename and announce subpath are baked in from the ONE control-plane
    definition (``ops/monitor/announce.ANNOUNCE_RUN_TERMINAL`` /
    ``ANNOUNCE_SUBPATH``), which is itself pinned in lock-step with the
    dispatcher's copy (row 12, ``tests/ops/monitor/test_announce.py``).
    """
    # MIRROR: execution/mapreduce/dispatch._touch_run_terminal_marker pinned-by tests/ops/monitor/test_sentinel.py::TestSentinelScript::test_body_is_atomic_tmp_plus_mv_and_always_exit_zero  # noqa: E501
    # MIRROR: ops/monitor/announce.ANNOUNCE_RUN_TERMINAL pinned-by tests/ops/monitor/test_announce.py::test_vocabulary_lockstep_with_standalone_dispatcher  # noqa: E501
    announce_dir = f"{remote_path.rstrip('/')}/{ANNOUNCE_SUBPATH}/{run_id}"
    marker = ANNOUNCE_RUN_TERMINAL
    return (
        "#!/bin/sh\n"
        f"# hpc-agent run-terminal SENTINEL for run {run_id}"
        " (crash-only-monitoring W1).\n"
        "# Rides a scheduler completion dependency (afterany / -hold_jid) behind the\n"
        "# run's array jobs; its SOLE act is touching the run-terminal WAKE marker so\n"
        "# run-end detection is 'stat one file' even when the dispatcher died mid-task.\n"
        "# A wake is a HINT, never a settle (doctrine row 11): the control plane always\n"
        "# re-reads the per-task markers (the census truth) after a wake.\n"
        "# Best-effort by design: every step exits 0 -- a sentinel fault must read as\n"
        "# 'no wake' (the poller keeps polling), never as a failed compute job.\n"
        f"__hpc_dir={shlex.quote(announce_dir)}\n"
        'mkdir -p "$__hpc_dir" 2>/dev/null || exit 0\n'
        f'__hpc_tmp="$__hpc_dir/{marker}.sentinel.$$.tmp"\n'
        "printf 'sentinel %s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
        ' > "$__hpc_tmp" 2>/dev/null || exit 0\n'
        f'mv "$__hpc_tmp" "$__hpc_dir/{marker}" 2>/dev/null\n'
        "exit 0\n"
    )


def stage_sentinel_script(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    _ssh_run: Any = None,
) -> str:
    """Write the rendered sentinel script to the cluster; return its RELATIVE path.

    ONE bounded ssh exec: ``mkdir -p`` the announce root, write the body to a
    tmp path, ``mv`` it into place (atomic same-dir rename — a half-written
    script is never submittable), ``chmod +x``. The body travels inside a
    single-quoted ``shlex.quote`` literal, so no heredoc/quoting hazards.

    Raises :class:`hpc_agent.errors.RemoteCommandFailed` on a non-zero rc so the
    opportunistic caller can disclose and move on; only the returncode is
    consulted (no stdout verdict — nothing to ack).
    """
    runner = _ssh_run if _ssh_run is not None else remote.ssh_run
    rel = sentinel_script_relpath(run_id)
    full = f"{remote_path.rstrip('/')}/{rel}"
    tmp = f"{full}.tmp"
    body = render_sentinel_script(remote_path=remote_path, run_id=run_id)
    cmd = (
        f"mkdir -p {shlex.quote(posixpath.dirname(full))} "
        f"&& printf '%s' {shlex.quote(body)} > {shlex.quote(tmp)} "
        f"&& mv {shlex.quote(tmp)} {shlex.quote(full)} "
        f"&& chmod +x {shlex.quote(full)}"
    )
    proc = runner(cmd, ssh_target=ssh_target)
    if getattr(proc, "returncode", 1) != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip()[:200]
        raise errors.RemoteCommandFailed(
            f"sentinel script staging failed (rc={getattr(proc, 'returncode', '?')}): {stderr}"
        )
    return rel


def maybe_submit_run_terminal_sentinel(
    backend: HPCBackend,
    *,
    experiment_dir: Path,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    job_name: str,
    depend_job_ids: list[str],
    cwd: Path | None = None,
    _ssh_run: Any = None,
) -> dict[str, Any]:
    """Opportunistically submit the run-terminal sentinel; NEVER raises.

    The one entry point the submit flow calls after the main array's job ids
    are in hand. Returns a small disclosure dict:

    * flag off (the default) → ``{"enabled": False, "submitted": False,
      "reason": "flag_off"}`` with ZERO cluster traffic — the submit is
      byte-identical to a pre-W1 submit.
    * flag on, submitted → ``{"enabled": True, "submitted": True,
      "sentinel_job_id": <id>}``; the id is also stamped onto the run sidecar's
      SEPARATE ``sentinel_job_id`` field (best-effort) — never ``job_ids``.
    * flag on, not submitted (pure-API backend, no dependency support, staging
      or qsub failure) → ``{"enabled": True, "submitted": False, "reason": ...}``,
      disclosed via a WARN at degrade time. The run proceeds exactly as today;
      the polling census remains the authority either way.
    """
    if not sentinel_enabled():
        return {"enabled": False, "submitted": False, "reason": "flag_off"}
    if not depend_job_ids:
        logger.warning(
            "sentinel job for run %s not submitted (no job ids to depend on); "
            "run proceeds with the polling census as before",
            run_id,
        )
        return {"enabled": True, "submitted": False, "reason": "no_depend_job_ids"}
    if not getattr(type(backend), "requires_ssh", True):
        # A pure-API backend has no login-node filesystem to stage the script
        # on (and its scheduler dialect carries no qsub-style dependency flag).
        logger.warning(
            "sentinel job for run %s not submitted (backend %s is not "
            "SSH/shared-filesystem shaped); run proceeds with the polling "
            "census as before",
            run_id,
            type(backend).__name__,
        )
        return {"enabled": True, "submitted": False, "reason": "backend_not_ssh"}
    try:
        script_rel = stage_sentinel_script(
            ssh_target=ssh_target,
            remote_path=remote_path,
            run_id=run_id,
            _ssh_run=_ssh_run,
        )
        sentinel_id = backend.submit_sentinel(
            script_path=script_rel,
            job_name=sentinel_job_name(job_name),
            depend_job_ids=[str(j) for j in depend_job_ids],
            cwd=cwd,
        )
    except Exception as exc:  # noqa: BLE001 — opportunistic by contract, disclosed
        logger.warning(
            "sentinel job for run %s not submitted (%s); run proceeds with the "
            "polling census as before",
            run_id,
            exc,
        )
        return {"enabled": True, "submitted": False, "reason": str(exc)}
    # Separate sidecar field — the run's job_ids accounting stays byte-identical.
    from hpc_agent.state.runs import stamp_sentinel_job

    stamp_sentinel_job(experiment_dir, run_id, job_id=sentinel_id)
    logger.info(
        "sentinel job %s submitted for run %s (afterany/hold behind %s); "
        "it writes %s/%s/%s on run terminal (wake hint only — the census decides)",
        sentinel_id,
        run_id,
        ",".join(str(j) for j in depend_job_ids),
        ANNOUNCE_SUBPATH,
        run_id,
        ANNOUNCE_RUN_TERMINAL,
    )
    return {"enabled": True, "submitted": True, "sentinel_job_id": sentinel_id}
