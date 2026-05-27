"""Pydantic models for the ``aggregate-flow`` workflow atom's wire contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import CombinedWaves, FailedWaves, RunIdLoose, RunIdStrict


class AggregateFlowSpec(BaseModel):
    """Spec passed to ``hpc-agent aggregate-flow --spec <file>``.

    Workflow atom that ensures every wave is combined on the cluster,
    pulls the per-wave partials locally, and merges them into a
    single aggregated-metrics dict via reduce_partials. Pairs with
    submit-flow + monitor-flow as the third workflow atom in the
    campaign composition pattern.
    """

    model_config = ConfigDict(extra="forbid", title="aggregate-flow input spec")

    run_id: RunIdStrict
    output_dir: str | None = Field(
        default=None,
        description=(
            "Local destination for pulled artifacts. Null defaults to "
            "`<experiment_dir>/_aggregated/<run_id>/`. The combiner "
            "partials land under `<output_dir>/_combiner/`."
        ),
    )
    ensure_all_combined: bool = Field(
        default=True,
        description=(
            "Before pulling, invoke combine-wave for every wave in the "
            "sidecar's wave_map that isn't yet in combined_waves. "
            "Disable when monitor-flow already combined everything in "
            "flight (idempotent either way; this just saves SSH "
            "round-trips)."
        ),
    )
    combiner_max_retries: int = Field(
        default=1,
        ge=0,
        description=(
            "Times to retry combine-wave with force=true after first "
            "failure. Beyond this, the wave is escalated and surfaces "
            "in failed_waves; aggregate-flow continues with whichever "
            "waves DID combine."
        ),
    )
    pull_summaries: bool = Field(
        default=False,
        description=(
            "After pulling _combiner/, also rsync result summary files "
            "matching summary_glob from the cluster's results/ directory."
        ),
    )
    summary_glob: str | None = Field(
        default=None,
        description=(
            "Required when pull_summaries=true. Rsync include pattern "
            "(e.g. 'metrics.json', 'qlike.json'). Ignored when "
            "pull_summaries=false."
        ),
    )
    results_subdir: str = Field(
        default="results",
        description=(
            "Cluster-side subdir under remote_path that holds the "
            "per-task result trees. Defaults to 'results' to match "
            "the framework convention."
        ),
    )
    min_rows: int = Field(
        default=0,
        ge=0,
        description=(
            "Non-empty-rows gate. When > 0, after combining + pulling, "
            "aggregate-flow runs the cluster-side status reporter with "
            "--min-rows: any task whose CSV result has fewer than this "
            "many data rows beyond the header is reported as a failing "
            "task id in `nonempty_failing_task_ids`. 0 (default) skips "
            "the gate — header-only CSVs are accepted."
        ),
    )
    mode: Literal["auto", "cluster-reduce", "combiner-only"] = Field(
        default="auto",
        description=(
            "Routing mode. 'auto' (default) picks cluster-reduce when "
            "the sidecar's aggregate_defaults.aggregate_cmd is set, "
            "otherwise combiner-only. 'cluster-reduce' forces the "
            "cluster-side reducer (raises if no aggregate_cmd is "
            "available). 'combiner-only' bypasses the reducer, pulls "
            "_combiner/ partials and reduces locally."
        ),
    )

    @model_validator(mode="after")
    def _require_summary_glob_when_pulling(self) -> AggregateFlowSpec:
        if self.pull_summaries and not self.summary_glob:
            raise ValueError("summary_glob is required when pull_summaries=true")
        return self


class AggregateFlowResult(BaseModel):
    """Shape of the ``data`` field on a successful ``aggregate-flow`` envelope."""

    model_config = ConfigDict(extra="forbid", title="aggregate-flow output data")

    run_id: RunIdLoose
    combined_waves: CombinedWaves
    failed_waves: FailedWaves
    waves_combined_this_call: list[int] = Field(
        description=(
            "Waves that combine-flow combined during this invocation "
            "(vs already-combined entering the call). Useful for "
            "caller logging."
        ),
    )
    combiner_dir_local: str = Field(
        description="Local path where the cluster's `_combiner/` directory was rsync'd.",
    )
    aggregated_metrics: dict[str, Any] = Field(
        description=(
            "Output of reduce_partials over the pulled "
            "combiner_dir_local. Mapping of run_id (or grid-point key) "
            "to aggregated metric dict. Empty when no waves combined "
            "successfully."
        ),
    )
    summaries_dir_local: str | None = Field(
        default=None,
        description=(
            "Local path where per-task summary files were pulled. Null when pull_summaries=false."
        ),
    )
    escalation_reason: str | None = Field(
        default=None,
        description=(
            "When non-null, indicates partial success — typically "
            "'combiner_failed_max_retries:waves=...'. The caller may "
            "inspect failed_waves and decide whether the partial "
            "aggregation is acceptable. Null when every wave combined "
            "cleanly."
        ),
    )
    nonempty_rows_checked: bool = Field(
        default=False,
        description=(
            "True when the non-empty-rows gate ran (spec.min_rows > 0). "
            "False when the gate was skipped — `nonempty_failing_task_ids` "
            "is then an empty list and carries no signal."
        ),
    )
    nonempty_failing_task_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Task ids (1-based) whose CSV result has fewer than "
            "spec.min_rows data rows beyond the header — i.e. tasks that "
            "wrote a file but no real data. Empty when the gate passed or "
            "was skipped. A non-empty list means the aggregate was "
            "computed over tasks that produced no usable rows."
        ),
    )
    columns_checked: bool = Field(
        default=False,
        description=(
            "True when the expected-columns / non-NaN-metric gate ran — "
            "i.e. the run sidecar's `results` block declared "
            "`expected_columns` and/or `metric_column` AND a local "
            "results directory was available to scan. False = skipped "
            "(no declared schema, or no pulled results)."
        ),
    )
    column_violations: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Per-result-file violations found by the columns gate: each "
            "entry is {path, missing_columns, metric_nan, error}. Empty "
            "when the gate passed or was skipped."
        ),
    )
