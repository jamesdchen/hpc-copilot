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
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.cli._dispatch import CliArg, CliShape, SchemaRef
from hpc_agent.execution.mapreduce.data_trace_contract import TRACE_TRANSPORT_FILENAME
from hpc_agent.execution.mapreduce.reduce.metrics import (
    collect_wave_errors,
    reduce_metrics,
    reduce_partials,
)
from hpc_agent.infra.backends import backend_requires_ssh
from hpc_agent.infra.io import atomic_write_json
from hpc_agent.infra.ssh_validation import validate_ssh_target
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.infra.transport import rsync_pull
from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.ops.monitor.reconcile import mark_terminal
from hpc_agent.ops.monitor.status import record_status
from hpc_agent.ops.monitor.terminal import _is_terminal
from hpc_agent.ops.scope_gate import assert_scopes_unlocked
from hpc_agent.state.block_terminal import terminal_block_key
from hpc_agent.state.data_trace import ingest_trace, trace_store_path
from hpc_agent.state.journal import load_run
from hpc_agent.state.run_record import TERMINAL_STATUSES
from hpc_agent.state.runs import read_run_sidecar, resolved_summary_artifact

__all__ = ["aggregate_flow", "AggregateFlowResult", "per_task_fallback_reducible"]


def per_task_fallback_reducible(summary_name: str) -> bool:
    """Whether the no-combiner per-task weighted-mean fallback CAN reduce a run
    whose declared summary artifact is *summary_name*.

    The fallback (:func:`_per_task_metrics_reduce` → :func:`reduce_metrics`) is a
    JSON weighted-mean: it ``json.load``s each per-task sidecar. A non-JSON
    artifact (run #12's pack-reduced ``causal_tune_linear/metrics_table.csv``) has
    NO path through it. This is the ONE definition of that limit — the run-path
    refusal (BEFORE the 40+ min pull, in ``_per_task_metrics_reduce``) and the
    aggregate-CHECK readiness surface (BEFORE the greenlight, in
    ``ops.aggregate_blocks``) both key on it, so the two can never disagree
    (run #12 finding 28).
    """
    return summary_name.lower().endswith(".json")


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
    #: Per-scope PRIOR look counts recorded by this reduction —
    #: ``{tag: {"prior_looks": int, "distinct_lineages": int}}`` — or ``None``
    #: for a scope-less run (existing consumers untouched). Two plain integers
    #: per tag; no metric is ever consulted (rigor-primitives T3).
    scope_looks: dict[str, dict[str, int]] | None = None
    #: Detach-by-contract handle (design §3; run-#10 F-K). Set only on the handle
    #: a DIRECT ``detach=true`` aggregate-flow invocation returns — the harvest
    #: runs in a durable detached worker and the data fields above are empty. Every
    #: synchronous / composed path leaves these at their defaults.
    started: bool = False
    watch: str | None = None
    detached_pid: int | None = None

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
            "scope_looks": self.scope_looks,
            "started": self.started,
            "watch": self.watch,
            "detached_pid": self.detached_pid,
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


def _run_scoped_results_subdir(
    experiment_dir: Path, run_id: str, record: Any, results_subdir: str
) -> str:
    """The run's OWN results subtree — the static prefix of its result_dir_template.

    Finding 19 (run #12): pulling the whole ``results/`` root drags every
    prior run's outputs through the transfer — the scp fallback cannot
    include-filter — turning a small metrics pull into an 1800s timeout. The
    template's static prefix (``results/causal_tune_linear/{estimator}/…`` →
    ``results/causal_tune_linear``) scopes the pull to this run. Falls back
    to *results_subdir* when the template is absent, carries no directory,
    or would escape the configured root. Canary siblings render under the
    same prefix, so the downstream canary exclusion is unchanged.
    """
    template = getattr(record, "result_dir_template", None)
    if not (isinstance(template, str) and template):
        try:
            from hpc_agent.state.runs import read_run_sidecar

            template = read_run_sidecar(experiment_dir, run_id).get("result_dir_template")
        except Exception:  # noqa: BLE001 — scoping is an optimization, never a gate
            template = None
    if not (isinstance(template, str) and template):
        return results_subdir
    head = template.split("{", 1)[0]
    scoped = head.rsplit("/", 1)[0] if "{" in template else head.rstrip("/")
    root = results_subdir.rstrip("/")
    if scoped and (scoped == root or scoped.startswith(root + "/")):
        return scoped
    return results_subdir


