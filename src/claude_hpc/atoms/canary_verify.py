"""``verify-canary`` workflow atom — wait + grep + output-check protocol.

Replaces the multi-step canary-verification prose at /submit-hpc Step
7b/8 with one workflow atom that:

1. Polls the canary's run record until the canary's job_ids are no
   longer alive on the cluster (terminal).
2. Greps the canary's stderr log for known failure markers
   (``[dispatch] FAILED``, ``Traceback``, ``ImportError``).
3. Optionally verifies an expected output artifact exists in the
   canary's result_dir.

Returns ``{ok, failure_kind, details, stderr_tail}`` so the caller
can branch on ``ok``: True → main array submit; False → surface
``stderr_tail`` to the user verbatim (don't paraphrase — the user
needs the raw error to fix it).

Currently the most fragile multi-step protocol in the slash command;
atomizing it eliminates the agent-judgment-on-each-step failure mode.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from claude_hpc import errors
from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc.runner import record_status

if TYPE_CHECKING:
    from pathlib import Path

# Stderr substrings that signal a canary failure. Lowercased; matched
# case-insensitively. Order matters for ``failure_kind`` reporting —
# more specific markers first so e.g. an ImportError isn't reported as
# a generic Traceback.
_FAILURE_MARKERS: tuple[tuple[str, str], ...] = (
    ("[dispatch] failed", "dispatcher_failed"),
    ("importerror", "import_error"),
    ("modulenotfounderror", "module_not_found"),
    ("traceback", "traceback"),
    ("oom-kill", "oom_killed"),
    ("out of memory", "oom_killed"),
    ("segmentation fault", "segfault"),
)

_DEFAULT_POLL_INTERVAL_SEC = 30
_DEFAULT_WAIT_BUDGET_SEC = 1800  # 30 min — long enough for a 1-task probe


@primitive(
    name="verify-canary",
    verb="workflow",
    composes=[record_status],
    side_effects=[
        SideEffect("ssh", "<cluster> (poll status + tail stderr)"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable],
    idempotent=True,
    idempotency_key="canary_run_id",
)
def verify_canary(
    experiment_dir: Path,
    *,
    canary_run_id: str,
    expect_output: str | None = None,
    poll_interval_sec: int = _DEFAULT_POLL_INTERVAL_SEC,
    wait_budget_sec: int = _DEFAULT_WAIT_BUDGET_SEC,
    log_dir: str = "logs",
    file_glob: str = "*",
) -> dict[str, Any]:
    """Wait for the canary to land terminal, then verify exit + outputs.

    Parameters
    ----------
    canary_run_id:
        Run ID of the 1-task canary submission. Lookup happens via the
        journal — the run record carries ``ssh_target``, ``remote_path``,
        ``job_ids``, ``job_name`` for the SSH polls.
    expect_output:
        Path (relative to ``remote_path`` or absolute) the canary
        should have written. Verified via SSH + ``[ -f ... ]``. ``None``
        skips the output check; the exit-code + log scan still run.
    poll_interval_sec, wait_budget_sec:
        Adaptive poll knobs. Exits early once the canary is terminal;
        otherwise gives up after *wait_budget_sec* with
        ``failure_kind="timeout"``.
    log_dir, file_glob:
        Threaded through to the cluster-side status reporter so it
        finds the canary's stderr log.

    Returns
    -------
    ``{ok, failure_kind, details, stderr_tail}``:

    * ``ok=True`` iff the canary exited 0, no known failure markers in
      stderr, and the expected output (if any) exists.
    * ``failure_kind`` is None on success; otherwise one of
      ``"dispatcher_failed"`` / ``"import_error"`` / ``"traceback"`` /
      ``"oom_killed"`` / ``"segfault"`` / ``"missing_output"`` /
      ``"timeout"`` / ``"abandoned"``.
    * ``details`` is a one-line human-readable summary the slash
      command can surface to the user above the raw stderr_tail.
    * ``stderr_tail`` is the last 50 lines of the canary's stderr
      log (or empty when not retrievable).

    Raises :class:`errors.SpecInvalid` on missing journal record or
    bad knobs; :class:`errors.SshUnreachable` on persistent SSH
    failure (the budget elapsed in failed polls).
    """
    if not canary_run_id:
        raise errors.SpecInvalid("canary_run_id is required")
    if int(poll_interval_sec) <= 0:
        raise errors.SpecInvalid("poll_interval_sec must be > 0")
    if int(wait_budget_sec) <= 0:
        raise errors.SpecInvalid("wait_budget_sec must be > 0")

    from claude_hpc._internal import session
    from claude_hpc.runner import fetch_task_logs
    from claude_hpc.runner.aggregate import verify_combiner_artifact
    from claude_hpc.runner.status import _ssh_status_report

    record = session.load_run(experiment_dir, canary_run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for canary run_id={canary_run_id!r}")

    deadline = time.monotonic() + int(wait_budget_sec)
    last_summary: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            report = _ssh_status_report(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                run_id=canary_run_id,
                job_ids=list(record.job_ids),
                job_name=record.job_name,
                log_dir=log_dir,
                file_glob=file_glob,
            )
        except (errors.RemoteCommandFailed, OSError):
            time.sleep(int(poll_interval_sec))
            continue
        last_summary = dict(report.get("summary") or {})
        complete = int(last_summary.get("complete") or 0)
        failed = int(last_summary.get("failed") or 0)
        running = int(last_summary.get("running") or 0)
        pending = int(last_summary.get("pending") or 0)
        # Terminal: complete == total OR (failed > 0 and no running/pending).
        if complete >= int(record.total_tasks) and record.total_tasks > 0:
            break
        if failed > 0 and running == 0 and pending == 0:
            break
        time.sleep(int(poll_interval_sec))
    else:
        return {
            "ok": False,
            "failure_kind": "timeout",
            "details": (
                f"canary {canary_run_id!r} did not terminate within "
                f"{wait_budget_sec}s (last summary: {last_summary})"
            ),
            "stderr_tail": "",
        }

    # Fetch the canary's stderr tail (1 task, task_id=0).
    logs = fetch_task_logs(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        job_name=record.job_name,
        job_ids=list(record.job_ids),
        scheduler="slurm" if "slurm" in record.cluster.lower() else "sge",
        task_ids=[0],
        lines=50,
    )
    stderr_tail = ""
    if logs and isinstance(logs[0], dict):
        stderr_tail = str(logs[0].get("content") or "")

    # Scan for failure markers.
    haystack = stderr_tail.lower()
    for marker, kind in _FAILURE_MARKERS:
        if marker in haystack:
            return {
                "ok": False,
                "failure_kind": kind,
                "details": (
                    f"canary {canary_run_id!r} stderr contains {marker!r} — "
                    f"likely {kind.replace('_', ' ')}."
                ),
                "stderr_tail": stderr_tail,
            }

    # Optional output verification.
    if expect_output:
        output_ok, output_detail = verify_combiner_artifact(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            expect_output=expect_output,
        )
        if not output_ok:
            return {
                "ok": False,
                "failure_kind": "missing_output",
                "details": (
                    f"canary {canary_run_id!r} expected output {expect_output!r} {output_detail}"
                ),
                "stderr_tail": stderr_tail,
            }

    # Final check: complete == total_tasks, no failures.
    summary_failed = int(last_summary.get("failed") or 0)
    if summary_failed > 0:
        return {
            "ok": False,
            "failure_kind": "abandoned",
            "details": (
                f"canary {canary_run_id!r} reported failed={summary_failed} "
                "but no recognized stderr marker found."
            ),
            "stderr_tail": stderr_tail,
        }

    return {
        "ok": True,
        "failure_kind": None,
        "details": (
            f"canary {canary_run_id!r} verified: exit 0, no error markers, "
            + (f"output {expect_output!r} present." if expect_output else "no output check.")
        ),
        "stderr_tail": stderr_tail,
    }
