"""Wave-3 trust change — the content-sha ledger + the ``reused`` auto-clear.

Pins the maintainer's trust invariant: *exact recurrence of signed content is
free; anything else costs one focused look at what changed.* Covers pieces 1, 2,
4 and the reuse legs of piece 9:

* the ledger reader spans every ``.hpc/notebooks/*.decisions.jsonl`` for prior
  HUMAN sign-offs of an exact content sha (piece 1);
* a modified section whose EXACT bytes were human-signed under a DIFFERENT audit
  auto-clears as the visibly-distinct ``reused`` status (piece 2);
* the KILL-INVARIANT — one byte of change moves the sha and NO reuse fires;
* a ``reused`` clearance drift-revokes exactly like any auto-clear when the sha
  moves;
* the graduation gate PASSES a backed reuse and REFUSES a forged ``reuse_of``
  naming content no human ever signed (piece 9);
* the recurrence nudge fires at ≥2 prior audits (piece 4).

TOY vocabulary only (widget lineage), never a real domain's words.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_auto_clear import (
    NotebookAutoClearResult,
    NotebookAutoClearSpec,
)
from hpc_agent.ops.notebook.auto_clear_op import notebook_auto_clear
from hpc_agent.ops.notebook.render_store import read_render_digest, write_render
from hpc_agent.ops.notebook_gate import assert_source_audited
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path

_NEW = "widget-new"
_OLD = "widget-old"

# A template section "model" with body A; the source MODIFIES it (body B) → the
# section is `modified` → HUMAN_REQUIRED, so nothing auto-clears it EXCEPT reuse.
_TEMPLATE = """# %%
# hpc-audit-section: model
model = fit(data)
"""
_SOURCE_B = """# %%
# hpc-audit-section: model
model = fit(data, widget=0.5)
"""
# One byte different from B (0.5 → 0.6): a distinct sha, so reuse must NOT fire.
_SOURCE_B2 = """# %%
# hpc-audit-section: model
model = fit(data, widget=0.6)
"""


def _sha(source_text: str, slug: str) -> str:
    parsed = parse_percent_source(source_text)
    return next(s.section_sha for s in parsed.sections if s.slug == slug)


def _write(exp: Path, source_text: str, *, audit_id: str = _NEW) -> None:
    (exp / "source.py").write_text(source_text, encoding="utf-8")
    (exp / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    (exp / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": audit_id,
                }
            }
        ),
        encoding="utf-8",
    )


def _human_signoff(
    exp: Path,
    audit_id: str,
    slug: str,
    section_sha: str,
    *,
    ts: str,
    actor: str | None = None,
) -> None:
    """Seed a HUMAN sign-off record directly (bypassing the append gate, as the
    render-store enrichment suite does) so the ledger has a prior attestation."""
    append_decision(
        exp,
        scope_kind="notebook",
        scope_id=audit_id,
        block=nb.SIGN_OFF_BLOCK,
        response=f"sign {slug}",
        resolved={"audit_id": audit_id, "section": slug, "section_sha": section_sha},
        attestor_id=actor,
        ts=ts,
    )


def _run_clear(exp: Path, audit_id: str = _NEW) -> NotebookAutoClearResult:
    return notebook_auto_clear(
        experiment_dir=exp,
        spec=NotebookAutoClearSpec.model_validate(
            {"audit_id": audit_id, "source": "source.py", "template": "template.py"}
        ),
    )


def _status(exp: Path, slug: str, audit_id: str = _NEW) -> str:
    source = parse_percent_source((exp / "source.py").read_text(encoding="utf-8"))
    template = parse_percent_source((exp / "template.py").read_text(encoding="utf-8"))
    rollup = nb.audit_module(exp, audit_id, source=source, required_slugs=template.slugs)
    return next(s.status for s in rollup.sections if s.slug == slug)


# ── piece 1: the ledger reader ───────────────────────────────────────────────


def test_ledger_finds_prior_human_signoff_across_audits(tmp_path: Path) -> None:
    _write(tmp_path, _SOURCE_B)
    sha_b = _sha(_SOURCE_B, "model")
    _human_signoff(tmp_path, _OLD, "model", sha_b, ts="2026-05-01T09:00:00Z")
    entries = nb.read_signoff_ledger(tmp_path, content_sha=sha_b, exclude_audit_id=_NEW)
    assert [e.audit_id for e in entries] == [_OLD]
    assert entries[0].content_sha == sha_b
    assert entries[0].ts == "2026-05-01T09:00:00Z"


def test_ledger_excludes_the_named_audit_and_mismatched_shas(tmp_path: Path) -> None:
    _write(tmp_path, _SOURCE_B)
    sha_b = _sha(_SOURCE_B, "model")
    # A sign-off under the SAME (excluded) audit, and one of DIFFERENT content.
    _human_signoff(tmp_path, _NEW, "model", sha_b, ts="2026-05-01T09:00:00Z")
    _human_signoff(tmp_path, _OLD, "model", "deadbeef", ts="2026-05-02T09:00:00Z")
    assert nb.read_signoff_ledger(tmp_path, content_sha=sha_b, exclude_audit_id=_NEW) == []


# ── piece 2: the reused auto-clear + its distinct status ─────────────────────


def test_exact_recurrence_reuses_and_status_is_distinct(tmp_path: Path) -> None:
    _write(tmp_path, _SOURCE_B)
    sha_b = _sha(_SOURCE_B, "model")
    _human_signoff(tmp_path, _OLD, "model", sha_b, ts="2026-05-01T09:00:00Z")
    result = _run_clear(tmp_path)
    cleared = {c.section: c for c in result.cleared}
    assert "model" in cleared, "the modified-but-recurring section must reuse-clear"
    assert cleared["model"].reuse_of is not None
    assert cleared["model"].reuse_of["audit_id"] == _OLD
    # Visibly distinct: NOT auto_cleared, NOT signed_current — the reused status.
    assert _status(tmp_path, "model") == nb.REUSED


def test_one_byte_change_never_reuses(tmp_path: Path) -> None:
    # The prior sign-off is of body B; the source is body B2 (one byte different)
    # → a different sha → the KILL-INVARIANT: changed content NEVER reuses.
    _write(tmp_path, _SOURCE_B2)
    _human_signoff(tmp_path, _OLD, "model", _sha(_SOURCE_B, "model"), ts="2026-05-01T09:00:00Z")
    result = _run_clear(tmp_path)
    assert result.cleared == []
    assert {s.section: s.reason for s in result.skipped}["model"] == "human_required"
    assert _status(tmp_path, "model") == nb.UNSIGNED


def test_reused_revokes_on_drift(tmp_path: Path) -> None:
    _write(tmp_path, _SOURCE_B)
    _human_signoff(tmp_path, _OLD, "model", _sha(_SOURCE_B, "model"), ts="2026-05-01T09:00:00Z")
    _run_clear(tmp_path)
    assert _status(tmp_path, "model") == nb.REUSED
    # Edit the section — the reuse clearance was bound to the old sha, so the
    # reducer reads it stale → unsigned (drift = unsigned by construction).
    (tmp_path / "source.py").write_text(_SOURCE_B2, encoding="utf-8")
    assert _status(tmp_path, "model") == nb.UNSIGNED


def test_reuse_is_idempotent(tmp_path: Path) -> None:
    _write(tmp_path, _SOURCE_B)
    _human_signoff(tmp_path, _OLD, "model", _sha(_SOURCE_B, "model"), ts="2026-05-01T09:00:00Z")
    _run_clear(tmp_path)
    second = _run_clear(tmp_path)
    assert second.cleared == []
    assert {s.section: s.reason for s in second.skipped}["model"] == "already-current"


# ── piece 9: the graduation gate with reuse ─────────────────────────────────


def test_gate_passes_with_a_backed_reused_section(tmp_path: Path) -> None:
    _write(tmp_path, _SOURCE_B)
    _human_signoff(tmp_path, _OLD, "model", _sha(_SOURCE_B, "model"), ts="2026-05-01T09:00:00Z")
    _run_clear(tmp_path)
    assert _status(tmp_path, "model") == nb.REUSED
    assert_source_audited(tmp_path)  # a backed reuse graduates


def test_gate_refuses_a_forged_reuse_of(tmp_path: Path) -> None:
    # Journal a reuse auto-clear whose reuse_of names an audit that never signed
    # this content — NO backing sign-off exists anywhere in the ledger.
    _write(tmp_path, _SOURCE_B)
    sha_b = _sha(_SOURCE_B, "model")
    nb.record_auto_clear(
        tmp_path,
        audit_id=_NEW,
        section="model",
        section_sha=sha_b,
        recompute=sha_b,
        reuse_of={"audit_id": "ghost", "ts": "2026-01-01T00:00:00Z", "section_sha": sha_b},
    )
    assert _status(tmp_path, "model") == nb.REUSED  # reduction trusts the record shape
    # …but the gate VERIFIES the reuse against the ledger and refuses the forgery.
    with pytest.raises(errors.SourceUnaudited) as exc:
        assert_source_audited(tmp_path)
    assert "reuse_of" in str(exc.value)


# ── piece 4: the recurrence nudge (emergent-reuse signal) ────────────────────


def test_recurrence_nudge_fires_at_two_prior_audits(tmp_path: Path) -> None:
    _write(tmp_path, _SOURCE_B)
    sha_b = _sha(_SOURCE_B, "model")
    _human_signoff(tmp_path, "widget-a1", "model", sha_b, ts="2026-05-01T09:00:00Z")
    _human_signoff(tmp_path, "widget-a2", "model", sha_b, ts="2026-05-02T09:00:00Z")
    from hpc_agent.ops.notebook.audit_view import build_audit_view

    view = build_audit_view(parse_percent_source(_SOURCE_B), parse_percent_source(_TEMPLATE), [])
    sv = next(s for s in view.sections if s.slug == "model")
    path = write_render(tmp_path, audit_id=_NEW, view=sv)
    body = path.read_text(encoding="utf-8")
    assert "recurred in 2 audits — candidate for src extraction" in body
    digest = read_render_digest(path)
    assert digest is not None and digest.prior_signoff is not None


def test_recurrence_nudge_absent_at_one_prior_audit(tmp_path: Path) -> None:
    _write(tmp_path, _SOURCE_B)
    sha_b = _sha(_SOURCE_B, "model")
    _human_signoff(tmp_path, "widget-a1", "model", sha_b, ts="2026-05-01T09:00:00Z")
    from hpc_agent.ops.notebook.audit_view import build_audit_view

    view = build_audit_view(parse_percent_source(_SOURCE_B), parse_percent_source(_TEMPLATE), [])
    sv = next(s for s in view.sections if s.slug == "model")
    body = write_render(tmp_path, audit_id=_NEW, view=sv).read_text(encoding="utf-8")
    assert "### prior sign-off" in body  # the advisory still shows
    assert "candidate for src extraction" not in body  # but no nudge at count 1


def test_reused_render_line_names_the_signing_actor(tmp_path: Path) -> None:
    # The reused render line names the actor who signed the ORIGINAL render (the
    # sign-off record's attestor_id, MH3) — an attributed sign-off renders a
    # "by <actor>" clause on the advisory line.
    _write(tmp_path, _SOURCE_B)
    sha_b = _sha(_SOURCE_B, "model")
    _human_signoff(tmp_path, "widget-a1", "model", sha_b, ts="2026-05-01T09:00:00Z", actor="opal")
    from hpc_agent.ops.notebook.audit_view import build_audit_view

    view = build_audit_view(parse_percent_source(_SOURCE_B), parse_percent_source(_TEMPLATE), [])
    sv = next(s for s in view.sections if s.slug == "model")
    path = write_render(tmp_path, audit_id=_NEW, view=sv)
    body = path.read_text(encoding="utf-8")
    line = "identical content signed 2026-05-01 by opal under audit widget-a1"
    assert line in body
    digest = read_render_digest(path)
    assert digest is not None and digest.prior_signoff == line


def test_reused_render_line_omits_actor_when_unattributed(tmp_path: Path) -> None:
    # An unattributed sign-off (zero/one declared actor → no attestor_id) renders
    # byte-identically to the pre-actor line: no "by" clause.
    _write(tmp_path, _SOURCE_B)
    sha_b = _sha(_SOURCE_B, "model")
    _human_signoff(tmp_path, "widget-a1", "model", sha_b, ts="2026-05-01T09:00:00Z")
    from hpc_agent.ops.notebook.audit_view import build_audit_view

    view = build_audit_view(parse_percent_source(_SOURCE_B), parse_percent_source(_TEMPLATE), [])
    sv = next(s for s in view.sections if s.slug == "model")
    body = write_render(tmp_path, audit_id=_NEW, view=sv).read_text(encoding="utf-8")
    assert "identical content signed 2026-05-01 under audit widget-a1" in body
    assert " by " not in next(
        ln for ln in body.splitlines() if ln.startswith("- identical content signed")
    )
