"""Regression tests: pre-existing non-directory at ``commands``/``skills``/``agents``.

``hpc-agent setup`` historically crashed with an opaque ``[WinError 183]``
when ``~/.claude/commands`` or ``~/.claude/skills`` (and later
``~/.claude/agents``) already existed as a non-directory file. The
current contract distinguishes by file size:

* **0-byte file** — silently unlinked. Empirically, that's the shape of
  stale scaffolding artifacts (Windows touch-then-crash, old hpc-agent
  versions). The cleared path is reported in ``result["cleared_collisions"]``.
* **Non-empty file** — :class:`FileExistsError` with a clear remediation,
  matching the historical guard, because the user might lose real content.

These tests pin both halves of the contract for all three target dirs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.agent_assets import install_agent_assets


def test_install_fails_clearly_when_commands_path_is_nonempty_file(tmp_path: Path) -> None:
    """A non-empty file at ``<claude>/commands`` surfaces a clear FileExistsError."""
    (tmp_path / "commands").write_bytes(b"stray content")

    with pytest.raises(FileExistsError) as excinfo:
        install_agent_assets(claude_dir=tmp_path)

    msg = str(excinfo.value)
    assert "commands" in msg
    assert "not a directory" in msg


def test_install_fails_clearly_when_skills_path_is_nonempty_file(tmp_path: Path) -> None:
    """A non-empty file at ``<claude>/skills`` surfaces a clear FileExistsError."""
    (tmp_path / "skills").write_bytes(b"stray content")

    with pytest.raises(FileExistsError) as excinfo:
        install_agent_assets(claude_dir=tmp_path)

    msg = str(excinfo.value)
    assert "skills" in msg
    assert "not a directory" in msg


def test_install_fails_clearly_when_agents_path_is_nonempty_file(tmp_path: Path) -> None:
    """A non-empty file at ``<claude>/agents`` surfaces a clear FileExistsError."""
    (tmp_path / "agents").write_bytes(b"stray content")

    with pytest.raises(FileExistsError) as excinfo:
        install_agent_assets(claude_dir=tmp_path)

    msg = str(excinfo.value)
    assert "agents" in msg
    assert "not a directory" in msg


def test_install_clears_zero_byte_collision_at_commands(tmp_path: Path) -> None:
    """A 0-byte file at ``<claude>/commands`` is silently cleared and the install proceeds."""
    stray = tmp_path / "commands"
    stray.write_bytes(b"")
    assert stray.is_file() and stray.stat().st_size == 0

    result = install_agent_assets(claude_dir=tmp_path)

    assert (tmp_path / "commands").is_dir()
    assert str(stray) in result["cleared_collisions"]
    assert len(result["commands_installed"]) > 0


def test_install_clears_zero_byte_collision_at_skills(tmp_path: Path) -> None:
    """A 0-byte file at ``<claude>/skills`` is silently cleared and the install proceeds."""
    stray = tmp_path / "skills"
    stray.write_bytes(b"")

    result = install_agent_assets(claude_dir=tmp_path)

    assert (tmp_path / "skills").is_dir()
    assert str(stray) in result["cleared_collisions"]
    assert len(result["skills_installed"]) > 0


def test_install_clears_zero_byte_collision_at_agents(tmp_path: Path) -> None:
    """A 0-byte file at ``<claude>/agents`` is silently cleared and the install proceeds.

    This is the demo-session bug: a stale 0-byte file (March 2026 timestamp)
    at ``~/.claude/agents`` was blocking hpc-agent install. With the new
    contract, the install proceeds and reports the cleared path.
    """
    stray = tmp_path / "agents"
    stray.write_bytes(b"")

    result = install_agent_assets(claude_dir=tmp_path)

    assert (tmp_path / "agents").is_dir()
    assert str(stray) in result["cleared_collisions"]
    assert "hpc-worker" in result["agents_installed"]


def test_install_dry_run_does_not_unlink_zero_byte_collision(tmp_path: Path) -> None:
    """``dry_run=True`` reports a 0-byte collision without removing it."""
    stray = tmp_path / "agents"
    stray.write_bytes(b"")

    result = install_agent_assets(claude_dir=tmp_path, dry_run=True)

    assert stray.is_file() and stray.stat().st_size == 0
    assert str(stray) in result["cleared_collisions"]
    assert result["wrote"] is False


def test_install_succeeds_when_skills_dir_already_exists(tmp_path: Path) -> None:
    """Pre-existing ``<claude>/skills`` directory is not an error."""
    (tmp_path / "skills").mkdir()

    result = install_agent_assets(claude_dir=tmp_path)

    assert result["wrote"] is True
    assert result["claude_dir"] == str(tmp_path)
    assert len(result["skills_installed"]) > 0
    assert (tmp_path / "skills").is_dir()
    assert result["cleared_collisions"] == []


def test_install_ships_the_haiku_pinned_worker_subagent(tmp_path: Path) -> None:
    """The haiku-pinned ``hpc-worker`` subagent definition installs into
    ``<claude>/agents/`` with its model pin intact — that pin riding with the
    definition is what makes inline mode's model choice harness-enforced."""
    result = install_agent_assets(claude_dir=tmp_path)

    assert "hpc-worker" in result["agents_installed"]
    worker = tmp_path / "agents" / "hpc-worker.md"
    assert worker.is_file()
    body = worker.read_text(encoding="utf-8")
    # The pin must survive the copy verbatim — the harness reads it from here.
    assert "model: haiku" in body
    assert "name: hpc-worker" in body
