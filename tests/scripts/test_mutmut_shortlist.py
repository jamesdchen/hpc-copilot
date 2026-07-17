"""Tests for the scheduled cluster-verb sweep scoping + zero-signal tripwire.

The sweep runs mutmut, which is Linux-CI-only, so these tests never invoke
mutmut. They pin the PURE-PYTHON levers the memo Unit A added:

* :func:`count_checked_mutants` — the tripwire measurement over ``*.meta``
  ``exit_code_by_key`` maps (checked = non-null exit code).
* ``tripwire`` mode — exit 1 when zero mutants were checked.
* ``--apply-tests-dir`` — narrow ``[tool.mutmut].tests_dir`` to the cluster-verb
  covering set (the fix that lets the sweep check mutants at all).
* the curated :data:`CLUSTER_VERB_TESTS` all exist on disk.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "mutmut_shortlist", REPO_ROOT / "scripts" / "mutmut_shortlist.py"
)
assert _SPEC is not None and _SPEC.loader is not None
ms = importlib.util.module_from_spec(_SPEC)
sys.modules["mutmut_shortlist"] = ms
_SPEC.loader.exec_module(ms)


def _write_meta(path: Path, exit_code_by_key: dict[str, int | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code_by_key": exit_code_by_key}), encoding="utf-8")


# ── count_checked_mutants ──────────────────────────────────────────────────


def test_all_null_is_zero_checked(tmp_path):
    """The run-29560911639 failure: every mutant null → 0 checked, total counted."""
    _write_meta(tmp_path / "a.meta", {"m1": None, "m2": None, "m3": None})
    checked, total = ms.count_checked_mutants(tmp_path)
    assert checked == 0
    assert total == 3


def test_mixed_verdicts_counted_as_checked(tmp_path):
    """Killed (1) / survived (0) / no-tests (33) / skipped (34) all count as checked."""
    _write_meta(tmp_path / "sub/a.meta", {"m1": 1, "m2": 0, "m3": None})
    _write_meta(tmp_path / "sub/b.meta", {"m4": 33, "m5": 34})
    checked, total = ms.count_checked_mutants(tmp_path)
    assert checked == 4  # 1, 0, 33, 34 — everything but the null
    assert total == 5


def test_missing_dir_is_zero(tmp_path):
    checked, total = ms.count_checked_mutants(tmp_path / "does-not-exist")
    assert (checked, total) == (0, 0)


def test_corrupt_meta_is_skipped(tmp_path):
    (tmp_path / "bad.meta").write_text("{not valid json", encoding="utf-8")
    _write_meta(tmp_path / "good.meta", {"m1": 1})
    checked, total = ms.count_checked_mutants(tmp_path)
    assert (checked, total) == (1, 1)


# ── tripwire mode ──────────────────────────────────────────────────────────


def test_tripwire_fails_on_zero_signal(tmp_path, capsys):
    """A sweep where every mutant is null must exit 1 (green→red conversion)."""
    _write_meta(tmp_path / "a.meta", {"m1": None, "m2": None})
    rc = ms.main(["tripwire", "--mutants-dir", str(tmp_path)])
    assert rc == 1
    assert "TRIPWIRE FAILED" in capsys.readouterr().err


def test_tripwire_passes_with_signal(tmp_path, capsys):
    _write_meta(tmp_path / "a.meta", {"m1": 1, "m2": None})
    rc = ms.main(["tripwire", "--mutants-dir", str(tmp_path)])
    assert rc == 0
    assert "tripwire OK" in capsys.readouterr().out


def test_tripwire_fails_when_no_mutants_generated(tmp_path):
    """No *.meta at all (mutmut infra crash) is also zero-signal → red."""
    assert ms.main(["tripwire", "--mutants-dir", str(tmp_path)]) == 1


def test_tally_mutants_separates_signal_from_checked(tmp_path):
    """_tally_mutants returns (signal, checked, total): signal = killed(1)+
    survived(0); checked adds 33 no-tests / 34 skipped; total counts every key."""
    _write_meta(tmp_path / "a.meta", {"m1": 1, "m2": 0, "m3": 33, "m4": 34, "m5": None})
    signal, checked, total = ms._tally_mutants(tmp_path)
    assert (signal, checked, total) == (2, 4, 5)


def test_tripwire_fails_when_all_no_tests(tmp_path, capsys):
    """triage-2 refinement: a sweep where every mutant is exit-33 'no tests' is
    checked>0 but has ZERO real signal → RED (the exit-33 green loophole closed)."""
    _write_meta(tmp_path / "a.meta", {"m1": 33, "m2": 33, "m3": 34})
    rc = ms.main(["tripwire", "--mutants-dir", str(tmp_path)])
    assert rc == 1
    assert "TRIPWIRE FAILED" in capsys.readouterr().err


# ── tests_dir scoping ──────────────────────────────────────────────────────


def test_cluster_verb_tests_all_exist():
    """Every curated cluster-verb test file exists — the sweep scopes to these."""
    missing = [t for t in ms.CLUSTER_VERB_TESTS if not (REPO_ROOT / t).is_file()]
    assert not missing, f"cluster-verb tests_dir references missing files: {missing}"


def test_resolve_tests_dir_nonempty():
    resolved = ms._resolve_tests_dir()
    assert resolved
    assert all((REPO_ROOT / t).is_file() for t in resolved)


def test_apply_tests_dir_narrows_tests_dir(tmp_path):
    """--apply-tests-dir rewrites BOTH paths_to_mutate and tests_dir, dropping
    the whole-suite default and preserving sibling keys."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.mutmut]\n"
        'paths_to_mutate = [\n    "src/hpc_agent/infra/throughput.py",\n]\n'
        'also_copy = [\n    "src/hpc_agent/",\n]\n'
        'tests_dir = ["tests/"]\n'
        'do_not_mutate = [\n    "src/hpc_agent/_wire/*",\n]\n',
        encoding="utf-8",
    )
    rc = ms.main(
        [
            "paths",
            "--apply-to-pyproject",
            str(pyproject),
            "--apply-tests-dir",
        ]
    )
    assert rc == 0
    text = pyproject.read_text(encoding="utf-8")
    # tests_dir no longer the whole suite; now the cluster-verb covering set.
    assert 'tests_dir = ["tests/"]' not in text
    assert "tests/ops/test_submit_flow_pure_api.py" in text
    # Sibling keys preserved.
    assert "also_copy" in text
    assert "do_not_mutate" in text
    # paths_to_mutate rewritten to the cluster verbs.
    assert "src/hpc_agent/ops/submit_flow.py" in text
    assert "throughput.py" not in text


def test_apply_without_tests_dir_flag_leaves_tests_dir(tmp_path):
    """Default (no --apply-tests-dir): tests_dir untouched (back-compat)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.mutmut]\n"
        'paths_to_mutate = [\n    "src/hpc_agent/infra/throughput.py",\n]\n'
        'tests_dir = ["tests/"]\n',
        encoding="utf-8",
    )
    ms.main(["paths", "--apply-to-pyproject", str(pyproject)])
    text = pyproject.read_text(encoding="utf-8")
    assert 'tests_dir = ["tests/"]' in text


def test_unknown_key_raises(tmp_path):
    """A pyproject missing the target key fails loudly, never silent no-scope."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.mutmut]\npaths_to_mutate = []\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        ms._replace_mutmut_array(pyproject.read_text(encoding="utf-8"), "tests_dir", ["tests/x.py"])
