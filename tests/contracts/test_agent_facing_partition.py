"""Invariant tests for the ``agent_facing`` partition.

Every primitive's ``agent_facing`` flag controls whether its full
body + schemas appear in ``capabilities --full`` (the LLM context
dump). Some verbs imply the answer by construction:

- ``workflow`` — the agent calls it directly; always agent-facing.
- ``scaffold`` — interactive setup helpers; always agent-facing.
- ``validate`` — agent decides whether to act on a probe; agent-facing.

These tests pin those invariants so a future PR that adds a
workflow without flipping ``agent_facing=True`` fails CI rather
than silently shipping a hidden workflow.

Other verbs (``query``, ``mutate``, ``submit``) are mixed: some
atoms are agent-facing (caller-driven), others are framework
internals composed inside workflows. Those don't get a blanket
invariant; they're inspected individually via the explicit
allowlists below.
"""

from __future__ import annotations

import pytest

from claude_hpc._internal.primitive import PrimitiveMeta, get_registry, register_primitives


@pytest.fixture(scope="module")
def registry() -> dict[str, PrimitiveMeta]:
    register_primitives()
    return get_registry()


def test_workflows_are_agent_facing(registry: dict[str, PrimitiveMeta]) -> None:
    """Every ``verb=workflow`` primitive must be agent-facing.

    Workflows compose atoms and emit envelopes the agent reads to
    drive its next move. A workflow flagged ``agent_facing=False``
    would be silently invisible in ``capabilities --full`` — a
    landmine for the next person who calls it expecting full schema
    + body context.
    """
    failures = [
        name for name, meta in registry.items() if meta.verb == "workflow" and not meta.agent_facing
    ]
    assert not failures, (
        "verb=workflow primitives must declare agent_facing=True "
        f"in their @primitive(...) decorator: {failures}"
    )


def test_scaffolds_are_agent_facing(registry: dict[str, PrimitiveMeta]) -> None:
    """Every ``verb=scaffold`` primitive must be agent-facing.

    Scaffolds (``axes-init``, ``build-executor``, ``interview``,
    etc.) are the ``/submit-hpc`` interview surface — the agent
    walks the user through them. Hiding one is a UX bug.
    """
    failures = [
        name for name, meta in registry.items() if meta.verb == "scaffold" and not meta.agent_facing
    ]
    assert not failures, (
        "verb=scaffold primitives must declare agent_facing=True "
        f"in their @primitive(...) decorator: {failures}"
    )


def test_validators_are_agent_facing(registry: dict[str, PrimitiveMeta]) -> None:
    """Every ``verb=validate`` primitive must be agent-facing.

    Validators (``check-preflight``, ``validate``) emit
    diagnostics the agent surfaces to the user before a stateful
    operation. The agent must read the schema to know how to
    interpret the result.
    """
    failures = [
        name for name, meta in registry.items() if meta.verb == "validate" and not meta.agent_facing
    ]
    assert not failures, (
        "verb=validate primitives must declare agent_facing=True "
        f"in their @primitive(...) decorator: {failures}"
    )


def test_partition_is_complete(registry: dict[str, PrimitiveMeta]) -> None:
    """Every primitive declares ``agent_facing`` (no implicit defaults sneaking in).

    Belt-and-suspenders: the dataclass has ``agent_facing: bool =
    False`` as a default, so a missing kwarg silently lands as
    False. This test confirms the registry is non-empty and every
    entry has the attribute.
    """
    assert registry, "primitive registry is empty — register_primitives() did not run"
    for name, meta in registry.items():
        assert hasattr(meta, "agent_facing"), f"{name} meta is missing agent_facing field"
        assert isinstance(meta.agent_facing, bool), (
            f"{name}: agent_facing must be bool, got {type(meta.agent_facing).__name__}"
        )
