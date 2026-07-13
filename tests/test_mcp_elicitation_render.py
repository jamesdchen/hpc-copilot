"""E-render DIGEST v2 — the sign-off popup is a SIGNING surface (three Jobs).

``docs/design/mcp-elicitation.md`` (RULING 2, 2026-07-09). The MCP prompt renderer
embeds a CODE-COMPUTED digest of the on-disk trusted render for a NOTEBOOK sign-off:

* Job 1 — BIND: audit id, section, ``view_sha12``, freshness (STALE → do-not-sign +
  pointer only).
* Job 2 — WHY YOUR JUDGMENT: the tier-trigger headline (which of diff/lint/asserts
  fired, with counts), the declared-assertion table (marked unverified — the trusted
  render is STATIC, no execution value to show), the lint-flag NAMES + locations, and
  per-hunk one-liners, plus the diff BODY in its own bounded block (run-#12
  finding 11 reversed the never-the-diff-body clause; truncation disclosed).
* Job 3 — ROUTE: the on-disk render path.

Plus the HONESTY RULE (oversize → an honest-refusal block, never a silent drop) and
the disclosed fallbacks (missing / stale / no-``view_sha``). Non-notebook refusals are
unchanged; no model-authored text ever enters the digest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent._kernel.extension import mcp_server as M
from hpc_agent.ops.notebook.audit_view import build_audit_view
from hpc_agent.ops.notebook.render_store import write_render
from hpc_agent.state.audit_source import parse_percent_source

if TYPE_CHECKING:
    from pathlib import Path

TEMPLATE = """\
# %%
# hpc-audit-section: model
def train():
    return 42
"""

SOURCE = """\
# %%
# hpc-audit-section: model
def train():
    return 99
