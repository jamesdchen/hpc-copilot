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
from hpc_agent.infra.ssh_validation import parse_remote_json, split_ack

if TYPE_CHECKING:
    from hpc_agent.infra.backends import HPCBackend

__all__ = [
    "ssh_status_report",
    "ssh_batch_scheduler_states",
    "ssh_marker_scan",
    "ssh_list_run_sidecars",
]

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


def _reporter_error_code_from_stdout(stdout: str) -> str | None:
    """Extract JUST the reporter's structured error ``code`` (not ``code: detail``).

    The bare code (e.g. ``sidecar_not_found``) is what a poll loop keys on to
    decide DETERMINISTIC-vs-transient
    (:func:`hpc_agent.errors.is_deterministic_reporter_failure`), so it rides
    first-class on the raised :class:`~hpc_agent.errors.RemoteCommandFailed` —
    never re-parsed from the human-facing message. ``None`` when stdout is not the
    structured error envelope or the first error carries no ``code``.
    """
    try:
        errs = json.loads(stdout).get("errors") or []
    except (ValueError, TypeError, AttributeError):
        return None
    if not errs:
        return None
    code = errs[0].get("code")
    return str(code) if code else None


# Sentinel that separates the reporter's JSON stdout from the piggy-backed
# watcher-ALARM trailer (see ``watcher_run_dir`` below). Chosen so it can never
# collide with the reporter's JSON body.
_WATCHER_ALARM_SENTINEL = "<<<HPC_WATCHER_ALARM>>>"

# Positive-evidence sentinel-ack for the status reporter read (run-12 finding 24;
# docs/design/connection-broker.md sentinel-ack ruling). Echoed as the LAST line
# of the remote command, carrying the reporter's exit code. Its PRESENCE proves
# the remote shell ran the reporter to completion; a rc-0 read that does NOT
# carry it is a channel severed / truncated mid-stream (NAT idle-drop, the
# asyncssh idle reaper, an expired remote deadline) — UNKNOWN, never "the
# reporter emitted nothing". Parsed back with
# :func:`hpc_agent.infra.ssh_validation.split_ack`.
_STATUS_ACK_PREFIX = "__HPC_STATUS_ACK__="


