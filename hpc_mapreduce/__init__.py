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
    "report_status",
    "detect_scheduler",
    "reduce_counters",
    # GPU selection
    "pick_gpu",
    # Map protocol
    "MapContext",
    "map_context",
    "collect_outputs",
    # Reduce
    "reduce_metrics",
    # Grid API
    "expand_grid",
    "build_task_manifest",
    "total_tasks",
]

from pathlib import Path

from hpc_mapreduce.infra.clusters import load_clusters_config
from hpc_mapreduce.infra.gpu import pick_gpu
from hpc_mapreduce.infra.remote import deploy_runtime, rsync_pull, rsync_push, ssh_run
from hpc_mapreduce.job.grid import build_task_manifest, expand_grid, total_tasks
from hpc_mapreduce.map.context import MapContext, collect_outputs, map_context
from hpc_mapreduce.reduce.metrics import reduce_metrics
from hpc_mapreduce.reduce.status import (
    check_results,
    detect_scheduler,
    reduce_counters,
    report_status,
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
