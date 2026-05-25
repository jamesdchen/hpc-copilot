"""Per-task metrics sidecar writer.

Stdlib-only.  Safe to import from an executor running on a cluster compute
node without the full ``hpc_agent`` install -- this module is deployed
alongside ``combiner.py`` by :func:`hpc_agent.infra.remote.deploy_runtime`.

Executors call :func:`write_metrics` at the end of their run to drop a
``metrics.json`` sidecar into ``$RESULT_DIR``.  The combiner
(``hpc_agent/models/mapreduce/combiner.py``) reads that sidecar per task to
aggregate metrics per grid point.
"""

from __future__ import annotations

__all__ = ["read_kw_env", "write_metrics"]

import contextlib
import json
import os
import tempfile

# Per-task kwargs are exported by the dispatcher as ``HPC_KW_<UPPER>=<value>``.
# The value carries no type information — everything arrives as a string. The
# executor is responsible for the cast (``int(env["lr"])`` etc.) because only
# the executor knows the intended type. Strategy libraries (Optuna, etc.) that
# care about types should pass through their own typed value to the dispatcher
# rather than relying on this helper to round-trip them.
_KW_PREFIX = "HPC_KW_"


def read_kw_env() -> dict[str, str]:
    """Return a dict of ``{lowercase_name: str_value}`` for every ``HPC_KW_*``
    env var the dispatcher exported for this task.

    Strips the ``HPC_KW_`` prefix, lowercases the key, and leaves the value
    as the str the env carries. Empty when no ``HPC_KW_*`` vars are set
    (open-loop task with no kwargs, or running outside the dispatcher).

    Stdlib-only; safe to import from an executor running on a cluster
    compute node without the full ``hpc_agent`` install — same
    deployment guarantee as :func:`write_metrics`.
    """
    return {
        k.removeprefix(_KW_PREFIX).lower(): v
        for k, v in os.environ.items()
        if k.startswith(_KW_PREFIX)
    }


def write_metrics(metrics: dict, *, result_dir: str | None = None) -> str:
    """Write ``metrics.json`` atomically into *result_dir* (default ``$RESULT_DIR``).

    The dispatcher (``hpc_agent/models/mapreduce/dispatch.py``) sets ``RESULT_DIR`` to
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(metrics, f)
            # fsync before replace — a node-level crash between the
            # rename and the OS page-cache writeback would otherwise
            # leave a zero-byte metrics.json, tripping the dispatcher's
            # idempotency skip on the next attempt.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

    return dst
