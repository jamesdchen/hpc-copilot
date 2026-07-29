"""``tag-session`` — mark this session as data for the dev loop to ingest.

The one devx affordance the wheel keeps after the 2026-07-28
dev-loop/product separation (user-ordered: "the wheel build should not have
dev loop stuff except for tagging sessions for devx to ingest as data").
Everything else dev-loop — the release procedure, workflow plans, handoff
packages, lints — lives repo-side; the product's sole contribution to its
own improvement is this: a session that notices friction, a good specimen,
or a tool gap appends one opaque record to the per-experiment
``.hpc/devx/session_tags.jsonl`` ledger, and the maintainer's repo-side
tooling ingests those ledgers later.

Deliberately inert inside the product: nothing reads a tag back to change
behavior — no gate, no journal, no decision path. A tag is data ABOUT the
session, never evidence (contrast ``append-decision``, where a record IS the
gate's input). That inertness is what makes the verb safe to ship in every
wheel and to delegate freely: it cannot launder anything.

This file lives at ``ops/`` top level (like ``harness_capabilities`` /
``supersession``), reaching only the ``state.*`` substrate.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.tag_session import TagSessionResult, TagSessionSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state import devx_tags

__all__ = ["tag_session"]

_PRIMITIVE = "tag-session"


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/devx/session_tags.jsonl"),
    ],
    error_codes=[],
    # Not idempotent: append-only ledger — an immediate retry appends a second
    # record. Honest duplication in an ingestion ledger beats a dedup scheme
    # the consumer would have to trust blind.
    idempotent=False,
    cli=CliShape(
        help=(
            "Append one opaque devx tag record ({tags, note?, session_id?, "
            "run_id?}, ts stamped at append) to the per-experiment "
            ".hpc/devx/session_tags.jsonl ledger - the one dev-loop seam the "
            "product ships (2026-07-28 separation). Marks this session as "
            "data for the maintainer's dev loop to ingest: friction noticed, "
            "a good specimen, a tool gap. Tags and note are OPAQUE to core "
            "and inert inside the product - nothing reads them back to change "
            "behavior, no gate or journal consumes them. Pure local flock-"
            "append, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=TagSessionSpec,
        schema_ref=SchemaRef(input="tag_session"),
    ),
    agent_facing=True,
)
def tag_session(*, experiment_dir: Path, spec: TagSessionSpec) -> TagSessionResult:
    """Append the tag record and echo it with the ledger path and count."""
    experiment_dir = Path(experiment_dir)
    record = devx_tags.append_session_tag(
        experiment_dir,
        tags=spec.tags,
        note=spec.note,
        session_id=spec.session_id,
        run_id=spec.run_id,
    )
    path = devx_tags.session_tags_path(experiment_dir)
    return TagSessionResult(
        path=str(path),
        record=record,
        count=len(devx_tags.read_session_tags(experiment_dir)),
    )
