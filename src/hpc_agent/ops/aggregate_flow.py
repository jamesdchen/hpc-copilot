"""``aggregate-flow``: workflow atom that finalizes a run's aggregated metrics.

Third workflow atom in the :mod:`hpc_agent.ops.submit_flow` /
:mod:`hpc_agent.ops.monitor_flow` family. Pipeline:

1. Read the per-run sidecar to discover the wave_map + remote_path.
2. (Optional, default on) ``ensure_all_combined`` — for every wave in
   the wave_map that isn't yet in ``record.combined_waves``, invoke
   ``runner.combine_wave``. The first attempt runs with ``force=false``;
   if it fails the subsequent ``max_retries`` attempts run with
   ``force=true``. Idempotent: already-combined waves are no-ops; this
   just guarantees no missing partials before the pull.
3. ``rsync_pull`` the cluster's ``_combiner/`` directory locally.
4. ``reduce_partials`` over the local dir → aggregated metrics dict.
5. (Optional) ``rsync_pull`` per-task result summaries matching
   ``summary_glob`` from the cluster's ``results/`` subtree.
6. (Optional) Run two deterministic post-aggregation gates:

   * **Non-empty rows** (``spec.min_rows > 0``) — the cluster-side
     status reporter is run with ``--min-rows``; task ids whose CSV
     result has fewer than ``min_rows`` data rows beyond the header are
     surfaced in ``nonempty_failing_task_ids``. ``ok`` from the combiner
     only means every task wrote *a file* — this gate proves the file
     has real data.
   * **Expected columns + non-NaN metric** — when the run sidecar's
     ``results`` block declares ``expected_columns`` / ``metric_column``,
     every pulled per-task result file is checked for the declared
     columns and a non-NaN metric value; violations land in
     ``column_violations``. A clean no-op when no schema is declared.

7. Return :class:`AggregateFlowResult` — paths + metrics + which waves
   were combined this call vs already-combined + the gate results.

Composition fit: the campaign loop's per-iteration code goes
``submit-flow → monitor-flow → aggregate-flow`` (or skips aggregate-flow
when the per-iteration metric is read directly from the sidecar's
reduce JSONs). Slash commands and external orchestrators consume the
same atom indistinguishably.

Idempotency: every individual step is idempotent. ``combine-wave`` dedups
already-combined waves; ``rsync_pull`` is a directory sync; ``reduce_partials``
is a pure function over the pulled files. Re-invoking aggregate-flow on
the same run_id is safe and cheap.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.cli._dispatch import CliArg, CliShape, SchemaRef
from hpc_agent.execution.mapreduce.reduce.metrics import collect_wave_errors, reduce_partials
from hpc_agent.infra.ssh_validation import validate_ssh_target
from hpc_agent.infra.transport import rsync_pull
from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.ops.monitor.reconcile import mark_terminal
from hpc_agent.ops.monitor.status import record_status
from hpc_agent.ops.monitor.terminal import _is_terminal
from hpc_agent.state.journal import load_run
from hpc_agent.state.run_record import TERMINAL_STATUSES
from hpc_agent.state.runs import read_run_sidecar

__all__ = ["aggregate_flow", "AggregateFlowResult"]


@dataclass(frozen=True)
class AggregateFlowResult:
    """Return shape of :func:`aggregate_flow`."""

    run_id: str
    combined_waves: list[int]
    failed_waves: list[int]
    waves_combined_this_call: list[int]
    combiner_dir_local: str
    aggregated_metrics: dict[str, dict[str, Any]]
    summaries_dir_local: str | None = None
    escalation_reason: str | None = None
    nonempty_rows_checked: bool = False
    nonempty_failing_task_ids: list[int] | None = None
    columns_checked: bool = False
    column_violations: list[dict[str, Any]] | None = None

    def to_envelope_data(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "combined_waves": list(self.combined_waves),
            "failed_waves": list(self.failed_waves),
            "waves_combined_this_call": list(self.waves_combined_this_call),
            "combiner_dir_local": self.combiner_dir_local,
            "aggregated_metrics": dict(self.aggregated_metrics),
            "summaries_dir_local": self.summaries_dir_local,
            "escalation_reason": self.escalation_reason,
            "nonempty_rows_checked": self.nonempty_rows_checked,
            "nonempty_failing_task_ids": list(self.nonempty_failing_task_ids or []),
            "columns_checked": self.columns_checked,
            "column_violations": list(self.column_violations or []),
        }


def _validate_ssh_target(ssh_target: str) -> str:
    """Wrap :func:`validate_ssh_target` to raise the surface-appropriate
    error type. See :mod:`hpc_agent.infra.remote.validate_ssh_target`.
    """
    try:
        return validate_ssh_target(ssh_target)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc


def _missing_waves(wave_map_keys: list[str], already_combined: list[int]) -> list[int]:
    """Wave numbers in the wave_map that aren't in combined_waves yet."""
    seen = set(already_combined)
    waves: list[int] = []
    for k in wave_map_keys:
        try:
            wave_num = int(k)
        except (TypeError, ValueError):
            continue
        if wave_num not in seen:
            waves.append(wave_num)
    return sorted(waves)


