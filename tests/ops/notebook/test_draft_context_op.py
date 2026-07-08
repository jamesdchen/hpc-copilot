"""Tests for the ``notebook-draft-context`` projection (toy domain only).

Covers engine resolution + signature/docstring extraction, name-match call sites
with the disclosed cap, inventory listing with manifest-cite-vs-hash fallback,
content-keyed cache hit/miss on content change, and the read-only contract (no
writes under the experiment dir). Fixtures use a toy widget/gadget vocabulary —
never quant terms — per the agnostic-fixtures rule.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.notebook_draft_context import NotebookDraftContextSpec
from hpc_agent.ops.notebook.draft_context_op import _CALL_SITE_CAP, notebook_draft_context

# ── fixtures ─────────────────────────────────────────────────────────────────

_TEMPLATE = """\
# %%
# hpc-audit-section: build-widget
from engines.widget import make_widget

# %%
# hpc-audit-section: report
from engines.report import summarize
import os
"""

_WIDGET_ENGINE = '''\
"""Widget engine module docstring."""


def make_widget(size, color="red"):
    """Make a widget of the given size and color.

    Longer body ignored.
    """
    return (size, color)
'''

_REPORT_ENGINE = '''\
def summarize(rows):
    """Summarize the rows into a single line."""
    return len(rows)


def _helper():
    return make_widget(1)
'''

_CALLER = """\
from engines.widget import make_widget
from engines.report import summarize


def run():
    a = make_widget(2, color="blue")
    b = make_widget(3)
    return summarize([a, b])
"""


def _setup(experiment_dir: Path) -> None:
    """Lay down template + an ``engines`` source tree + a caller module."""
    (experiment_dir / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    eng = experiment_dir / "src" / "engines"
    eng.mkdir(parents=True)
    (experiment_dir / "src" / "engines" / "__init__.py").write_text("", encoding="utf-8")
    (eng / "widget.py").write_text(_WIDGET_ENGINE, encoding="utf-8")
    (eng / "report.py").write_text(_REPORT_ENGINE, encoding="utf-8")
    (experiment_dir / "src" / "caller.py").write_text(_CALLER, encoding="utf-8")


def _run(experiment_dir: Path, **overrides):
    spec = NotebookDraftContextSpec(
        template="template.py",
        source_roots=overrides.pop("source_roots", ["src"]),
        input_roots=overrides.pop("input_roots", []),
        inventory_roots=overrides.pop("inventory_roots", []),
        audit_id=overrides.pop("audit_id", None),
    )
    return notebook_draft_context(experiment_dir=experiment_dir, spec=spec)


@pytest.fixture(autouse=True)
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the cache home to a SIBLING of the experiment dir.

    A sibling (never a child of ``tmp_path``) so the read-only contract test can
    assert the experiment dir is untouched — the real cache home is ~/.claude/hpc,
    likewise outside any experiment dir.
    """
    home = tmp_path.parent / f"{tmp_path.name}__home"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    return home


# ── template sections ────────────────────────────────────────────────────────


