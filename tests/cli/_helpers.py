"""Shared subprocess + envelope helpers for CLI smoke tests.

Extracted from the previously-1380-LOC ``test_agent_cli.py`` so the
split-out test files (envelope / submit / aggregate / status / misc)
can share the same subprocess wrapper without re-importing each
other.
"""

from __future__ import annotations

import json

from tests._subprocess import run_cli as _run_cli_proc

__all__ = ["run_cli", "parse_envelope", "SUBMIT_SPEC", "env_without_ssh_agent"]


def env_without_ssh_agent() -> dict[str, str]:
    """Inherit PATH so the CLI binary works, but strip SSH_AUTH_SOCK so
    the SSH fail-fast gate kicks in. Also sets HPC_JOURNAL_DIR so the
    journal lookup isn't what fails first."""
    import os

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # No SSH_AUTH_SOCK on purpose.
    }
    # A Python subprocess on Windows needs SystemRoot/COMSPEC to even start,
    # and resolves the home dir from USERPROFILE (not HOME). Carry them when
    # present so this deliberately-stripped env doesn't fail to *spawn* on
    # win32 for reasons unrelated to the SSH gate under test. No-op on POSIX
    # (these vars aren't set there) → the Linux env stays byte-identical.
    # Mirrors the _spawn_env helper added for #163/#166.
    for _var in ("SystemRoot", "COMSPEC", "USERPROFILE"):
        _val = os.environ.get(_var)
        if _val is not None:
            env[_var] = _val
    return env


def run_cli(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Invoke the CLI as a subprocess and return (exit_code, stdout, stderr).

    Delegates to the canonical :func:`tests._subprocess.run_cli` (which
    always passes a ``timeout=`` hang-guard and forwards the isolated
    journal home into the child), keeping the ``(rc, out, err)`` tuple
    shape the CLI smoke-test consumers rely on.
    """
    proc = _run_cli_proc(*args, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def parse_envelope(stdout: str) -> dict:
    """Parse the single-line JSON envelope from stdout. Asserts shape."""
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line; got {len(lines)}"
    obj = json.loads(lines[0])
    assert isinstance(obj, dict), f"envelope must be a JSON object; got {type(obj).__name__}"
    return obj


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
