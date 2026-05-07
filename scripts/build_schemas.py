"""Regenerate JSON Schemas under ``claude_hpc/schemas/`` from Pydantic models.

The wire SoT is the JSON file (every external consumer reads it).
The *authoring* SoT is the Pydantic model under
``claude_hpc/_schema_models/``. This script bridges the two: it
calls ``model.model_json_schema()`` (or ``adapter.json_schema()``
for root-array schemas) for every entry in ``SCHEMA_REGISTRY`` and
writes / diffs the matching JSON file.

Same generator pattern as ``build_primitive_frontmatter.py``,
``build_primitive_index.py``, and ``build_operations_index.py``:
pre-commit + CI run ``--check`` so editing a Pydantic model without
regenerating the JSON is a CI failure.

Usage::

    uv run python scripts/build_schemas.py            # diff
    uv run python scripts/build_schemas.py --check    # CI gate
    uv run python scripts/build_schemas.py --write    # apply

Style policy: emit whatever Pydantic v2 produces (``anyOf`` for
nullables, auto-titles per field, etc.). The wire validators and
LLM consumers don't care about cosmetic differences; chasing
byte-equality with hand-authored schemas isn't worth a custom
``GenerateJsonSchema`` subclass. The script does inject
``$schema``, ``$id``, and (when the model docstring or
``model_config['title']`` is present) reorder the top-level keys
into the conventional layout.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any, Union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pydantic import BaseModel, TypeAdapter  # noqa: E402

from claude_hpc._schema_models.aggregate_flow import (  # noqa: E402
    AggregateFlowResult,
    AggregateFlowSpec,
)
from claude_hpc._schema_models.axes import AxesConfig  # noqa: E402
from claude_hpc._schema_models.best_submit_window import (  # noqa: E402
    BestSubmitWindowResult,
    BestSubmitWindowSpec,
)
from claude_hpc._schema_models.build_executor import BuildExecutorResult  # noqa: E402
from claude_hpc._schema_models.build_submit_spec import BuildSubmitSpecInput  # noqa: E402
from claude_hpc._schema_models.build_tasks_py import BuildTasksPyInput  # noqa: E402
from claude_hpc._schema_models.campaign import CampaignAdapter  # noqa: E402
from claude_hpc._schema_models.campaign_health import (  # noqa: E402
    CampaignHealthResult,
    CampaignHealthSpec,
)
from claude_hpc._schema_models.campaign_manifest import CampaignManifest  # noqa: E402
from claude_hpc._schema_models.capabilities import CapabilitiesResult  # noqa: E402
from claude_hpc._schema_models.cluster_reduce import ClusterReduceResult  # noqa: E402
from claude_hpc._schema_models.clusters import (  # noqa: E402
    ClustersDescribeResult,
    ClustersListResult,
)
from claude_hpc._schema_models.combine_wave import CombineWaveResult  # noqa: E402
from claude_hpc._schema_models.decide_monitor_arm import (  # noqa: E402
    DecideMonitorArmResult,
    DecideMonitorArmSpec,
)
from claude_hpc._schema_models.discover import DiscoverResult  # noqa: E402
from claude_hpc._schema_models.envelope import EnvelopeAdapter  # noqa: E402
from claude_hpc._schema_models.failures import FailuresResult  # noqa: E402
from claude_hpc._schema_models.find_prior_run import FindPriorRunResult  # noqa: E402
from claude_hpc._schema_models.inspect_cluster import InspectClusterResult  # noqa: E402
from claude_hpc._schema_models.interview import (  # noqa: E402
    InterviewEnvelope,
    InterviewSpec,
)
from claude_hpc._schema_models.list_in_flight import ListInFlightResult  # noqa: E402
from claude_hpc._schema_models.monitor_flow import (  # noqa: E402
    MonitorFlowResult,
    MonitorFlowSpec,
)
from claude_hpc._schema_models.monitor_summary import MonitorSummaryResult  # noqa: E402
from claude_hpc._schema_models.plan_submit import PlanSubmitResult  # noqa: E402
from claude_hpc._schema_models.predict_queue_wait import (  # noqa: E402
    PredictQueueWaitResult,
    PredictQueueWaitSpec,
)
from claude_hpc._schema_models.preflight import PreflightResult  # noqa: E402
from claude_hpc._schema_models.recall import RecallEnvelope, RecallSpec  # noqa: E402
from claude_hpc._schema_models.reconcile import ReconcileResult  # noqa: E402
from claude_hpc._schema_models.resubmit import ResubmitSpec  # noqa: E402
from claude_hpc._schema_models.runtime_prior import RuntimePriorResult  # noqa: E402
from claude_hpc._schema_models.stages import StagesAdapter  # noqa: E402
from claude_hpc._schema_models.status import StatusResult  # noqa: E402
from claude_hpc._schema_models.submit import SubmitResult, SubmitSpec  # noqa: E402
from claude_hpc._schema_models.submit_flow import (  # noqa: E402
    SubmitFlowResult,
    SubmitFlowSpec,
)
from claude_hpc._schema_models.submit_flow_batch import (  # noqa: E402
    SubmitFlowBatchResult,
    SubmitFlowBatchSpec,
)
from claude_hpc._schema_models.suggest_setup_action import (  # noqa: E402
    SuggestSetupActionResult,
)
from claude_hpc._schema_models.summarize_submit_plan import (  # noqa: E402
    SummarizeSubmitPlanResult,
)
from claude_hpc._schema_models.validate import ValidateResult, ValidateSpec  # noqa: E402
from claude_hpc._schema_models.validate_campaign import (  # noqa: E402
    ValidateCampaignReport,
    ValidateCampaignSpec,
)
from claude_hpc._schema_models.validate_executor_signatures import (  # noqa: E402
    ValidateExecutorSignaturesResult,
    ValidateExecutorSignaturesSpec,
)
from claude_hpc._schema_models.validate_input_dataset import (  # noqa: E402
    ValidateInputDatasetResult,
    ValidateInputDatasetSpec,
)
from claude_hpc._schema_models.validate_walltime_against_history import (  # noqa: E402
    ValidateWalltimeAgainstHistoryResult,
    ValidateWalltimeAgainstHistorySpec,
)
from claude_hpc._schema_models.verify_aggregation_complete import (  # noqa: E402
    VerifyAggregationCompleteResult,
)
from claude_hpc._schema_models.verify_canary import VerifyCanaryResult  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "claude_hpc" / "schemas"

_ID_BASE = "https://github.com/jamesdchen/claude-hpc/schemas"

# Each entry: (Pydantic model OR TypeAdapter, schema filename).
# The schema's ``$id`` is derived from the filename
# (``<_ID_BASE>/<filename>``) — no need to repeat the URL here.
SCHEMA_REGISTRY: list[tuple[Union[type[BaseModel], TypeAdapter[Any]], str]] = [
    # Cross-cutting wire envelope + persisted-data shapes
    (EnvelopeAdapter, "envelope.json"),
    (AxesConfig, "axes.json"),
    (CampaignManifest, "campaign_manifest.json"),
    (CampaignAdapter, "campaign.output.json"),
    # Workflows
    (AggregateFlowSpec, "aggregate_flow.input.json"),
    (AggregateFlowResult, "aggregate_flow.output.json"),
    (MonitorFlowSpec, "monitor_flow.input.json"),
    (MonitorFlowResult, "monitor_flow.output.json"),
    (SubmitFlowSpec, "submit_flow.input.json"),
    (SubmitFlowResult, "submit_flow.output.json"),
    (SubmitFlowBatchSpec, "submit_flow_batch.input.json"),
    (SubmitFlowBatchResult, "submit_flow_batch.output.json"),
    (VerifyCanaryResult, "verify_canary.output.json"),
    # Scaffolds
    (BuildExecutorResult, "build_executor.output.json"),
    (BuildSubmitSpecInput, "build_submit_spec.input.json"),
    (BuildTasksPyInput, "build_tasks_py.input.json"),
    (InterviewSpec, "interview.input.json"),
    (InterviewEnvelope, "interview.output.json"),
    # Validate
    (PreflightResult, "preflight.output.json"),
    (ValidateSpec, "validate.input.json"),
    (ValidateResult, "validate.output.json"),
    (ValidateCampaignSpec, "validate_campaign.input.json"),
    (ValidateCampaignReport, "validate_campaign.output.json"),
    (ValidateExecutorSignaturesSpec, "validate_executor_signatures.input.json"),
    (ValidateExecutorSignaturesResult, "validate_executor_signatures.output.json"),
    (ValidateInputDatasetSpec, "validate_input_dataset.input.json"),
    (ValidateInputDatasetResult, "validate_input_dataset.output.json"),
    (ValidateWalltimeAgainstHistorySpec, "validate_walltime_against_history.input.json"),
    (ValidateWalltimeAgainstHistoryResult, "validate_walltime_against_history.output.json"),
    # Mutate / submit
    (ClusterReduceResult, "cluster_reduce.output.json"),
    (CombineWaveResult, "combine_wave.output.json"),
    (ReconcileResult, "reconcile.output.json"),
    (ResubmitSpec, "resubmit.input.json"),
    (SubmitSpec, "submit.input.json"),
    (SubmitResult, "submit.output.json"),
    # Query
    (BestSubmitWindowSpec, "best_submit_window.input.json"),
    (BestSubmitWindowResult, "best_submit_window.output.json"),
    (CampaignHealthSpec, "campaign_health.input.json"),
    (CampaignHealthResult, "campaign_health.output.json"),
    (CapabilitiesResult, "capabilities.output.json"),
    (ClustersDescribeResult, "clusters_describe.output.json"),
    (ClustersListResult, "clusters_list.output.json"),
    (DecideMonitorArmSpec, "decide_monitor_arm.input.json"),
    (DecideMonitorArmResult, "decide_monitor_arm.output.json"),
    (DiscoverResult, "discover.output.json"),
    (FailuresResult, "failures.output.json"),
    (FindPriorRunResult, "find_prior_run.output.json"),
    (InspectClusterResult, "inspect_cluster.output.json"),
    (ListInFlightResult, "list_in_flight.output.json"),
    (MonitorSummaryResult, "monitor_summary.output.json"),
    (PlanSubmitResult, "plan_submit.output.json"),
    (PredictQueueWaitSpec, "predict_queue_wait.input.json"),
    (PredictQueueWaitResult, "predict_queue_wait.output.json"),
    (RecallSpec, "recall.input.json"),
    (RecallEnvelope, "recall.output.json"),
    (RuntimePriorResult, "runtime_prior.output.json"),
    (StagesAdapter, "stages.input.json"),
    (StatusResult, "status.output.json"),
    (SuggestSetupActionResult, "suggest_setup_action.output.json"),
    (SummarizeSubmitPlanResult, "summarize_submit_plan.output.json"),
    (VerifyAggregationCompleteResult, "verify_aggregation_complete.output.json"),
]


def _emit_schema(model_or_adapter: Any) -> dict[str, Any]:
    """Call the right schema-emit method for either a BaseModel or a TypeAdapter."""
    if isinstance(model_or_adapter, TypeAdapter):
        return model_or_adapter.json_schema()  # type: ignore[no-any-return]
    if isinstance(model_or_adapter, type) and issubclass(model_or_adapter, BaseModel):
        return model_or_adapter.model_json_schema()
    raise TypeError(f"unexpected schema source: {model_or_adapter!r}")


def _normalize(schema: dict, schema_id: str) -> dict:
    """Inject ``$schema`` / ``$id`` and reorder top-level keys.

    Pydantic v2 emits a draft-2020-12 schema with no ``$schema``
    declaration and no ``$id``; the project's hand-authored files
    carry both. We add them and reorder the top-level keys so the
    diff stays readable.
    """
    schema = dict(schema)
    schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
    schema["$id"] = schema_id
    preferred_order = (
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "required",
        "additionalProperties",
        "properties",
        "items",
        "minItems",
        "$defs",
    )
    ordered = {k: schema[k] for k in preferred_order if k in schema}
    for k, v in schema.items():
        if k not in ordered:
            ordered[k] = v
    return ordered


def _emit(model_or_adapter: Any, fname: str) -> str:
    schema = _emit_schema(model_or_adapter)
    schema = _normalize(schema, f"{_ID_BASE}/{fname}")
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    write = "--write" in sys.argv
    check = "--check" in sys.argv

    drift: list[tuple[Path, str, str]] = []  # (path, old, new)
    for src, fname in SCHEMA_REGISTRY:
        path = SCHEMAS_DIR / fname
        try:
            new = _emit(src, fname)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: emitting {fname}: {exc!r}", file=sys.stderr)
            return 2
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        if old != new:
            drift.append((path, old, new))

    if not drift:
        print(f"schemas up to date ({len(SCHEMA_REGISTRY)} models)")
        return 0

    if check:
        print(
            f"ERROR: {len(drift)} schema file(s) out of date — "
            "run scripts/build_schemas.py --write to regenerate",
            file=sys.stderr,
        )
        for path, _, _ in drift:
            print(f"  {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1

    if write:
        for path, _, new in drift:
            path.write_text(new, encoding="utf-8")
            print(f"  wrote {path.relative_to(REPO_ROOT)}")
        print(f"regenerated {len(drift)} schema file(s)")
        return 0

    # Default: print a diff so the human can preview without writing.
    for path, old, new in drift:
        rel = path.relative_to(REPO_ROOT)
        print(f"--- a/{rel}")
        print(f"+++ b/{rel}")
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            n=3,
        )
        sys.stdout.write("".join(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
