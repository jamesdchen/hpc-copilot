"""Tests for the ``notebook-scaffold-template`` primitive.

Every refusal gets a fires test AND the happy path gets a passes test: empty
slug list, duplicate slug (named), malformed slug (named, and no file left on
disk), an existing output file, plus the round-trip-mismatch guard (proven
able to FIRE by stubbing the parser — and proven to delete the partial file).
The happy path asserts the written scaffold round-trips through the ONE
grammar (:func:`parse_percent_source`) with exactly the requested slugs, that
the markers come from :func:`format_section_marker` (one definition, both
directions), and that the result's ``module_sha`` matches a recompute.

The path-bootstrap preamble cell gets three tests: it sits before the first
marker and parses as PREAMBLE (unchanged slugs); exec'd under a simulated
interactive kernel (no ``__file__``, cwd = a subdirectory of a ``.hpc``-marked
experiment root) it normalizes cwd + ``sys.path`` to the experiment root; and
with no ``.hpc`` ancestor it leaves the environment untouched (never a chdir
to the filesystem root).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_scaffold_template import NotebookScaffoldTemplateSpec
from hpc_agent.ops.notebook import scaffold_template_op
from hpc_agent.ops.notebook.scaffold_template_op import notebook_scaffold_template
from hpc_agent.state.audit_source import (
    format_section_marker,
    parse_percent_source,
    sha256_normalized,
)

_SLUGS = ["load-data", "fit-model", "report"]


def _run(experiment_dir: Path, slugs: list[str], output_path: str = "template.py"):
    spec = NotebookScaffoldTemplateSpec(slugs=slugs, output_path=output_path)
    return notebook_scaffold_template(experiment_dir=experiment_dir, spec=spec)


# ── happy path ────────────────────────────────────────────────────────────────


def test_happy_path_writes_and_round_trips(tmp_path: Path) -> None:
    result = _run(tmp_path, _SLUGS)
    out = tmp_path / "template.py"
    assert out.is_file()
    assert result.output_path == str(out)
    assert result.slugs == _SLUGS

    text = out.read_text(encoding="utf-8")
    parsed = parse_percent_source(text)
    assert list(parsed.slugs) == _SLUGS
    assert result.module_sha == parsed.module_sha == sha256_normalized(text)

    # The docstring is preamble (belongs to no section, covered by module_sha).
    assert parsed.preamble.startswith('"""')

    # One definition: every marker line in the file IS the write-side grammar's
    # rendering — never a re-spelled literal.
    for slug in _SLUGS:
        assert format_section_marker(slug) in text.splitlines()

    # Each section carries the one-line placeholder (caller-owned body).
    for section in parsed.sections:
        assert "caller-owned section body" in section.source


# ── the path-bootstrap preamble cell ─────────────────────────────────────────


def _generated_preamble(tmp_path: Path) -> str:
    """Scaffold a template and return its parsed PREAMBLE text."""
    _run(tmp_path, _SLUGS)
    text = (tmp_path / "template.py").read_text(encoding="utf-8")
    return parse_percent_source(text).preamble


def test_preamble_cell_sits_before_first_marker_with_unchanged_slugs(
    tmp_path: Path,
) -> None:
    _run(tmp_path, _SLUGS)
    text = (tmp_path / "template.py").read_text(encoding="utf-8")
    parsed = parse_percent_source(text)

    # Slugs are unchanged by the extra marker-less cell.
    assert list(parsed.slugs) == _SLUGS

    # The bootstrap cell is emitted, and it lands in the PREAMBLE (outside
    # every audit section) — before the first marker line in the raw text.
    assert "Path bootstrap" in parsed.preamble
    assert 'while not (_ROOT / ".hpc").is_dir()' in parsed.preamble
    assert text.index("Path bootstrap") < text.index(format_section_marker(_SLUGS[0]))
    # It never leaks into a section body.
    for section in parsed.sections:
        assert "Path bootstrap" not in section.source

    # Deterministic bytes: a second scaffold emits identical content.
    result2 = _run(tmp_path, _SLUGS, output_path="template2.py")
    assert (tmp_path / "template2.py").read_text(encoding="utf-8") == text
    assert result2.module_sha == parse_percent_source(text).module_sha


