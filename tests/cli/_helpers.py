"""Shared subprocess + envelope helpers for CLI smoke tests.

Extracted from the previously-1380-LOC ``test_agent_cli.py`` so the
split-out test files (envelope / submit / aggregate / status / misc)
can share the same subprocess wrapper without re-importing each
other.
"""

from __future__ import annotations

import json
import subprocess
import sys

__all__ = ["run_cli", "parse_envelope", "SUBMIT_SPEC", "env_without_ssh_agent"]


def env_without_ssh_agent() -> dict[str, str]:
    """Inherit PATH so the CLI binary works, but strip SSH_AUTH_SOCK so
    the SSH fail-fast gate kicks in. Also sets HPC_JOURNAL_DIR so the
    journal lookup isn't what fails first."""
    import os

    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # No SSH_AUTH_SOCK on purpose.
    }


def run_cli(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Invoke the CLI as a subprocess and return (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def parse_envelope(stdout: str) -> dict:
    """Parse the single-line JSON envelope from stdout. Asserts shape."""
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line; got {len(lines)}"
    return json.loads(lines[0])


SUBMIT_SPEC: dict = {
    "profile": "ml",
    "cluster": "hoffman2",
    "ssh_target": "user@hoffman2.idre.ucla.edu",
    "remote_path": "/u/scratch/exp",
    "job_name": "ml",
    "run_id": "ml-20260429-153012-abcd1234",
    "job_ids": ["12345"],
    "total_tasks": 6,
}
