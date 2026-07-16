"""Tests for the ADVISORY suggest-tests tool (devx B1).

Pins the fire paths of the three mapping passes plus the two contract
invariants of an *advisory* selector:

1. Mirror-path mapping (``src/hpc_agent/errors.py`` -> ``tests/test_errors.py``).
2. Import-graph hit (``ops/decision/journal/verify_relay.py`` is exercised by
   ``tests/ops/test_verify_relay.py``, which imports it directly — the mirror
   path ``tests/ops/decision/journal/`` does not exist, so only the import
   graph can find it).
3. Cross-consumer map hit (a ``block_drive`` lifecycle change fans out to the
   workflows' ``test_blocks.py`` even though those tests never import it).
4. An unmapped changed source file surfaces LOUDLY (never silently dropped).
5. The advisory disclaimer is present in the output.

Each case drives ``suggest`` against the REAL tree with a controlled diff
(``changed_files`` is monkeypatched), so the mapping logic is exercised without
depending on the working tree's actual git state.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "suggest_tests", REPO_ROOT / "scripts" / "suggest_tests.py"
)
assert _SPEC is not None and _SPEC.loader is not None
st = importlib.util.module_from_spec(_SPEC)
sys.modules["suggest_tests"] = st
_SPEC.loader.exec_module(st)


def _run(monkeypatch, changed: list[str]):
    """Drive ``suggest`` with a fixed diff (bypassing git)."""
    monkeypatch.setattr(st, "changed_files", lambda ref="HEAD": [Path(c) for c in changed])
    return st.suggest("HEAD")


def test_mirror_path_mapping(monkeypatch) -> None:
    """``src/hpc_agent/errors.py`` maps to its mirror ``tests/test_errors.py``."""
    sel = _run(monkeypatch, ["src/hpc_agent/errors.py"])
    assert "tests/test_errors.py" in sel.pytest_args()
    assert "tests/test_errors.py" in sel.reasons["mirror"]


def test_import_graph_hit(monkeypatch) -> None:
    """A module with no mirror dir is still found via a direct test import."""
    sel = _run(monkeypatch, ["src/hpc_agent/ops/decision/journal/verify_relay.py"])
    args = sel.pytest_args()
    assert "tests/ops/test_verify_relay.py" in args
    assert "tests/ops/test_verify_relay.py" in sel.reasons["import-graph"]
    # The bogus mirror path must NOT have been invented.
    assert not any(a.startswith("tests/ops/decision/journal/") for a in args)


def test_cross_consumer_map_hit(monkeypatch) -> None:
    """A block_drive change fans out to the curated cross-consumer targets."""
    sel = _run(monkeypatch, ["src/hpc_agent/_kernel/lifecycle/block_drive.py"])
    args = sel.pytest_args()
    for expected in (
        "tests/ops/monitor/test_blocks.py",
        "tests/ops/aggregate/test_blocks.py",
        "tests/meta/campaign/test_blocks.py",
        "tests/ops/attention",
    ):
        assert expected in args, f"{expected} missing from {args}"
    assert "tests/ops/monitor/test_blocks.py" in sel.reasons["cross-consumer"]


def test_unmapped_file_surfaces_loudly(monkeypatch) -> None:
    """A source file no pass can map is listed loudly, never dropped."""
    ghost = "src/hpc_agent/ops/_no_test_for_this_zzz.py"
    sel = _run(monkeypatch, [ghost])
    assert Path(ghost) in sel.unmapped
    assert sel.pytest_args() == []  # nothing invented
    out = st.render(sel, "HEAD")
    assert "UNMAPPED" in out
    assert ghost in out


def test_non_src_change_is_noted_not_unmapped(monkeypatch) -> None:
    """Docs/test/config changes are set aside, not reported as unmapped source."""
    sel = _run(monkeypatch, ["docs/internals/suggest-tests.md", "pyproject.toml"])
    assert Path("docs/internals/suggest-tests.md") in sel.non_src
    assert sel.unmapped == []
    out = st.render(sel, "HEAD")
    assert "non-src" in out


def test_advisory_line_present(monkeypatch) -> None:
    """Every render carries the advisory disclaimer — CI runs everything."""
    sel = _run(monkeypatch, ["src/hpc_agent/errors.py"])
    out = st.render(sel, "HEAD")
    assert st.ADVISORY_LINE in out
    assert "CI runs everything" in out
    assert "full suite" in out.lower()


def test_main_exits_zero_and_prints_advisory(monkeypatch, capsys) -> None:
    """The CLI entrypoint prints the advisory report and exits 0."""
    monkeypatch.setattr(st, "changed_files", lambda ref="HEAD": [Path("src/hpc_agent/errors.py")])
    rc = st.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "advisory selection" in out
    assert "pytest " in out
