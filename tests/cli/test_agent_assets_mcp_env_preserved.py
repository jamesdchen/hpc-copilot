"""The MCP registration heal preserves a user-set ``env`` (run #8 fallout).

``_register_mcp_server`` heals OUR keys (interpreter path after a moved venv,
args shape) by rewriting the ``hpc-agent`` entry — but the entry can also
carry the USER'S config: an ``env`` dict such as ``HPC_SSH_ENGINE=asyncssh``
opting that server into the connection engine. Rewriting wholesale silently
un-set the flag on every ``install-commands`` run (which fires from setup,
the release skill, and doc'd reinstall flows), so the opt-in never survived
to the next session. The heal must carry a non-empty existing ``env``
forward verbatim, and an entry differing ONLY by that preserved env is
``already-present`` (idempotence), not an endless ``updated`` loop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from hpc_agent.agent_assets import _register_mcp_server


def _seed(claude_dir: Path, entry: dict) -> Path:
    config_path = claude_dir.parent / ".claude.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"hpc-agent": entry}}, indent=2), encoding="utf-8"
    )
    return config_path


def _stale_entry_with_env() -> dict:
    return {
        "type": "stdio",
        "command": "C:/moved/venv/python.exe",  # stale — must be healed
        "args": ["-m", "hpc_agent", "mcp-serve", "--allow-mutations", "--catalog", "curated"],
        "env": {"HPC_SSH_ENGINE": "asyncssh"},  # user's — must survive
    }


def test_heal_preserves_user_env(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    config_path = _seed(claude_dir, _stale_entry_with_env())

    result = _register_mcp_server(claude_dir, dry_run=False)
    assert result["action"] == "updated"

    healed = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["hpc-agent"]
    assert healed["command"] == sys.executable, "stale interpreter path must be healed"
    assert healed["env"] == {"HPC_SSH_ENGINE": "asyncssh"}, (
        "a user-set env must survive the heal — clobbering it un-sets the "
        "engine opt-in on every install-commands run"
    )


def test_entry_differing_only_by_preserved_env_is_already_present(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    _seed(claude_dir, _stale_entry_with_env())
    assert _register_mcp_server(claude_dir, dry_run=False)["action"] == "updated"
    # Second run: the healed entry (our keys + their env) must be stable.
    assert _register_mcp_server(claude_dir, dry_run=False)["action"] == "already-present"


def test_empty_env_is_not_resurrected(tmp_path: Path) -> None:
    """An empty ``env: {}`` carries no user config — the healed entry drops it
    (matching the template) rather than pinning an empty dict forever."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    entry = _stale_entry_with_env()
    entry["env"] = {}
    config_path = _seed(claude_dir, entry)
    assert _register_mcp_server(claude_dir, dry_run=False)["action"] == "updated"
    healed = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["hpc-agent"]
    assert "env" not in healed
