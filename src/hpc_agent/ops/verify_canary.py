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


def _failure_features(stderr_tail: str, log_path: str | None) -> dict[str, Any]:
    """Build the structured ``failure_features`` envelope for a failed canary.

    Two layers of evidence the LLM (or human) would otherwise have to
    fetch by hand:

    * **cluster_log_tail** — the raw last ~50 lines of the canary's
      cluster log, verbatim. ``stderr_tail`` already holds this; we
      attach it under the structured key so downstream consumers don't
      have to know which top-level field to read. ``log_path`` records
      the remote path (when known) so the user can ``ssh`` over and
      tail more if they need to.

    * **classified_error** — the structured pattern match from
      :data:`failure_signatures.CATALOG` (the same classifier
      ``ops/recover`` uses for resubmit categorization). Gives the
      decision-maker an ``error_class`` + ``suggested_fix`` + the
      ``matched_pattern`` regex, so an agent can act on the structured
      remediation instead of paraphrasing the log tail. ``None`` when
      the stderr is empty (nothing to classify).
    """
    # Import inside the helper — `failure_signatures` lives in `ops/recover`,
    # and `verify_canary` is loaded at module-discovery time. Keeping the
    # import lazy avoids dragging the recover package in for callers that
    # only ever hit the happy path.
    from hpc_agent.ops.recover.failure_signatures import classify

    classified: dict[str, Any] | None = None
    if stderr_tail:
        classified = classify(stderr_tail, exit_code=None)

    return {
        "cluster_log_tail": stderr_tail,
        "log_path": log_path,
        "classified_error": classified,
    }


_DEFAULT_POLL_INTERVAL_SEC = 30
_DEFAULT_WAIT_BUDGET_SEC = 1800  # 30 min — long enough for a 1-task probe


# Remote snippet that round-trips the canary's latest checkpoint ON THE CLUSTER
# — where ``hpc_agent`` is importable in the run's own env and where a resume
# would actually reload it. Emits a single JSON line on stdout:
#   {"status": "missing"}                              → no checkpoint written
#   {"status": "unloadable", "path": ...}              → file present, won't load
#   {"status": "ok", "path": ..., "next_iteration": N} → loadable; resumes at N
# ``read_latest_checkpoint`` returns next_iteration>0 only when a checkpoint
# actually LOADED (it walks newest→oldest and skips corrupt files), so a present
# file with next_iteration<=0 is the "wrong format" signal — distinct from a
# legitimately checkpointed ``None`` state, which still yields next_iteration>0.
#
# After reading, it REMOVES the canary's _checkpoints/ dir: the canary checkpoint
# is a throwaway probe, and a result_dir_template WITHOUT {run_id} (e.g.
# "results/task_{task_id}") would otherwise have the MAIN run's task 0 share the
# dir and resume off the canary's checkpoint (run_iterations always reads latest).
# Cleanup runs after the JSON is emitted so the verdict is unaffected; harmless
# for the recommended {run_id}-scoped template where there is no sharing.
_REMOTE_CHECKPOINT_SNIPPET = (
    "import json,sys,shutil\n"
    "from hpc_agent.experiment_kit.checkpoint import "
    "latest_checkpoint, read_latest_checkpoint, checkpoint_dir\n"
    "d=sys.argv[1]\n"
    "p=latest_checkpoint(d)\n"
    "if p is None:\n"
    "    print(json.dumps({'status':'missing'}))\n"
    "else:\n"
    "    _,nxt=read_latest_checkpoint(d)\n"
    "    if int(nxt)<=0:\n"
    "        print(json.dumps({'status':'unloadable','path':str(p)}))\n"
    "    else:\n"
    "        print(json.dumps({'status':'ok','path':str(p),'next_iteration':int(nxt)}))\n"
    "shutil.rmtree(str(checkpoint_dir(d)), ignore_errors=True)\n"
)


