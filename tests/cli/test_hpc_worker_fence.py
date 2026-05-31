"""The inline ``hpc-worker`` subagent is invoke-only.

Its frontmatter ``PreToolUse`` hook blocks any Bash command that isn't
``hpc-agent`` / ``git`` — the inline-path analog of the ``--bare`` worker's
``--allowedTools`` fence (and stricter: it also rejects shell chaining /
substitution, so ``hpc-agent x && rm -rf /`` can't smuggle a second command).
Subagent frontmatter ``tools:`` can't command-scope Bash, so a frontmatter
PreToolUse hook is the only mechanism that enforces this separately from the
parent session's permissions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_AGENT = Path(__file__).resolve().parents[2] / "src/slash_commands/agents/hpc-worker.md"

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="the hook shells jq")


def _hook_command() -> str:
    fm = _AGENT.read_text(encoding="utf-8").split("---")[1]
    doc = yaml.safe_load(fm)
    pre = doc["hooks"]["PreToolUse"]
    entry = next(e for e in pre if e["matcher"] == "Bash")
    return entry["hooks"][0]["command"]


def _rc(cmd: str) -> int:
    payload = json.dumps({"tool_input": {"command": cmd}})
    return subprocess.run(
        ["bash", "-c", _hook_command()],
        input=payload,
        text=True,
        capture_output=True,
        timeout=30,
    ).returncode


@needs_jq
@pytest.mark.parametrize(
    "cmd",
    [
        "hpc-agent submit-flow --spec spec.json --experiment-dir .",
        "hpc-agent status --run-id r1",
        "git commit -m 'scaffold tasks.py + cli.py'",
    ],
)
def test_allows_hpc_agent_and_git(cmd: str) -> None:
    assert _rc(cmd) == 0


@needs_jq
@pytest.mark.parametrize(
    "cmd",
    [
        "ssh host qstat",
        "rsync -az a b",
        "scp a b",
        "scancel 12345",
        "qsub job.sh",
        "python -c import_os",
        "curl http://example.com",
        "hpc-agent submit && rm -rf /",  # chaining must not smuggle a 2nd command
        "git status | sh",  # pipe to a shell is chaining
        "rm -rf /",
    ],
)
def test_blocks_everything_else(cmd: str) -> None:
    assert _rc(cmd) == 2
