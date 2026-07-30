"""Recover-suite isolation: the harness CONFIG dir, like the journal home.

``doctor`` gained a local, read-only probe over the installed harness config
(:func:`hpc_agent.ops.recover.doctor._consent_forward_hook_probe` — is the
consent-forwarding PreToolUse hook present and current?). Its input resolves
through :func:`hpc_agent.agent_assets.resolve_claude_dir`, i.e. the DEVELOPER's
real ``~/.claude`` unless ``CLAUDE_CONFIG_DIR`` says otherwise.

Without this fixture the doctor tests would read that real config and their
verdicts would depend on whether the developer had re-run ``install-commands``
lately — green in clean CI, red on a workstation mid-upgrade. That is exactly
the environment-dependent flake class the repo's ``_isolated_journal_home``
guard exists to prevent (and which a night of "pre-existing failure"
adjudications has already been spent on once). Every test in this package gets
an empty, hermetic config dir; a test that wants a populated one sets its own
``CLAUDE_CONFIG_DIR`` via ``monkeypatch`` (which lands after this fixture and
therefore wins).

The same hermetic-``CLAUDE_CONFIG_DIR`` idiom is used by
``tests/ops/test_harness_capabilities.py`` and ``tests/cli/test_install_config_dir.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_claude_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "_claude_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
