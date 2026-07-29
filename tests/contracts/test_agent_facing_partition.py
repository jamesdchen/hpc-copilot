"""Invariant tests for the ``agent_facing`` partition.

Every primitive's ``agent_facing`` flag controls whether its full
body + schemas appear in ``capabilities --full`` (the LLM context
dump).

**Reachability comes first.** ``agent_facing`` claims "the agent calls
this primitive directly" (`docs/internals/adding-a-primitive.md`), so
it is bounded by whether the agent *can*: both agent surfaces invoke a
primitive by rendering its ``CliShape`` into argv — the CLI dispatcher
directly, and the MCP server through ``_cli_primitives()`` /
``_invoke_cli()``, which the tiered catalog's generic ``run-primitive``
tool recurses into rather than bypasses. A primitive with ``cli=None``
is therefore unreachable from every agent surface, and flagging one
agent-facing only spends the context budget the flag exists to protect.
That bound is pinned by :func:`test_cli_less_primitives_are_not_agent_facing`.

Within the reachable set, some verbs imply the answer by construction:

- ``workflow`` — the agent calls it directly; always agent-facing.
- ``scaffold`` — interactive setup helpers; always agent-facing.
- ``validate`` — agent decides whether to act on a probe; agent-facing.

These tests pin those invariants so a future PR that adds a
workflow without flipping ``agent_facing=True`` fails CI rather
than silently shipping a hidden workflow. Each is scoped to the
primitives that *have* a CLI: the verb implication is about what the
agent does with a probe it can run, so it cannot outrank reachability
(before this scoping the two rules contradicted each other, and the
verb implication won — parking seven uninvokable validators in the
context dump).

Other verbs (``query``, ``mutate``, ``submit``) are mixed: some
atoms are agent-facing (caller-driven), others are framework
internals composed inside workflows. Those don't get a blanket
invariant; they're inspected individually via the explicit
allowlists below.
"""

from __future__ import annotations

import pytest

from hpc_agent._kernel.registry.primitive import PrimitiveMeta, get_registry, register_primitives


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
        name
        for name, meta in registry.items()
        if meta.verb == "workflow" and meta.cli and not meta.agent_facing
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
        name
        for name, meta in registry.items()
        if meta.verb == "scaffold" and meta.cli and not meta.agent_facing
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

    Scoped to validators the agent can actually run. A ``cli=None``
    validator is a composed sub-check whose findings reach the agent
    folded into its composer's envelope (``validate-campaign``,
    ``submit-pipeline``), never as a probe of its own — see
    :func:`test_cli_less_primitives_are_not_agent_facing`.
    """
    failures = [
        name
        for name, meta in registry.items()
        if meta.verb == "validate" and meta.cli and not meta.agent_facing
    ]
    assert not failures, (
        "verb=validate primitives must declare agent_facing=True "
        f"in their @primitive(...) decorator: {failures}"
    )


def test_cli_less_primitives_are_not_agent_facing(registry: dict[str, PrimitiveMeta]) -> None:
    """A primitive with no ``CliShape`` must not claim ``agent_facing=True``.

    Both agent surfaces reach a primitive by rendering its ``CliShape``
    into argv: the CLI dispatcher directly, and the MCP server via
    ``McpServer._cli_primitives()`` → ``_invoke_cli()``. The tiered
    catalog's generic ``run-primitive`` tool is not an escape hatch — it
    recurses into the same ``call_tool`` path, which resolves through
    ``_invocable()`` (CLI-gated) and then asserts ``isinstance(shape,
    CliShape)``. So ``cli=None`` means no CLI subcommand, no MCP tool,
    and no way for an agent to invoke it.

    Flagging such a primitive agent-facing has exactly one effect:
    ``render_llms_full`` expands its body and both schemas into the
    context dump the flag exists to keep honest. That is a pure cost —
    the agent reads a contract it cannot call.

    This bound is why the verb-implication tests above are scoped to
    CLI-bearing primitives. The two rules used to contradict each other
    for ``verb=validate`` with no CLI, and the verb rule won: seven
    composed sub-validators of ``validate-campaign`` / ``submit-pipeline``
    shipped ~32 KB of schema into every full catalog render while being
    uninvokable. Composed sub-checks surface through their composer's
    envelope; that composer is the agent-facing verb.
    """
    failures = sorted(name for name, meta in registry.items() if not meta.cli and meta.agent_facing)
    assert not failures, (
        "primitives with no CLI shape cannot be agent-facing (no CLI "
        "subcommand and no MCP tool exists to invoke them); declare "
        f"agent_facing=False in their @primitive(...) decorator: {failures}"
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
