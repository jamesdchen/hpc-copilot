"""Pydantic models for the ``harness-capabilities`` query verb.

The wire surface of LSP-style capability NEGOTIATION for the harness contract
(``docs/internals/harness-contract.md``, "Capability negotiation"). The verb is a
pure READ that DETECTS the capability set a conforming harness provides — the
declaration IS what code can verify (installed hooks in ``~/.claude/settings.json``,
the utterance log's presence for this repo, the MCP elicitation flag), never a
self-asserted manifest. A capability the code cannot observe reads ``"unknown"``,
not ``true``.

Boundary posture: every field is a mechanical observation — a hook's module-path
needle is present or absent, a file exists or does not, a compile-time flag is set
or not. Nothing here is a judgement about whether the harness is "good"; the tier
CONSEQUENCE of each absence is quoted verbatim from the contract's friction-tier
language, not computed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HarnessCapabilitiesSpec(BaseModel):
    """Inputs to ``harness-capabilities`` — none required.

    The verb detects the capability set for the ``--experiment-dir`` repo against
    the harness config it can read (``~/.claude/settings.json``, honoring
    ``CLAUDE_CONFIG_DIR``). The spec is intentionally EMPTY: ``{}`` is the whole
    valid input. ``extra="forbid"`` still rejects a bogus key, so the verb has the
    same spec-invalid contract as every other ``--spec`` primitive.
    """

    model_config = ConfigDict(extra="forbid", title="harness-capabilities input spec")


class CapabilityEntry(BaseModel):
    """The detected state of ONE harness capability.

    ``present`` is a tri-state: ``true`` / ``false`` when code can observe the
    capability, ``"unknown"`` when there is no detection seam yet (the honest
    non-answer — never asserted ``true``). ``channel`` names the seam the
    detection reads; ``evidence`` carries the raw observations the verdict rolled
    up from, so a caller can audit the ``present`` bit rather than trust it.
    """

    model_config = ConfigDict(extra="forbid", title="harness capability report")

    present: bool | str = Field(
        description=(
            "true / false when code can observe the capability; the string "
            '"unknown" when there is no detection seam (never asserted true).'
        ),
    )
    channel: str = Field(
        description="The seam the detection reads (the hook, the log, the core machinery).",
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="The raw observations the `present` verdict rolled up from.",
    )


class HarnessCapabilitiesResult(BaseModel):
    """The detected capability set plus the tier consequence of each absence.

    ``capabilities`` maps the contract capability names to their detected
    :class:`CapabilityEntry`. ``tier_consequences`` maps the same names to the
    exact degrade each absence implies — the contract's named friction tiers,
    quoted, not invented. This is detection-as-negotiation: what the code can
    verify IS the declaration.
    """

    model_config = ConfigDict(extra="forbid", title="harness-capabilities output data")

    capabilities: dict[str, CapabilityEntry] = Field(
        default_factory=dict,
        description="Capability name -> its detected report.",
    )
    tier_consequences: dict[str, str] = Field(
        default_factory=dict,
        description="Capability name -> the named tier its absence degrades to.",
    )
    harness_contract_version: str = Field(
        default="",
        description=(
            "The SemVer of the harness contract this verb reports against — the "
            "single ``HARNESS_CONTRACT_VERSION`` constant beside the verb "
            "(``ops/harness_capabilities.py``), pinned equal to the "
            "``docs/internals/harness-contract.md`` version line and the "
            "conformance kit's stamped version (conformance-kit D-K6/K10). Within "
            "major 1 the contract is additive-only."
        ),
    )
