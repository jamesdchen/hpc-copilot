"""``read-runtime-prior`` primitive — plugin-owned registry wrapper.

The compute function (``hpc_agent.state.runtime_prior.roll_up_quantiles``)
stays in the public ``hpc-agent`` package; the public package drops its
``@primitive`` decorator as part of the scheduling-strategy extraction.
This module re-attaches the decorator so the plugin owns the registry
entry. The wrapper signature mirrors the original verbatim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._internal.primitive import primitive
from hpc_agent.state.runtime_prior import roll_up_quantiles as _roll_up_quantiles

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["roll_up_quantiles"]


@primitive(
    name="read-runtime-prior",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli="hpc-agent runtime-prior --profile <name> --cluster <name> [--cmd-sha <sha>]",
)
def roll_up_quantiles(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    cmd_sha: str | None = None,
    quantiles: tuple[float, ...] = (0.5, 0.95, 0.99),
) -> dict[str, Any]:
    """Group runtime samples by ``gpu_type`` and compute quantile distributions.

    Thin pass-through to ``hpc_agent.state.runtime_prior.roll_up_quantiles``;
    see that function for the full behaviour contract.
    """
    return _roll_up_quantiles(
        experiment_dir,
        profile=profile,
        cluster=cluster,
        cmd_sha=cmd_sha,
        quantiles=quantiles,
    )