# Matches a combiner partial file name (``wave_<N>.json``) — same shape
# the reducer enforces in :mod:`hpc_agent.execution.mapreduce.reduce.metrics`.
# Anchored so ``wave_3.runtime.json`` does not slip through.
_WAVE_PARTIAL_NAME_RE = re.compile(r"^wave_(\d+)\.json$")


def _incremental_include_patterns(
    combiner_local: Path, combined_waves: list[int]
) -> list[str] | None:
    """Return rsync ``--include`` patterns to fetch only not-yet-pulled waves.

    Returns ``None`` when the caller should fall back to an unfiltered
    pull. Two cases produce ``None``:

    * ``combined_waves`` is empty — no per-wave state to narrow on; the
      caller intentionally relies on whatever the cluster has under
      ``_combiner/`` (the "no wave_map" path documented above).
    * No combiner files are present locally yet (first call) AND every
      combined wave is missing locally — an unfiltered pull is the
      simplest equivalent and avoids emitting a long argv.

    Otherwise returns a list of ``wave_<N>.json`` / ``wave_<N>.runtime.json``
    include patterns sized to the diff. An empty diff (everything already
    pulled) returns ``["wave_NONE_SENTINEL"]`` so rsync's filter excludes
    every file — the rsync still runs (cheap directory stat) but
    transfers nothing.
    """
    if not combined_waves:
        return None
    have_locally: set[int] = set()
    if combiner_local.is_dir():
        for child in combiner_local.iterdir():
            m = _WAVE_PARTIAL_NAME_RE.match(child.name)
            if m is not None:
                have_locally.add(int(m.group(1)))
    needed = sorted(set(combined_waves) - have_locally)
    if not have_locally and len(needed) == len(set(combined_waves)):
        # First call (or nothing pulled yet) — no benefit to narrowing.
        return None
    if not needed:
        # Everything already pulled: include a non-matching pattern so
        # the trailing ``--exclude='*'`` skips every wave file, leaving
        # the local tree untouched while still confirming connectivity.
        return ["wave_NONE_SENTINEL.json"]
    patterns: list[str] = []
    for wave in needed:
        patterns.append(f"wave_{wave}.json")
        patterns.append(f"wave_{wave}.runtime.json")
    return patterns


def _nonempty_failing_task_ids(
    run_id: str,
    *,
    ssh_target: str,
    remote_path: str,
    job_ids: list[str],
    job_name: str,
    min_rows: int,
) -> list[int]:
    """Return task ids whose CSV result has fewer than *min_rows* data rows.

    Runs the cluster-side status reporter twice — once with ``--min-rows 0``
    (a file with just a header still counts complete) and once with
    ``--min-rows <min_rows>`` — and diffs the two ``complete`` task sets.
    A task that is ``complete`` at min_rows=0 but NOT at min_rows=N wrote a
    result file with too few real data rows: that is the precise
    "wrote something, but no real data" signal Check 1 gates on.

    Pure read-only: two SSH round-trips, no cluster-side or local writes.
    """
    from hpc_agent.infra.cluster_status import ssh_status_report

    def _complete_ids(rows: int) -> set[int]:
        report = ssh_status_report(
            ssh_target=ssh_target,
            remote_path=remote_path,
            run_id=run_id,
            job_ids=job_ids,
            job_name=job_name,
            min_rows=rows,
        )
        out: set[int] = set()
        for tid_str, entry in (report.get("tasks") or {}).items():
            if isinstance(entry, dict) and entry.get("status") == "complete":
                try:
                    out.add(int(tid_str))
                except (TypeError, ValueError):
                    continue
        return out

    complete_lenient = _complete_ids(0)
    complete_strict = _complete_ids(min_rows)
    return sorted(complete_lenient - complete_strict)


