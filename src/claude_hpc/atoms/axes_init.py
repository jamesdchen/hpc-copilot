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
)
def axes_init(
    *,
    experiment_dir: Path,
    homogeneous_axes: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Write ``<experiment>/.hpc/axes.yaml`` with the supplied homogeneity hints.

    Refuses to overwrite an existing ``axes.yaml`` unless ``force=True`` —
    the user may have hand-edited it. The framework's contract: every
    field in axes.yaml is one the framework can independently act on,
    so re-deriving without consent could clobber an intentional override.

    Returns ``{axes_path, homogeneous_axes, wrote, reason}``. ``wrote``
    is False when the file already existed and ``force`` was not set.
    """
    from claude_hpc.planning.axes import axes_path, write_axes

    target = axes_path(experiment_dir)
    if target.exists() and not force:
        return {
            "axes_path": str(target),
            "homogeneous_axes": list(homogeneous_axes or []),
            "wrote": False,
            "reason": (
                f"{target} already exists; pass force=true to overwrite. "
                "(Refuse-without-force preserves any hand-edits the user made.)"
            ),
        }

    written = write_axes(
        experiment_dir,
        homogeneous_axes=list(homogeneous_axes) if homogeneous_axes is not None else None,
    )
    return {
        "axes_path": str(written),
        "homogeneous_axes": list(homogeneous_axes or []),
        "wrote": True,
        "reason": f"wrote {written}",
    }
