"""Fire-path tests for ``lint_skill_mcp_reachability``.

The lint mechanizes the architect memo §1 coupling: a verb a SKILL body names as
MCP-direct MUST be reachable from the curated MCP catalog, or the agent drops to
the CLI and hand-rolls the call (the run-#8 unreachable-verb class). Per the
engineering principle "every lint rule must demonstrate its fire path", these
pin BOTH that the real tree passes AND that a synthetic MCP-direct-but-unreachable
verb is a hard error — the detection markers, the registry filter, and the
reachability check each exercised.
"""

from __future__ import annotations

import importlib.util
import sys

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_skill_mcp_reachability", REPO_ROOT / "scripts" / "lint_skill_mcp_reachability.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_skill_mcp_reachability"] = lint
_SPEC.loader.exec_module(lint)


# ─── the real tree passes ────────────────────────────────────────────────────


def test_real_tree_is_reachable() -> None:
    """Every MCP-direct verb the shipped SKILLs name is curated-reachable —
    the state this unit's ``_CURATED_EXTRA_VERBS`` additions (read-decisions,
    verify-relay, attention-queue, revise-resolved) establish."""
    assert lint.main() == 0


# ─── detection: the two markers, per-line and fence-aware ────────────────────


def test_inline_tag_marker_detected() -> None:
    body = "On a nudge, call `revise-resolved` (MCP-direct) — never hand-edit a spec."
    assert lint.find_mcp_direct_verbs(body) == {"revise-resolved"}


def test_inline_tag_tolerates_reordered_and_prose_parenthetical() -> None:
    # hpc-status phrases attention-queue's tag as "(read-only MCP, direct — …)":
    # the parenthetical mentions MCP and direct in the other order, with prose.
    body = "`attention-queue` (read-only MCP, direct — no spec-file round-trip) is the digest."
    assert lint.find_mcp_direct_verbs(body) == {"attention-queue"}


def test_direct_through_mcp_enumeration_detected() -> None:
    body = (
        "- Read-only QUERY verbs go DIRECT through MCP: `status-snapshot`, "
        "`read-decisions`, `verify-relay` are pure reads: call the typed MCP tool."
    )
    assert lint.find_mcp_direct_verbs(body) == {
        "status-snapshot",
        "read-decisions",
        "verify-relay",
    }


def test_non_verb_backtick_tokens_ignored() -> None:
    # Spans carrying spaces / slashes / dots / leading dashes are not verb tokens.
    body = (
        "go DIRECT through MCP — do NOT `Write` a `.hpc/specs/*.json` file and shell "
        "`hpc-agent <verb> --spec …` or pass `--spec`."
    )
    # `Write` fails the lowercase-leading rule; the rest carry non-token chars.
    assert lint.find_mcp_direct_verbs(body) == set()


def test_fenced_code_block_is_skipped() -> None:
    body = "\n".join(
        [
            "Some prose.",
            "```bash",
            "hpc-agent `ghost-verb` (MCP-direct)  # example only",
            "```",
            "call `read-decisions` (MCP-direct) for real.",
        ]
    )
    # The fenced example does not count; only the live directive does.
    assert lint.find_mcp_direct_verbs(body) == {"read-decisions"}


def test_plain_backtick_verb_without_marker_is_not_detected() -> None:
    body = "The `submit-flow` verb runs the whole chain."
    assert lint.find_mcp_direct_verbs(body) == set()


# ─── check(): reachability, pure with injected sets ──────────────────────────


def test_check_fires_on_unreachable_mcp_direct_verb() -> None:
    errors = lint.check(
        {"skills/fake/SKILL.md": "call `foo-read` (MCP-direct) now"},
        registry_verbs={"foo-read"},
        curated_verbs=set(),
    )
    assert len(errors) == 1
    assert "foo-read" in errors[0]
    assert "curated" in errors[0]


def test_check_clean_when_reachable() -> None:
    errors = lint.check(
        {"skills/fake/SKILL.md": "call `foo-read` (MCP-direct) now"},
        registry_verbs={"foo-read"},
        curated_verbs={"foo-read"},
    )
    assert errors == []


def test_check_ignores_non_registry_tokens() -> None:
    # A backtick token that passes the verb-shape regex but is not a primitive
    # (an example, the binary name) is out of scope — never a reachability error.
    errors = lint.check(
        {"skills/fake/SKILL.md": "`hpc-agent` verbs go DIRECT through MCP"},
        registry_verbs=set(),
        curated_verbs=set(),
    )
    assert errors == []


# ─── end-to-end fire path against the LIVE registry + curated catalog ────────


def test_main_fires_for_a_live_registry_verb_left_uncurated(monkeypatch, capsys) -> None:
    """A synthetic SKILL naming a REAL registry query verb that is deliberately
    NOT curated (``scope-status`` — the code comment keeps it out of the curated
    set) as MCP-direct trips ``main`` through the live registry + curated sources.
    """
    monkeypatch.setattr(
        lint,
        "_load_skill_texts",
        lambda: {"skills/synthetic/SKILL.md": "call `scope-status` (MCP-direct)"},
    )
    assert lint.main() == 1
    err = capsys.readouterr().err
    assert "scope-status" in err
    assert "skills/synthetic/SKILL.md" in err