assert train() == 99, "sanity"
"""

AUDIT_ID = "audit-77"
SECTION = "model"


def _write_render(tmp_path: Path, *, lint_findings=()):  # type: ignore[no-untyped-def]
    src = parse_percent_source(SOURCE)
    tmpl = parse_percent_source(TEMPLATE)
    view = build_audit_view(src, tmpl, list(lint_findings))
    sv = next(s for s in view.sections if s.slug == SECTION)
    write_render(tmp_path, audit_id=AUDIT_ID, view=sv)
    return sv


def _notebook_args(view_sha: str) -> dict:
    return {
        "spec": {
            "scope_kind": "notebook",
            "scope_id": AUDIT_ID,
            "block": "notebook-sign-off",
            "response": "some model response",
            "resolved": {"section": SECTION, "view_sha": view_sha},
        }
    }


# ─── Job 1 BIND + Job 2 WHY + Job 3 ROUTE, on a fresh render ─────────────────


def test_prompt_embeds_digest_v2_three_jobs(tmp_path: Path) -> None:
    sv = _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    # Job 1 BIND: the digest header + view_sha12 (not the full sha) + fresh + identity.
    assert "Reviewed render digest" in prompt
    assert sv.view_sha[:12] in prompt
    assert "(fresh)" in prompt
    assert f"audit {AUDIT_ID} / section {SECTION}" in prompt
    # Job 2 WHY: the tier-trigger headline names the fired legs with counts.
    assert "requires your judgment" in prompt
    assert "diff: modified" in prompt
    assert "assertions: 1 unverified" in prompt
    # Job 2 assert table: the DECLARED assertion, marked unverified (static audit).
    assert "declared, unverified" in prompt
    assert "train() == 99" in prompt
    # Job 3 ROUTE: the on-disk render path.
    render = M._render_elicitation_prompt  # keep import warm
    assert "full render on disk:" in prompt and str(tmp_path) in prompt
    assert render is M._render_elicitation_prompt
    # The code-selected identifiers ride too (D5).
    assert AUDIT_ID in prompt and SECTION in prompt
    # NOT the fallback / stale line.
    assert "render digest unavailable" not in prompt
    assert "do NOT sign" not in prompt


def test_prompt_surfaces_diff_hunks_and_bounded_body(tmp_path: Path) -> None:
    sv = _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    # A per-hunk one-liner (line range + first changed line) is present …
    assert "diff from template:" in prompt
    assert "hunk(s):" in prompt
    assert "L" in prompt  # a source line range like L1 / L1–4
    # … AND the diff body rides in its own bounded block (run-#12 finding 11
    # reversed RULING 2's never-the-diff-body clause: a signing surface must
    # carry enough of the change to review).
    assert "Diff from template (code-read from the render):" in prompt
    assert "```diff" in prompt


def test_prompt_diff_body_truncation_is_disclosed(tmp_path: Path, monkeypatch) -> None:
    # Squeeze the embed budget so the fixture's diff overruns it — the cut is
    # on a line boundary and the elision count is DISCLOSED, never silent.
    monkeypatch.setattr(M, "_DIFF_EMBED_MAX_BYTES", 20)
    sv = _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    assert "more diff lines — the full render on disk carries them" in prompt


def test_prompt_surfaces_lint_flag_names_and_locations(tmp_path: Path) -> None:
    findings = [
        {"slug": SECTION, "rule": "executes_live", "evidence": {"line": 4, "path": "x.csv"}},
        {"slug": SECTION, "rule": "template_import_shadowed", "evidence": {"name": "np"}},
    ]
    sv = _write_render(tmp_path, lint_findings=findings)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    # NAMES + locations, not just a count.
    assert "lint flags (2)" in prompt
    assert "executes_live @ L4" in prompt
    assert "template_import_shadowed @ np" in prompt
    # The headline counts the flags too.
    assert "lint: 2 flag(s)" in prompt


def test_prompt_is_bounded(tmp_path: Path) -> None:
    sv = _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    # A signing dialog stays bounded: digest budget + the disclosed-truncation
    # diff-body budget, plus the fixed instructional text.
    assert len(prompt) < 2000 + M._DIFF_EMBED_MAX_BYTES


# ─── the HONESTY RULE: oversize → honest-refusal, never a silent drop ────────


def test_oversize_digest_emits_honest_refusal_not_a_silent_trim(
    tmp_path: Path, monkeypatch
) -> None:
    # Squeeze the budget so the normal fixture overruns it; the composer must then
    # REFUSE to digest (naming the counts) rather than compress until an item drops.
    monkeypatch.setattr(M, "_DIGEST_BLOCK_MAX_BYTES", 60)
    sv = _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    assert "too large to digest honestly" in prompt
    assert "assertions" in prompt  # the counts are disclosed
    assert "read the render" in prompt
    assert "full render on disk:" in prompt
    # It did NOT silently keep a partial table — the per-item bullets are gone.
    assert "declared, unverified" not in prompt


def test_capped_lists_disclose_elision_never_silent(tmp_path: Path) -> None:
    # More assertions than the render_store cap → the digest discloses the elision.
    from hpc_agent.ops.notebook import render_store

    n = render_store._DIGEST_MAX_ASSERTIONS + 3
    extra = "".join(f"assert train() == {i}\n" for i in range(n))
    source = SOURCE + extra
    src = parse_percent_source(source)
    tmpl = parse_percent_source(TEMPLATE)
    sv = next(s for s in build_audit_view(src, tmpl, []).sections if s.slug == SECTION)
    write_render(tmp_path, audit_id=AUDIT_ID, view=sv)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    assert "more — read the render" in prompt


# ─── Job 1 freshness: STALE render → do-not-sign, pointer only ───────────────


def test_stale_render_says_do_not_sign(tmp_path: Path) -> None:
    # A render exists for the CURRENT view, but the sign-off binds a DIFFERENT
    # view_sha (source drifted → the content address moved) → STALE. Job 1: say
    # do-not-sign, name the signed view_sha12, show only the pointer.
    _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args("f00df00df00df00d"), tmp_path)
    assert "do NOT sign" in prompt
    assert "f00df00df00d" in prompt  # the signed view_sha12 named
    assert "Read pane" in prompt
    # It must NOT summarize a render as if it were the signed view.
    assert "Reviewed render digest" not in prompt
    assert "declared, unverified" not in prompt


# ─── the disclosed fallbacks (missing / no-view_sha / no-context) ────────────


def test_missing_render_says_do_not_sign(tmp_path: Path) -> None:
    # No render at the signed content address → the same STALE-or-missing
    # freshness failure (never a summarized digest).
    prompt = M._render_elicitation_prompt(_notebook_args("deadbeefdeadbeef"), tmp_path)
    assert "do NOT sign" in prompt
    assert "deadbeefdead" in prompt
    assert "Read pane" in prompt
    assert AUDIT_ID in prompt and SECTION in prompt
    assert "Reviewed render digest" not in prompt


def test_no_view_sha_discloses_reason(tmp_path: Path) -> None:
    args = {
        "spec": {
            "scope_kind": "notebook",
            "scope_id": AUDIT_ID,
            "block": "notebook-sign-off",
            "resolved": {"section": SECTION},  # no view_sha bound yet
        }
    }
    prompt = M._render_elicitation_prompt(args, tmp_path)
    assert "no bound view_sha" in prompt


def test_no_experiment_dir_falls_back_without_crash() -> None:
    prompt = M._render_elicitation_prompt(_notebook_args("abc123def456"))
    assert "render digest unavailable" in prompt
    assert AUDIT_ID in prompt


# ─── non-notebook refusals carry NO digest block (unchanged) ─────────────────


def test_non_notebook_scope_has_no_digest_block(tmp_path: Path) -> None:
    args = {
        "spec": {
            "scope_kind": "scope",
            "scope_id": "s1",
            "block": "scope-unlock",
            "resolved": {"section": "should-not-appear", "view_sha": "abc123"},
        }
    }
    prompt = M._render_elicitation_prompt(args, tmp_path)
    assert "Reviewed render digest" not in prompt
    assert "render digest unavailable" not in prompt
    assert "should-not-appear" not in prompt
    assert "scope-unlock" in prompt
