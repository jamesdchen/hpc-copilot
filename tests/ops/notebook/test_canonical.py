"""Tests for the ONE canonical-view definition (full-view-recompute upgrade).

``ops/notebook/canonical.py::build_canonical_view`` is the single server-side view
builder the T8 sign-off gate, the ``notebook-audit-view`` / ``notebook-auto-clear``
verbs, and the render plugin all route through. These pin: the recorded-config
read (coercion + absent defaults), determinism, and the un-fakeability property
(caller findings can never enter — the lint is recomputed).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent.ops.notebook.audit_view import AUTO_CLEARED, HUMAN_REQUIRED
from hpc_agent.ops.notebook.canonical import (
    AuditConfig,
    build_canonical_view,
    read_recorded_config,
)

if TYPE_CHECKING:
    from pathlib import Path

_TEMPLATE = """# %%
# hpc-audit-section: load
import pandas as pd

data = pd.read_csv("data/input.csv")
"""

_SOURCE = _TEMPLATE  # byte-identical → inherited


def _write(tmp_path: Path, *, data_present: bool, config: dict | None = None) -> None:
    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    block = {"source": "source.py", "template": "template.py", "audit_id": "a1"}
    if config:
        block.update(config)
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": block}), encoding="utf-8"
    )
    if data_present:
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "input.csv").write_text("x\n1\n", encoding="utf-8")


def test_read_recorded_config_defaults_when_absent(tmp_path: Path) -> None:
    """No interview.json → conservative defaults (empty roots, source order)."""
    cfg = read_recorded_config(tmp_path, "a1")
    assert cfg == AuditConfig(input_roots=[], source_roots=[], attention_order=None)


def test_read_recorded_config_reads_and_coerces(tmp_path: Path) -> None:
    _write(
        tmp_path,
        data_present=True,
        config={"input_roots": ["data"], "source_roots": ["src"], "attention_order": ["load"]},
    )
    cfg = read_recorded_config(tmp_path, "a1")
    assert cfg.input_roots == ["data"]
    assert cfg.source_roots == ["src"]
    assert cfg.attention_order == ["load"]


def test_read_recorded_config_predates_fields(tmp_path: Path) -> None:
    """A block WITHOUT the config fields → defaults (byte-compat with old records)."""
    _write(tmp_path, data_present=True)
    assert read_recorded_config(tmp_path, "a1") == AuditConfig()


def test_build_canonical_view_deterministic(tmp_path: Path) -> None:
    _write(tmp_path, data_present=True, config={"input_roots": ["."]})
    cfg = read_recorded_config(tmp_path, "a1")
    v1 = build_canonical_view(
        tmp_path, audit_id="a1", source_relpath="source.py", template_relpath="template.py", cfg=cfg
    )
    v2 = build_canonical_view(
        tmp_path, audit_id="a1", source_relpath="source.py", template_relpath="template.py", cfg=cfg
    )
    assert v1.view_sha == v2.view_sha
    assert [s.view_sha for s in v1.sections] == [s.view_sha for s in v2.sections]


def test_build_canonical_view_passes_output_roots_through(tmp_path: Path) -> None:
    """A recorded output_root exempts a write-target literal from the
    executes-live flag inside the canonical view (the run-#10 noise fix)."""
    source = """\
# %%
# hpc-audit-section: load
OUT = "results/out.json"
"""
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(source, encoding="utf-8")
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": "a1",
                    "input_roots": ["data"],
                    "output_roots": ["results"],
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = read_recorded_config(tmp_path, "a1")
    assert cfg.output_roots == ["results"]
    view = build_canonical_view(
        tmp_path, audit_id="a1", source_relpath="source.py", template_relpath="template.py", cfg=cfg
    )
    load = {s.slug: s for s in view.sections}["load"]
    # The missing output literal does not flag — the inherited section clears.
    assert load.tier == AUTO_CLEARED
    assert not load.lint_flags

    # Without the recorded output_root the same literal flags (the pair).
    rootless = AuditConfig(input_roots=["data"])
    flagged = build_canonical_view(
        tmp_path,
        audit_id="a1",
        source_relpath="source.py",
        template_relpath="template.py",
        cfg=rootless,
    )
    assert {s.slug: s.tier for s in flagged.sections}["load"] == HUMAN_REQUIRED


def test_build_canonical_view_recomputes_lint(tmp_path: Path) -> None:
    """The lint is REAL: a present data path auto-clears the inherited section; a
    missing one flags it human_required — the caller never supplies findings."""
    _write(tmp_path, data_present=True, config={"input_roots": ["."]})
    cfg = read_recorded_config(tmp_path, "a1")
    present = build_canonical_view(
        tmp_path, audit_id="a1", source_relpath="source.py", template_relpath="template.py", cfg=cfg
    )
    assert {s.slug: s.tier for s in present.sections}["load"] == AUTO_CLEARED

    (tmp_path / "data" / "input.csv").unlink()
    missing = build_canonical_view(
        tmp_path, audit_id="a1", source_relpath="source.py", template_relpath="template.py", cfg=cfg
    )
    load = {s.slug: s for s in missing.sections}["load"]
    assert load.tier == HUMAN_REQUIRED
    assert load.view_sha != {s.slug: s for s in present.sections}["load"].view_sha