def _wrap_reporter_command(base_cmd: str, watcher_run_dir: str | None) -> str:
    """Append the sentinel-ack (+ optional watcher probe) to the reporter command.

    The reporter's exit code is captured into ``__hpc_rc`` immediately, so the
    trailing ack ``echo`` (always rc 0) and the optional watcher ``touch`` / ``cat``
    (a missing ALARM ``cat`` exits non-zero) cannot flip a healthy report into a
    spurious failure — the closing ``exit $__hpc_rc`` re-surfaces the reporter's
    own rc as the ssh returncode (unchanged contract).

    Sequencing (run-12 findings 5/20/24): the ack is echoed LAST — after the
    watcher-ALARM trailer — so ANY mid-stream truncation loses it. It rides INSIDE
    ``ssh_run``'s ``timeout … bash -c '<cmd>'`` wrapper, so a remote-deadline
    expiry (rc 124) or a severed channel kills the shell before the echo → no ack.
    Client-side, :func:`ssh_status_report` reads the ack's ABSENCE with a rc-0 read
    as the positive proof the channel died, and raises rather than parse-and-trust
    a truncated stream. The ALARM contents still follow the
    :data:`_WATCHER_ALARM_SENTINEL` line so the reader can split them off first.

    Design §5 (hybrid monitor): when *watcher_run_dir* is set the SAME ssh call
    also stamps ``<dir>/.hpc_last_read`` (client-alive marker) and reads back
    ``<dir>/.hpc_watcher_ALARM``; ``None`` leaves those off (byte-identical command
    apart from the always-present ack).
    """
    parts = [f"{base_cmd}; __hpc_rc=$?"]
    if watcher_run_dir is not None:
        d = shlex.quote(watcher_run_dir.rstrip("/"))
        parts.append(f"touch {d}/.hpc_last_read 2>/dev/null")
        parts.append(f"printf '\\n%s\\n' {shlex.quote(_WATCHER_ALARM_SENTINEL)}")
        parts.append(f"cat {d}/.hpc_watcher_ALARM 2>/dev/null")
    # Affirmative token LAST, carrying the reporter's own rc; then re-exit with it.
    parts.append(f'echo "{_STATUS_ACK_PREFIX}$__hpc_rc"')
    parts.append("exit $__hpc_rc")
    return "; ".join(parts)


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
    # Import guard: when the (possibly empty) activation leaves ``hpc_agent``
    # unimportable, ``python -m hpc_agent...`` exits **1** — which the poll
    # loop's ``_classify_poll_failure`` reads as "transient" and rides the full
    # wait budget (run #7 live: 30 min of rc=1 polls against a green canary).
    # Probing the import first and exiting **127** routes module-absence into
    # the EXISTING deterministic-env class (rc 126/127), which escalates after
    # ~3 consecutive polls with the wrong/absent-conda-env diagnosis.
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"{remote_activation}"
        f"{_ENV_PYTHON} -c 'import hpc_agent' 2>/dev/null || exit 127; "
        f"{_ENV_PYTHON} -m hpc_agent.execution.mapreduce.reduce.status "
        f"--run-id {shlex.quote(run_id)} "
        f"--job-ids {shlex.quote(job_ids_csv)} "
        f"--job-name {shlex.quote(job_name)} "
        f"--log-dir {shlex.quote(log_dir)} "
        f"--file-glob {shlex.quote(file_glob)} "
        f"--min-rows {shlex.quote(str(int(min_rows)))}"
    )
    cmd = _wrap_reporter_command(cmd, watcher_run_dir)
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    # Strip the positive-evidence ack line FIRST (it is the last echo, so it
    # survives only a complete read); ``ack_rc is None`` ⇒ the channel was severed
    # / truncated mid-stream. Then split the watcher-ALARM trailer off the
    # remaining bytes before touching the JSON (present only when
    # ``watcher_run_dir`` was set; neither sentinel can occur in reporter JSON).
    clean, ack_rc = split_ack(proc.stdout or "", _STATUS_ACK_PREFIX)
    json_stdout = clean
    watcher_alarm: str | None = None
    if watcher_run_dir is not None and _WATCHER_ALARM_SENTINEL in clean:
        head, _, tail = clean.partition(f"\n{_WATCHER_ALARM_SENTINEL}\n")
        json_stdout = head
        watcher_alarm = tail.strip() or None
    if proc.returncode != 0:
        # Prefer the reporter's own structured error (on stdout) over the stderr
        # noise (Lmod reload notices) that otherwise masks the real cause. rc!=0
        # (incl. the import-guard's 127 and a remote-deadline's 124) surfaces the
        # real returncode regardless of the ack.
        structured = _reporter_error_from_stdout(json_stdout)
        stderr = proc.stderr.strip()[:200]
        if structured and stderr:
            detail = f"{structured} [stderr: {stderr}]"
        else:
            detail = structured or stderr or "(no output)"
        raise RemoteCommandFailed(
            f"status reporter failed (rc={proc.returncode}): {detail}",
            returncode=proc.returncode,
            # Lift the reporter's structured code first-class so a poll loop can
            # escalate a DETERMINISTIC reporter fault (a never-shipped / torn
            # sidecar answers EVERY poll ``sidecar_not_found`` and will not heal by
            # waiting — finding 7) FAST instead of riding the full wait budget.
            reporter_error_code=_reporter_error_code_from_stdout(json_stdout),
        )
    # rc 0 but NO affirmative ack: a severed / truncated channel delivered a
    # clean-looking rc-0 read that never carried the reporter to completion
    # (run-12 finding 24 — NAT idle-drop / asyncssh idle reaper). Refuse to
    # parse-and-trust a truncated stream; raise transient so every consumer
    # routes it to UNKNOWN (unable_to_verify / reporter_unreachable), never a
    # settled "the reporter emitted nothing" verdict.
    if ack_rc is None:
        raise RemoteCommandFailed(
            "status reporter channel severed / output truncated: rc 0 but no "
            "positive-evidence ack (__HPC_STATUS_ACK__) — the remote command did "
            "not run to completion; refusing to parse a truncated stream.",
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


def ssh_list_run_sidecars(*, ssh_target: str, remote_path: str) -> list[str]:
    """List the run-sidecar basenames present under ``<remote_path>/.hpc/runs/``.

    Diagnostic-only, plain ``sh`` (no python, no activation — it must answer even
    when the run env is broken). Used when a poll escalates on a DETERMINISTIC
    ``sidecar_not_found``: naming which sidecar the reporter polled AND which
    siblings actually shipped turns "reporter unreachable" into "the ``-canary2``
    sidecar was never pushed; ``-canary`` is present" (finding 7's one-definition
    remedy). Best-effort: a missing dir / ssh error returns ``[]`` (the caller
    still discloses the polled path, just without the sibling list).
    """
    runs_dir = f"{remote_path.rstrip('/')}/.hpc/runs"
    cmd = f"ls -1 {shlex.quote(runs_dir)}/ 2>/dev/null || true"
    try:
        proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    except (RemoteCommandFailed, OSError):
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


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
    transport failure (non-zero rc) AND on a MISSING sentinel-ack: the
    positive-evidence rule (docs/design/connection-broker.md) — an empty read
    that does not carry the query's affirmative ack token is a silently
    truncated / never-run channel (UNKNOWN), and reading it as "every job left
    the queue" would flip a fleet of live runs to terminal on one silent blip.
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
    clean, ran_ok = backend_cls.scheduler_query_ran(proc.stdout)
    if not ran_ok:
        raise SshUnreachable(
            "batch scheduler-state query returned no positive-evidence ack "
            "(silent/empty read — the query did not run to completion, or the "
            "scheduler binary itself failed); refusing to read absence as "
            "'all jobs terminal'."
        )
    return backend_cls.parse_scheduler_states(clean, job_ids)