def _verify_remote_checkpoint(
    *,
    ssh_target: str,
    remote_path: str,
    ckpt_result_dir: str,
    remote_activation: str,
) -> dict[str, Any]:
    """Round-trip the canary's checkpoint on the cluster; return a status dict.

    Runs :data:`_REMOTE_CHECKPOINT_SNIPPET` over SSH under the run's activation
    (so ``hpc_agent`` imports in the right env, exactly like the status reporter).
    *ckpt_result_dir* is the canary task's result dir (absolute, or relative to
    *remote_path*); the snippet looks under its ``_checkpoints/``.

    Returns ``{"status": "ok"|"missing"|"unloadable"|"probe_failed", ...}``. A
    ``probe_failed`` (ssh error / unparseable output) carries ``detail`` so the
    caller can fail the gate loudly rather than silently pass an unverified run.
    """
    import json
    import shlex

    from hpc_agent.infra.remote import ssh_run

    target = ckpt_result_dir
    if not target.startswith("/"):
        target = f"{remote_path.rstrip('/')}/{target.lstrip('/')}"
    # remote_activation already ends in " && " (or is empty); cd into remote_path
    # so a relative result dir resolves the same way the dispatcher rendered it.
    cmd = (
        f"{remote_activation}cd {shlex.quote(remote_path)} && "
        f"python3 -c {shlex.quote(_REMOTE_CHECKPOINT_SNIPPET)} {shlex.quote(target)}"
    )
    try:
        res = ssh_run(cmd, ssh_target=ssh_target)
    except (errors.RemoteCommandFailed, OSError) as exc:
        return {"status": "probe_failed", "detail": f"ssh checkpoint probe raised: {exc}"}
    if res.returncode != 0:
        return {
            "status": "probe_failed",
            "detail": (
                f"remote checkpoint probe exited {res.returncode}: "
                f"{(res.stderr or '').strip()[:300]}"
            ),
        }
    line = (res.stdout or "").strip().splitlines()
    if not line:
        return {"status": "probe_failed", "detail": "remote checkpoint probe produced no output"}
    try:
        parsed = json.loads(line[-1])
    except json.JSONDecodeError:
        return {
            "status": "probe_failed",
            "detail": f"remote checkpoint probe output not JSON: {line[-1][:200]!r}",
        }
    if not isinstance(parsed, dict) or "status" not in parsed:
        return {"status": "probe_failed", "detail": f"unexpected probe payload: {parsed!r}"}
    return parsed


def _resolve_canary_checkpoint_dir(
    sidecar: dict[str, Any],
    *,
    canary_run_id: str,
    explicit: str | None,
) -> str:
    """The canary task-0 result dir whose ``_checkpoints/`` the probe inspects.

    An explicit *explicit* wins. Otherwise derive it from the canary sidecar's
    ``result_dir_template`` rendered for task 0 (``{task_id}`` / ``{run_id}``) —
    the common case. A template that also references per-task kwargs cannot be
    rendered without ``tasks.resolve(0)``, so it raises
    :class:`errors.SpecInvalid` asking the caller to pass ``checkpoint_result_dir``
    explicitly (the same path it would pass for ``expect_output``).
    """
    if explicit:
        return explicit
    template = sidecar.get("result_dir_template")
    if not isinstance(template, str) or not template:
        raise errors.SpecInvalid(
            f"checkpoint verification for canary {canary_run_id!r} needs the canary's "
            "result dir, but its sidecar carries no result_dir_template and no explicit "
            "checkpoint_result_dir was passed."
        )
    try:
        rendered: str = template.format(task_id=0, run_id=canary_run_id)
        return rendered
    except (KeyError, IndexError) as exc:
        raise errors.SpecInvalid(
            f"cannot derive the canary checkpoint dir: result_dir_template "
            f"{template!r} references {exc} (a per-task kwarg) so it can't be "
            "rendered locally. Pass checkpoint_result_dir explicitly (the canary's "
            "task-0 result dir, relative to remote_path)."
        ) from None