def _combine_missing(
    experiment_dir: Path,
    run_id: str,
    *,
    ssh_target: str,
    remote_path: str,
    waves: list[int],
    max_retries: int,
) -> tuple[list[int], list[tuple[int, str]]]:
    """Run combine-wave for each missing wave; return (combined_now, failures).

    Failures is a list of (wave, stderr_tail) for waves that exhausted retries.
    Combined-now lists the waves that went from missing → combined this call.
    """
    combined_now: list[int] = []
    failures: list[tuple[int, str]] = []
    for wave in waves:
        for attempt in range(1, max_retries + 2):  # initial + max_retries
            ok, _stdout, stderr = combine_wave(
                experiment_dir,
                run_id,
                wave=wave,
                ssh_target=ssh_target,
                remote_path=remote_path,
                force=(attempt > 1),
            )
            if ok:
                combined_now.append(wave)
                break
            if attempt > max_retries:
                failures.append((wave, (stderr or "").strip()[-500:]))
                break
    return combined_now, failures


def _combiner_only_reduce(
    experiment_dir: Path,
    run_id: str,
    *,
    record: Any,
    combiner_local: Path,
) -> tuple[dict[str, Any], list[int]]:
    """Pull the cluster ``_combiner/`` partials and reduce them locally.

    The default aggregation path. Returns ``(aggregated_metrics,
    incomplete_waves)``.

    Incremental rsync: rather than walking the entire cluster-side
    ``_combiner/`` tree on every call (slow for runs with 1000+ waves even when
    nothing changed), narrow the pull to the waves not already present locally.
    State source is ``record.combined_waves``; the diff against locally-present
    ``wave_<N>.json`` files is the set still to fetch. When the diff equals the
    full set (first call) or ``combined_waves`` is empty, an unfiltered pull is
    emitted so behaviour matches the original.
    """
    include_patterns = _incremental_include_patterns(combiner_local, list(record.combined_waves))
    pull = rsync_pull(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        remote_subdir="_combiner",
        local_dir=str(combiner_local),
        include=include_patterns,
    )
    if pull.returncode != 0:
        # No partials at all is a terminal pull failure; partials present
        # but rsync hiccupped is recoverable on retry — surface either way.
        stderr_tail = (pull.stderr or "").strip()
        # Diagnose the most common failure shape: the cluster-side
        # ``_combiner/`` directory doesn't exist at all. That means the
        # cluster combiner step never ran (usually: the reporter died on
        # a missing module / env issue), not that rsync had a transient
        # hiccup. Surface the three concrete recovery paths so the
        # caller doesn't waste a retry-loop on a precondition failure.
        # Match both rsync's wording and OpenSSH scp's — different
        # transports surface the same condition with slightly different
        # phrasing depending on the platform.
        no_such = "No such file or directory" in stderr_tail or "does not exist" in stderr_tail
        if no_such:
            has_agg_cmd = False
            try:
                sidecar = read_run_sidecar(experiment_dir, run_id)
                has_agg_cmd = bool((sidecar.get("aggregate_defaults") or {}).get("aggregate_cmd"))
            except (
                FileNotFoundError,
                OSError,
                json.JSONDecodeError,
                UnicodeDecodeError,
                errors.HpcError,
            ):
                pass
            cluster_reduce_hint = (
                f"Run `hpc-agent cluster-reduce --run-id {run_id}` — uses the "
                f"sidecar's aggregate_cmd directly, no combiner needed."
                if has_agg_cmd
                else (
                    f"`hpc-agent cluster-reduce --run-id {run_id}` is NOT available "
                    f"here because the run sidecar has no aggregate_defaults.aggregate_cmd. "
                    f"Configure one at submit time (write_run_sidecar's aggregate_defaults arg) "
                    f"to enable this path on future runs."
                )
            )
            raise errors.RemoteCommandFailed(
                f"the cluster-side _combiner/ for run_id {run_id!r} does not "
                f"exist at {record.remote_path}/{run_id}/_combiner/ — the "
                f"combiner step never ran. Usually this means the cluster-side "
                f"reporter died (check per-task stderr under "
                f"{record.remote_path}/{run_id}/logs/). Three recovery paths: "
                f"(1) fix the cluster env (likely a missing Python module) and "
                f"resubmit — addresses the root cause. "
                f"(2) {cluster_reduce_hint} "
                f"(3) scp the raw per-task results locally and reduce on the laptop. "
                f"rsync_pull stderr: {stderr_tail[:300]}"
            )
        raise errors.RemoteCommandFailed(
            f"rsync_pull of _combiner failed (exit {pull.returncode}): {stderr_tail[:300]}"
        )

    # Reduce locally.
    aggregated = reduce_partials(combiner_local)
    # Waves where the combiner couldn't read every task's metrics.json
    # contribute a partial grid_points set; reduce_partials means over
    # only the readable subset. Surface those waves so the caller does
    # not treat the aggregate as computed over the full task set.
    incomplete_waves = sorted(collect_wave_errors(combiner_local))
    return aggregated, incomplete_waves


