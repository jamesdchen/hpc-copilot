"""``classify-axis`` primitive — record a classified ``DataAxis``.

The agent classifies a ``@register_run`` function's series axis (by
reading ``run()`` and conducting the classification interview — see the
``hpc-classify-axis`` skill); this primitive only *records* the resolved
:data:`~hpc_agent.experiment_kit.axis.DataAxis` into
``<experiment>/.hpc/axes.yaml``'s ``executors.<run_name>`` block. Same
agent-reasons / primitive-records split as ``axes-init``.

``DataAxis`` (this primitive) and the ``axes.yaml`` *scheduling* axes
(``homogeneous_axes`` / ``pick_array_axis``) are unrelated concepts:
``DataAxis`` is *how to split the totally-ordered series correctly*; the
scheduling axes are *which sweep dimension to promote onto the task
array*. This primitive touches only the former — the ``executors`` block
— and round-trips ``axes`` / ``homogeneous_axes`` untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.classify_axis import ClassifyAxisInput
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["classify_axis"]


@primitive(
    name="classify-axis",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/axes.yaml"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Record a @register_run function's classified DataAxis "
            "(independent / associative / bounded_halo / sequential) into "
            "<experiment>/.hpc/axes.yaml's `executors` block. Spec file is "
            "{run_name, run_signature_sha, data_axis: {kind, halo?, "
            "monoid?}, classified_by?}. The agent classifies; this "
            "primitive only records."
        ),
        spec_arg=True,
        schema_ref=SchemaRef(input="classify_axis"),
        spec_model=ClassifyAxisInput,
        experiment_dir_arg=True,
    ),
    agent_facing=True,
)
def classify_axis(
    experiment_dir: Path,
    *,
    spec: ClassifyAxisInput,
) -> dict[str, Any]:
    """Record *spec*'s classified ``DataAxis`` into ``.hpc/axes.yaml``.

    The wire-validated ``spec`` carries ``run_name``,
    ``run_signature_sha``, the classified ``data_axis`` block, and
    ``classified_by``; ``experiment_dir`` is the framework-context
    kwarg. The resolved ``data_axis`` is sanity-checked by constructing
    the live :data:`DataAxis` (which compiles and structurally validates
    a ``bounded_halo`` halo expression) *before* anything is written.

    The entry is merged into ``executors.<run_name>`` via
    :func:`hpc_agent.state.axes.upsert_executor`, so any existing
    ``axes`` / ``homogeneous_axes`` hints and other executors' entries
    survive. Re-running with the same spec overwrites the entry
    byte-equivalently modulo the ``classified_at`` timestamp.

    Returns ``{axes_path, run_name, data_axis, classified_by,
    classified_at, wrote}``.

    Raises ``errors.SpecInvalid`` (mapped to ``spec_invalid``) when the
    ``data_axis`` block is internally inconsistent — most often a
    ``bounded_halo`` whose ``halo.expr`` is not safe arithmetic over the
    run's parameters.
    """
    from hpc_agent.experiment_kit.axis_config import HaloExprError, data_axis_from_config
    from hpc_agent.state.axes import axes_path, upsert_executor

    data_axis = spec.data_axis.model_dump(exclude_none=True, mode="json")
    # Normalise: an 'associative' axis with no monoid stated defaults to
    # 'moments' — store it explicitly so the recorded block is unambiguous.
    if data_axis.get("kind") == "associative" and "monoid" not in data_axis:
        data_axis["monoid"] = "moments"

    # Fail fast: build the live DataAxis so a malformed halo expression
    # (or any other inconsistency) is rejected before the disk write.
    try:
        data_axis_from_config(data_axis)
    except (HaloExprError, ValueError, TypeError) as exc:
        raise errors.SpecInvalid(f"data_axis is not a valid classification: {exc}") from exc

    classified_at = utcnow().isoformat()
    entry = {
        "run_signature_sha": spec.run_signature_sha,
        "data_axis": data_axis,
        "classified_by": spec.classified_by,
        "classified_at": classified_at,
    }

    try:
        written = upsert_executor(experiment_dir, spec.run_name, executor_entry=entry)
    except ValueError as exc:
        # write_axes' cross-validation / a schema violation surfaces here.
        raise errors.SpecInvalid(str(exc)) from exc

    return {
        "axes_path": str(written if written is not None else axes_path(experiment_dir)),
        "run_name": spec.run_name,
        "data_axis": data_axis,
        "classified_by": spec.classified_by,
        "classified_at": classified_at,
        "wrote": True,
    }
