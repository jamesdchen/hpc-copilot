"""Render-store enrichment — the src digest (slice 1) + prior sign-off (slice 3).

``write_render`` enriches each section render, from the audit's recorded config +
journals, with two PRESENTATION-ONLY blocks: the ``### linked sources`` src digest
(the engine versions the section binds) and a ``### prior sign-off`` advisory (this
exact content already human-signed under another audit). Both are BYTE-ABSENT when
they do not apply — an existing render is unchanged — and neither enters ``view_sha``.
``read_render_digest`` parses both back off the code-written render bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.ops.notebook.audit_view import build_audit_view
from hpc_agent.ops.notebook.render_store import read_render_digest, render_bytes, write_render
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import append_decision

_TEMPLATE = "# %%\n# hpc-audit-section: model\nX = 0\n"
_SOURCE = """# %%
# hpc-audit-section: model
from engine import train
r = train(3)
"""
_AUDIT = "audit-9"


def _experiment(tmp_path: Path, *, opt_in: bool = True) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text(
        "def train(x, y=1):\n    '''Train the model.'''\n    return x + y\n", encoding="utf-8"
    )
    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    if opt_in:
        block = {
            "source": "source.py",
            "template": "template.py",
            "audit_id": _AUDIT,
            "source_roots": ["src"],
        }
        (tmp_path / "interview.json").write_text(
            json.dumps({"audited_source": block}), encoding="utf-8"
        )
    return tmp_path


def _section_view(source_text: str = _SOURCE):
    src = parse_percent_source(source_text)
    tmpl = parse_percent_source(_TEMPLATE)
    view = build_audit_view(src, tmpl, [])
    return next(sv for sv in view.sections if sv.slug == "model")


# ── slice 1: the src digest ──────────────────────────────────────────────────


def test_src_digest_appears_with_linked_sources(tmp_path: Path) -> None:
    _experiment(tmp_path)
    sv = _section_view()
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    body = path.read_text(encoding="utf-8")
    assert "### linked sources" in body
    # module, path:lineno, signature, and module_sha12 all present.
    assert "engine.train @ src/engine.py:1" in body
    assert "`x, y=1`" in body
    assert "module_sha " in body
    digest = read_render_digest(path)
    assert digest is not None
    assert digest.linked_engine_count == 1
    assert len(digest.linked_engines) == 1
    assert "engine.train @ src/engine.py:1" in digest.linked_engines[0]


def test_src_digest_byte_absent_without_linked_sources(tmp_path: Path) -> None:
    # A source that imports nothing under source_roots → no engine block at all,
    # and the render is byte-identical to the un-enriched render_bytes (the pin).
    src_text = "# %%\n# hpc-audit-section: model\nr = 1 + 2\n"
    (tmp_path / "source.py").write_text(src_text, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": _AUDIT,
                    "source_roots": ["src"],
                }
            }
        ),
        encoding="utf-8",
    )
    sv = _section_view(src_text)
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    body = path.read_text(encoding="utf-8")
    assert "### linked sources" not in body
    assert "### prior sign-off" not in body
    # Byte-identical to the un-enriched render.
    assert body == render_bytes(audit_id=_AUDIT, view=sv)
    digest = read_render_digest(path)
    assert digest is not None
    assert digest.linked_engine_count == 0
    assert digest.linked_engines == ()
    assert digest.prior_signoff is None


def test_src_digest_caps_and_discloses_more(tmp_path: Path) -> None:
    # 8 distinct engine modules → the render lists 6 and discloses "+2 more".
    (tmp_path / "src").mkdir()
    imports = []
    for i in range(8):
        (tmp_path / "src" / f"eng{i}.py").write_text(f"V = {i}\n", encoding="utf-8")
        imports.append(f"import eng{i}")
    src_text = "# %%\n# hpc-audit-section: model\n" + "\n".join(imports) + "\n"
    (tmp_path / "source.py").write_text(src_text, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": _AUDIT,
                    "source_roots": ["src"],
                }
            }
        ),
        encoding="utf-8",
    )
    sv = _section_view(src_text)
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    body = path.read_text(encoding="utf-8")
    assert "… +2 more" in body
    digest = read_render_digest(path)
    assert digest is not None
    assert digest.linked_engine_count == 8  # full count preserved
    assert len(digest.linked_engines) == 6  # list capped


def test_src_digest_absent_for_standalone_audit(tmp_path: Path) -> None:
    # No interview.json opt-in → no source path in config → fail-open (no engines).
    _experiment(tmp_path, opt_in=False)
    sv = _section_view()
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    assert "### linked sources" not in path.read_text(encoding="utf-8")


def test_standalone_audit_src_digest_via_recorded_config(tmp_path: Path) -> None:
    # 6a gap-fix pin: a STANDALONE audit (no interview opt-in) whose config was
    # journaled via ``notebook-record-config`` takes the recorded-config path
    # too — its journaled source_roots + the caller-named source_relpath enrich
    # the src digest exactly like an opted-in audit.
    from hpc_agent.state import notebook_audit as nb

    _experiment(tmp_path, opt_in=False)
    nb.record_audit_config(tmp_path, audit_id=_AUDIT, input_roots=[], source_roots=["src"])
    sv = _section_view()
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv, source_relpath="source.py")
    body = path.read_text(encoding="utf-8")
    assert "### linked sources" in body
    assert "engine.train @ src/engine.py:1" in body


def test_standalone_audit_no_relpath_stays_absent(tmp_path: Path) -> None:
    # Even with a journaled config, a standalone audit whose caller names NO
    # source_relpath has no source-path seat → the digest stays byte-absent
    # (the fail-open boundary the explicit seat does not cross).
    from hpc_agent.state import notebook_audit as nb

    _experiment(tmp_path, opt_in=False)
    nb.record_audit_config(tmp_path, audit_id=_AUDIT, input_roots=[], source_roots=["src"])
    sv = _section_view()
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    assert "### linked sources" not in path.read_text(encoding="utf-8")


def test_explicit_relpath_wins_over_interview_block(tmp_path: Path) -> None:
    # The caller-declared source_relpath is what was actually rendered — it
    # wins over the interview block's ``source`` for enrichment (advisory
    # presentation, fail-open; the recorded roots still come from the config).
    _experiment(tmp_path)
    (tmp_path / "src" / "alteng.py").write_text("Q = 1\n", encoding="utf-8")
    alt = "# %%\n# hpc-audit-section: model\nimport alteng\n"
    (tmp_path / "alt.py").write_text(alt, encoding="utf-8")
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "alt.py",
                    "template": "template.py",
                    "audit_id": _AUDIT,
                    "source_roots": ["src"],
                }
            }
        ),
        encoding="utf-8",
    )
    sv = _section_view()
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv, source_relpath="source.py")
    body = path.read_text(encoding="utf-8")
    assert "engine.train @ src/engine.py:1" in body
    assert "alteng" not in body


# ── slice 3: the prior sign-off advisory ─────────────────────────────────────


def _record_prior_signoff(tmp_path: Path, audit_id: str, sv, *, ts: str) -> None:
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=audit_id,
        block="notebook-sign-off",
        response=f"sign {sv.slug}",
        resolved={
            "audit_id": audit_id,
            "section": sv.slug,
            "section_sha": sv.section_sha,
            "view_sha": sv.view_sha,
        },
        ts=ts,
    )


def test_prior_signoff_line_appears_from_different_audit(tmp_path: Path) -> None:
    _experiment(tmp_path)
    sv = _section_view()
    _record_prior_signoff(tmp_path, "audit-OLD", sv, ts="2026-05-01T09:00:00Z")
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    body = path.read_text(encoding="utf-8")
    assert "### prior sign-off" in body
    assert "identical content signed 2026-05-01 under audit audit-OLD" in body
    digest = read_render_digest(path)
    assert digest is not None
    assert digest.prior_signoff == "identical content signed 2026-05-01 under audit audit-OLD"


def test_prior_signoff_absent_when_only_current_audit_signed(tmp_path: Path) -> None:
    # A sign-off under the CURRENT audit is not a "prior" one — no advisory.
    _experiment(tmp_path)
    sv = _section_view()
    _record_prior_signoff(tmp_path, _AUDIT, sv, ts="2026-05-01T09:00:00Z")
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    assert "### prior sign-off" not in path.read_text(encoding="utf-8")


def test_prior_signoff_absent_when_content_differs(tmp_path: Path) -> None:
    # A prior sign-off of DIFFERENT content (different section_sha) is not a match.
    _experiment(tmp_path)
    sv = _section_view()
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id="audit-OLD",
        block="notebook-sign-off",
        response="sign model",
        resolved={"audit_id": "audit-OLD", "section": "model", "section_sha": "deadbeef"},
        ts="2026-05-01T09:00:00Z",
    )
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    assert "### prior sign-off" not in path.read_text(encoding="utf-8")


def test_prior_signoff_never_changes_view_sha(tmp_path: Path) -> None:
    # The advisory is presentation-only: the content address (view_sha) is stable
    # whether or not a prior sign-off exists.
    _experiment(tmp_path)
    sv = _section_view()
    _record_prior_signoff(tmp_path, "audit-OLD", sv, ts="2026-05-01T09:00:00Z")
    path = write_render(tmp_path, audit_id=_AUDIT, view=sv)
    digest = read_render_digest(path)
    assert digest is not None
    assert digest.view_sha == sv.view_sha  # unchanged by the advisory block