def test_template_sections_verbatim(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = _run(tmp_path)
    slugs = [s.slug for s in result.template_sections]
    assert slugs == ["build-widget", "report"]
    # cell prose carried verbatim (the import line is inside the section source).
    assert "from engines.widget import make_widget" in result.template_sections[0].source


# ── resolved engines: signature + docstring extraction ───────────────────────


def test_engine_resolution_signature_and_doc(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = _run(tmp_path)
    by_name = {e.name: e for e in result.resolved_engines}

    widget = by_name["make_widget"]
    assert widget.resolved is True
    assert widget.module == "engines.widget"
    assert widget.symbol == "make_widget"
    assert widget.file == "src/engines/widget.py"
    assert widget.symbol_lineno == 4  # the def line in _WIDGET_ENGINE
    assert widget.signature == "size, color='red'"
    assert widget.doc == "Make a widget of the given size and color."
    assert widget.module_sha  # non-empty normalized hash

    summarize = by_name["summarize"]
    assert summarize.resolved is True
    assert summarize.signature == "rows"
    assert summarize.doc == "Summarize the rows into a single line."


def test_stdlib_import_listed_unresolved(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = _run(tmp_path)
    os_engine = next(e for e in result.resolved_engines if e.name == "os")
    assert os_engine.resolved is False
    assert os_engine.file is None
    assert os_engine.module_sha is None


# ── name-match call sites + disclosed cap ────────────────────────────────────


def test_call_sites_name_match(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = _run(tmp_path)
    groups = {g.name: g for g in result.call_sites}

    make = groups["make_widget"]
    # two calls in caller.py, one in report.py (_helper) = 3 total.
    assert make.count == 3
    assert make.cap == _CALL_SITE_CAP
    assert make.truncated is False
    assert all(":" in s for s in make.sites)
    assert any("src/caller.py:" in s for s in make.sites)


def test_call_site_cap_disclosed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(tmp_path)
    # Force a tiny cap so truncation is observable without a huge fixture.
    monkeypatch.setattr("hpc_agent.ops.notebook.draft_context_op._CALL_SITE_CAP", 1)
    result = _run(tmp_path)
    make = next(g for g in result.call_sites if g.name == "make_widget")
    assert make.count == 3
    assert make.cap == 1
    assert make.truncated is True
    assert len(make.sites) == 1


# ── inventory: hash + manifest cite fallback ─────────────────────────────────


def test_inventory_hashes_when_no_manifest(tmp_path: Path) -> None:
    _setup(tmp_path)
    data = tmp_path / "inputs"
    data.mkdir()
    (data / "a.txt").write_bytes(b"hello widget")
    result = _run(tmp_path, input_roots=["inputs"])
    listing = next(li for li in result.inventory if li.root == "inputs")
    assert listing.kind == "input"
    assert listing.manifest_cited is False
    entry = next(e for e in listing.entries if e.relpath == "inputs/a.txt")
    assert entry.cited is False
    assert entry.size == len(b"hello widget")
    assert entry.sha12 == hashlib.sha256(b"hello widget").hexdigest()[:12]


def test_inventory_cites_manifest(tmp_path: Path) -> None:
    _setup(tmp_path)
    data = tmp_path / "inputs"
    data.mkdir()
    (data / "a.txt").write_bytes(b"hello widget")
    hpc = tmp_path / ".hpc"
    hpc.mkdir(exist_ok=True)
    manifest = {
        "inputs/a.txt": {"sha256": "deadbeef" * 8, "size": 999, "built_by": "toy"},
        "_doc_sha": "not-a-file-entry",  # a meta field must be skipped defensively
    }
    (hpc / "data_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = _run(tmp_path, inventory_roots=["inputs"])
    listing = next(li for li in result.inventory if li.root == "inputs")
    assert listing.kind == "inventory"
    assert listing.manifest_cited is True
    entry = next(e for e in listing.entries if e.relpath == "inputs/a.txt")
    assert entry.cited is True
    assert entry.sha12 == ("deadbeef" * 8)[:12]
    assert entry.size == 999


# ── cache hit / miss ─────────────────────────────────────────────────────────


def test_cache_hit_returns_identical_and_writes_cache(tmp_path: Path, cache_home: Path) -> None:
    _setup(tmp_path)
    first = _run(tmp_path)
    cache_files = list((cache_home / "draft_context_cache").glob("*.json"))
    assert len(cache_files) == 1
    second = _run(tmp_path)
    assert second.model_dump() == first.model_dump()
    # still exactly one cache file (a hit did not mint a new key).
    assert len(list((cache_home / "draft_context_cache").glob("*.json"))) == 1


def test_cache_miss_on_content_change(tmp_path: Path, cache_home: Path) -> None:
    _setup(tmp_path)
    first = _run(tmp_path)
    # change an engine's signature -> the projection must change + a new key mints.
    (tmp_path / "src" / "engines" / "widget.py").write_text(
        '''\
"""Widget engine module docstring."""


def make_widget(size, color="red", opacity=1.0):
    """Make a widget of the given size and color."""
    return (size, color, opacity)
''',
        encoding="utf-8",
    )
    second = _run(tmp_path)
    widget = next(e for e in second.resolved_engines if e.name == "make_widget")
    assert widget.signature == "size, color='red', opacity=1.0"
    assert second.model_dump() != first.model_dump()
    assert len(list((cache_home / "draft_context_cache").glob("*.json"))) == 2


# ── read-only contract ───────────────────────────────────────────────────────


def test_read_only_no_writes_under_experiment_dir(tmp_path: Path) -> None:
    _setup(tmp_path)

    def _snapshot() -> dict[str, float]:
        return {str(p): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    before = _snapshot()
    _run(tmp_path, input_roots=["src"])
    after = _snapshot()
    # No file created or modified under the experiment dir (cache lives in HOME).
    assert before == after


# ── malformed spec ───────────────────────────────────────────────────────────


def test_missing_template_raises(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        notebook_draft_context(
            experiment_dir=tmp_path,
            spec=NotebookDraftContextSpec(template="nope.py"),
        )


# ── audit_id root default ────────────────────────────────────────────────────


def test_roots_default_from_recorded_config(tmp_path: Path) -> None:
    _setup(tmp_path)
    from hpc_agent.state import notebook_audit

    notebook_audit.record_audit_config(
        tmp_path,
        audit_id="toy-audit",
        input_roots=[],
        source_roots=["src"],
        attention_order=None,
        output_roots=[],
    )
    # source_roots omitted (None) -> defaults from the recorded config.
    spec = NotebookDraftContextSpec(template="template.py", audit_id="toy-audit")
    result = notebook_draft_context(experiment_dir=tmp_path, spec=spec)
    assert result.source_roots == ["src"]
    widget = next(e for e in result.resolved_engines if e.name == "make_widget")
    assert widget.resolved is True
