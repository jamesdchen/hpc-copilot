"""U-ENV: the harness CONFIG-dir resolver honors ``CLAUDE_CONFIG_DIR`` on BOTH
the install WRITE path and the capability READ probe — closing the read/write
asymmetry (a relocated config used to get capabilities WRITTEN to ``~/.claude``
where the env-honoring probe never LOOKED — a latent correctness bug, not just a
lockout).

Fire-tests for the premortem deltas that bind U-ENV:

* the asymmetry itself — install-then-probe AGREE under a relocated config
  (RED before the fix: the write path ignored the env);
* **D4** — nothing is written outside the resolved tree bar the one intentional
  ``.claude.json`` parent-sibling, and NO write reaches the default ``~/.claude``
  (the missed-``DEFAULT_CLAUDE_DIR``-call-site guard);
* idempotent re-install under the relocated dir.

**D1** (the journal home IGNORES ``CLAUDE_CONFIG_DIR``) is pinned separately in
``tests/state/test_homedir_leaf.py`` — folding the journal axis into this
resolver would relocate every existing user's run history on upgrade.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent._wire.queries.harness_capabilities import HarnessCapabilitiesSpec
from hpc_agent.agent_assets import install_agent_assets, resolve_claude_dir
from hpc_agent.ops.harness_capabilities import harness_capabilities


@pytest.fixture
def relocated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A hermetic relocated ``CLAUDE_CONFIG_DIR`` plus a sandboxed home.

    ``CLAUDE_CONFIG_DIR`` points at ``<tmp>/cfg/claude``; ``Path.home`` is
    redirected to ``<tmp>/home`` so the LITERAL default (``DEFAULT_CLAUDE_DIR``
    → ``~/.claude``) resolves INSIDE the sandbox — any missed call site that
    still writes to the default is then caught landing at ``<tmp>/home/.claude``
    instead of touching the real home. ``HPC_JOURNAL_DIR`` is cleared so the
    journal axis stays on its own default (the D1 fence).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    d = tmp_path / "cfg" / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    monkeypatch.delenv("HPC_JOURNAL_DIR", raising=False)
    return d


def test_resolver_honors_claude_config_dir(relocated: Path) -> None:
    """The single shared resolver reads the relocation knob."""
    assert resolve_claude_dir() == relocated


def test_install_writes_and_probe_reads_the_same_relocated_dir(
    relocated: Path, tmp_path: Path
) -> None:
    """The asymmetry, closed: install honors ``CLAUDE_CONFIG_DIR`` (write side)
    and the capability probe reads the hooks back from the SAME dir (read side).

    RED before the fix: the write path used ``DEFAULT_CLAUDE_DIR`` (ignoring the
    env), so settings.json landed at the sandboxed ``~/.claude`` while the probe
    read the relocated dir and reported the capabilities ABSENT.
    """
    result = install_agent_assets()  # claude_dir=None -> resolve_claude_dir()

    assert Path(result["claude_dir"]) == relocated
    assert (relocated / "settings.json").exists()

    caps = harness_capabilities(
        experiment_dir=tmp_path, spec=HarnessCapabilitiesSpec()
    ).capabilities
    # The probe reads the relocated settings.json the installer just wrote.
    assert caps["utterance_log"].present is True
    assert caps["relay_enforcement"].present is True


def test_explicit_claude_dir_kwarg_still_overrides_the_env(relocated: Path, tmp_path: Path) -> None:
    """Hermeticity guard: an explicit ``--claude-dir`` still wins over the env, so
    a test (or a deliberate operator override) never bleeds into
    ``$CLAUDE_CONFIG_DIR``.
    """
    explicit = tmp_path / "explicit"
    result = install_agent_assets(claude_dir=explicit)

    assert Path(result["claude_dir"]) == explicit
    assert (explicit / "settings.json").exists()
    assert not (relocated / "settings.json").exists()


def test_nothing_written_outside_the_resolved_tree(relocated: Path, tmp_path: Path) -> None:
    """D4: with ``CLAUDE_CONFIG_DIR`` set and ``--claude-dir`` unset, the ONLY
    write outside the resolved tree is the ``.claude.json`` parent-sibling, and
    NO write reaches the default ``~/.claude`` (the missed-call-site guard).
    """
    install_agent_assets()

    # The one intentional out-of-tree write: the MCP registration sibling.
    sibling = relocated.parent / ".claude.json"
    assert sibling.exists()
    # The default location (DEFAULT_CLAUDE_DIR -> sandboxed ~/.claude) was never
    # touched — no call site still writes there.
    assert not (tmp_path / "home" / ".claude").exists()

    # Every file under the sandbox is inside the resolved tree OR is exactly the
    # ``.claude.json`` sibling — nothing else escapes.
    for p in tmp_path.rglob("*"):
        if p.is_dir():
            continue
        assert p == sibling or relocated in p.parents, f"unexpected out-of-tree write: {p}"


def test_reinstall_is_idempotent_under_relocation(relocated: Path) -> None:
    """A second install under the same relocated dir adds no duplicate hook/grant."""
    first = install_agent_assets()
    assert first["settings_hook"]["action"] == "added"

    second = install_agent_assets()
    # Re-running heals in place: the entries are already present (a same-process
    # re-run keeps the identical command), nothing is re-appended.
    assert second["settings_hook"]["action"] in ("already-present", "updated")
    assert second["settings_stop_multiplex_hook"]["action"] in ("already-present", "updated")
    assert second["settings_permissions"]["action"] in ("already-present", "updated")
