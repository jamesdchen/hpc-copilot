"""Pydantic model for the ``load-context`` query primitive's output.

Mirrors the dict returned by :func:`hpc_agent.meta.campaign.atoms.load_context.load_context`.
The nested objects (``latest_run``, ``in_flight`` rows, ``campaigns`` rows)
carry ``extra="allow"`` because their key set is driven by sidecar /
journal schema versions that evolve independently — the model pins the
*stable* identity fields a consumer relies on and tolerates the rest,
so a sidecar-schema bump does not break this contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._schema_models._shared import RunIdLoose
from hpc_agent._schema_models.spawn_contract import SpawnRequest


class _LatestRun(BaseModel):
    """Newest run sidecar projected to identity + v2 config snapshot."""

    model_config = ConfigDict(extra="allow")

    run_id: RunIdLoose
    is_orphan: bool


class _InFlightRow(BaseModel):
    """One journal record still in flight."""

    model_config = ConfigDict(extra="allow")

    run_id: RunIdLoose
    campaign_id: str | None = None
    cluster: str | None = None
    stage: str | None = None
    status: str | None = None


class _CampaignRow(BaseModel):
    """One campaign with a sidecar, plus its cursor iteration when present."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str
    iterations_submitted: int = Field(ge=0)


class _Delegate(BaseModel):
    """The next workflow step described as a delegable unit of work.

    For an ``agent``-kind step ``spawn_request`` carries the shared
    :class:`SpawnRequest` contract — the campaign driver feeds it to
    ``run_workflow``, which renders the canonical worker prompt and
    invokes a fresh-context worker. ``None`` for ``cli``-kind steps.
    The ``prompt`` field carries that canonical prompt pre-rendered.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["cli", "agent"]
    step: str
    run_id: str | None
    campaign_id: str | None = None
    experiment_dir: str
    reason: str
    prompt: str
    spawn_request: SpawnRequest | None = None


class LoadContextResult(BaseModel):
    """On-disk workflow context reconstructed for a fresh-context step.

    A subagent, a restarted session, or a cron tick has no
    conversational memory; this is the single source of truth it reads
    instead — run sidecars, the journal, and campaign cursors projected
    into one envelope.
    """

    model_config = ConfigDict(extra="forbid", title="load-context output data")

    experiment_dir: str
    latest_run: _LatestRun | None
    in_flight: list[_InFlightRow]
    campaigns: list[_CampaignRow]
    next_step_hint: Literal["submit", "monitor", "aggregate", "decide"]
    delegate: _Delegate
    warnings: list[str]
