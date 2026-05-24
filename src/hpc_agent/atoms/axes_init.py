"""``axes-init`` primitive — write the per-experiment axes config.

Thin wrapper around :func:`hpc_agent.state.axes.write_axes` with
existence-check + ``--force`` semantics so the slash command (and any
non-Claude-Code agent) can call this safely. The agent does the
introspection + classification work upstream; this primitive just
records the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    import argparse
    from pathlib import Path


def _axes_init_arg_pre(ns: argparse.Namespace) -> dict[str, Any]:
    """Parse ``--axes "NAME:SIZE,..."`` and ``--homogeneous-axes`` into list/dict shapes.

    Both flags are typed as comma-separated strings on the CLI (humans
    type them; argparse can't natively coerce into the primitive's
    ``list[dict]`` shape), so this hook does the comma-split + key-value
    parse before the primitive is called.
    """
    homogeneous = (
        [s.strip() for s in ns.homogeneous_axes.split(",") if s.strip()]
        if getattr(ns, "homogeneous_axes", None)
        else []
    )
    axes_list: list[dict[str, Any]] = []
    raw_axes = getattr(ns, "axes", None)
    if raw_axes:
        for tok in raw_axes.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if ":" not in tok:
                raise errors.SpecInvalid(f"--axes entry {tok!r} must be NAME:SIZE")
            name, _, size_s = tok.partition(":")
            try:
                size = int(size_s)
            except ValueError as exc:
                raise errors.SpecInvalid(f"--axes entry {tok!r} has non-integer size") from exc
            axes_list.append({"name": name.strip(), "size": size})
    return {"axes": axes_list or None, "homogeneous_axes": homogeneous}


@primitive(
    name="axes-init",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/axes.yaml"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Write <experiment>/.hpc/axes.yaml with per-axis homogeneity "
            "hints used by the cold-start axis_picker. The agent typically "
            "calls this once per repo at deploy time after introspecting "
            "tasks.py."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--axes",
                type=str,
                default="",
                help=(
                    "Comma-separated NAME:SIZE pairs for every parallel axis "
                    "(e.g. 'model:4,data:3,window:20'). Order defines the "
                    "cartesian-product convention; required for submit-flow's "
                    "wave_map building."
                ),
            ),
            CliArg(
                "--homogeneous-axes",
                type=str,
                default="",
                help=("Comma-separated axis names to mark homogeneous (e.g. 'window,fold')."),
            ),
            CliArg(
                "--force",
                action="store_true",
                help=("Overwrite an existing axes.yaml. Default is refuse-without-force."),
            ),
        ),
        arg_pre=_axes_init_arg_pre,
    ),
    agent_facing=True,
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
    from hpc_agent.state.axes import axes_path, write_axes

    target = axes_path(experiment_dir)
    if target.exists() and not force:
        # On refuse, echo the on-disk state (not the requested values) so
        # callers don't mistake the refusal for "your axes were accepted".
        from hpc_agent.state.axes import read_axes

        try:
            existing = read_axes(experiment_dir) or {}
        except (FileNotFoundError, OSError, ValueError):
            existing = {}
        return {
            "axes_path": str(target),
            "axes": list(existing.get("axes") or []),
            "homogeneous_axes": list(existing.get("homogeneous_axes") or []),
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
