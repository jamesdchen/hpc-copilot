"""B4 route-through contract — every unbounded utterance-pool consumer in
``ops/decision/journal.py`` either routes through the shared ``ts >= anchor``
filter (:func:`_fresh_human_texts` / :func:`_fresh_authored_text`) or carries a
documented exemption.

The B4 exploit (philosophy-audit 2026-07 sweep log) is that an authorship gate
reads the WHOLE utterance log with no temporal anchor, so the utterance that
CREATED a target permanently satisfies the gate's naming leg. The five exposed
gates were routed through the shared filter; this test is the never-fires
assertion that a NEW gate cannot ship reading the unbounded pool without a
deliberate, documented decision.

Enforced as an AST scan over the module source: any function that calls an
UNFILTERED pool reader must be on the exemption allowlist below (with a reason)
— a filtered gate calls ``_fresh_*`` instead and never trips the scan.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

from hpc_agent.ops.decision import journal


def _journal_pkg_source() -> str:
    """The CONCATENATED source of every ``journal`` PACKAGE submodule.

    ``journal`` is now a package (``ops/decision/journal/``), so
    ``inspect.getsource(journal)`` would return only ``__init__.py`` and the B4
    route-through scan would go VACUOUS — passing without seeing a single gate.
    Concatenate every submodule instead so the AST scan sees the whole gate
    surface (the ``from __future__`` lines are stripped so the concatenation stays
    a parseable module).
    """
    pkg_dir = pathlib.Path(journal.__file__).parent
    parts: list[str] = []
    for py in sorted(pkg_dir.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        text = "\n".join(
            line for line in text.splitlines() if not line.startswith("from __future__")
        )
        parts.append(text)
    return "\n\n".join(parts)


# The UNFILTERED readers: calling one reads the utterance log with NO temporal
# anchor. ``read_utterances`` is the raw store read; the other three are the base
# text readers built directly on it (``_fresh_human_texts`` is the SANCTIONED
# filter and is itself exempt below).
_UNFILTERED_READERS = frozenset(
    {
        "read_utterances",
        "_harness_human_texts",
        "_actor_scoped_human_texts",
        "_registration_authored_text",
    }
)

# name -> documented reason. A consumer here reads the unbounded pool BY DESIGN.
# The five fixed gates are NOT here — they call ``_fresh_human_texts`` /
# ``_fresh_authored_text`` and so never appear as unfiltered consumers.
_EXEMPT: dict[str, str] = {
    "_fresh_human_texts": (
        "THE shared ts>=anchor filter itself — reads the raw store, then applies the anchor"
    ),
    "_harness_human_texts": "base unfiltered store reader the filter and scoped reader build on",
    "_actor_scoped_human_texts": (
        "base actor-scoped text reader; the anchored callers supply the anchor"
    ),
    "_registration_authored_text": (
        "base text reader for the sha-prefix-bound FILING gates — an 8+ hex "
        "prefix cannot pre-exist the artifact it fingerprints (temporal binding "
        "by vocabulary impossibility; B4 sweep ALIGNED row)"
    ),
    "_assert_human_authorship": (
        "field-derivation semantics, not attestation — the kickoff prompt stating "
        "the goal IS the intended standing evidence (B4 sweep field-ownership "
        "ALIGNED row)"
    ),
    "_assert_registration_full": "R6 sha-prefix leg (prerequisite content_sha) — B4 ALIGNED",
    "_assert_registration_review_floor": "R6 sha-prefix leg (dossier sha) — B4 ALIGNED",
    "_assert_conformance_verdict_authorship": "sha-prefix leg (cited receipt sha) — B4 ALIGNED",
    "_assert_reproduction_verdict_authorship": "sha-prefix leg (sample content_sha) — B4 ALIGNED",
    "_assert_conclusion_full": "E-shape sha-prefix leg (cited sha) — B4 ALIGNED",
    "_assert_challenge_filing_full": "C-gate sha-prefix leg (target + cited sha) — B4 ALIGNED",
    "_bound_consent_records": (
        "bound-capture reader (USER RULING 3, 2026-07-12): selects ONLY utterances "
        "carrying an exact ``bound`` binding a view-aware surface wrote — the chat "
        "hook cannot forge one, so the B4 ts>=anchor exploit (a creation utterance "
        "permanently satisfies a NAMING leg) cannot apply. Same temporal-binding-by-"
        "vocabulary-impossibility class as the sha-prefix FILING gates."
    ),
}


def _function_calls(node: ast.FunctionDef) -> set[str]:
    """Every called-function short name inside *node* (Name.id or Attribute.attr)."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _unfiltered_consumers(source: str) -> set[str]:
    """Names of top-level functions that call any unfiltered pool reader."""
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and _function_calls(node) & _UNFILTERED_READERS:
            hits.add(node.name)
    return hits


def test_every_unbounded_consumer_is_filtered_or_exempt() -> None:
    consumers = _unfiltered_consumers(_journal_pkg_source())
    offenders = sorted(consumers - set(_EXEMPT))
    assert not offenders, (
        "unbounded read_utterances consumer(s) in ops/decision/journal.py must "
        "route through the shared ts>=anchor filter (_fresh_human_texts / "
        "_fresh_authored_text) or be added to the documented B4 exemption "
        f"allowlist with a reason: {offenders}"
    )


def test_every_exemption_reason_is_nonempty() -> None:
    empty = sorted(name for name, reason in _EXEMPT.items() if not reason.strip())
    assert not empty, f"B4 exemption(s) missing a documented reason: {empty}"


def test_route_through_guard_fires_on_synthetic_consumer() -> None:
    """A1 guard-can-fire: a new gate reading the raw pool IS caught by the scan
    and is NOT pre-exempted, so the contract test above would fail on it."""
    synthetic = textwrap.dedent(
        """
        def _assert_new_gate(experiment_dir, spec, resolved):
            texts = read_utterances(experiment_dir)
            return texts
        """
    )
    hits = _unfiltered_consumers(synthetic)
    assert "_assert_new_gate" in hits
    assert "_assert_new_gate" not in _EXEMPT
