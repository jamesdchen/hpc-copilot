"""hpc-mapreduce: MapReduce-style HPC orchestrator for Claude Code.

Provides pluggable HPC backends (SGE, SLURM), remote execution utilities,
GPU selection, and experiment-agnostic grid dispatch. Cluster infrastructure
is configured via clusters.yaml; experiment setup is conversational.
"""

__all__ = [
    # Package root
    "_PACKAGE_ROOT",
    # Config & discovery
    "load_clusters_config",
    "get_template_path",
    # Remote execution
    "ssh_run",
    "rsync_push",
    "rsync_pull",
    "deploy_runtime",
    # Job status & results
    "check_results",
    "check_results_from_manifest",
    "report_status",
    "report_status_from_manifest",
    "rollup_by_grid_point",
    "detect_scheduler",
    # Shim cache
    "shim_cache_key",
    "load_cached_shim",
    "save_shim",
    # GPU selection
    "pick_gpu",
    # Reduce
    "reduce_metrics",
    "reduce_backtest",
    "reduce_partials",
    "classify_failure",
    # Grid API
    "expand_grid",
    "expand_backtest",
    "build_task_manifest",
    "total_tasks",
    "attach_wave_map",
    "MANIFEST_SCHEMA_VERSION",
    # Cluster constraints
    "ClusterConstraints",
    "parse_constraints",
    # Throughput optimizer
    "WorkloadSpec",
    "SubmissionPlan",
    "compute_submission_plan",
    "build_wave_map",
    # Resubmit
    "compact_task_ids",
    "ResubmitBatch",
    "ResubmitPlan",
    "resubmit_plan",
    # Remote
    "run_combiner",
    "run_combiner_checked",
]

from pathlib import Path

from hpc_mapreduce.infra.clusters import load_clusters_config
from hpc_mapreduce.infra.gpu import pick_gpu
from hpc_mapreduce.infra.remote import (
    deploy_runtime,
    rsync_pull,
    rsync_push,
    run_combiner,
    run_combiner_checked,
    ssh_run,
)
from hpc_mapreduce.job.constraints import ClusterConstraints, parse_constraints
from hpc_mapreduce.job.grid import (
    MANIFEST_SCHEMA_VERSION,
    attach_wave_map,
    build_task_manifest,
    expand_backtest,
    expand_grid,
    total_tasks,
)
from hpc_mapreduce.job.resubmit import (
    ResubmitBatch,
    ResubmitPlan,
    compact_task_ids,
    resubmit_plan,
)
from hpc_mapreduce.job.throughput import (
    SubmissionPlan,
    WorkloadSpec,
    build_wave_map,
    compute_submission_plan,
)
from hpc_mapreduce.map.shim import load_cached_shim, save_shim, shim_cache_key
from hpc_mapreduce.reduce.classify import classify_failure
from hpc_mapreduce.reduce.metrics import reduce_backtest, reduce_metrics, reduce_partials
from hpc_mapreduce.reduce.status import (
    check_results,
    check_results_from_manifest,
    detect_scheduler,
    report_status,
    report_status_from_manifest,
    rollup_by_grid_point,
)

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def get_template_path(scheduler: str, template: str) -> Path:
    """Return the absolute path to a job template shipped with claude-hpc.

    Parameters
    ----------
    scheduler : ``"sge"`` or ``"slurm"``
    template : template name without extension (e.g. ``"cpu_array"``, ``"gpu_array"``)

    Returns
    -------
    Path to the template file.

    Raises
    ------
    FileNotFoundError
        If the resolved template does not exist on disk.
    """
    ext = ".sh" if scheduler == "sge" else ".slurm"
    path = Path(__file__).parent / "templates" / scheduler / f"{template}{ext}"
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path
