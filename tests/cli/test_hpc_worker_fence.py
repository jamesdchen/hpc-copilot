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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

_AGENT = Path(__file__).resolve().parents[2] / "src/slash_commands/agents/hpc-worker.md"


def _find_bash() -> str | None:
    """Locate a POSIX bash for running the hook.

    On Windows a bare ``bash`` resolves to the WSL launcher stub
    (``C:\\Windows\\System32\\bash.exe``), which exits 1 for *every* command
    when no distro is installed — so the fence script never runs and all
    cases fail uniformly. Prefer Git Bash (always present on the GitHub
    windows runner) and return ``None`` if no real POSIX bash is found so the
    test skips rather than exercising the stub.
    """
    if sys.platform != "win32":
        return "bash"
    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
        candidates.append(Path(git).resolve().parents[1] / "bin" / "bash.exe")
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


_BASH = _find_bash()

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="the hook shells jq")
needs_bash = pytest.mark.skipif(_BASH is None, reason="no POSIX bash (Git Bash) found")


def _hook_command() -> str:
    fm = _AGENT.read_text(encoding="utf-8").split("---")[1]
    doc = yaml.safe_load(fm)
    pre = doc["hooks"]["PreToolUse"]
    entry = next(e for e in pre if e["matcher"] == "Bash")
    return entry["hooks"][0]["command"]


def _rc(cmd: str) -> int:
    payload = json.dumps({"tool_input": {"command": cmd}})
    # Run the hook from an LF-forced temp script via an explicit POSIX bash
    # (Git Bash on Windows — see _find_bash). Forward-slash the path so Git
    # Bash accepts it. mkstemp + explicit unlink so Windows can reopen the
    # closed file for bash to read.
    assert _BASH is not None  # guarded by @needs_bash
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", newline="\n") as f:
            f.write(_hook_command())
        return subprocess.run(
            [_BASH, path.replace("\\", "/")],
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
        ).returncode
    finally:
        os.unlink(path)


@needs_bash
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


@needs_bash
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
