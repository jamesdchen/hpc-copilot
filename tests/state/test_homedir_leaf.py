"""Leaf journal-home resolver (``state._homedir``): semantics parity + no cold import.

MIRROR pin for ``hpc_agent.state.run_record::current_homedir`` — the leaf must
resolve the same path in every observable case while never importing
``run_record`` unless it is already loaded (the ~85ms dataclasses + inspect tax
the cold-path caches exist to avoid).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hpc_agent.state._homedir import journal_homedir


def test_env_journal_dir_wins(tmp_path, monkeypatch):
    """Step 1: a nonempty ``HPC_JOURNAL_DIR`` wins outright."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    assert journal_homedir() == Path(str(tmp_path))


def test_empty_env_is_treated_as_unset(tmp_path, monkeypatch):
    """An empty ``HPC_JOURNAL_DIR`` falls through (parity with current_homedir)."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", "")
    # With run_record loaded and its HPC_HOMEDIR patched, we fall to step 2.
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", Path(str(tmp_path)))
    assert journal_homedir() == Path(str(tmp_path))


def test_delegates_to_run_record_hpc_homedir_seam(tmp_path, monkeypatch):
    """Step 2: when run_record is already loaded, the back-compat
    ``monkeypatch.setattr(run_record, "HPC_HOMEDIR", ...)`` seam still redirects.
    """
    monkeypatch.delenv("HPC_JOURNAL_DIR", raising=False)
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", Path(str(tmp_path)))
    assert journal_homedir() == Path(str(tmp_path))


def test_default_is_dot_claude_hpc_when_run_record_absent():
    """Step 3: with no env and run_record NOT loaded, the default is
    ``~/.claude/hpc`` — exercised in a subprocess so ``run_record`` is genuinely
    absent (an in-process test cannot unload it).
    """
    code = (
        "import sys; from pathlib import Path; "
        "from hpc_agent.state._homedir import journal_homedir; "
        "print(journal_homedir() == Path.home() / '.claude' / 'hpc')"
    )
    env = {k: v for k, v in os.environ.items() if k != "HPC_JOURNAL_DIR"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=env,
    )
    assert proc.stdout.strip() == "True", (
        f"leaf default did not resolve to ~/.claude/hpc. stderr:\n{proc.stderr}"
    )


def test_no_env_call_does_not_import_run_record():
    """The whole point: calling ``journal_homedir`` with no env must NOT drag
    ``run_record`` in. Subprocess-isolated so a sibling test that already
    imported it cannot mask the regression.
    """
    code = (
        "import sys; from hpc_agent.state._homedir import journal_homedir; "
        "journal_homedir(); "
        "print('hpc_agent.state.run_record' in sys.modules)"
    )
    env = {k: v for k, v in os.environ.items() if k != "HPC_JOURNAL_DIR"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=env,
    )
    assert proc.stdout.strip() == "False", (
        "journal_homedir() eagerly imported run_record — the leaf must resolve "
        f"without it when nothing else has loaded it. stderr:\n{proc.stderr}"
    )