def test_preamble_exec_normalizes_cwd_and_sys_path_to_hpc_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exec'd without __file__ from <root>/sub, the preamble walks up to the
    ``.hpc``-marked experiment root and normalizes cwd + sys.path to it."""
    preamble = _generated_preamble(tmp_path)

    exp = tmp_path / "exp"
    (exp / ".hpc").mkdir(parents=True)
    sub = exp / "sub"
    sub.mkdir()

    monkeypatch.setattr(sys, "path", list(sys.path))  # snapshot; auto-restored
    monkeypatch.chdir(sub)  # simulated interactive-kernel cwd; auto-restored

    globs: dict[str, object] = {}  # no __file__ — the interactive-cell case
    exec(compile(preamble, "<preamble>", "exec"), globs)  # noqa: S102

    assert Path.cwd().resolve() == exp.resolve()
    assert Path(sys.path[0]).resolve() == exp.resolve()


def test_preamble_exec_without_hpc_ancestor_leaves_environment_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``.hpc`` ancestor → neither cwd nor sys.path changes (a scaffold used
    outside an experiment repo must never chdir toward the filesystem root)."""
    preamble = _generated_preamble(tmp_path)

    sub = tmp_path / "exp" / "sub"  # no .hpc anywhere up the tree
    sub.mkdir(parents=True)
    if any((p / ".hpc").is_dir() for p in [sub, *sub.parents]):
        pytest.skip("an ancestor of tmp_path carries a real .hpc dir")

    path_snapshot = list(sys.path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(sub)

    globs: dict[str, object] = {}
    exec(compile(preamble, "<preamble>", "exec"), globs)  # noqa: S102

    assert Path.cwd().resolve() == sub.resolve()
    assert sys.path == path_snapshot


def test_happy_path_creates_parent_dirs_and_resolves_relative(tmp_path: Path) -> None:
    result = _run(tmp_path, ["only-section"], output_path="nested/dir/tpl.py")
    out = tmp_path / "nested" / "dir" / "tpl.py"
    assert out.is_file()
    assert result.output_path == str(out)
    assert list(parse_percent_source(out.read_text(encoding="utf-8")).slugs) == ["only-section"]


# ── refusals (each proven to fire) ───────────────────────────────────────────


def test_empty_slugs_refused(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="at least one section slug"):
        _run(tmp_path, [])
    assert not (tmp_path / "template.py").exists()


def test_duplicate_slug_refused_early_and_named(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match=r"duplicate section slug 'fit-model'"):
        _run(tmp_path, ["load-data", "fit-model", "fit-model"])
    assert not (tmp_path / "template.py").exists()


def test_malformed_slug_refused_named_and_no_file_left(tmp_path: Path) -> None:
    # The marker grammar itself refuses (one definition) — the offending slug
    # is named, and the refusal happens BEFORE any filesystem write.
    with pytest.raises(errors.SpecInvalid, match=r"bad slug!"):
        _run(tmp_path, ["load-data", "bad slug!"])
    assert not (tmp_path / "template.py").exists()


def test_existing_output_file_refused(tmp_path: Path) -> None:
    out = tmp_path / "template.py"
    out.write_text("# pre-existing\n", encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="already exists"):
        _run(tmp_path, _SLUGS)
    # Never clobbered.
    assert out.read_text(encoding="utf-8") == "# pre-existing\n"


# ── the round-trip guard can actually fire ───────────────────────────────────


def test_round_trip_mismatch_deletes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verification guard fires on a slug mismatch and deletes the file.

    A correct renderer can't produce a mismatch naturally, so the parser seam
    is stubbed to read back the WRONG slugs — proving the guard can fire (the
    engineering-principles bar) and that it never leaves a partial file.
    """
    real_parse = scaffold_template_op.parse_percent_source

    def _wrong_slugs(text: str):
        return real_parse(text.replace("fit-model", "fit-mode1"))

    monkeypatch.setattr(scaffold_template_op, "parse_percent_source", _wrong_slugs)
    with pytest.raises(errors.SpecInvalid, match="round-trip verification failed"):
        _run(tmp_path, _SLUGS)
    assert not (tmp_path / "template.py").exists()


def test_round_trip_parse_failure_deletes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(text: str):
        raise errors.SpecInvalid("stub: unparseable")

    monkeypatch.setattr(scaffold_template_op, "parse_percent_source", _boom)
    with pytest.raises(errors.SpecInvalid, match="failed its own round-trip parse"):
        _run(tmp_path, _SLUGS)
    assert not (tmp_path / "template.py").exists()