@primitive(
    name="verify-canary",
    verb="workflow",
    composes=["poll-run-status"],
    side_effects=[
        SideEffect("ssh", "<cluster> (poll status + tail stderr)"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.SubmissionIncomplete],
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
                "--verify-checkpoint",
                action="store_true",
                help=(
                    "Checkpoint canary (#294 PR4): the canary wrote a checkpoint then "
                    "killed itself; assert a loadable checkpoint survived under the "
                    "stable _checkpoints/ dir (read_latest_checkpoint succeeds on the "
                    "cluster). Replaces the exit-0/output success criteria — a "
                    "preempted canary is EXPECTED. Use for auto_resume_on_kill runs."
                ),
            ),
            CliArg(
                "--checkpoint-result-dir",
                type=str,
                default=None,
                help=(
                    "Canary task-0 result dir (relative to remote_path or absolute) "
                    "whose _checkpoints/ the round-trip probe inspects. Omit to derive "
                    "it from the canary sidecar's result_dir_template (works unless the "
                    "template references per-task kwargs). Only used with "
                    "--verify-checkpoint."
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
    verify_checkpoint: bool = False,
    checkpoint_result_dir: str | None = None,
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
    verify_checkpoint:
        Checkpoint-canary mode (#294 PR4). When True, the canary was fired with
        ``HPC_CHECKPOINT_CANARY=1`` so it wrote a checkpoint at iteration 1 then
        killed itself at iteration 2 via the dispatcher's SIGTERM path. This
        SWAPS the success criteria: instead of "exit 0 + output present" (a
        preempted canary never completes), the gate passes iff a *loadable*
        checkpoint survived under the canary's ``_checkpoints/`` dir — proven by
        running ``read_latest_checkpoint`` on the cluster. Fails the gate with
        ``checkpoint_missing`` (no checkpoint written) or ``checkpoint_unloadable``
        (a wrong/non-portable checkpoint format) so the "my checkpoint can't be
        reloaded" class is caught BEFORE the long main array launches. The
        reporter_unreachable / timeout poll-failure paths still apply.
    checkpoint_result_dir:
        Only with *verify_checkpoint*. The canary task-0 result dir (relative to
        ``remote_path`` or absolute) whose ``_checkpoints/`` the round-trip probe
        inspects. ``None`` derives it from the canary sidecar's
        ``result_dir_template`` (task 0) — pass it explicitly when the template
        references per-task kwargs that can't be rendered locally.
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
      failed — broken cluster-side reporter) / ``"completed_unknown"`` (the
      job left the scheduler queue without recording a completion and no
      stderr marker explains why — resolved fast instead of timing out) /
      ``"timeout"`` / ``"abandoned"`` / (checkpoint mode) ``"checkpoint_missing"``
      / ``"checkpoint_unloadable"``.
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
    if expect_output and canary_run_id not in expect_output:
        # The canary writes its output under ``results/<canary_run_id>/...``
        # (the ``-canary`` suffix is part of the run_id). An ``expect_output``
        # built for the MAIN run_id — or copied from a literal example like
        # ``results/seed_42/metrics.json`` — can never match, so the check
        # would report ``missing_output`` for a canary that actually passed
        # and silently gate the main array. Refuse it at the boundary rather
        # than let a divined path mint a false negative (the per-task
        # completion count already verifies the canary produced output).
        raise errors.SpecInvalid(
            f"expect_output {expect_output!r} does not reference the canary run_id "
            f"{canary_run_id!r}: the canary writes under results/{canary_run_id}/... (note "
            f"the '-canary' suffix), so a path built for the main run_id or a literal example "
            f"like 'results/seed_42/metrics.json' can never match and would falsely fail a "
            f"canary that succeeded. Omit expect_output (the completion count already verifies "
            f"the canary's output) or pass the canary's real result path."
        )

    from hpc_agent.infra.cluster_logs import fetch_task_logs
    from hpc_agent.infra.cluster_status import ssh_status_report
    from hpc_agent.infra.clusters import remote_activation_for_sidecar
    from hpc_agent.ops.aggregate.runner import verify_combiner_artifact
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    record = load_run(experiment_dir, canary_run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for canary run_id={canary_run_id!r}")
    if not record.job_ids:
        # The qsub call structurally succeeded (a sidecar exists) but
        # cluster-side init crashed before ``job_ids`` got populated, so
        # we cannot poll scheduler state at all. Without this guard the
        # poll loop silently classifies the canary as "abandoned" (see
        # `SESSION_HANDOFF.md` "Still open" — verify-canary fallthrough),
        # masking a structural submission failure as a recoverable one.
        # The registry-backed remediation routes the operator at the
        # sidecar + cluster-side logs to find the real failure.
        raise errors.SubmissionIncomplete(
            f"canary run_id={canary_run_id!r} has no job_ids in its run record — "
            "the qsub/sbatch call structurally succeeded but cluster-side init "
            "crashed before the sidecar was fully populated; scheduler state "
            "cannot be polled. Inspect the run sidecar and cluster-side logs "
            "to find the precise failure.",
            run_id=canary_run_id,
            experiment_dir=str(experiment_dir),
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
        )
    if int(record.total_tasks) <= 0:
        # A 0-task canary can never satisfy `complete >= total_tasks > 0`,
        # so the poll loop would always exit via timeout. Reject up-front
        # with a clearer error.
        raise errors.SpecInvalid(
            f"canary run_id={canary_run_id!r} has total_tasks={record.total_tasks}; "
            "a canary must have at least 1 task"
        )

    # The control-plane status reporter runs on the login node via ssh —
    # it needs the run's conda activation, else it falls to the bare
    # login-node python that lacks ``hpc_agent``, every poll raises, and
    # the loop reports ``reporter_unreachable`` instead of the canary's
    # real status (#176). Derive the activation from the canary's sidecar
    # (cluster + resolved env) exactly as ``ops/monitor/status.py`` does
    # for the normal status path. Best-effort: an unreadable sidecar
    # yields ``""`` → bare python (the unchanged fallback).
    try:
        _canary_sidecar = read_run_sidecar(experiment_dir, canary_run_id)
    except (OSError, ValueError):
        _canary_sidecar = {}
    remote_activation = remote_activation_for_sidecar(_canary_sidecar)

    deadline = time.monotonic() + int(wait_budget_sec)
    last_summary: dict[str, Any] = {}
    last_poll_error: Exception | None = None
    got_report = False
    # A vanished canary (finished/failed fast and left the scheduler queue
    # before we polled) shows an all-zero LIVE summary: no result file yet
    # (complete=0), and the scheduler no longer lists the job so it adds
    # nothing to running/pending/failed. That is NOT terminal on its own —
    # it also describes the transient window right after qsub, before the
    # scheduler registers the array. So we require the all-zero state to
    # PERSIST across consecutive polls before declaring the job gone, rather
    # than riding the full wait_budget_sec polling an absent job (#193).
    vanished_polls = 0
    _VANISHED_POLLS_TO_TERMINAL = 2
    job_vanished = False
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
                remote_activation=remote_activation,
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
        unknown = int(last_summary.get("unknown") or 0)
        # Terminal: complete == total OR (failed > 0 and no running/pending).
        if complete >= int(record.total_tasks) and record.total_tasks > 0:
            break
        if failed > 0 and running == 0 and pending == 0:
            break
        # Job absent from the scheduler's live view: nothing complete, nothing
        # failed, nothing queued/running, nothing in the unknown bucket. Count
        # consecutive such polls; once it persists, the canary finished (or
        # died) and left the queue — break fast as ``completed_unknown`` rather
        # than time out. The stderr scan below still runs, so a real failure
        # marker (OOM, traceback) is preferred over the bland unknown verdict.
        if complete == 0 and failed == 0 and running == 0 and pending == 0 and unknown == 0:
            vanished_polls += 1
            if vanished_polls >= _VANISHED_POLLS_TO_TERMINAL:
                job_vanished = True
                break
        else:
            vanished_polls = 0
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
                "failure_features": _failure_features("", None),
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
            "failure_features": _failure_features("", None),
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
    log_path: str | None = None
    if logs and isinstance(logs[0], dict):
        stderr_tail = str(logs[0].get("content") or "")
        log_path = logs[0].get("path") if isinstance(logs[0].get("path"), str) else None

    # Checkpoint canary (#294 PR4): SWAP the success criteria. The canary was
    # SUPPOSED to be preempted (exit 130) after writing one checkpoint, so the
    # exit-0/marker/output assertions below don't apply — a preempted dispatch is
    # the expected outcome, not a failure. The ONLY thing that matters is that a
    # loadable checkpoint survived under the stable _checkpoints/ dir. Run the
    # round-trip probe on the cluster (where a resume would reload it) and gate on
    # it directly. A genuine executor crash before iteration 1 leaves no
    # checkpoint → checkpoint_missing, with the real stderr classified into
    # failure_features for the operator.
    if verify_checkpoint:
        ckpt_dir = _resolve_canary_checkpoint_dir(
            _canary_sidecar, canary_run_id=canary_run_id, explicit=checkpoint_result_dir
        )
        probe = _verify_remote_checkpoint(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            ckpt_result_dir=ckpt_dir,
            remote_activation=remote_activation,
        )
        status = probe.get("status")
        if status == "ok":
            return {
                "ok": True,
                "failure_kind": None,
                "details": (
                    f"canary {canary_run_id!r} checkpoint round-trip verified: a loadable "
                    f"checkpoint survived the preemption kill under {ckpt_dir}/_checkpoints "
                    f"(resumes at iteration {probe.get('next_iteration')})."
                ),
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": None,
            }
        if status == "missing":
            return {
                "ok": False,
                "failure_kind": "checkpoint_missing",
                "details": (
                    f"canary {canary_run_id!r} wrote NO checkpoint under {ckpt_dir}/_checkpoints "
                    "before the iteration-2 kill — the executor never checkpointed (does it drive "
                    "its loop through run_iterations / write_checkpoint?), or it crashed before "
                    "iteration 1. A run that opted into auto_resume_on_kill would make NO progress "
                    "on resume. See the stderr tail / classified_error for the cause."
                ),
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": _failure_features(stderr_tail, log_path),
            }
        if status == "unloadable":
            return {
                "ok": False,
                "failure_kind": "checkpoint_unloadable",
                "details": (
                    f"canary {canary_run_id!r} wrote a checkpoint ({probe.get('path')}) but "
                    "read_latest_checkpoint could NOT reload it — the checkpoint format does not "
                    "round-trip (e.g. a pickle that needs a class/lib absent at resume time). A "
                    "long auto_resume_on_kill run would discover this only at resume, hours in. "
                    "Fix the checkpoint format before launching the main array."
                ),
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": _failure_features(stderr_tail, log_path),
            }
        # probe_failed (ssh error / unparseable output) — fail loudly rather than
        # silently pass an unverified checkpoint, same posture as reporter_unreachable.
        return {
            "ok": False,
            "failure_kind": "reporter_unreachable",
            "details": (
                f"canary {canary_run_id!r} checkpoint probe could not run: "
                f"{probe.get('detail')}. Cannot confirm the checkpoint round-trips, so the "
                "canary CANNOT be trusted — fix the cluster env (hpc-agent importable in the "
                "run's conda env) before submitting the main array."
            ),
            "stderr_tail": stderr_tail,
            "metrics_fingerprint": None,
            "failure_features": _failure_features(stderr_tail, log_path),
        }

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
                "failure_features": _failure_features(stderr_tail, log_path),
            }

    # The canary left the scheduler queue without ever recording a completion
    # and no stderr marker explains why (#193). We can't trust it as passed —
    # something ended the job (a fast non-zero exit that cleared the queue, a
    # scheduler kill, a vanished array) — but we also resolved it in seconds
    # instead of riding the full wait budget. Fail with ``completed_unknown``
    # so the two-phase gate refuses the main array and the agent investigates,
    # rather than the old behaviour of reporting ``timeout`` after 30 minutes.
    if job_vanished and int(last_summary.get("complete") or 0) < int(record.total_tasks):
        return {
            "ok": False,
            "failure_kind": "completed_unknown",
            "details": (
                f"canary {canary_run_id!r} left the scheduler queue without "
                "recording a completion and no stderr marker explains why — it "
                "finished or was killed too fast to observe. Refusing to pass the "
                "canary; inspect the job log / scheduler accounting before "
                f"submitting the main array (last summary: {last_summary})."
            ),
            "stderr_tail": stderr_tail,
            "metrics_fingerprint": None,
            "failure_features": _failure_features(stderr_tail, log_path),
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
                "failure_features": _failure_features(stderr_tail, log_path),
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
            "failure_features": _failure_features(stderr_tail, log_path),
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

    # #249: record this cmd_sha as canary-validated so a re-submit of the SAME
    # cmd_sha within HPC_CANARY_TTL_SEC skips the redundant canary. Best-effort,
    # success-only — a failed canary never reaches here, so it is never cached.
    _cmd_sha = str(_canary_sidecar.get("cmd_sha") or "")
    if _cmd_sha:
        from hpc_agent import __version__ as _pkg_version
        from hpc_agent.state import canary_cache

        canary_cache.record_canary_validated(
            canary_cache.canary_cache_key(cmd_sha=_cmd_sha, version=_pkg_version or "")
        )

    return {
        "ok": True,
        "failure_kind": None,
        "details": (
            f"canary {canary_run_id!r} verified: exit 0, no error markers, "
            + (f"output {expect_output!r} present." if expect_output else "no output check.")
        ),
        "stderr_tail": stderr_tail,
        "metrics_fingerprint": metrics_fingerprint,
        # ``failure_features`` is only attached to ``ok=False`` envelopes — the
        # success envelope intentionally omits it so consumers can use its
        # presence as a "this is a failed canary" sentinel.
        "failure_features": None,
    }
