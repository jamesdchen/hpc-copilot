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
