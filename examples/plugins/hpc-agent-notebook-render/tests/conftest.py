"""Offline fixtures — a tmp experiment repo with source/template .py.

No network, no hpc-agent install assumptions beyond the workspace ``.[dev]`` the
CI plugins job installs first. The utterance-namespace helper points
``HPC_JOURNAL_DIR`` at a tmp home so the no-scaffold write path is exercised both
ways (namespace present vs absent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# A template whose two slugs are the required inventory. ``header`` is shared
# byte-for-byte with the source (auto-clearable); ``analysis`` differs.
TEMPLATE = """# %%
# hpc-audit-section: header
import os

# %%
# hpc-audit-section: analysis
value = 0
"""

# Source: ``header`` inherited (empty diff, no assertions -> auto_cleared tier),
# ``analysis`` MODIFIED (value 0 -> 42, so it is human_required with a diff token
# ``value``).
SOURCE = """# %%
# hpc-audit-section: header
import os

# %%
# hpc-audit-section: analysis
value = 42
"""

# A source whose sections execute — one prints, one raises.
SOURCE_EXEC = """# %%
# hpc-audit-section: ok
print("hello world")

# %%
# hpc-audit-section: boom
raise ValueError("nope")
"""


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    """A tmp experiment dir with ``source.py`` + ``template.py`` written."""
    (tmp_path / "source.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(TEMPLATE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def exec_experiment(tmp_path: Path) -> Path:
    """A tmp experiment dir whose source EXECUTES (print + raise)."""
    (tmp_path / "source.py").write_text(SOURCE_EXEC, encoding="utf-8")
    (tmp_path / "template.py").write_text(TEMPLATE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HPC_JOURNAL_DIR at a tmp home; return it (namespace NOT created)."""
    home = tmp_path / "journal-home"
    home.mkdir()
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    return home
