"""SSH-driven cluster status reporter.

Both ``ops/monitor`` (`poll-run-status`), ``ops/aggregate`` (the
`aggregate-flow` composite reads status before reduce), and
``ops/recover`` (`failures` atom enriches failures with status) need
the same remote-side status JSON. Living here means none of those
subjects reach into another to fetch it.

The function shells out via :func:`hpc_agent.infra.remote.ssh_run`
to ``python -m hpc_agent.execution.mapreduce.reduce.status`` on the
cluster head node, parses the returned JSON, and raises
:class:`~hpc_agent.errors.RemoteCommandFailed` on transport or
parse failure.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import TYPE_CHECKING

from hpc_agent.errors import RemoteCommandFailed, SshUnreachable
from hpc_agent.infra import remote
from hpc_agent.infra.ssh_validation import parse_remote_json

if TYPE_CHECKING:
    from hpc_agent.infra.backends import HPCBackend

__all__ = ["ssh_status_report", "ssh_batch_scheduler_states", "ssh_marker_scan"]

# Pin the reporter to the *activated env's* interpreter. A CARC ``module load
# python/X`` (or an Lmod auto-reload) hijacks a bare ``python`` on PATH even
# after ``conda activate``, so the reporter runs under the WRONG interpreter
# (``hpc_agent`` is installed for the env's python, not the module's) → an
# import/version failure → ``rc != 0`` → ``unable_to_verify`` that stalls the
# monitor. After ``conda activate`` exports ``$CONDA_PREFIX``,
# ``$CONDA_PREFIX/bin/python`` is the env's python regardless of a later PATH
# swap; with no conda env active the expansion is empty and it falls back to a
# bare ``python`` (unchanged behaviour). Offline-pinned form; cluster efficacy
# is a live-verify item.
_ENV_PYTHON = "${CONDA_PREFIX:+$CONDA_PREFIX/bin/}python"


def _reporter_error_from_stdout(stdout: str) -> str | None:
    """Extract the reporter's structured error (``errors: [{code, detail}]``).

    On a handled failure the reporter writes that doc to **stdout** AND exits
    non-zero (its ``_emit_err`` default is ``exit_code=2``). Surface ``code:
    detail`` so the real cause (e.g. ``tasks_py_import_error``) reaches the
    operator instead of the stderr noise — an Lmod ``python/X => python/Y``
    reload notice — that otherwise masks it. Returns ``None`` if stdout is not
    the structured error envelope.
    """
    try:
        errors = json.loads(stdout).get("errors") or []
    except (ValueError, TypeError, AttributeError):
        return None
    if not errors:
        return None
    first = errors[0]
    code, detail = first.get("code"), first.get("detail")
    return f"{code}: {detail}" if detail else (str(code) if code else None)


# Sentinel that separates the reporter's JSON stdout from the piggy-backed
# watcher-ALARM trailer (see ``watcher_run_dir`` below). Chosen so it can never
# collide with the reporter's JSON body.
_WATCHER_ALARM_SENTINEL = "<<<HPC_WATCHER_ALARM>>>"


def _wrap_with_watcher_probe(base_cmd: str, watcher_run_dir: str) -> str:
    """Fold the hybrid-monitor client half into the reporter command (zero round-trip).

    Design §5: on every status read the laptop client (a) stamps a
    ``.hpc_last_read`` marker cluster-side so the watcher can tell the client is
    alive, and (b) surfaces the watcher's ``.hpc_watcher_ALARM`` if one exists.
    Both ride the SAME ssh call as the reporter — no extra round-trip.

    The reporter's exit code is preserved (``exit $__hpc_rc``) so the trailing
    ``touch`` / ``cat`` (a missing ALARM ``cat`` exits non-zero) cannot flip a
    healthy report into a spurious failure. The ALARM contents follow a sentinel
    line so :func:`ssh_status_report` can split them off before JSON parsing.
    """
    d = shlex.quote(watcher_run_dir.rstrip("/"))
    return (
        f"{base_cmd}; __hpc_rc=$?; "
        f"touch {d}/.hpc_last_read 2>/dev/null; "
        f"printf '\\n%s\\n' {shlex.quote(_WATCHER_ALARM_SENTINEL)}; "
        f"cat {d}/.hpc_watcher_ALARM 2>/dev/null; "
        f"exit $__hpc_rc"
    )


def ssh_status_report(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    job_ids: list[str],
    job_name: str,
    log_dir: str = "logs",
    file_glob: str = "*",
    min_rows: int = 0,
    remote_activation: str = "",
    watcher_run_dir: str | None = None,
) -> dict:
    """Run the on-cluster status reporter (``--run-id``) and return parsed JSON.

    The reporter reads ``.hpc/runs/<run_id>.json`` for run metadata
    and ``.hpc/tasks.py`` for per-task kwargs, then emits the JSON
    envelope pinned by ``docs/reference/python-api-contract.md``
    (summary / tasks / rollup / errors).

    ``min_rows`` is forwarded to the cluster-side reporter's
    ``--min-rows`` flag: a completed task whose CSV result has fewer
    than ``min_rows`` data rows beyond the header is demoted
    ``complete`` → ``failed``. The default ``0`` accepts header-only
    CSVs (legitimately-empty results).

    ``watcher_run_dir`` (design §5 hybrid monitor) opts the caller into the
    client half of the cluster-side watcher: when set, the SAME ssh call also
    stamps ``<dir>/.hpc_last_read`` (the client-alive marker the watcher checks)
    and reads back ``<dir>/.hpc_watcher_ALARM`` if present — surfaced in the
    returned dict under ``watcher_alarm`` (``None`` when absent). Zero extra
    round-trip; callers that leave it ``None`` get the byte-identical command
    and no ``watcher_alarm`` key.
    """
    job_ids_csv = ",".join(job_ids)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"{remote_activation}"
        f"{_ENV_PYTHON} -m hpc_agent.execution.mapreduce.reduce.status "
        f"--run-id {shlex.quote(run_id)} "
        f"--job-ids {shlex.quote(job_ids_csv)} "
        f"--job-name {shlex.quote(job_name)} "
        f"--log-dir {shlex.quote(log_dir)} "
        f"--file-glob {shlex.quote(file_glob)} "
        f"--min-rows {shlex.quote(str(int(min_rows)))}"
    )
    if watcher_run_dir is not None:
        cmd = _wrap_with_watcher_probe(cmd, watcher_run_dir)
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    # Split the watcher-ALARM trailer off before touching the JSON (only present
    # when ``watcher_run_dir`` was set; the sentinel can't occur in reporter JSON).
    json_stdout = proc.stdout
    watcher_alarm: str | None = None
    if watcher_run_dir is not None and _WATCHER_ALARM_SENTINEL in (proc.stdout or ""):
        head, _, tail = proc.stdout.partition(f"\n{_WATCHER_ALARM_SENTINEL}\n")
        json_stdout = head
        watcher_alarm = tail.strip() or None
    if proc.returncode != 0:
        # Prefer the reporter's own structured error (on stdout) over the stderr
        # noise (Lmod reload notices) that otherwise masks the real cause.
        structured = _reporter_error_from_stdout(json_stdout)
        stderr = proc.stderr.strip()[:200]
        if structured and stderr:
            detail = f"{structured} [stderr: {stderr}]"
        else:
            detail = structured or stderr or "(no output)"
        raise RemoteCommandFailed(
            f"status reporter failed (rc={proc.returncode}): {detail}",
            returncode=proc.returncode,
        )
    report = parse_remote_json(json_stdout, source_label="status reporter")
    if watcher_run_dir is not None:
        report["watcher_alarm"] = watcher_alarm
    return report


def ssh_marker_scan(*, ssh_target: str, remote_path: str, run_id: str) -> dict:
    """Scan for the dispatcher's terminal ``.hpc_failed`` markers with plain ``sh``.

    The cluster-side dispatch preamble writes
    ``$RESULT_DIR/.hpc_failed/<run_id>.<task>.failed`` after it gives up on a
    task (``hpc_preamble.sh`` ~:319; ``$RESULT_DIR`` defaults to the job cwd =
    ``remote_path``) precisely so terminal FAILURE state survives a broken run
    env — the exact case where :func:`ssh_status_report`'s python-based reporter
    dies ``rc 127`` because the run's conda env never provided
    ``python``/``hpc_agent``. This reads those markers with a bare ``ls | grep``:
    NO remote python, NO activation prefix — it MUST work when the env is broken,
    which is the whole point.

    Returns ``{"failed_markers": [names], "count": N}`` where *names* are the
    marker basenames (``<run_id>.<task>.failed``). An empty result
    (``count == 0``) proves only the ABSENCE of a failure marker for this run —
    NEVER success: a marker-less blind run is unverifiable, so the caller keeps
    the never-pass-unverified posture (present → ``canary_failed``; absent →
    ``reporter_unreachable``).
    """
    marker_dir = f"{remote_path.rstrip('/')}/.hpc_failed"
    # run_id is filesystem-validated to ``^[A-Za-z0-9._\\-]+$``; escape it for the
    # ERE so its ``.`` separators stay literal, and anchor the whole basename.
    pattern = "^" + re.escape(run_id) + r"\..*\.failed$"
    # Plain sh: list the marker dir (a missing dir → empty via ``2>/dev/null``),
    # keep only THIS run's terminal markers, and never fail the pipeline (grep
    # exits 1 on no match → ``|| true`` so ssh_run sees rc 0 and reads empty
    # stdout). No python, no conda activation — survives the broken env.
    cmd = f"ls -1 {shlex.quote(marker_dir)}/ 2>/dev/null | grep -E {shlex.quote(pattern)} || true"
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    names = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return {"failed_markers": names, "count": len(names)}


def ssh_batch_scheduler_states(
    *,
    ssh_target: str,
    backend_cls: type[HPCBackend],
    job_ids: list[str],
) -> dict[str, str]:
    """ONE scheduler query for *all* of *job_ids*; return ``{job_id: raw_state}``.

    The batched counterpart of :func:`ssh_status_report`: instead of running
    the on-cluster reporter per run, this issues a single
    ``qstat -u $USER`` (SGE/PBS) / ``squeue`` (SLURM) over one SSH connection
    and parses every requested job id's raw scheduler state at once — the
    connection-storm fix (Nextflow/Parsl query the scheduler once for all
    jobs, not once per job). The same login node, the same query, regardless
    of how many runs share it.

    Returns the raw-token map (``parse_scheduler_states`` shape); the caller
    runs ``backend_cls.batch_status`` to fold tokens into ``TaskStatus``
    values. Job ids absent from the scheduler output are omitted (they have
    left the queue — terminal). Raises :class:`SshUnreachable` on an SSH
    transport failure (the state commands append ``|| true`` so a reachable
    cluster always returns rc 0; a non-zero rc is transport, not "no jobs").
    """
    if not job_ids:
        return {}
    cmd = backend_cls.build_scheduler_state_cmd(job_ids)
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise SshUnreachable(
            f"batch scheduler-state query failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )
    return backend_cls.parse_scheduler_states(proc.stdout, job_ids)
