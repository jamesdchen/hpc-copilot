"""A GPU/DL vision executor: a tiny ResNet-style train step over an lr sweep.

Fixture for the GPU corpus case. Its ``torch`` import is the classification
signal that flips the offline resolver to the GPU/DL resource profile
(≥1 gpu, more cpus, a multi-hour walltime band) instead of the CPU/ML default
— exactly the ``info.imports`` rule the ``/submit-hpc`` Step 4 table encodes.

The ``torch`` import is intentionally guarded: the eval reads this file as
TEXT to classify it and never imports the module, so the fixture works even
where torch is not installed. The guard documents that and keeps the file
importable for anyone who does run it directly.
"""

from __future__ import annotations

try:  # torch is the GPU/DL classification signal; import is optional at runtime
    import torch  # noqa: F401
except ImportError:  # pragma: no cover - fixture is classified by text, not import
    torch = None  # type: ignore[assignment]

from hpc_agent.experiment_kit import register_run


@register_run
def run(lr: float = 1e-3, seed: int = 0) -> dict:
    """Return a per-task metric for a trivial training step.

    The body is a placeholder — the eval asserts the agent decides to run a
    GPU job (the right cluster, the right resource profile), not the loss it
    reaches. ``lr`` and ``seed`` are the sweep axes the corpus references.
    """
    return {"lr": lr, "seed": seed, "loss": 1.0 / (1.0 + lr)}
