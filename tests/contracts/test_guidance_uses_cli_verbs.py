"""Contract: agent/user-facing guidance PROSE speaks CLI verbs, not registry names.

Run-#12 finding 22: framework guidance told a relay agent to run
``reconcile-journal`` (the *registry* name); the CLI only knows the verb
``reconcile`` (``_verb_module_map`` binds them). ``dispatch reconcile-journal``
said "unknown command" while ``describe reconcile-journal --schema`` errored
differently — two contradictory surfaces, two burned round-trips. The
CLI-verbs-over-Python-internals doctrine (#200) says agent-facing prose must
speak the invocable CLI name.

This lint scans the known offender modules' string LITERALS for any registry
primitive name whose CLI verb differs (the set is *derived* from the one shared
alias map, :func:`hpc_agent.cli._verb_aliases.differing_registry_names`, so the
lint and the describe/dispatch resolvers can never drift). A match inside a
prose literal fails CI; the fix is to write the CLI verb instead.

Scoping choice (why it does not false-positive on legitimate registry-name
mentions):

* **Comments** are invisible to the AST, so they are excluded for free — a
  ``# the composed reconcile-journal touches the cluster`` note never fires.
* **Docstrings** (module / class / function) legitimately DISCUSS the registry
  graph (``composes=["reconcile-journal"]`` explained in prose); they are
  detected structurally and excluded.
* **Bare single-token references** — a string literal whose entire content IS
  the registry name — are structural addresses of the primitive by its
  registry name (``composes=["reconcile-journal"]``, an ``{"action":
  "reconcile-journal"}`` recommendation value the registry resolves), not human
  guidance. They are excluded; the registry graph is keyed by registry name.

What remains — the registry name embedded in a multi-word natural-language
string ("Run reconcile-journal to confirm ...") — is exactly the guidance that
misleads an agent, and is what this lint flags.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from hpc_agent.cli._verb_aliases import differing_registry_names

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "hpc_agent"

# The modules whose agent/user-facing guidance strings historically named the
# registry primitive (finding 22). The lint pins them so a reintroduction fails.
_OFFENDER_MODULES: tuple[Path, ...] = (
    _SRC / "ops" / "campaign_run.py",
    _SRC / "ops" / "status_pipeline.py",
    _SRC / "ops" / "status_blocks.py",
    _SRC / "_wire" / "workflows" / "status_blocks.py",
)


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """The id() of every module/class/function docstring Constant node."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _name_token_re(name: str) -> re.Pattern[str]:
    """Match *name* as a whole hyphenated token (not a fragment of a longer id)."""
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")


def prose_violations(source: str, label: str) -> list[str]:
    """Return one message per differing-registry-name found in a prose literal.

    A prose literal is a non-docstring string constant that is more than a bare
    single-token registry reference. Reusable so the fire-path test can feed it
    synthetic source instead of a real file.
    """
    needles = differing_registry_names()
    tree = ast.parse(source, filename=label)
    docstrings = _docstring_node_ids(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        text = node.value
        stripped = text.strip()
        if stripped in needles:
            # Bare single-token structural reference (composes=/action:) — the
            # primitive is legitimately addressed by its registry name here.
            continue
        for name in needles:
            if _name_token_re(name).search(text):
                violations.append(
                    f"{label}:{node.lineno}: guidance literal names registry "
                    f"primitive {name!r}; write its CLI verb instead "
                    f"(see _verb_aliases.registry_name_to_cli_verb()[{name!r}])"
                )
                break
    return violations


def test_scan_is_not_vacuous() -> None:
    """The needle set must contain the finding's subject — else the scan is a no-op."""
    assert "reconcile-journal" in differing_registry_names(), (
        "reconcile-journal must be a differing registry name (CLI verb "
        "`reconcile`) derived from VERB_MODULE_MAP — the lint pins nothing "
        "if the alias map lost it"
    )


def test_offender_modules_speak_cli_verbs() -> None:
    for module in _OFFENDER_MODULES:
        source = module.read_text(encoding="utf-8")
        label = str(module.relative_to(_REPO_ROOT)).replace("\\", "/")
        violations = prose_violations(source, label)
        assert not violations, (
            "agent-facing guidance must name CLI verbs, not registry primitive "
            "names (run-#12 finding 22):\n" + "\n".join(violations)
        )


def test_lint_fires_on_a_reintroduced_registry_name() -> None:
    """Fire path: a prose literal naming the registry name is flagged (mutation)."""
    mutated = '''
"""Module docstring may mention reconcile-journal freely."""
REASON = "Run reconcile-journal to confirm before re-submitting."
'''
    violations = prose_violations(mutated, "synthetic")
    assert any("reconcile-journal" in v for v in violations), (
        "the lint must flag a registry name embedded in a prose string literal"
    )


def test_lint_ignores_docstrings_comments_and_bare_tokens() -> None:
    """Scoping: docstrings, comments, and bare structural tokens do not fire."""
    clean = '''
"""Docstring discussing composes=["reconcile-journal"] is fine."""
COMPOSES = ["reconcile-journal"]  # bare token: structural registry reference
RECOMMEND = {"action": "reconcile-journal"}
REASON = "Run `reconcile --run-id <id>` to confirm."  # comment: reconcile-journal
'''
    assert prose_violations(clean, "synthetic") == [], (
        "bare-token registry references, docstrings, and comments are legitimate and must not fire"
    )
