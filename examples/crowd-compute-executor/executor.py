"""Container-portable executor demonstrating the dispatcher env contract.

This file proves one claim: an executor that reads its inputs from the
dispatcher's documented env vars and writes outputs to ``$RESULT_DIR``
runs unchanged on an HPC cluster *or* inside a container on a
crowd-compute platform. The contract (see
``docs/integrations/CONTRACT.md``, "Dispatcher-side env vars"):

* ``HPC_TASK_ID``  — 0-based task index; the determinism seed.
* ``HPC_KW_<KEY>`` — one per kwarg from ``tasks.resolve(task_id)``,
  uppercased key, JSON-encoded value.
* ``RESULT_DIR``   — directory to write outputs into. On-cluster this
  is the WIP dir that promotes atomically on exit-0; in a container
  the platform-side launcher ships it out after exit.

Stdlib-only **by design** — the same rule as the shipped standalone
templates (engineering-principles Q3): a container image must not
require the hpc-agent package, so the two tiny helpers below duplicate
``metrics_io.read_kw_env`` / ``metrics_io.write_metrics`` semantics
rather than importing them.

The compute body is a deliberately boring Monte-Carlo pi estimate so
the contract plumbing is the only thing this file teaches.
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile

_KW_PREFIX = "HPC_KW_"


def read_kwargs() -> dict:
    """Read the dispatcher's ``HPC_KW_<KEY>`` exports into kwargs.

    Mirrors ``hpc_agent.execution.mapreduce.metrics_io.read_kw_env``:
    keys are lowercased, values stay raw strings — the dispatcher
    exports ``str(value)`` and does no encoding, so a JSON decode here
    would re-type values (``"1e3"`` -> ``1000.0``) and break the "same
    image runs under a SLURM dispatcher" portability claim. Executors
    that need typed values parse their own flags, same as on-cluster.
    """
    kwargs: dict = {}
    for key, raw in os.environ.items():
        if not key.startswith(_KW_PREFIX):
            continue
        kwargs[key[len(_KW_PREFIX) :].lower()] = raw
    return kwargs


def write_metrics(metrics: dict, result_dir: str) -> str:
    """Atomically write ``metrics.json`` into *result_dir*.

    Mirrors ``metrics_io.write_metrics``: tempfile in the same dir,
    then ``os.replace``, so a reader never observes a half-written
    sidecar. Include scalar ``n_samples`` so the combiner's weighted
    mean weights this task correctly.
    """
    fd, tmp = tempfile.mkstemp(dir=result_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(metrics, fh, sort_keys=True)
        dst = os.path.join(result_dir, "metrics.json")
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return dst


def compute(task_id: int, n_samples: int) -> dict:
    """Monte-Carlo pi estimate, seeded from the task id.

    Same task_id -> same numbers regardless of which node (cluster or
    crowd) runs it — the reproducibility convention every executor
    should follow.
    """
    rng = random.Random(task_id)
    hits = sum(1 for _ in range(n_samples) if rng.random() ** 2 + rng.random() ** 2 <= 1.0)
    return {"pi_estimate": 4.0 * hits / n_samples, "n_samples": n_samples}


def main() -> int:
    try:
        task_id = int(os.environ["HPC_TASK_ID"])
        result_dir = os.environ["RESULT_DIR"]
    except KeyError as exc:
        print(f"executor: missing required env var {exc}", file=sys.stderr)
        return 1
    os.makedirs(result_dir, exist_ok=True)

    kwargs = read_kwargs()
    n_samples = int(kwargs.get("n_samples", 100_000))

    metrics = compute(task_id, n_samples)

    with open(os.path.join(result_dir, "result.json"), "w") as fh:
        json.dump({"task_id": task_id, "kwargs": kwargs, **metrics}, fh, sort_keys=True)
    write_metrics(metrics, result_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