def _per_task_metrics_reduce(
    experiment_dir: Path,
    run_id: str,
    *,
    record: Any,
    out: Path,
    results_subdir: str,
    summary_name: str,
) -> dict[str, Any]:
    """Weighted-mean the per-task summary file over SSH — the no-combiner default.

    ``summary_name`` is the run's declared per-task summary filename (F-J),
    resolved by the caller at the seam via ``resolved_summary_artifact`` — the
    pull filter, the local rglob, and :func:`reduce_metrics` all key on it, so a
    run whose executor emits e.g. ``results_reduce.json`` is reduced instead of
    read as a harvest gap (run #10). An undeclared run resolves to
    ``metrics.json`` upstream, keeping this path byte-identical.

    The SSH analogue of the LOCAL / pure-API ``reduce_metrics`` fallback
    (#342). When a run was submitted with NO reducer (``aggregate_cmd``) and
    NO cluster-side combiner ever ran, there is no ``_combiner/`` tree to
    reduce — but the per-task ``results/<...>/metrics.json`` sidecars the
    tasks wrote are still on the cluster. Rather than error out (which forced
    the skill to improvise the mean by hand — the very "LLM in the compute
    loop" failure this framework exists to prevent), pull those sidecars and
    run the SAME deterministic :func:`reduce_metrics` weighted-mean the local
    path uses. Reduction is ALWAYS code, never the model.

    Pulls the cluster ``results/`` subtree (filtered to ``metrics.json``
    files so the transfer stays small), discovers the per-task dirs locally,
    and weighted-means across them keyed by ``run_id`` — the same
    ``{run_id: {...}}`` shape :func:`reduce_partials` and the pure-API path
    return, so the rest of the flow is identical.

    Raises :class:`errors.RemoteCommandFailed` when even the per-task
    ``metrics.json`` files cannot be pulled (nothing deterministic to reduce)
    or when the pull succeeds but yields zero readable sidecars — in either
    case there is no numeric input, and fabricating a mean is exactly the
    failure mode being closed.
    """
    # Refuse BEFORE the (potentially 40+ minute) pull when this fallback can
    # never succeed: reduce_metrics parses JSON sidecars only, so a run whose
    # declared summary artifact isn't JSON (run #12: the pack-reduced
    # `causal_tune_linear/metrics_table.csv`) has no path through here — the
    # old behavior paid the full results/ mirror twice and then blamed the
    # tasks ("likely never wrote") for a reducer-side format limit.
    if not per_task_fallback_reducible(summary_name):
        raise errors.RemoteCommandFailed(
            f"no cluster-side _combiner/ for run_id {run_id!r} and the run "
            f"declares a non-JSON summary artifact ({summary_name!r}) — the "
            f"no-combiner per-task fallback is a JSON weighted-mean "
            f"(reduce_metrics) and can NEVER reduce it, so refusing before "
            f"pulling {results_subdir}/. Reduce this run with its own reducer "
            f"(an aggregate_cmd / pack reducer that understands the artifact), "
            f"or re-submit declaring a JSON summary artifact."
        )
    results_local = out / "_per_task_results"
    scoped_subdir = _run_scoped_results_subdir(experiment_dir, run_id, record, results_subdir)
    pull = rsync_pull(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        remote_subdir=scoped_subdir,
        local_dir=str(results_local),
        include=[summary_name],
    )
    if pull.returncode != 0:
        stderr_tail = (pull.stderr or "").strip()
        raise errors.RemoteCommandFailed(
            f"no cluster-side _combiner/ for run_id {run_id!r} AND the per-task "
            f"{results_subdir}/ fallback pull failed (exit {pull.returncode}). "
            f"There is no deterministic numeric input to reduce — refusing to "
            f"fabricate an aggregate. Check that the run actually wrote per-task "
            f"{summary_name} sidecars under {record.remote_path}/{results_subdir}/. "
            f"rsync_pull stderr: {stderr_tail[:300]}"
        )

    # Every directory under the pulled tree that carries the declared summary
    # file is a per-task result dir. reduce_metrics scans each for that sidecar
    # and weighted-means across tasks — identical reduce semantics to the
    # combiner, run on the locally-pulled per-task sidecars.
    #
    # A summary artifact may be PATH-shaped (`sub/metrics.json`): the task dir
    # is the match minus ALL of the artifact's components, not `p.parent` —
    # `p.parent` keeps the artifact's own subdir, and reduce_metrics rejoining
    # `dir / summary_name` then doubles it (`.../sub/sub/metrics.json`) so
    # every sidecar reads as missing (run #12's "found no readable sidecars"
    # against 2700 mirrored files).
    summary_depth = len(PurePosixPath(summary_name).parts)

    def _task_dir(match: Path) -> Path:
        for _ in range(summary_depth):
            match = match.parent
        return match

    result_dirs = sorted(
        {str(_task_dir(p)) for p in results_local.rglob(summary_name) if p.is_file()}
    )
    # Canary anti-contamination (run #6 harvest): the ``<run_id>-canary``
    # sibling writes its metrics.json under the SAME results/ subtree (its
    # result_dir_template renders with the canary's run_id, whose name has the
    # MAIN id as a prefix), so the recursive scan sweeps it in and the mean
    # double-counts the canary's task (empirical: an 11-row average for a
    # 10-task run — the seed-mean 45/11 tell). The determinism fingerprint's
    # SECOND canary (``<run_id>-canary2``) contaminates the SAME way, so the
    # exclusion covers the whole ``-canary`` FAMILY — the one #258 suffix
    # definition (``_sibling_run_ids``), never a second hardcoded "-canary".
    from hpc_agent.ops.monitor.reconcile import _sibling_run_ids

    canary_ids = set(_sibling_run_ids(run_id))
    result_dirs = [d for d in result_dirs if canary_ids.isdisjoint(Path(d).parts)]
    # Cardinality gate (finding-21 family): MORE contributing rows than the
    # run's task count is PROVABLE foreign contamination (another run sharing
    # the results/ subtree) — averaging them silently corrupts the aggregate,
    # so refuse loudly naming the surplus. FEWER is a legitimate partial run
    # (failed tasks) and stays the existing partial-machinery's concern.
    total = int(getattr(record, "total_tasks", 0) or 0)
    if total > 0 and len(result_dirs) > total:
        raise errors.RemoteCommandFailed(
            f"per-task reduce for run_id {run_id!r} found {len(result_dirs)} "
            f"result dirs with {summary_name} but the run has only {total} tasks "
            f"— the {results_subdir}/ subtree carries FOREIGN rows (another "
            "run's results sharing the tree), and averaging them would corrupt "
            f"the aggregate. Contributing dirs: {result_dirs}. Remove or "
            "relocate the foreign results (or re-run with a run-scoped "
            "result_dir_template like 'results/{run_id}/task_{task_id}'), then "
            "re-aggregate."
        )
    aggregated = reduce_metrics(result_dirs, filename=summary_name)
    if not aggregated:
        # Say what was actually observed: "no files matched" and "files
        # matched but none parsed" are different failures with different
        # remediations — the old single message blamed the tasks either way.
        if result_dirs:
            detail = (
                f"the pull mirrored {len(result_dirs)} {summary_name} sidecars "
                f"but NONE parsed as JSON (corrupt or non-JSON content) — "
                f"inspect one, e.g. under {results_local}."
            )
        else:
            detail = (
                f"the tasks likely never wrote {summary_name}; inspect "
                f"per-task stderr under {record.remote_path}/{run_id}/logs/."
            )
        raise errors.RemoteCommandFailed(
            f"no cluster-side _combiner/ for run_id {run_id!r} and the per-task "
            f"{results_subdir}/ fallback found no readable {summary_name} sidecars "
            f"under {record.remote_path}/{results_subdir}/. There is no numeric "
            f"input to reduce — refusing to fabricate an aggregate. {detail}"
        )

    # T4 ingestion-at-harvest (docs/design/data-trace.md §"Storage: emission
    # is transport"): pull + ingest each task's ``_trace.jsonl`` beside the
    # metrics harvest. Fires AFTER the metrics pull and NEVER blocks it — a
    # trace pull/parse failure is disclosed, never a harvest failure.
    _ingest_task_traces(
        experiment_dir,
        run_id,
        record=record,
        out=out,
        results_subdir=results_subdir,
    )
    return {run_id: aggregated}


