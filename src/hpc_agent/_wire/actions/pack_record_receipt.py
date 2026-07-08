"""Pydantic models for the ``pack-record-receipt`` mutate verb (domain-packs T3/T5).

Wire surface over :mod:`hpc_agent.ops.pack.record_receipt_op` — the pack
analogue of the notebook render receipt (``docs/design/domain-packs.md``,
"Receipt naming + the gate contract"). A pack RECEIPT is a CODE attestation
that a domain check (run entirely outside core, DP2) reported ``passed`` for a
named SLOT against a set of checked files, at the current bind's manifest sha.

**No caller-suppliable sha — the enforcement row.** The spec carries NO sha
field of any kind. ``pack-record-receipt`` recomputes ON DISK the sha of every
``checked`` file AND the current bind's manifest sha, and builds ``content_sha``
server-side (the parse IS the recompute). A caller cannot assert a receipt for
content not on disk — this is the exact closure of the v1 receipt-laundering
hole, one layer up (the enforcement map's "receipt shas are server-computed"
row; the local field-set pin in ``tests/_wire/test_pack_wire.py`` holds it).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class PackRecordReceiptSpec(BaseModel):
    """Inputs to ``pack-record-receipt`` — a slot outcome, NEVER a sha.

    ``pack`` / ``slot`` are caller-authored slugs (the slot is the caller's name
    for one obligation, DP4; core never invents one). ``checked`` lists the
    experiment-relative paths the domain check covered — the verb recomputes
    each file's sha from disk. ``passed`` is a mechanical boolean (comparison,
    not interpretation). ``evidence`` is OPAQUE — an arbitrary caller payload
    core records verbatim and never reads for meaning.

    There is deliberately NO ``content_sha`` / ``manifest_sha`` / per-file sha
    field: every sha is server-computed from disk at record time.
    """

    model_config = ConfigDict(extra="forbid", title="pack-record-receipt input spec")

    pack: RunIdStrict = Field(
        description="The pack (filesystem-safe slug) whose current bind this receipt is recorded under."
    )
    slot: RunIdStrict = Field(
        description=(
            "Caller-authored slot slug this receipt fills — the caller's name for one "
            "obligation (DP4). Opaque to core; never invented or defaulted."
        ),
    )
    checked: list[str] = Field(
        default_factory=list,
        description=(
            "Experiment-relative paths the domain check covered. The verb recomputes "
            "each file's sha ON DISK — a receipt binds the freshly-hashed content, so "
            "it reads stale the instant any checked file drifts."
        ),
    )
    passed: bool = Field(
        description="Mechanical outcome the domain check reported. `true` is required for a gate to accept the slot.",
    )
    evidence: dict[str, Any] | str | None = Field(
        default=None,
        description=(
            "OPAQUE check evidence (arbitrary nested payload or free text). Recorded "
            "verbatim; NEVER read by core for meaning."
        ),
    )


class PackRecordReceiptResult(BaseModel):
    """Echo of the journaled receipt — every sha server-recomputed.

    ``content_sha`` is the canonical-JSON sha the verb built server-side from
    ``{manifest_sha, checked: {relpath: sha, ...}}`` — the freshness key a gate
    recomputes at read time. ``passed`` echoes the recorded outcome.
    """

    model_config = ConfigDict(extra="forbid", title="pack-record-receipt output data")

    pack: str
    version: str
    manifest_sha: str
    slot: str
    content_sha: str
    passed: bool
