"""Best-effort axes-driven wave_map derivation.

Extracted from :mod:`hpc_agent.state.runs` so the sidecar lifecycle
module stays focused on path helpers, sidecar I/O, and lifecycle
(find / prune / update). The helper here is a pure derivation: given an
experiment directory and a task count, it consults ``axes.yaml`` and
returns the throughput optimizer's wave_map (or ``None`` on any miss).

Re-exported from :mod:`hpc_agent.state.runs` (under the original
``_maybe_derive_wave_map`` underscore name) for backwards compatibility.
"""

from __future__ import annotations

import warnings
from pathlib import Path

__all__ = ["derive_wave_map"]


def derive_wave_map(experiment_dir: Path, *, task_count: int) -> dict[str, list[int]] | None:
    """Best-effort axes-driven wave_map derivation. Returns None on any miss.

    Silent on the happy path; emits a :class:`UserWarning` only when
    ``axes.yaml`` is present with a full enumeration but the cartesian
    product of axis sizes disagrees with *task_count* — that's a sign
    of a misconfigured deploy and the user wants to hear about it.
    """
    import jsonschema
    import yaml

    from hpc_agent import errors

    try:
        from hpc_agent.state.axes import (
            compute_wave_map,
            pick_array_axis,
            read_axes,
        )
    except ImportError:
        return None

    try:
        config = read_axes(experiment_dir)
    except (jsonschema.ValidationError, yaml.YAMLError, ValueError, OSError, errors.JournalCorrupt):
        return None
    if config is None or not config.get("axes"):
        return None

    sizes = [int(a["size"]) for a in config["axes"]]
    product = 1
    for s in sizes:
        product *= s
    if product != task_count:
        warnings.warn(
            f"axes.yaml product ({product}) != task_count ({task_count}); "
            "skipping auto-derived wave_map. Re-run /hpc-axes-init or pass "
            "wave_map explicitly.",
            UserWarning,
            stacklevel=3,
        )
        return None

    picked_name, picker_reason = pick_array_axis(experiment_dir)
    if picked_name is None:
        # Picker couldn't choose (no homogeneous_axes hint, no qualifying
        # axis after CV scoring, etc). axes.yaml HAD a multi-axis
        # enumeration; degrading to single-wave aggregation silently
        # surprises the user when they later see per-wave combiner
        # output collapse. Warn loudly so the operator knows what
        # changed; the degraded path still works (downstream treats
        # a missing wave_map as "single implicit wave-0").
        warnings.warn(
            f"axes.yaml declared multi-axis enumeration but pick_array_axis "
            f"returned None ({picker_reason!r}); sidecar will lack wave_map "
            "and downstream auto-combine-waves will degrade to single-wave "
            "aggregation. Add homogeneous_axes to axes.yaml or pass "
            "wave_map explicitly to /submit to enforce a specific shape.",
            UserWarning,
            stacklevel=3,
        )
        return None
    try:
        derived = compute_wave_map(experiment_dir, picked_axis=picked_name)
    except (ValueError, jsonschema.ValidationError):
        return None
    return {str(k): list(v) for k, v in derived.items()}
