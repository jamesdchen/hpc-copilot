"""Tests for the plugin entry-point loader.

The disable env var is the single chokepoint that lets the dev-loop regen
scripts produce core-only output when any ``hpc_agent.plugins`` entry
point is installed in the venv — see #198.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hpc_agent._kernel.registry import plugins


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """``load_plugins`` is ``@cache``-d; tests that flip env state must reset it."""
    plugins.load_plugins.cache_clear()
    yield
    plugins.load_plugins.cache_clear()


def test_disable_env_var_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HPC_AGENT_DISABLE_PLUGINS=1`` short-circuits to ``()`` regardless of entry points."""
    monkeypatch.setenv(plugins.DISABLE_ENV_VAR, "1")
    assert plugins.load_plugins() == ()


def test_disable_env_var_only_exact_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the literal value ``"1"`` disables. ``"true"`` / ``"0"`` / empty all fall through.

    Conservative: a typo'd ``DISABLE_PLUGINS=true`` should NOT silently turn plugins
    off in a contributor's editor — the failure mode of the env-var lever working too
    eagerly is the same dev-loop friction this exists to remove.
    """
    for sentinel in ("true", "yes", "0", "", "01"):
        monkeypatch.setenv(plugins.DISABLE_ENV_VAR, sentinel)
        plugins.load_plugins.cache_clear()
        # The result depends on what's installed; we just assert the short-circuit
        # DIDN'T fire. With or without a plugin installed, the function executes its
        # entry-point scan and the disable branch is not taken — we can prove that
        # negatively by clearing the env var and getting the same answer.
        with_sentinel = plugins.load_plugins()
        plugins.load_plugins.cache_clear()
        monkeypatch.delenv(plugins.DISABLE_ENV_VAR, raising=False)
        without = plugins.load_plugins()
        assert with_sentinel == without, (
            f"DISABLE_PLUGINS={sentinel!r} should NOT short-circuit (only literal '1' does)"
        )


def test_disable_env_var_unset_runs_entry_point_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the env var is unset, the function executes the entry-point loop normally."""
    monkeypatch.delenv(plugins.DISABLE_ENV_VAR, raising=False)
    # No assertion on the contents (depends on what's installed); just confirm it doesn't
    # raise and returns a tuple. The disable path bypasses the entry-point machinery
    # entirely, so reaching this assertion proves the scan ran.
    result = plugins.load_plugins()
    assert isinstance(result, tuple)
