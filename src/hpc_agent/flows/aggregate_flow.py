"""``aggregate-flow``: workflow atom that finalizes a run's aggregated metrics.

Third workflow atom in the :mod:`hpc_agent.flows.submit_flow` /
:mod:`hpc_agent.flows.monitor_flow` family. Pipeline:

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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent import errors, runner
from hpc_agent._internal import session
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent._schema_models.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.remote import rsync_pull, validate_ssh_target
from hpc_agent.mapreduce.reduce.metrics import collect_wave_errors, reduce_partials
from hpc_agent.runner import combine_wave, record_status
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
# the reducer enforces in :mod:`hpc_agent.mapreduce.reduce.metrics`.
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
    from hpc_agent.runner.status import ssh_status_report

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
            ok, _stdout, stderr = runner.combine_wave(
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


@primitive(
    name="aggregate-flow",
    verb="workflow",
    composes=[combine_wave, record_status],
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
        experiment_dir_arg=True,
        requires_ssh=True,
        dry_run_arg=True,
        dry_run_passthrough_keys=("run_id", "ensure_all_combined", "pull_summaries", "output_dir"),
    ),
    agent_facing=True,
)
def aggregate_flow(
    experiment_dir: Path,
    *,
    spec: AggregateFlowSpec,
    mode: str = "auto",
    aggregate_cmd: str | None = None,
    aggregate_output_path: str | None = None,
) -> AggregateFlowResult:
    """Finalize a run's aggregation; return paths + reduced metrics.

    The wire-validated ``spec`` carries the user-facing knobs
    (``run_id``, ``output_dir``, ``ensure_all_combined``,
    ``combiner_max_retries``, ``pull_summaries``, ``summary_glob``,
    ``results_subdir``); ``mode`` / ``aggregate_cmd`` /
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
    ensure_all_combined = spec.ensure_all_combined
    combiner_max_retries = spec.combiner_max_retries
    pull_summaries = spec.pull_summaries
    summary_glob = spec.summary_glob
    results_subdir = spec.results_subdir

    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for {run_id!r}; submit the run first")

    # Precondition gate: aggregating a run that monitor-flow has not
    # driven to a terminal state risks reducing over partial data and
    # reporting plausible-but-wrong metrics. ``ensure_all_combined=false``
    # is the documented opt-in for a deliberate partial aggregate, so it
    # bypasses the gate.
    if ensure_all_combined and record.status not in session.TERMINAL_STATUSES:
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
    except (FileNotFoundError, OSError, json.JSONDecodeError):
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
        from hpc_agent.atoms.cluster_reduce import cluster_reduce

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
            summaries_dir_local=str(cr["output_path_local"]),
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
    except (FileNotFoundError, OSError, json.JSONDecodeError):
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
            record = session.load_run(experiment_dir, run_id)
            if record is None:  # pragma: no cover — defensive
                raise errors.JournalCorrupt(f"record vanished for {run_id!r}")

    # Pull the combined partials.
    #
    # Incremental rsync: rather than walking the entire cluster-side
    # ``_combiner/`` tree on every call (which is slow for runs with
    # 1000+ waves even when nothing has changed), narrow the pull to the
    # waves that aren't already present locally. State source is
    # ``record.combined_waves`` (the authoritative set of waves the
    # cluster has produced ``wave_<N>.json`` for, just updated above by
    # ``_combine_missing``). The diff against locally-present
    # ``wave_<N>.json`` files is the set we still need to fetch.
    #
    # Fallback: when the diff equals the full set (first call) or
    # ``combined_waves`` is empty (no wave_map, falling back to whatever
    # is on the cluster — see the comment on ``wave_map_keys`` above),
    # we emit an unfiltered pull so behavior matches the original.
    combiner_local = out / "_combiner"
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
        raise errors.RemoteCommandFailed(
            f"rsync_pull of _combiner failed (exit {pull.returncode}): "
            f"{(pull.stderr or '').strip()[:300]}"
        )

    # Reduce locally.
    aggregated = reduce_partials(combiner_local)
    # Waves where the combiner couldn't read every task's metrics.json
    # contribute a partial grid_points set; reduce_partials means over
    # only the readable subset. Surface those waves so the caller does
    # not treat the aggregate as computed over the full task set.
    incomplete_waves = sorted(collect_wave_errors(combiner_local))

    # Ingest runtime samples (timing + axis_bindings) from the pulled
    # ``wave_*.runtime.json`` files into <experiment>/.hpc/runtimes/.
    # Best-effort: a missing or malformed runtime sidecar must NOT abort
    # the aggregate (the user wants their metrics, not a prior
    # bookkeeping failure). The warm-axis-picker on the next submit
    # picks up whatever landed.
    try:
        from hpc_agent.state.runtime_prior import ingest_runtime_samples_from_combiner_dir

        ingested = ingest_runtime_samples_from_combiner_dir(
            combiner_local,
            experiment_dir=experiment_dir,
            profile=record.profile,
            cluster=record.cluster,
            cmd_sha=(
                read_run_sidecar(experiment_dir, run_id).get("cmd_sha")
                if combiner_local.is_dir()
                else None
            ),
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
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            results_block = None
    if isinstance(results_block, dict) and summaries_local is not None:
        raw_cols = results_block.get("expected_columns")
        expected_columns = [str(c) for c in raw_cols] if isinstance(raw_cols, list) else []
        raw_metric = results_block.get("metric_column")
        metric_column = raw_metric if isinstance(raw_metric, str) and raw_metric else None
        if expected_columns or metric_column:
            from hpc_agent.atoms.aggregation_invariants import check_result_columns

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