_TASK_DIR_RE = re.compile(r"\d+(?!.*\d)")  # the LAST run of digits in a name


def _task_id_from_dir(result_dir: Path) -> int | None:
    """Extract the integer task id from a per-task result dir (``task-<n>``).

    The ``result_dir_template`` renders ``task-{task_id}`` / ``task_{task_id}``;
    the trailing integer is the key the trace store files a task by
    (``task-<n>.jsonl``). Returns ``None`` when the dir name carries no integer
    — an unkeyable trace is a DISCLOSED skip, never a fabricated key.
    """
    m = _TASK_DIR_RE.search(result_dir.name)
    return int(m.group(0)) if m else None


def _ingest_task_traces(
    experiment_dir: Path,
    run_id: str,
    *,
    record: Any,
    out: Path,
    results_subdir: str,
) -> dict[str, int]:
    """Pull each task's ``_trace.jsonl`` and ingest it — data-trace **T4**.

    Ingestion-at-harvest: the trace transport files the tasks emitted beside
    their outputs are pulled (one extra rsync on the already-flowing per-task
    seam) and moved into the one canonical store via :func:`ingest_trace`,
    scope ``("run", run_id)``. Absence is the NORMAL shape for a non-emitting
    run — silent, harvest identical.

    NEVER blocks the harvest — the trace is EVIDENCE, not a gate. Every failure
    mode is DISCLOSED (skip-counted + logged), never raised:

    * the trace pull failing (cluster hiccup / no ``_trace.jsonl`` anywhere —
      rsync 404s the include) — the metrics harvest already succeeded, log+return;
    * an absent trace for a task — silent (the task simply emitted none);
    * a torn / schema-invalid trace — :func:`ingest_trace` refuses it (T1 is
      strict; an invalid record never enters the trust chain), counted as a
      disclosed skip.

    DOUBLE-INGEST GUARD (re-harvest safety): the cluster ``_trace.jsonl``
    persists and is re-pulled every harvest, so a naive re-ingest would append
    a second copy to the store. Before ingesting a task we check the store — if
    ``task-<n>.jsonl`` already exists the task was ingested on a prior harvest
    and is skipped. (Local runs never reach this seam; they ingest at emission
    per T2's fallback rule, so there is no double-ingest there either.)

    Canary-family siblings are excluded exactly as the metrics reduce excludes
    them — their ``_trace.jsonl`` shares the same ``results/`` subtree. Returns
    a counts dict (the disclosure surface for tests/callers; no run gate).
    """
    log = logging.getLogger(__name__)
    counts = {"pulled": 0, "ingested": 0, "skipped_existing": 0, "skipped_invalid": 0}

    traces_local = out / "_per_task_traces"
    scoped_subdir = _run_scoped_results_subdir(experiment_dir, run_id, record, results_subdir)
    try:
        pull = rsync_pull(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            remote_subdir=scoped_subdir,
            local_dir=str(traces_local),
            include=[TRACE_TRANSPORT_FILENAME],
        )
    except OSError as exc:  # a transport-layer explosion is still just evidence
        log.warning(
            "data-trace T4: trace pull for run_id %r raised %s — harvest "
            "unaffected, traces skipped",
            run_id,
            exc,
        )
        return counts
    if pull.returncode != 0:
        # Absence is NORMAL for a non-emitting run (the subtree carries no
        # _trace.jsonl, rsync's include matches nothing) — DISCLOSE, never fail.
        log.info(
            "data-trace T4: no per-task _trace.jsonl pulled for run_id %r "
            "(rsync exit %d) — traces skipped, harvest unaffected",
            run_id,
            pull.returncode,
        )
        return counts

    from hpc_agent.ops.monitor.reconcile import _sibling_run_ids

    canary_ids = set(_sibling_run_ids(run_id))
    trace_files = sorted(
        p
        for p in traces_local.rglob(TRACE_TRANSPORT_FILENAME)
        if p.is_file() and canary_ids.isdisjoint(p.parts)
    )
    for trace_path in trace_files:
        counts["pulled"] += 1
        task = _task_id_from_dir(trace_path.parent)
        if task is None:
            log.warning(
                "data-trace T4: cannot key task for trace %s (no task-<n> "
                "component in %r) — disclosed skip",
                trace_path,
                trace_path.parent.name,
            )
            counts["skipped_invalid"] += 1
            continue
        # Double-ingest guard: a prior harvest already moved this task's trace
        # into the store; re-pulling the persistent cluster copy must not append
        # a second time.
        if trace_store_path(experiment_dir, "run", run_id, task).exists():
            counts["skipped_existing"] += 1
            continue
        try:
            ingest_trace(experiment_dir, "run", run_id, task, trace_path)
        except errors.SpecInvalid as exc:
            log.warning(
                "data-trace T4: task %d trace for run_id %r is invalid (%s) — "
                "disclosed skip, harvest unaffected",
                task,
                run_id,
                exc,
            )
            counts["skipped_invalid"] += 1
        except OSError as exc:
            log.warning(
                "data-trace T4: task %d trace for run_id %r failed to ingest "
                "(%s) — disclosed skip, harvest unaffected",
                task,
                run_id,
                exc,
            )
            counts["skipped_invalid"] += 1
        else:
            counts["ingested"] += 1

    if counts["pulled"]:
        log.info(
            "data-trace T4: run_id %r traces — %d ingested, %d already-present, "
            "%d skipped-invalid (of %d pulled)",
            run_id,
            counts["ingested"],
            counts["skipped_existing"],
            counts["skipped_invalid"],
            counts["pulled"],
        )
    return counts


