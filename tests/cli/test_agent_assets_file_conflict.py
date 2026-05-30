"""Regression tests: clear error when ``commands``/``skills`` is a file.

``hpc-agent setup`` historically crashed with an opaque ``[WinError 183]``
when ``~/.claude/commands`` or ``~/.claude/skills`` already existed as a
non-directory file (e.g. a stray 0-byte file on Windows). These tests pin
that the install now raises a clear ``FileExistsError`` with an
actionable message *before* attempting any ``mkdir``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.agent_assets import install_agent_assets


def test_install_fails_clearly_when_commands_path_is_file(tmp_path: Path) -> None:
    """A file at ``<claude>/commands`` surfaces a clear FileExistsError."""
    (tmp_path / "commands").write_bytes(b"")

    with pytest.raises(FileExistsError) as excinfo:
        install_agent_assets(claude_dir=tmp_path)

    msg = str(excinfo.value)
    assert "commands" in msg
    assert "not a directory" in msg


def test_install_fails_clearly_when_skills_path_is_file(tmp_path: Path) -> None:
    """A file at ``<claude>/skills`` surfaces a clear FileExistsError."""
    (tmp_path / "skills").write_bytes(b"")

    with pytest.raises(FileExistsError) as excinfo:
        install_agent_assets(claude_dir=tmp_path)

    msg = str(excinfo.value)
    assert "skills" in msg
    assert "not a directory" in msg


def test_install_succeeds_when_skills_dir_already_exists(tmp_path: Path) -> None:
    """Pre-existing ``<claude>/skills`` directory is not an error."""
    (tmp_path / "skills").mkdir()

    result = install_agent_assets(claude_dir=tmp_path)

    assert result["wrote"] is True
    assert result["claude_dir"] == str(tmp_path)
    assert len(result["skills_installed"]) > 0
    assert (tmp_path / "skills").is_dir()


def test_install_fails_clearly_when_agents_path_is_file(tmp_path: Path) -> None:
    """A file at ``<claude>/agents`` surfaces a clear FileExistsError."""
    (tmp_path / "agents").write_bytes(b"")

    with pytest.raises(FileExistsError) as excinfo:
        install_agent_assets(claude_dir=tmp_path)

    msg = str(excinfo.value)
    assert "agents" in msg
    assert "not a directory" in msg


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
