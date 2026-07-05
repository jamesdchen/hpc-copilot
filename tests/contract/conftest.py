"""Contract-test scope helpers.

Contract tests live behind the ``contract`` pytest marker so they can be
selected (``pytest -m contract``) or excluded (``pytest -m 'not
contract'``) independently of the rest of the suite. They take the
public CLI as their boundary — never reach into module internals — so
they catch the exact regressions an upstream caller (slash command,
worker prompt, MARs experiment runner) would hit at runtime.

The ``contract`` and ``lint`` markers are registered here (not in
``pyproject.toml``) so this work stays scoped to ``tests/contract/`` —
landing the WS4 enforcement infra without bumping the release tooling.
Once the inventory pass settles and we want to ship the markers as a
permanent gate, move the registrations into the top-level
``[tool.pytest.ini_options].markers`` list and delete this hook.
"""

from __future__ import annotations

import pytest

from tests._subprocess import run_cli

_CLI_TIMEOUT_SEC = 30

# The three verbs whose parametrized contract probes stay on REAL subprocess
# invocation (``python -m hpc_agent ...``); everything else runs through the
# shipped ``_in_process_cli_runner`` (the MCP warm runner), which drives the
# SAME ``cli.dispatch.main(argv)`` path — parser → model_validate → primitive
# → envelope — so the (exit_code, error_code, category, retry_safe) contract
# is identical, without paying ~1.2s of Python cold-start per probe.
#
# Why these three (one per envelope regime, so the real process-entry path
# stays covered end-to-end):
#
# * ``status-snapshot`` — read-only block verb; probes the all-optional-model
#   bogus-key rejection path (EMPTY_SPEC_OVERRIDES).
# * ``submit-s1``       — mutating submit-family block verb; probes the
#   required-field ``{}`` → spec_invalid path.
# * ``aggregate-flow``  — error path: its ``{}`` probe currently surfaces an
#   ``internal`` envelope (uncaught exception mapped by the real interpreter
#   exit path) — exactly the regime where subprocess/in-process parity is
#   most at risk, so it keeps the real process boundary.
SUBPROCESS_SAMPLE_VERBS: frozenset[str] = frozenset(
    {"status-snapshot", "submit-s1", "aggregate-flow"}
)


def invoke_cli(argv: list[str], *, timeout: float = _CLI_TIMEOUT_SEC) -> tuple[int, str, str]:
    """Run ``hpc-agent <argv...>``; return ``(exit_code, stdout, stderr)``.

    Verbs in :data:`SUBPROCESS_SAMPLE_VERBS` go through a real
    ``python -m hpc_agent`` subprocess (console entry-path coverage);
    every other invocation is dispatched in-process via the shipped
    ``_in_process_cli_runner`` for speed. Both return the same
    ``(exit_code, stdout-envelope, stderr)`` contract.
    """
    verb = argv[0] if argv else ""
    if verb in SUBPROCESS_SAMPLE_VERBS:
        # run_cli also forwards the per-test journal home (HPC_JOURNAL_DIR)
        # into the child so a subprocess probe never writes ~/.claude/hpc.
        proc = run_cli(*argv, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    from hpc_agent._kernel.extension.mcp_server import _in_process_cli_runner

    return _in_process_cli_runner(list(argv))


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``contract`` and ``lint`` markers.

    Without this, the top-level ``--strict-markers`` setting in
    pyproject.toml would reject the markers and every test under this
    directory would error at collection time.
    """
    config.addinivalue_line(
        "markers",
        "contract: WS4 contract tests — primitive-remediation envelope "
        "shape + schema-roundtrip remediation guidance.",
    )
    config.addinivalue_line(
        "markers",
        "lint: WS4 prose/structure lints (e.g. SKILL.md gold-standard pattern).",
    )
