"""Per-task metrics sidecar writer.

Stdlib-only.  Safe to import from an executor running on a cluster compute
node without the full ``hpc_mapreduce`` install -- this module is deployed
alongside ``combiner.py`` by :func:`hpc_mapreduce.infra.remote.deploy_runtime`.

Executors call :func:`write_metrics` at the end of their run to drop a
``metrics.json`` sidecar into ``$RESULT_DIR``.  The combiner
(``hpc_mapreduce/map/combiner.py``) reads that sidecar per task to
aggregate metrics per grid point.
"""

from __future__ import annotations

__all__ = ["write_metrics"]

import contextlib
import json
import os
import tempfile


def write_metrics(metrics: dict, *, result_dir: str | None = None) -> str:
    """Write ``metrics.json`` atomically into *result_dir* (default ``$RESULT_DIR``).

    The dispatcher (``hpc_mapreduce/map/dispatch.py``) sets ``RESULT_DIR`` to
    the per-task WIP directory, so writing there means the sidecar is
    promoted atomically with the rest of the task's raw outputs on success.
    If the task crashes after the sidecar is written, the WIP dir is
    preserved for forensics instead of polluting the final result dir.

    Atomicity: the dict is serialized to a tempfile in the same directory,
    then ``os.replace``'d onto ``metrics.json``.  Readers (the combiner)
    never observe a half-written file.

    Parameters
    ----------
    metrics:
        Arbitrary JSON-serialisable dict.  Include a scalar ``n_samples``
        to weight the combiner's weighted mean; missing ``n_samples``
        defaults to 1 per task.
    result_dir:
        Target directory.  If ``None`` (default), falls back to the
        ``RESULT_DIR`` environment variable that the dispatcher exports.

    Returns
    -------
    str
        Absolute (or as-passed) path to the written ``metrics.json``.

    Raises
    ------
    RuntimeError
        If neither *result_dir* nor ``$RESULT_DIR`` is set.
    """
    rdir = result_dir if result_dir is not None else os.environ.get("RESULT_DIR")
    if not rdir:
        raise RuntimeError(
            "write_metrics: no result_dir given and $RESULT_DIR is unset "
            "(are you running outside the HPC dispatcher?)"
        )

    os.makedirs(rdir, exist_ok=True)
    dst = os.path.join(rdir, "metrics.json")

    fd, tmp = tempfile.mkstemp(prefix=".metrics.", suffix=".json", dir=rdir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(metrics, f)
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

    return dst
