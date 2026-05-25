"""Byte-exact snapshot tests for the spawn pipeline's cacheable prefix.

These tests are only meaningful because :func:`render_spawn_parts`
produces a deterministic prefix from the static worker-prompt
templates — the consumer (a ``claude -p --bare`` subprocess) has a
stochastic model boundary, but the prompt construction does not.

The fixture for each workflow lives at
``tests/worker_prompts/fixtures/<workflow>.prefix.txt`` and is the
authoritative record of the bytes shipped to the worker. Any prose
change to ``src/hpc_agent/worker_prompts/<workflow>.md`` (or to the
scaffold prefix in ``hpc_agent._kernel.extension.spawn_prompt.render_spawn_parts``)
deliberately changes the fixture. To accept the change, regenerate:

    WORKER_PROMPT_SNAPSHOT_UPDATE=1 uv run pytest tests/worker_prompts/test_prefix_snapshot.py

The byte diff in the regenerated fixture is what the reviewer sees in
the PR — a hand-readable record of what the worker now reads.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hpc_agent._kernel.extension.spawn_prompt import render_spawn_parts
from hpc_agent._wire.spawn_contract import WorkflowName

from tests._registry_helpers import is_pro_installed

FIXTURE_DIR = Path(__file__).parent / "fixtures"
WORKFLOWS: tuple[WorkflowName, ...] = ("submit", "status", "aggregate", "campaign")

# Workflows whose worker-prompt body is overlaid by ``hpc-agent-pro``'s
# ``worker_prompt_assets`` plugin attribute. The fixtures in this directory
# are core-only; when the plugin is installed in the same env, the rendered
# prefix is the plugin's overlay and the snapshot mismatch is expected.
# Keep this list in sync with the ``worker_prompts/`` directory under
# ``hpc-agent-pro/src/hpc_agent_pro/``.
_PRO_OVERRIDDEN_WORKFLOWS: frozenset[WorkflowName] = frozenset({"submit"})


def _fixture_path(workflow: str) -> Path:
    return FIXTURE_DIR / f"{workflow}.prefix.txt"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_cacheable_prefix_matches_fixture(workflow: WorkflowName) -> None:
    """The rendered prefix bytes equal the committed fixture, verbatim."""
    if is_pro_installed() and workflow in _PRO_OVERRIDDEN_WORKFLOWS:
        pytest.skip(
            f"hpc-agent-pro overlays the {workflow!r} worker prompt; "
            "the core fixture doesn't apply when pro is installed in the same env"
        )
    actual = render_spawn_parts(
        workflow=workflow, experiment_dir="/exp", fields={}
    ).cacheable_prefix
    fixture = _fixture_path(workflow)

    if os.environ.get("WORKER_PROMPT_SNAPSHOT_UPDATE"):
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        fixture.write_text(actual, encoding="utf-8")
        return

    if not fixture.is_file():
        raise AssertionError(
            f"missing fixture {fixture}; regenerate with "
            "WORKER_PROMPT_SNAPSHOT_UPDATE=1 uv run pytest "
            "tests/worker_prompts/test_prefix_snapshot.py"
        )

    expected = fixture.read_text(encoding="utf-8")
    if actual != expected:
        first_div = next(
            (i for i, (a, b) in enumerate(zip(actual, expected, strict=False)) if a != b),
            min(len(actual), len(expected)),
        )
        raise AssertionError(
            f"prefix for {workflow!r} drifted from fixture {fixture}. "
            "If the change is deliberate, regenerate with "
            "WORKER_PROMPT_SNAPSHOT_UPDATE=1 uv run pytest "
            "tests/worker_prompts/test_prefix_snapshot.py "
            "and review the byte diff in the PR.\n\n"
            f"first diverging char: index {first_div}"
        )


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_cacheable_prefix_fits_token_budget(workflow: WorkflowName) -> None:
    """The prefix fits a generous token budget — guards against bloat
    that would blow the prompt cache budget on the spawned worker.

    Conservative chars-per-token estimate (~4). 80_000 chars ≈ 20K
    tokens — a sanity ceiling, not a tight constraint. The whole point
    of inlining the procedure rather than referencing it is that the
    bytes are cached; if we exceed this, the procedure has grown to
    where it belongs in primitive docs the worker fetches lazily via
    ``hpc-agent describe``.
    """
    prefix = render_spawn_parts(
        workflow=workflow, experiment_dir="/exp", fields={}
    ).cacheable_prefix
    char_budget = 80_000
    assert len(prefix) <= char_budget, (
        f"{workflow!r} prefix is {len(prefix)} chars (~{len(prefix) // 4} tokens), "
        f"exceeding the {char_budget}-char ceiling. Trim the procedure or "
        "move details into a primitive doc the worker can fetch via "
        "`hpc-agent describe`."
    )
