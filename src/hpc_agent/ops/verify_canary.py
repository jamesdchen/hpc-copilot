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

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

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
    composes=["poll-run-status"],
    side_effects=[
        SideEffect("ssh", "<cluster> (poll status + tail stderr)"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable],
    idempotent=True,
    idempotency_key="canary_run_id",
    cli=CliShape(
        help=(
            "Wait + grep + output-check for a 1-task canary submission. "
            "Polls until terminal, scans stderr for known failure markers, "
            "optionally checks expect_output exists. Returns "
            "{ok, failure_kind, details, stderr_tail}."
        ),
        experiment_dir_arg=True,
        requires_ssh=True,
        args=(
            CliArg(
                "--canary-run-id",
                type=str,
                required=True,
                help="Run ID of the canary (typically <main_run_id>-canary).",
            ),
            CliArg(
                "--expect-output",
                type=str,
                default=None,
                help="Optional path (relative to remote_path) the canary should have written.",
            ),
            CliArg(
                "--fingerprint",
                type=str,
                default=None,
                help=(
                    "Optional remote path (relative to result_dir or absolute) to "
                    "SHA256 over SSH; hex digest returned as data.metrics_fingerprint."
                ),
            ),
            CliArg(
                "--poll-interval-sec",
                type=int,
                default=30,
                help="Seconds between status polls (default 30).",
            ),
            CliArg(
                "--wait-budget-sec",
                type=int,
                default=1800,
                help="Total seconds to wait for terminal before giving up (default 1800).",
            ),
        ),
    ),
    agent_facing=True,
)
def verify_canary(
    experiment_dir: Path,
    *,
    canary_run_id: str,
    expect_output: str | None = None,
    fingerprint: str | None = None,
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
    fingerprint:
        Relative path (under the canary's result_dir) of a file to
        SHA256 over SSH. The hex digest is returned as
        ``data.metrics_fingerprint``. Lets the caller diff the canary
        output against a local reference run of the same task to detect
        framework-induced divergence (different GPU SKU, library drift,
        env-var collision). ``None`` skips the fingerprint.
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
      ``"dispatcher_failed"`` / ``"import_error"`` / ``"module_not_found"`` /
      ``"traceback"`` / ``"oom_killed"`` / ``"segfault"`` /
      ``"missing_output"`` / ``"reporter_unreachable"`` (every status poll
      failed — broken cluster-side reporter) / ``"timeout"`` /
      ``"abandoned"``.
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

    from hpc_agent.infra.cluster_logs import fetch_task_logs
    from hpc_agent.infra.cluster_status import ssh_status_report
    from hpc_agent.ops.aggregate.runner import verify_combiner_artifact
    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, canary_run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for canary run_id={canary_run_id!r}")
    if int(record.total_tasks) <= 0:
        # A 0-task canary can never satisfy `complete >= total_tasks > 0`,
        # so the poll loop would always exit via timeout. Reject up-front
        # with a clearer error.
        raise errors.SpecInvalid(
            f"canary run_id={canary_run_id!r} has total_tasks={record.total_tasks}; "
            "a canary must have at least 1 task"
        )

    deadline = time.monotonic() + int(wait_budget_sec)
    last_summary: dict[str, Any] = {}
    last_poll_error: Exception | None = None
    got_report = False
    while time.monotonic() < deadline:
        try:
            report = ssh_status_report(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                run_id=canary_run_id,
                job_ids=list(record.job_ids),
                job_name=record.job_name,
                log_dir=log_dir,
                file_glob=file_glob,
            )
        except (errors.RemoteCommandFailed, OSError) as exc:
            last_poll_error = exc
            time.sleep(int(poll_interval_sec))
            continue
        got_report = True
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
        # Distinguish a broken reporter (EVERY poll raised, we never got a
        # single status read) from a genuine slow/stuck run (polls
        # succeeded but the run never went terminal). A broken reporter
        # must fail the canary LOUDLY with the real cause — otherwise it
        # masquerades as a timeout, the agent retries, and the main array
        # submits against a cluster whose results can't even be read. This
        # is exactly the failure mode where 8 tasks die on a module/env
        # error but the canary "passes" because verification couldn't run.
        if not got_report and last_poll_error is not None:
            return {
                "ok": False,
                "failure_kind": "reporter_unreachable",
                "details": (
                    f"canary {canary_run_id!r}: every status poll failed — the "
                    f"cluster-side reporter never returned (last error: {last_poll_error}). "
                    "The scheduler may have run the job, but the framework cannot read its "
                    "result, so the canary CANNOT be trusted as passed. Common cause: "
                    "hpc-agent not importable in the cluster's python (wrong/absent conda "
                    "env) or a module-load failure in the job preamble. Fix the cluster env "
                    "before submitting the main array."
                ),
                "stderr_tail": "",
                "metrics_fingerprint": None,
            }
        return {
            "ok": False,
            "failure_kind": "timeout",
            "details": (
                f"canary {canary_run_id!r} did not terminate within "
                f"{wait_budget_sec}s (last summary: {last_summary})"
            ),
            "stderr_tail": "",
            "metrics_fingerprint": None,
        }

    # Fetch the canary's stderr tail (1 task, task_id=0).
    # Resolve scheduler from clusters.yaml — substring-matching on the
    # cluster name misroutes any cluster whose name doesn't literally
    # contain "slurm" (discovery, hoffman2, cascade, …) to the SGE
    # log-path template.
    from hpc_agent.infra.clusters import load_clusters_config

    try:
        clusters_cfg = load_clusters_config()
    except Exception:  # noqa: BLE001
        clusters_cfg = {}
    scheduler = (clusters_cfg.get(record.cluster) or {}).get("scheduler")
    if not scheduler:
        raise errors.SpecInvalid(
            f"cannot resolve scheduler for canary cluster {record.cluster!r}: "
            f"absent from clusters.yaml or missing a 'scheduler' key — refusing "
            f"to guess 'slurm' and risk misrouting the SGE log fetch"
        )

    logs = fetch_task_logs(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        job_name=record.job_name,
        job_ids=list(record.job_ids),
        scheduler=scheduler,
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
                "metrics_fingerprint": None,
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
                "metrics_fingerprint": None,
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
            "metrics_fingerprint": None,
        }

    # Optional fingerprint. Best-effort: a fingerprint failure does NOT
    # invalidate the canary (the run itself is fine; we just couldn't
    # hash). Returns ``None`` when unavailable rather than raising.
    # ``fingerprint`` is treated as an absolute remote path or a path
    # relative to ``record.remote_path``; the caller is responsible for
    # constructing it (the sidecar's ``result_dir_template`` plus
    # ``tasks.resolve(0)`` is the canonical local-side derivation).
    metrics_fingerprint: str | None = None
    if fingerprint:
        import shlex

        from hpc_agent.infra.remote import ssh_run

        target = fingerprint
        if not target.startswith("/"):
            target = f"{record.remote_path.rstrip('/')}/{target.lstrip('/')}"
        try:
            sha = ssh_run(
                f"sha256sum {shlex.quote(target)} 2>/dev/null | awk '{{print $1}}'",
                ssh_target=record.ssh_target,
            )
            if sha.returncode == 0:
                metrics_fingerprint = sha.stdout.strip() or None
        except (errors.RemoteCommandFailed, OSError):
            metrics_fingerprint = None

    return {
        "ok": True,
        "failure_kind": None,
        "details": (
            f"canary {canary_run_id!r} verified: exit 0, no error markers, "
            + (f"output {expect_output!r} present." if expect_output else "no output check.")
        ),
        "stderr_tail": stderr_tail,
        "metrics_fingerprint": metrics_fingerprint,
    }
