"""E-render — the sign-off elicitation popup carries the render digest (D5 + E-render).

``docs/design/mcp-elicitation.md`` (SHIPPED 2026-07-09). The MCP prompt renderer
embeds a CODE-COMPUTED digest (diff stats, assert table, lint-flag count) + the
``view_sha12`` of the on-disk trusted render for a NOTEBOOK sign-off, and discloses
a reason line when the render is missing/stale — never a crash, never an unmarked
silent omission, never model-authored text. Non-notebook refusals are unchanged.
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


def _write_render(tmp_path: Path):  # type: ignore[no-untyped-def]
    src = parse_percent_source(SOURCE)
    tmpl = parse_percent_source(TEMPLATE)
    view = build_audit_view(src, tmpl, [])
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


def test_prompt_embeds_digest_and_sha12(tmp_path: Path) -> None:
    sv = _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    # The digest block is present with the section's view_sha12 (not the full sha).
    assert "Reviewed render digest" in prompt
    assert sv.view_sha[:12] in prompt
    # The three audit-view digest categories are surfaced.
    assert "classification: modified" in prompt
    assert "diff from template:" in prompt
    assert "assertions declared: 1" in prompt
    assert "lint flags: 0" in prompt
    # The code-selected identifiers ride too (D5).
    assert AUDIT_ID in prompt and SECTION in prompt
    # NOT the fallback.
    assert "render digest unavailable" not in prompt


def test_prompt_is_bounded(tmp_path: Path) -> None:
    sv = _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args(sv.view_sha), tmp_path)
    # A digest is a small dialog, not a full render dump — bounded well under a
    # terminal-scrolling render (RULING 1's reading-ergonomics point).
    assert len(prompt) < 2000
    # The diff BODY never enters the message — only counts (no unbounded echo).
    assert "```diff" not in prompt
    assert "return 99" not in prompt


def test_missing_render_discloses_reason(tmp_path: Path) -> None:
    # No render written → the disk lookup misses; the prompt discloses WHY.
    prompt = M._render_elicitation_prompt(_notebook_args("deadbeefdeadbeef"), tmp_path)
    assert "render digest unavailable" in prompt
    assert "deadbeefdead" in prompt  # the view_sha12 named in the reason
    assert "Read pane" in prompt
    # Still a valid, identifier-bearing prompt (never a crash).
    assert AUDIT_ID in prompt and SECTION in prompt
    assert "Reviewed render digest" not in prompt


def test_stale_render_discloses_reason(tmp_path: Path) -> None:
    # A render exists for the CURRENT view, but the sign-off binds a DIFFERENT
    # view_sha (source drifted) → the content address misses → disclosed fallback.
    _write_render(tmp_path)
    prompt = M._render_elicitation_prompt(_notebook_args("f00df00df00df00d"), tmp_path)
    assert "render digest unavailable" in prompt
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


def test_no_experiment_dir_falls_back_without_crash() -> None:
    # The pure call (no experiment context) still yields a valid notebook prompt.
    prompt = M._render_elicitation_prompt(_notebook_args("abc123def456"))
    assert "render digest unavailable" in prompt
    assert AUDIT_ID in prompt