def _cluster_final_reduce(
    experiment_dir: Path,
    run_id: str,
    *,
    record: Any,
    out: Path,
) -> tuple[dict[str, Any], list[int]]:
    """Run the cross-wave reduce ON THE CLUSTER, pull only the aggregate (#254).

    Opt-in via ``HPC_CLUSTER_FINAL_REDUCE=1``. Invokes the combiner's ``--final``
    mode (:func:`hpc_agent.infra.transport.run_final_reduce`) so the cluster
    writes a single ``_aggregated/<run_id>/metrics_aggregate.json``, then pulls
    just that KB-scale file instead of every ``_combiner/wave_*.json``. The
    combiner is stdlib-only, so the run's env activation is threaded through (a
    too-old login-node python3 would still fail) and the aggregate's
    ``aggregated_metrics`` is byte-for-byte what the local reduce produces.
    Returns ``(aggregated_metrics, incomplete_waves)``.
    """
    from hpc_agent.infra.clusters import remote_activation_for_sidecar
    from hpc_agent.infra.transport import run_final_reduce

    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError, errors.HpcError):
        sidecar = {}
    remote_activation = remote_activation_for_sidecar(sidecar)

    proc = run_final_reduce(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        run_id=run_id,
        force=True,  # idempotent: aggregate_flow may be re-run; always refresh
        remote_activation=remote_activation,
    )
    if proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"cluster final-reduce for run_id {run_id!r} failed "
            f"(exit {proc.returncode}): {(proc.stderr or '').strip()[:300]}"
        )

    agg_local = out / "_aggregated" / run_id
    pull = rsync_pull(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        remote_subdir=f"_aggregated/{run_id}",
        local_dir=str(agg_local),
        include=["metrics_aggregate.json"],
    )
    if pull.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"pull of metrics_aggregate.json for run_id {run_id!r} failed "
            f"(exit {pull.returncode}): {(pull.stderr or '').strip()[:300]}"
        )
    try:
        data = json.loads((agg_local / "metrics_aggregate.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise errors.RemoteCommandFailed(
            f"cluster final-reduce produced no readable metrics_aggregate.json "
            f"for run_id {run_id!r}: {exc}"
        ) from exc

    aggregated = data.get("aggregated_metrics") or {}
    incomplete = (data.get("provenance") or {}).get("incomplete_waves") or []
    return aggregated, [int(w) for w in incomplete]


def _aggregate_flow_arg_pre(ns: Any) -> dict[str, Any]:
    """Resolve ``--spec`` vs ``--run-id`` shortcut for ``aggregate-flow``.

    The canonical authoring path is ``--spec <file>``; ``--run-id <id>``
    is a 1-field shortcut for the common case where every other
    ``AggregateFlowSpec`` field is at its default. Mutually exclusive:
    passing both is ambiguous (a JSON spec is a superset, but silently
    preferring one would mask a caller bug); passing neither leaves the
    primitive uninvocable.

    The dispatcher pre-loads ``--spec`` and stores it under ``kwargs["spec"]``
    (or ``None`` when omitted, since ``spec_required=False`` here). This hook
    runs after that, so returning ``{"spec": ...}`` overrides the dispatcher's
    value with the synthesized one.
    """
    spec_path = getattr(ns, "spec", None)
    run_id = getattr(ns, "run_id", None)
    if spec_path is not None and run_id is not None:
        raise errors.SpecInvalid(
            "aggregate-flow: pass either --spec <file> or --run-id <id>, "
            "not both (ambiguous — pick one)."
        )
    if spec_path is None and run_id is None:
        raise errors.SpecInvalid(
            "aggregate-flow requires either --spec <file> (full JSON "
            "AggregateFlowSpec) or --run-id <id> (1-field shortcut when "
            "every other field is at its default)."
        )
    if spec_path is None:
        # --run-id shortcut: synthesize a minimal spec. ``run_id`` was
        # asserted non-None above, but pydantic also re-validates the
        # string against the RunIdStrict pattern — surface that as
        # SpecInvalid like every other spec-validation error path.
        assert run_id is not None  # narrowing: guarded by the both-None branch
        try:
            return {"spec": AggregateFlowSpec(run_id=str(run_id))}
        except Exception as exc:  # noqa: BLE001 — pydantic ValidationError shape
            raise errors.SpecInvalid(str(exc)) from exc
    # --spec path: dispatcher already loaded and validated it; nothing to add.
    return {}


@primitive(
    name="aggregate-flow",
    verb="workflow",
    composes=["combine-wave", "poll-run-status", "mark-run-terminal"],
    side_effects=[
        SideEffect("ssh", "<cluster>"),
        SideEffect("sync-pull", "<ssh_target>:<remote_path> -> <experiment_dir>/_aggregated/"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json"),
    ],
    error_codes=[
        errors.SshUnreachable,
        errors.CombinerFailed,
        errors.OutputsMissing,
        errors.JournalCorrupt,
        errors.PreconditionFailed,
        errors.SpecInvalid,  # mode/spec validation, ssh-target check
        errors.RemoteCommandFailed,  # rsync failure in the cluster-reduce path
    ],
    idempotent=True,
    idempotency_key="run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
    cli=CliShape(
        help=(
            "Workflow atom: ensure all waves combined on the cluster, "
            "rsync the _combiner/ partials locally, reduce_partials over "
            "them, optionally pull per-task summaries. Third atom in the "
            "submit-flow → monitor-flow → aggregate-flow campaign chain."
        ),
        spec_arg=True,
        spec_model=AggregateFlowSpec,
        schema_ref=SchemaRef(input="aggregate_flow"),
        spec_required=False,  # --run-id is the alternative; arg_pre enforces XOR.
        experiment_dir_arg=True,
        requires_ssh=True,
        dry_run_arg=True,
        dry_run_passthrough_keys=("run_id", "ensure_all_combined", "pull_summaries", "output_dir"),
        args=(
            CliArg(
                "--run-id",
                type=str,
                default=None,
                help=(
                    'Shortcut for the 1-field spec {"run_id": <id>}. Mutually '
                    "exclusive with --spec. Use --spec when overriding any other "
                    "AggregateFlowSpec field (output_dir, ensure_all_combined, "
                    "combiner_max_retries, pull_summaries, summary_glob, "
                    "results_subdir, min_rows, mode)."
                ),
            ),
        ),
        arg_pre=_aggregate_flow_arg_pre,
    ),
    agent_facing=True,
)
def aggregate_flow(
    experiment_dir: Path,
    *,
    spec: AggregateFlowSpec,
    mode: str | None = None,
    aggregate_cmd: str | None = None,
    aggregate_output_path: str | None = None,
) -> AggregateFlowResult:
    """Finalize a run's aggregation; return paths + reduced metrics.

    The wire-validated ``spec`` carries the user-facing knobs
    (``run_id``, ``output_dir``, ``ensure_all_combined``,
    ``combiner_max_retries``, ``pull_summaries``, ``summary_glob``,
    ``results_subdir``, ``mode``); ``aggregate_cmd`` /
    ``aggregate_output_path`` are framework-mode flags that don't
    belong on the wire (the framework decides which mode to enter
    based on ``aggregate_defaults`` recorded on the run sidecar).

    Raises
    ------
    JournalCorrupt:
        No sidecar for run_id.
    SpecInvalid:
        pull_summaries=True without summary_glob; malformed ssh_target.
    SshUnreachable, RemoteCommandFailed:
        rsync or SSH layer errors propagated from the underlying helpers.
    """
    # Destructure the spec into typed locals so the body reads naturally
    # and mypy sees each field's narrowed type. The spec itself is
    # the wire-validated authoring SoT (schemas/aggregate_flow.input.json
    # is regenerated from AggregateFlowSpec).
    run_id = spec.run_id
    output_dir = spec.output_dir
    # #188: aggregate-flow appends ``_combiner/`` to output_dir for the pulled
    # wave partials; the cluster combiner appends another ``_combiner/`` of its
    # own. If the caller's output_dir already ENDS in ``_combiner``, the joined
    # path becomes ``<...>/_combiner/_combiner/wave_*.json`` and the consumer
    # (verify-aggregation-complete, the local reducer) silently sees zero
    # partials. Refuse at intake rather than nest.
    if output_dir is not None and Path(output_dir).name == "_combiner":
        raise errors.SpecInvalid(
            f"output_dir basename is '_combiner' ({output_dir!r}); aggregate-flow "
            "would nest the wave partials at '<output_dir>/_combiner/wave_*.json', "
            "producing '_combiner/_combiner/' on disk. Use a parent directory or "
            "different name (default: <experiment_dir>/_aggregated/<run_id>)."
        )
    ensure_all_combined = spec.ensure_all_combined
    combiner_max_retries = spec.combiner_max_retries
    pull_summaries = spec.pull_summaries
    summary_glob = spec.summary_glob
    results_subdir = spec.results_subdir
    # Explicit kwarg overrides spec (back-compat seam); otherwise read from
    # spec where mode now lives as a wire-validated Literal.
    if mode is None:
        mode = spec.mode

    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for {run_id!r}; submit the run first")

    # Skip-monitor reconcile (opt-in): the caller went straight to aggregate
    # on a short run without running monitor-flow, so the journal still says
    # in_flight. Poll the cluster ONCE and, if it confirms the run is done,
    # mark the journal terminal using the SAME completion logic monitor-flow
    # uses (`_is_terminal` → `mark-run-terminal`). If the cluster shows the
    # run still genuinely running, `_is_terminal` returns None and the gate
    # below still fires — aggregate never reconciles a running run.
    if spec.reconcile_terminal and ensure_all_combined and record.status not in TERMINAL_STATUSES:
        refreshed = record_status(
            experiment_dir,
            run_id,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            job_ids=record.job_ids,
            job_name=record.job_name,
        )
        terminal_state, _ = _is_terminal(refreshed.last_status or {}, int(record.total_tasks))
        if terminal_state is not None:
            record = mark_terminal(experiment_dir, run_id, status=terminal_state)

    # Precondition gate: aggregating a run that monitor-flow has not
    # driven to a terminal state risks reducing over partial data and
    # reporting plausible-but-wrong metrics. ``ensure_all_combined=false``
    # is the documented opt-in for a deliberate partial aggregate, so it
    # bypasses the gate.
    if ensure_all_combined and record.status not in TERMINAL_STATUSES:
        raise errors.PreconditionFailed(
            f"run {run_id!r} is {record.status!r}, not terminal; monitor-flow "
            "has not driven it to complete/failed/abandoned. Aggregating now "
            "risks partial or wrong metrics. Pass ensure_all_combined=false to "
            "aggregate partial results deliberately."
        )

    if mode not in {"auto", "combiner-only", "cluster-reduce"}:
        raise errors.SpecInvalid(
            f"mode must be 'auto'|'combiner-only'|'cluster-reduce', got {mode!r}"
        )
    if pull_summaries and not summary_glob:
        raise errors.SpecInvalid("summary_glob is required when pull_summaries=true")

    # Resolve output_dir.
    out = experiment_dir / "_aggregated" / run_id if output_dir is None else Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    _validate_ssh_target(record.ssh_target)

    # Mode resolution + cluster-reduce short-circuit. The cluster-reduce
    # path runs the user's reducer on the cluster and pulls only its
    # single JSON output (KB) — bypasses the bulk per-task rsync_pull
    # that drags GBs of raw chunks to local. Falls back to combiner-
    # only when no aggregate_cmd is available; mode='auto' makes that
    # decision; mode='cluster-reduce' raises if no command is found.
    sidecar_for_cmd: dict[str, Any] = {}
    try:
        sidecar_for_cmd = read_run_sidecar(experiment_dir, run_id) or {}
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError, errors.HpcError):
        sidecar_for_cmd = {}
    if not sidecar_for_cmd:
        # Local sidecar absent: the caller no longer rsyncs it by hand —
        # aggregate-flow owns its inputs. Self-source aggregate_defaults by
        # SSH-reading the remote sidecar we already have access to. Best-effort:
        # a remote read failure leaves the combiner-only fallback intact.
        from hpc_agent.ops.aggregate.runner import _read_remote_sidecar

        try:
            sidecar_for_cmd = (
                _read_remote_sidecar(
                    ssh_target=record.ssh_target,
                    remote_path=record.remote_path,
                    run_id=run_id,
                )
                or {}
            )
        except (errors.HpcError, OSError, ValueError):
            sidecar_for_cmd = {}
    resolved_aggregate_cmd = aggregate_cmd or (
        (sidecar_for_cmd.get("aggregate_defaults") or {}).get("aggregate_cmd")
    )
    if mode == "cluster-reduce" or (mode == "auto" and resolved_aggregate_cmd):
        if not resolved_aggregate_cmd:
            raise errors.SpecInvalid(
                "mode='cluster-reduce' requires aggregate_cmd= or "
                "aggregate_defaults.aggregate_cmd on the run sidecar."
            )
        from hpc_agent.ops.aggregate.cluster_reduce import cluster_reduce

        cr = cluster_reduce(
            experiment_dir,
            run_id=run_id,
            aggregate_cmd=resolved_aggregate_cmd,
            output_path=aggregate_output_path,
            local_dir=out,
        )
        return AggregateFlowResult(
            run_id=run_id,
            combined_waves=list(record.combined_waves),
            failed_waves=list(record.failed_waves),
            waves_combined_this_call=[],
            combiner_dir_local=str(out),
            aggregated_metrics=(cr["reduced"] if isinstance(cr.get("reduced"), dict) else {}),
            # cluster-reduce performs the reduction on the cluster and
            # pulls the single reduced output; there is no separate
            # per-task summaries directory in this branch. The field is
            # documented as "set when pull_summaries=true" — leaving
            # None preserves that contract. ``output_path_local`` from
            # ``cluster_reduce`` is the single reduced *file* and lives
            # under ``combiner_dir_local`` already.
            summaries_dir_local=None,
            escalation_reason=None,
        )

    # Read the sidecar's wave_map (record carries combined_waves but not
    # wave_map — that lives in the per-run sidecar JSON, under
    # <experiment_dir>/.hpc/runs/). ``read_run_sidecar`` guarantees
    # ``wave_map`` is a dict; missing/unreadable sidecars yield empty.
    wave_map_keys: list[str] = []
    try:
        sidecar_data = read_run_sidecar(experiment_dir, run_id)
        wave_map_keys = list((sidecar_data.get("wave_map") or {}).keys())
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError, errors.HpcError):
        # No wave_map → no waves to ensure. Aggregation falls back to
        # whatever's already in _combiner/ on the cluster.
        pass

    waves_combined_this_call: list[int] = []
    combiner_failures: list[tuple[int, str]] = []
    if ensure_all_combined and wave_map_keys:
        missing = _missing_waves(wave_map_keys, list(record.combined_waves))
        if missing:
            waves_combined_this_call, combiner_failures = _combine_missing(
                experiment_dir,
                run_id,
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                waves=missing,
                max_retries=combiner_max_retries,
            )
            # Re-read after combine-wave updated the journal.
            record = load_run(experiment_dir, run_id)
            if record is None:  # pragma: no cover — defensive
                raise errors.JournalCorrupt(f"record vanished for {run_id!r}")

    # Obtain the aggregated metrics + the incomplete-wave set. By default this
    # pulls the cluster ``_combiner/`` partials and reduces them locally; with
    # ``HPC_CLUSTER_FINAL_REDUCE=1`` (#254) the cross-wave reduce runs ON THE
    # CLUSTER and only the single KB ``metrics_aggregate.json`` is pulled —
    # one RTT, not hundreds of wave_*.json transfers. Both paths yield the same
    # ``(aggregated_metrics, incomplete_waves)``, so the rest of the flow is
    # identical; the local path stays the default (opt-in, debug-reachable).
    combiner_local = out / "_combiner"
    if os.environ.get("HPC_CLUSTER_FINAL_REDUCE") == "1":
        aggregated, incomplete_waves = _cluster_final_reduce(
            experiment_dir, run_id, record=record, out=out
        )
    else:
        aggregated, incomplete_waves = _combiner_only_reduce(
            experiment_dir, run_id, record=record, combiner_local=combiner_local
        )

    # Ingest runtime samples (timing + axis_bindings) from the pulled
    # ``wave_*.runtime.json`` files into <experiment>/.hpc/runtimes/.
    # Best-effort: a missing or malformed runtime sidecar must NOT abort
    # the aggregate (the user wants their metrics, not a prior
    # bookkeeping failure). The warm-axis-picker on the next submit
    # picks up whatever landed.
    try:
        from hpc_agent.state.runtime_prior import ingest_runtime_samples_from_combiner_dir

        cmd_sha_for_ingest: str | None = None
        if combiner_local.is_dir():
            try:
                cmd_sha_for_ingest = read_run_sidecar(experiment_dir, run_id).get("cmd_sha")
            except (
                FileNotFoundError,
                OSError,
                json.JSONDecodeError,
                UnicodeDecodeError,
                errors.HpcError,
            ):
                # Best-effort cmd_sha tag — a corrupt/too-new sidecar must
                # not crash runtime ingestion; degrade to None.
                cmd_sha_for_ingest = None
        ingested = ingest_runtime_samples_from_combiner_dir(
            combiner_local,
            experiment_dir=experiment_dir,
            profile=record.profile,
            cluster=record.cluster,
            cmd_sha=cmd_sha_for_ingest,
        )
        if ingested:
            print(
                f"[aggregate-flow] ingested {ingested} runtime samples "
                f"into .hpc/runtimes/{record.profile}.{record.cluster}.json"
            )
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        # Runtime ingestion is best-effort — a corrupt sidecar or
        # missing runtime file MUST NOT crash aggregate_flow.
        pass

    # Optionally pull summaries.
    summaries_local: str | None = None
    if pull_summaries:
        sl = out / "summaries"
        sp = rsync_pull(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            remote_subdir=results_subdir,
            local_dir=str(sl),
            include=[summary_glob] if summary_glob else None,
        )
        if sp.returncode != 0:
            # Non-fatal — caller may have asked for a glob that matches
            # nothing yet. Surface via escalation_reason so they see it.
            combiner_failures.append(
                (-1, f"summary rsync failed: {(sp.stderr or '').strip()[:300]}")
            )
        summaries_local = str(sl)

    # Check 1 — non-empty rows. `aggregate-flow` returning ok only means
    # every task wrote *a file*; it does not mean the file has real data.
    # When spec.min_rows > 0, run the cluster-side status reporter and
    # surface the task ids whose CSV result has fewer than min_rows data
    # rows — i.e. tasks that wrote a header-only / under-populated file.
    nonempty_rows_checked = False
    nonempty_failing: list[int] = []
    if spec.min_rows > 0:
        nonempty_failing = _nonempty_failing_task_ids(
            run_id,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            job_ids=list(record.job_ids),
            job_name=record.job_name,
            min_rows=spec.min_rows,
        )
        nonempty_rows_checked = True

    # Check 2 — expected columns + non-NaN metric. Deterministic given a
    # declared schema in the run sidecar's `results` block. Runs against
    # the locally-pulled per-task result files (summaries_local); a clean
    # no-op when no schema is declared or summaries were not pulled.
    columns_checked = False
    column_violations: list[dict[str, Any]] = []
    results_block = (sidecar_for_cmd or {}).get("results")
    if not isinstance(results_block, dict):
        try:
            results_block = (read_run_sidecar(experiment_dir, run_id) or {}).get("results")
        except (
            FileNotFoundError,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            errors.HpcError,
        ):
            results_block = None
    if isinstance(results_block, dict) and summaries_local is not None:
        raw_cols = results_block.get("expected_columns")
        expected_columns = [str(c) for c in raw_cols] if isinstance(raw_cols, list) else []
        raw_metric = results_block.get("metric_column")
        metric_column = raw_metric if isinstance(raw_metric, str) and raw_metric else None
        if expected_columns or metric_column:
            from hpc_agent.ops.aggregate.invariants import check_result_columns

            summary_pattern = results_block.get("summary_pattern")
            file_glob = (
                summary_pattern if isinstance(summary_pattern, str) and summary_pattern else "*.csv"
            )
            col_report = check_result_columns(
                Path(summaries_local),
                expected_columns=expected_columns,
                metric_column=metric_column,
                file_glob=file_glob,
            )
            columns_checked = bool(col_report["checked"])
            column_violations = list(col_report["violations"])

    escalation_parts: list[str] = []
    if combiner_failures:
        escalation_parts.append(
            "combiner_failed_max_retries:waves=" + ",".join(str(w) for w, _ in combiner_failures)
        )
    if incomplete_waves:
        escalation_parts.append(
            "partial_waves:metrics_unreadable_for_some_tasks:waves="
            + ",".join(str(w) for w in incomplete_waves)
        )
    if nonempty_failing:
        escalation_parts.append(
            "empty_result_rows:tasks=" + ",".join(str(t) for t in nonempty_failing)
        )
    if column_violations:
        escalation_parts.append(f"column_violations:files={len(column_violations)}")
    escalation: str | None = "; ".join(escalation_parts) if escalation_parts else None

    return AggregateFlowResult(
        run_id=run_id,
        combined_waves=list(record.combined_waves),
        failed_waves=list(record.failed_waves),
        waves_combined_this_call=waves_combined_this_call,
        combiner_dir_local=str(combiner_local),
        aggregated_metrics=aggregated,
        summaries_dir_local=summaries_local,
        escalation_reason=escalation,
        nonempty_rows_checked=nonempty_rows_checked,
        nonempty_failing_task_ids=nonempty_failing,
        columns_checked=columns_checked,
        column_violations=column_violations,
    )