def _combiner_only_reduce(
    experiment_dir: Path,
    run_id: str,
    *,
    record: Any,
    combiner_local: Path,
    summary_name: str,
    results_subdir: str = "results",
    out: Path | None = None,
) -> tuple[dict[str, Any], list[int], str]:
    """Pull the cluster ``_combiner/`` partials and reduce them locally.

    The default aggregation path. Returns ``(aggregated_metrics,
    incomplete_waves, source)`` — ``source`` is ``"local_reduce"`` when the
    combiner partials were reduced locally, or ``"per_task_fallback"`` when
    the no-combiner default fell back to the per-task ``metrics.json``
    weighted-mean. The caller stamps ``source`` into the durable local
    aggregate artifact so its provenance is honest.

    Incremental rsync: rather than walking the entire cluster-side
    ``_combiner/`` tree on every call (slow for runs with 1000+ waves even when
    nothing changed), narrow the pull to the waves not already present locally.
    State source is ``record.combined_waves``; the diff against locally-present
    ``wave_<N>.json`` files is the set still to fetch. When the diff equals the
    full set (first call) or ``combined_waves`` is empty, an unfiltered pull is
    emitted so behaviour matches the original.

    No-combiner default (#352): when the ``_combiner/`` tree does not exist on
    the cluster (the combiner step never ran — the common shape for a
    ``@register_run`` SSH sweep submitted with no reducer) AND no
    ``aggregate_cmd`` is configured on the sidecar, fall back to
    :func:`_per_task_metrics_reduce` — the SAME deterministic weighted-mean
    over per-task ``metrics.json`` the LOCAL / pure-API path uses. Reduction
    stays in code. When an ``aggregate_cmd`` IS configured the original
    cluster-reduce remediation hint is raised instead (the caller chose a
    custom reducer; silently meaning would mask their intent).
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
            if not has_agg_cmd:
                # No combiner ran AND no custom reducer configured — the
                # @register_run-SSH-sweep-with-no-reducer shape (#352). Fall
                # back to the SAME deterministic weighted-mean over per-task
                # metrics.json the LOCAL / pure-API path uses, so reduction is
                # ALWAYS code, never the model improvising a mean by hand.
                aggregated = _per_task_metrics_reduce(
                    experiment_dir,
                    run_id,
                    record=record,
                    out=(out if out is not None else combiner_local.parent),
                    results_subdir=results_subdir,
                    summary_name=summary_name,
                )
                # No wave partials → no per-wave incomplete-task signal to
                # surface; the per-task fallback either reduced over readable
                # sidecars or raised above.
                return aggregated, [], "per_task_fallback"
            # has_agg_cmd is True here (the no-cmd case fell back above): the
            # caller configured a custom reducer, so cluster-reduce is the
            # right remediation — silently meaning their metrics.json would
            # mask that intent.
            cluster_reduce_hint = (
                f"Run `hpc-agent cluster-reduce --run-id {run_id}` — uses the "
                f"sidecar's aggregate_cmd directly, no combiner needed."
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
    return aggregated, incomplete_waves, "local_reduce"


def _pure_api_reduce(
    experiment_dir: Path,
    run_id: str,
    *,
    record: Any,
    out: Path,
    mode: str,
    aggregate_cmd: str | None,
) -> dict[str, dict[str, Any]]:
    """Fetch a run's artifacts over the backend API and reduce them LOCALLY.

    The pure-API counterpart of the SSH reduce dispatch for a backend whose
    ``requires_ssh`` capability is ``False`` (#337 Class B). There is no login
    node and no shared filesystem to ``rsync_pull`` from: the backend's
    :meth:`HPCBackend.fetch_results` hook downloads the run's artifacts into
    *out*, and reduction runs LOCALLY. ZERO ``rsync_pull`` — the whole point of
    the capability split. The backend is constructed via the shared
    :func:`backend_for_record` helper so core never names the concrete (plugin)
    backend module; it routes through the registry.

    Reduction *choice* mirrors the SSH path's ``mode`` resolution, so a pure-API
    backend is NOT locked into the numeric weighted-mean:

    * ``cluster-reduce`` (or ``auto`` + a resolved ``aggregate_cmd``) runs the
      caller-owned reducer over the fetched artifacts via :func:`local_reduce`
      (the local analogue of cluster-reduce) — honouring custom / non-mean /
      non-``metrics.json`` reductions exactly as the SSH path does, just
      executed locally rather than over SSH.
    * otherwise (``combiner-only``, or ``auto`` with no command) falls back to
      the weighted-mean :func:`reduce_metrics` over each task's ``metrics.json``
      — the historical pure-API behaviour, unchanged.
    """
    from hpc_agent.infra.backends.remote_factory import backend_for_record

    backend = backend_for_record(record)
    result_dirs = backend.fetch_results(run_id, str(out))

    # Resolve the caller-owned reducer: explicit kwarg > the LOCAL sidecar's
    # ``aggregate_defaults.aggregate_cmd``. Local only — a pure-API backend has
    # no remote sidecar to SSH-read, so the SSH path's remote-sidecar fallback
    # does not apply here.
    resolved_aggregate_cmd = aggregate_cmd
    if resolved_aggregate_cmd is None:
        try:
            sidecar = read_run_sidecar(experiment_dir, run_id) or {}
        except (
            FileNotFoundError,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            errors.HpcError,
        ):
            sidecar = {}
        resolved_aggregate_cmd = (sidecar.get("aggregate_defaults") or {}).get("aggregate_cmd")

    if mode == "cluster-reduce" or (mode == "auto" and resolved_aggregate_cmd):
        if not resolved_aggregate_cmd:
            raise errors.SpecInvalid(
                "mode='cluster-reduce' requires aggregate_cmd= or "
                "aggregate_defaults.aggregate_cmd on the run sidecar."
            )
        from hpc_agent.ops.aggregate.local_reduce import local_reduce

        # ``aggregate_output_path`` is deliberately NOT threaded here: it carries
        # cluster-path semantics (resolved under ``remote_path`` by cluster-
        # reduce), and an absolute cluster path would make the local
        # ``mkdir`` target the control plane's filesystem. The local output is
        # internal anyway — the reduced JSON is returned inline below — so
        # local-reduce keeps its own local default location.
        cr = local_reduce(
            run_id=run_id,
            results_dir=out,
            aggregate_cmd=resolved_aggregate_cmd,
        )
        reduced = cr.get("reduced")
        # Surface the reducer's JSON directly when it's a dict, matching the SSH
        # cluster-reduce branch. The contract allows any JSON shape; a non-dict
        # output (list/scalar) has no ``dict[str, dict]`` shape for
        # ``aggregated_metrics``, so it collapses to ``{}`` — same as SSH.
        return reduced if isinstance(reduced, dict) else {}

    # ``fetch_results`` returns the per-task dirs it wrote (``task-<i>``), each
    # holding a ``metrics.json``. ``reduce_metrics`` scans each dir for that
    # sidecar and weighted-means across tasks — identical reduce semantics to
    # the SSH path's combiner, run on the locally-fetched artifacts.
    return {run_id: reduce_metrics(result_dirs)}


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
    # fallback_cluster (run #7): the submit-flow sidecar carries no cluster, so
    # the final reduce would run bare login python without it (rc=127, the
    # blind-watch class at the harvest surface).
    remote_activation = remote_activation_for_sidecar(
        sidecar, fallback_cluster=getattr(record, "cluster", None)
    )

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

    # Land the pulled aggregate at the CANONICAL flat location
    # ``out/metrics_aggregate.json`` — the exact path verify-reproduction reads
    # (``_aggregated/<run_id>/metrics_aggregate.json`` when ``out`` is the
    # default). ``out`` was already ``.../_aggregated/<run_id>``, so appending
    # ``_aggregated/<run_id>`` again nested the file at
    # ``_aggregated/<run_id>/_aggregated/<run_id>/`` where no comparator looks —
    # the cluster-final arm of the same L2 gap. ``remote_subdir`` still names the
    # cluster-side ``_aggregated/<run_id>`` source dir; only the LOCAL sink flattens.
    agg_local = out
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


def _persist_local_aggregate(
    out: Path,
    run_id: str,
    *,
    aggregated: dict[str, Any],
    incomplete_waves: list[int],
    source: str,
) -> None:
    """Atomically persist a reduce path's output as the durable comparator artifact.

    The ONE persistence definition for every LOCAL-reducing path — the SSH
    combiner-only default, the pure-API path, and the cluster-reduce path all
    route their reduced metrics through here (verifier finding L2). Each of
    those returns ``aggregated_metrics`` inline and otherwise persists only
    path-specific scratch (``_combiner/wave_*.json`` partials, a reducer-named
    output file) — none of which is the single durable file a later consumer
    (verify-reproduction, which byte-diffs two runs' reduced metrics) can read.
    Write ``<out>/metrics_aggregate.json`` matching the shape
    :func:`_cluster_final_reduce` produces/consumes: ``{"aggregated_metrics":
    ..., "provenance": {...}}``. ``out`` is
    ``<experiment_dir>/_aggregated/<run_id>`` by default, so the artifact lands
    at ``_aggregated/<run_id>/metrics_aggregate.json``.

    The opt-in ``HPC_CLUSTER_FINAL_REDUCE`` path does NOT call this: it produces
    a RICHER aggregate on the cluster (waves/manifest/errors_per_wave) and pulls
    it to the SAME canonical flat location, so verify-reproduction reads it too —
    routing it through here would downgrade that artifact to the leaner shape.

    Harvest-guard posture: BEST-EFFORT. A failed write logs a loud warning and
    NEVER aborts the harvest — the reduced metrics are already returned inline;
    only the durable-artifact convenience is lost until the next re-aggregate.
    """
    payload = {
        "aggregated_metrics": aggregated,
        "provenance": {
            "incomplete_waves": list(incomplete_waves),
            "source": source,
            "reduced_at": utcnow_iso(),
        },
    }
    agg_path = out / "metrics_aggregate.json"
    try:
        atomic_write_json(agg_path, payload)
    except (OSError, ValueError, TypeError) as exc:
        print(
            f"[aggregate-flow] WARNING: failed to persist the durable local "
            f"aggregate for run_id {run_id!r} at {agg_path}: {exc!r}. The reduced "
            f"metrics are still returned inline; verify-reproduction has no "
            f"durable artifact for this run until it is re-aggregated."
        )


def _record_scope_looks(
    experiment_dir: Path, run_id: str, *, reducer_block: str
) -> dict[str, dict[str, int]] | None:
    """Record one look per scope tag at a success terminal; return the PRIOR counts.

    Rigor-primitives T3 look recording. For every tag on the run's sidecar
    ``scopes``: FIRST snapshot :func:`state.scopes.count_prior_looks` (PRIOR by
    construction — this run's look is not on the ledger yet), THEN
    :func:`state.scopes.record_look` (deduped on ``(scope, run_id)``, so a
    replay of the same run re-reports the same counts and never double-counts).

    Returns ``{tag: {"prior_looks": int, "distinct_lineages": int}}`` — two
    plain integers per tag, no metric ever consulted — or ``None`` for a
    scope-less / sidecar-less run so existing consumers are untouched.
    """
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError, errors.HpcError):
        return None
    scopes = sidecar.get("scopes")
    if not scopes:
        return None
    from hpc_agent.state.scopes import count_prior_looks, lineage_root, record_look

    cmd_sha = str(sidecar.get("cmd_sha") or "")
    root = lineage_root(experiment_dir, run_id)
    out: dict[str, dict[str, int]] = {}
    for tag in scopes:
        tag_str = str(tag)
        out[tag_str] = count_prior_looks(experiment_dir, tag_str)
        record_look(
            experiment_dir,
            tag_str,
            run_id=run_id,
            cmd_sha=cmd_sha,
            lineage_root=root,
            reducer_block=reducer_block,
        )
    return out or None


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


# ── aggregate-flow detach-by-contract helpers (design §3; run-#10 F-K) ────────
#
# aggregate-flow is a COMPOSED atom (default detach OFF) — the detach seat only
# fires for a DIRECT top-level invocation that opts in. The block-terminal store,
# the detached lease, and the doctor dead-worker scan all key it under its VERB
# ("aggregate-flow") — the SAME string ``_spawn_detached`` stamps into the lease.
_AGG_FLOW_BLOCK_KEY = terminal_block_key("aggregate-flow")


def _agg_flow_cmd_sha(experiment_dir: Path, run_id: str) -> str:
    """The run's tree fingerprint (``cmd_sha``) from its sidecar, or ``""``.

    The identity a terminal replay is keyed on (mirrors the submit-block /
    status-watch recorders): a moved/absent ``cmd_sha`` → the replay refuses
    (re-execute), never a false hit.
    """
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (OSError, ValueError, errors.HpcError):
        return ""
    return str((sidecar or {}).get("cmd_sha") or "")


def _detached_agg_flow_spec_dict(spec: AggregateFlowSpec) -> dict[str, Any]:
    """Serialize *spec* with ``detach`` forced OFF for the detached child.

    The child runs the SAME aggregate-flow body synchronously (its harvest IS the
    point), so its spec must carry ``detach=False`` — a truthy detach would fork
    forever.
    """
    return spec.model_copy(update={"detach": False}).model_dump(mode="json")


def _replay_agg_flow_terminal(experiment_dir: Path, run_id: str) -> AggregateFlowResult | None:
    """Return a finished aggregate-flow worker's recorded terminal for the CURRENT
    tree, else ``None`` (run #7 idempotent re-invoke).

    Replays ONLY when the current sidecar ``cmd_sha`` equals the one recorded with
    the terminal. A moved/absent ``cmd_sha``, an absent record, or a corrupt record
    all return ``None`` so the caller re-executes.
    """
    from hpc_agent.state.block_terminal import read_terminal

    record = read_terminal(experiment_dir, run_id, _AGG_FLOW_BLOCK_KEY)
    if record is None:
        return None
    current_sha = _agg_flow_cmd_sha(experiment_dir, run_id)
    if not current_sha or str(record.get("cmd_sha") or "") != current_sha:
        return None
    stored = record.get("result")
    if not isinstance(stored, dict):
        return None
    try:
        return AggregateFlowResult(**stored)
    except TypeError:
        return None


def _record_agg_flow_terminal(experiment_dir: Path, result: AggregateFlowResult) -> None:
    """Record a genuine aggregate-flow terminal so a re-invoke replays it.

    Runs on the SYNCHRONOUS path — which is exactly what the detached child
    executes — so the parent's replay finds it. A run with no run_id carries
    nothing to key on; the detached HANDLE (``started``) is not terminal and is
    skipped.
    """
    if not result.run_id or result.started:
        return
    from hpc_agent.state.block_terminal import record_terminal

    record_terminal(
        experiment_dir,
        run_id=result.run_id,
        block=_AGG_FLOW_BLOCK_KEY,
        cmd_sha=_agg_flow_cmd_sha(experiment_dir, result.run_id),
        result_dump=result.to_envelope_data(),
    )


def _detached_agg_flow_result(
    *, run_id: str, pid: int, log_path: str | None
) -> AggregateFlowResult:
    """The immediate-return handle for a detached aggregate-flow (design §3).

    The data fields are empty (the reduced metrics arrive on completion, read from
    the journal); ``started`` / ``watch`` / ``detached_pid`` carry the handle that
    ``_is_detached`` / ``wait-detached`` key on.
    """
    return AggregateFlowResult(
        run_id=run_id,
        combined_waves=[],
        failed_waves=[],
        waves_combined_this_call=[],
        combiner_dir_local=str(log_path or ""),
        aggregated_metrics={},
        started=True,
        watch="journal",
        detached_pid=pid,
    )


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

    Detach-by-contract (design §3; run-#10 F-K) is handled HERE, in the thin
    wrapper; the reduce itself is :func:`_aggregate_flow_impl`. aggregate-flow is a
    COMPOSED atom (harvest-guard's §5 guaranteed harvest, submit-s4, aggregate-run,
    campaign-run all call it SYNCHRONOUSLY and consume its metrics inline), so
    ``detach`` defaults OFF and the wrapper is a pass-through on every composed
    path. Only a DIRECT top-level invocation that opts in (``detach=true`` — the
    MCP seam forces an agent to) detaches: the sync gates (no journal record →
    ``JournalCorrupt``; a locked evidence scope → ``ScopeLocked``) fire in the
    PARENT before the spawn, then a durable detached worker owns the combine +
    rsync harvest and the wrapper returns a ``{started, watch: journal,
    detached_pid}`` handle. A re-invoke after the worker finished REPLAYS the
    recorded terminal. On the synchronous path the wrapper records the terminal so
    the parent's replay finds it.
    """
    if spec.detach:
        # gate → detach ordering PROOF: the fail-fast sync gates run in the parent.
        record = load_run(experiment_dir, spec.run_id)
        if record is None:
            raise errors.JournalCorrupt(
                f"no journal record for {spec.run_id!r}; submit the run first"
            )
        assert_scopes_unlocked(experiment_dir, spec.run_id)
        replay = _replay_agg_flow_terminal(experiment_dir, spec.run_id)
        if replay is not None:
            return replay

        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached

        launch = launch_submit_block_detached(
            verb="aggregate-flow",
            experiment_dir=str(experiment_dir),
            spec=_detached_agg_flow_spec_dict(spec),
        )
        return _detached_agg_flow_result(
            run_id=launch.run_id, pid=launch.pid, log_path=launch.log_path
        )

    result = _aggregate_flow_impl(
        experiment_dir,
        spec=spec,
        mode=mode,
        aggregate_cmd=aggregate_cmd,
        aggregate_output_path=aggregate_output_path,
    )
    _record_agg_flow_terminal(experiment_dir, result)
    return result


def _aggregate_flow_impl(
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

    # Scope gate (rigor-primitives T3): a run whose caller-attached evidence
    # scope is locked must not be reduced — a lock is deliberate human state
    # and reducing it would spend a reserved look. Pure LOCAL read, and it
    # fires BEFORE the opt-in reconcile poll below so a locked run triggers
    # zero SSH of any kind (the enforcement-map contract "the gate fires
    # before any SSH on a locked run" — verifier finding L1, 2026-07-07).
    # Scope-less or sidecar-less runs pass untouched.
    assert_scopes_unlocked(experiment_dir, run_id)

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

    # Class B (#337): a pure-API backend (``requires_ssh=False``) has no login
    # node and no shared filesystem — there is nothing to ``rsync_pull``. Core
    # dispatches on the *capability*, never on the scheduler name: the backend's
    # ``fetch_results`` hook pulls the run's artifacts over its API and we reduce
    # LOCALLY. Reduction honours ``mode`` / ``aggregate_cmd`` exactly as the SSH
    # path does (custom reducer when selected, else the weighted-mean), just run
    # locally — so a pure-API backend is not locked into the numeric mean. The
    # entire SSH ladder below (ssh-target validation, combine-wave,
    # ``_combiner/`` pull, cluster final-reduce, summaries pull, cluster-side
    # row/column gates) is skipped — those steps presuppose a shared filesystem
    # the pure-API backend lacks. SSH families default ``requires_ssh=True`` and
    # fall through unchanged.
    if not backend_requires_ssh(record.backend):
        aggregated = _pure_api_reduce(
            experiment_dir,
            run_id,
            record=record,
            out=out,
            mode=mode,
            aggregate_cmd=aggregate_cmd,
        )
        # Persist the durable comparator artifact through the ONE seam every
        # reduce path routes through (verifier finding L2): a pure-API run must
        # leave verify-reproduction a byte-readable ``metrics_aggregate.json``
        # just like the SSH default path does — otherwise it is honestly but
        # needlessly ``incomparable``. No wave partials on this path, so
        # ``incomplete_waves`` is empty. A reducer that emitted a non-dict JSON
        # (list/scalar) already collapsed to ``{}`` above, so the persisted
        # ``aggregated_metrics`` is that same empty dict — honestly comparable-
        # but-empty (no keyed metrics), never a fabricated scalar.
        _persist_local_aggregate(
            out, run_id, aggregated=aggregated, incomplete_waves=[], source="pure_api"
        )
        return AggregateFlowResult(
            run_id=run_id,
            combined_waves=list(record.combined_waves),
            failed_waves=list(record.failed_waves),
            waves_combined_this_call=[],
            combiner_dir_local=str(out),
            aggregated_metrics=aggregated,
            summaries_dir_local=None,
            escalation_reason=None,
            scope_looks=_record_scope_looks(experiment_dir, run_id, reducer_block="aggregate-flow"),
        )

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
        # ``cluster_reduce`` pulls the reducer's single JSON output to
        # ``out/<basename>.json`` (a reducer-named file), NOT the
        # ``metrics_aggregate.json`` verify-reproduction reads. Route the
        # reduced metrics through the ONE persistence seam (verifier finding
        # L2) so a cluster-reduced original + reproduction are comparable
        # end-to-end. A reducer that emitted a non-dict JSON (list/scalar) has
        # no ``dict[str, dict]`` keyed shape, so ``cr_aggregated`` is ``{}`` and
        # the run stays honestly incomparable (no keyed metrics to diff) — the
        # honest equivalent, never a fabricated comparability.
        cr_aggregated = cr["reduced"] if isinstance(cr.get("reduced"), dict) else {}
        _persist_local_aggregate(
            out, run_id, aggregated=cr_aggregated, incomplete_waves=[], source="cluster_reduce"
        )
        return AggregateFlowResult(
            run_id=run_id,
            combined_waves=list(record.combined_waves),
            failed_waves=list(record.failed_waves),
            waves_combined_this_call=[],
            combiner_dir_local=str(out),
            aggregated_metrics=cr_aggregated,
            # cluster-reduce performs the reduction on the cluster and
            # pulls the single reduced output; there is no separate
            # per-task summaries directory in this branch. The field is
            # documented as "set when pull_summaries=true" — leaving
            # None preserves that contract. ``output_path_local`` from
            # ``cluster_reduce`` is the single reduced *file* and lives
            # under ``combiner_dir_local`` already.
            summaries_dir_local=None,
            escalation_reason=None,
            scope_looks=_record_scope_looks(experiment_dir, run_id, reducer_block="aggregate-flow"),
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
        aggregated, incomplete_waves, reduce_source = _combiner_only_reduce(
            experiment_dir,
            run_id,
            record=record,
            combiner_local=combiner_local,
            results_subdir=results_subdir,
            out=out,
            # The run's declared per-task summary filename (F-J), resolved ONCE
            # at this seam from the sidecar already read above; threaded down so
            # the per-task fallback pull/rglob/reduce key on the real file (e.g.
            # results_reduce.json) instead of the metrics.json hardcode that read
            # run #10 as a harvest gap. Absent/blank sidecar → metrics.json.
            summary_name=resolved_summary_artifact(sidecar_for_cmd),
        )
        # Persist a durable local aggregate artifact on the DEFAULT path. The
        # combiner-only reduce (and its per-task fallback) return
        # ``aggregated_metrics`` inline and otherwise persist only the pulled
        # ``_combiner/wave_*.json`` partials — there is no single durable file a
        # later consumer (verify-reproduction, which diffs two runs) can
        # byte-read. Mirror the cluster-final path's
        # ``_aggregated/<run_id>/metrics_aggregate.json``. Best-effort: a failed
        # write warns loudly and never aborts the harvest.
        _persist_local_aggregate(
            out,
            run_id,
            aggregated=aggregated,
            incomplete_waves=incomplete_waves,
            source=reduce_source,
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
    summary_pull_error: str | None = None
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
            # Non-fatal — an unreachable host / wrong results_subdir / permission
            # error (a glob matching nothing exits 0, so this is a genuine
            # transport failure). Carry the real stderr in its OWN escalation
            # token — never overload combiner_failures with a sentinel wave -1
            # that both renders "combiner exhausted retries on wave -1" AND drops
            # the stderr on the floor. Leave summaries_local None so the column
            # check below does not silently validate an empty/partial dir.
            summary_pull_error = (sp.stderr or "").strip()[:300]
        else:
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
    if summary_pull_error is not None:
        escalation_parts.append(f"summary_rsync_failed:{summary_pull_error}")
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
        scope_looks=_record_scope_looks(experiment_dir, run_id, reducer_block="aggregate-flow"),
    )
