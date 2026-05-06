"""``axes-init`` primitive — write the per-experiment axes config.

Thin wrapper around :func:`claude_hpc.planning.axes.write_axes` with
existence-check + ``--force`` semantics so the slash command (and any
non-Claude-Code agent) can call this safely. The agent does the
introspection + classification work upstream; this primitive just
records the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_hpc import errors
from claude_hpc._internal._primitive import SideEffect, primitive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="axes-init",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/axes.yaml"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli="hpc-mapreduce axes-init",
)
def axes_init(
    *,
    experiment_dir: Path,
    axes: list[dict[str, Any]] | None = None,
    homogeneous_axes: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Write ``<experiment>/.hpc/axes.yaml`` with the supplied hints.

    Refuses to overwrite an existing ``axes.yaml`` unless ``force=True`` —
    the user may have hand-edited it. The framework's contract: every
    field in axes.yaml is one the framework can independently act on,
    so re-deriving without consent could clobber an intentional override.

    *axes* is the ordered list of every parallel axis, each item
    ``{"name": str, "size": int}``. The order defines the
    cartesian-product convention used by :func:`compute_wave_map`.
    *homogeneous_axes* is the cold-start hint for the picker; if
    supplied alongside *axes*, every name must appear in *axes*.

    Returns ``{axes_path, axes, homogeneous_axes, wrote, reason}``.
    """
    from claude_hpc.planning.axes import axes_path, write_axes

    target = axes_path(experiment_dir)
    if target.exists() and not force:
        return {
            "axes_path": str(target),
            "axes": list(axes or []),
            "homogeneous_axes": list(homogeneous_axes or []),
            "wrote": False,
            "reason": (
                f"{target} already exists; pass force=true to overwrite. "
                "(Refuse-without-force preserves any hand-edits the user made.)"
            ),
        }

    try:
        written = write_axes(
            experiment_dir,
            axes=axes,
            homogeneous_axes=list(homogeneous_axes) if homogeneous_axes is not None else None,
        )
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc

    return {
        "axes_path": str(written),
        "axes": list(axes or []),
        "homogeneous_axes": list(homogeneous_axes or []),
        "wrote": True,
        "reason": f"wrote {written}",
    }
