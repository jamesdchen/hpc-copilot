"""Test ``scripts/lint_primitive_doc_templates.py``.

Constructs synthetic doc bodies that violate the agent_facing /
template alignment, then asserts the linter classifier flags them.
Doesn't subprocess-run the script — just exercises its
``_classify_body`` heuristic directly so the test stays fast and
doesn't depend on the registry round-trip.
"""

from __future__ import annotations

import importlib.util

from tests._paths import REPO_ROOT


def _load_lint_module():
    """Import scripts/lint_primitive_doc_templates.py without running main()."""
    path = REPO_ROOT / "scripts" / "lint_primitive_doc_templates.py"
    spec = importlib.util.spec_from_file_location("_lint_doc_templates_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_lint = _load_lint_module()


def test_classify_body_detects_agent_facing_template() -> None:
    body = """
# my-primitive

A short description.

## Inputs

- foo: bar

## Outputs

`{ok, data}`

## Errors

- spec_invalid

## Idempotency

Yes.
"""
    agent, internal = _lint._classify_body(body)
    assert agent >= 4, f"expected ≥4 agent-facing headers, got {agent}"
    assert internal == 0, f"expected 0 internal headers, got {internal}"


def test_classify_body_detects_internal_template() -> None:
    body = """
# my-primitive

A short description.

## Composers

- workflow-x
- workflow-y

## Invariants

- Pure read.
- No mutation.

## Coupling

The shape mirrors X.

## Failure modes

- Empty input → error.
"""
    agent, internal = _lint._classify_body(body)
    assert agent == 0, f"expected 0 agent-facing headers, got {agent}"
    assert internal >= 4, f"expected ≥4 internal headers, got {internal}"


def test_is_stub_recognises_documentation_pending_marker() -> None:
    stub = "# foo\n\n_Documentation pending._\n"
    assert _lint._is_stub(stub)


def test_is_stub_recognises_short_bodies() -> None:
    short = "# foo\n\none paragraph.\n"
    assert _lint._is_stub(short)


def test_classify_body_mixed_template_does_not_lean_strongly() -> None:
    """A body with one agent header and one internal header is a tie;
    the linter's ``leaning_*`` checks use strict ``>``, so ties are
    lenient (no warning fires). This test pins the lenient-on-tie
    behavior so a future heuristic change doesn't accidentally start
    crying wolf on hybrid docs."""
    body = """
# x

## Inputs

- foo

## Composers

- bar
"""
    agent, internal = _lint._classify_body(body)
    assert agent == internal == 1


def test_strip_frontmatter_removes_yaml_block() -> None:
    text = """---
name: foo
verb: query
---
# foo

Body.
"""
    stripped = _lint._strip_frontmatter(text)
    assert "name: foo" not in stripped
    assert "# foo" in stripped


def test_lint_passes_on_real_repo() -> None:
    """Smoke: the linter exits 0 on the current repo state.

    A future PR that adds a primitive without docs (or with the
    wrong template) trips this test the same way it would trip the
    pre-commit gate.
    """
    rc = _lint.main()
    assert rc == 0, "lint_primitive_doc_templates exits non-zero on current repo"
