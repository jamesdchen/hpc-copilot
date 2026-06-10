"""Wire model for the ``validate-parents-ready`` atom.

The readiness piece of the DAG kernel
(``docs/design/dag-kernel.md``): a run that declares ``parents`` on
its submit spec consumes those runs' outputs, so submitting before
every parent reached terminal-success reads partial or absent results
— silently, since the child's tasks materialize from whatever the
parent's result dirs hold at import time.

The check is the ∀-parents quantifier over machinery that already
exists per-run: each parent must have a local sidecar AND a journal
record whose status is ``complete``. Anything else — sidecar missing,
journal record absent (in flight elsewhere, or journal wiped),
``in_flight``, ``failed``, ``abandoned`` — yields an ``error``
finding naming the parent and the fix.

Pure local validator — reads ``.hpc/runs/`` sidecars and the journal
at ``~/.claude/hpc/<repo_hash>/``. No SSH, no qsub. Knows nothing
about what flows across the edge: readiness is a lifecycle predicate,
never a content check (content is the experiment's, per the kernel's
boundary).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict  # noqa: TC001
from hpc_agent._wire.workflows.validate_campaign import (
    ValidatorFinding,  # noqa: TC001 — Pydantic resolves the annotation at runtime
)


class ValidateParentsReadySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_run_ids: list[RunIdStrict] = Field(
        min_length=1,
        description=(
            "The run_ids the about-to-submit spec declares as `parents`. "
            "Order is irrelevant (parents are a set); duplicates are "
            "checked once."
        ),
    )


class ValidateParentsReadyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ValidatorFinding] = Field(default_factory=list)
    parent_states: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-parent observed state, keyed by run_id: a JournalStatus "
            "value (`complete` / `in_flight` / `failed` / `abandoned`), "
            "`missing` (no sidecar), or `unknown` (sidecar present, no "
            "journal record — possibly in flight on another machine, or "
            "a wiped journal). Every requested parent appears exactly "
            "once. Ready iff every value is `complete` — equivalently, "
            "iff `findings` is empty."
        ),
    )
