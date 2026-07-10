"""E-render — the bounded digest read off a content-addressed render file.

``docs/design/mcp-elicitation.md`` E-render (RULING 1: digest, not full render).
:func:`~hpc_agent.ops.notebook.render_store.read_render_digest` derives its digest
from the CODE-WRITTEN render bytes only (never the notebook source): diff stats,
the assert table (bounded), the lint-flag count, and the header identifiers. These
tests build a real :class:`SectionView`, write it via ``write_render``, and read it
back — so the parser is pinned against the exact writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.ops.notebook.audit_view import build_audit_view
from hpc_agent.ops.notebook.render_store import (
    _DIGEST_MAX_ASSERTIONS,
    read_render_digest,
    render_path,
    write_render,
)
from hpc_agent.state.audit_source import parse_percent_source

if TYPE_CHECKING:
    from pathlib import Path

TEMPLATE = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 42
"""

# `model` body edited (a diff) + two declared assertions.
SOURCE = """\
# %%
# hpc-audit-section: setup
import numpy as np
x = 1

# %%
# hpc-audit-section: model
def train():
    return 99
assert train() == 99, "sanity"
assert train() > 0
"""


def _section_view(source_text: str, lint_findings=()):  # type: ignore[no-untyped-def]
    src = parse_percent_source(source_text)
    tmpl = parse_percent_source(TEMPLATE)
    view = build_audit_view(src, tmpl, list(lint_findings))
    return next(sv for sv in view.sections if sv.slug == "model")


def _write(tmp_path: Path, source_text: str, lint_findings=()):  # type: ignore[no-untyped-def]
    sv = _section_view(source_text, lint_findings)
    path = write_render(tmp_path, audit_id="audit-1", view=sv)
    return sv, path


def test_digest_present_diff_asserts_and_header(tmp_path: Path) -> None:
    sv, path = _write(tmp_path, SOURCE)
    digest = read_render_digest(path)
    assert digest is not None
    # Header identifiers ride straight off the code-written render.
    assert digest.view_sha == sv.view_sha
    assert digest.section == "model"
    assert digest.section_sha == sv.section_sha
    assert digest.audit_id == "audit-1"
    assert digest.classification == "modified"
    # Two declared assertions, both surfaced (under the cap).
    assert digest.assertion_count == 2
    assert len(digest.assertions) == 2
    assert any("train() == 99" in a for a in digest.assertions)
    # A real diff from the template (return 42 → 99, plus two added asserts).
    assert digest.diff_added > 0
    assert digest.diff_removed > 0
    assert digest.lint_flag_count == 0


def test_digest_counts_lint_flags(tmp_path: Path) -> None:
    findings = [
        {"slug": "model", "code": "X001", "msg": "flag one"},
        {"slug": "model", "code": "X002", "msg": "flag two"},
    ]
    _sv, path = _write(tmp_path, SOURCE, findings)
    digest = read_render_digest(path)
    assert digest is not None
    assert digest.lint_flag_count == 2


def test_digest_bounds_assertion_list(tmp_path: Path) -> None:
    # More assertions than the cap → the list is capped, the count is full.
    body_asserts = "\n".join(f"assert train() == {i}" for i in range(_DIGEST_MAX_ASSERTIONS + 4))
    source = TEMPLATE.replace("    return 42\n", "    return 99\n") + body_asserts + "\n"
    _sv, path = _write(tmp_path, source)
    digest = read_render_digest(path)
    assert digest is not None
    assert digest.assertion_count == _DIGEST_MAX_ASSERTIONS + 4
    assert len(digest.assertions) == _DIGEST_MAX_ASSERTIONS  # capped
    # Bounded per entry: nothing in the digest exceeds the char cap grossly.
    assert all(len(a) <= 121 for a in digest.assertions)


def test_digest_carries_tier_and_diff_hunk_one_liners(tmp_path: Path) -> None:
    _sv, path = _write(tmp_path, SOURCE)
    digest = read_render_digest(path)
    assert digest is not None
    # The tier rides off the section header ``## section: model  [tier: …]``.
    assert digest.tier in ("human_required", "auto_cleared")
    # Per-hunk one-liners: a source line range + the first changed line, never the
    # diff body. The ``model`` body changed (return 42 → 99) so there is a hunk.
    assert digest.diff_hunk_count >= 1
    assert len(digest.diff_hunks) >= 1
    joined = " ".join(digest.diff_hunks)
    assert joined.startswith("L") or " L" in joined  # a line range like L1 / L1–4
    # A changed line (the added assert or the new return) is surfaced, truncated.
    assert any(("return" in h or "assert" in h) for h in digest.diff_hunks)


def test_digest_lists_lint_flag_names_and_locations(tmp_path: Path) -> None:
    findings = [
        {"slug": "model", "rule": "executes_live", "evidence": {"line": 9, "path": "x.csv"}},
        {"slug": "model", "rule": "template_import_shadowed", "evidence": {"name": "np"}},
    ]
    _sv, path = _write(tmp_path, SOURCE, findings)
    digest = read_render_digest(path)
    assert digest is not None
    assert digest.lint_flag_count == 2
    assert "executes_live @ L9" in digest.lint_flags
    assert "template_import_shadowed @ np" in digest.lint_flags


def test_missing_render_reads_none(tmp_path: Path) -> None:
    sv = _section_view(SOURCE)
    path = render_path(tmp_path, audit_id="audit-1", section="model", view_sha=sv.view_sha)
    assert not path.exists()
    assert read_render_digest(path) is None


def test_headerless_file_reads_none(tmp_path: Path) -> None:
    path = tmp_path / "not-a-render.md"
    path.write_text("## section: model\n\njust some body, no header block\n", encoding="utf-8")
    assert read_render_digest(path) is None
