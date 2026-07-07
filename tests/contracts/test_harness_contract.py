"""Contract: the harness-contract doc + the utterance-log write-API surface.

``docs/internals/harness-contract.md`` is the NORMATIVE spec a second conforming
harness (the scheduled v1.5 jupytext render) implements against — the
vendor-lock-in defense (``docs/design/notebook-audit.md``, "THE HARNESS
CONTRACT"). Its load-bearing invariants are prose + an importable-only API with
no single lint that holds them, so — the drift-guard philosophy of
``test_notebook_audit_skill_guidance`` / ``test_authorship_elicitation_guidance``
— this binds:

* the doc names the three capabilities, the friction-tier degrade seam, the
  no-scaffold + human-typed provenance, and the FROZEN write-API record schema;
* ``state/utterances.py``'s ``__all__`` carries the four API names a harness
  imports (the reader + writer + locator + cap);
* NO primitive / CLI verb writes an utterance — the lock-1 posture: appending is
  the harness's exclusive out-of-band act, never a call the LLM can make.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / "docs/internals/harness-contract.md"


def _doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


def test_harness_contract_doc_exists() -> None:
    assert _DOC.is_file(), f"{_DOC} must exist — the normative harness spec"


def test_doc_names_the_three_capabilities() -> None:
    text = _doc_text().lower()
    # The three capabilities the notebook-audit HARNESS CONTRACT is defined against.
    assert "utterance log" in text or "utterance-log" in text, (
        "capability 1: the out-of-band human-utterance log must be named"
    )
    assert "relay" in text and ("verbatim" in text or "enforcement" in text), (
        "capability 2: the relay/verbatim enforcement point must be named"
    )
    assert "background" in text and "wake" in text, (
        "capability 3: backgrounding/wake for detached waits must be named"
    )


def test_doc_names_the_friction_degrade_seam() -> None:
    """Capability 1 absent degrades to the journal-response friction tier at the
    EXACT code seam ``_harness_human_texts`` returning None (the honest tier)."""
    text = _doc_text()
    assert "_harness_human_texts" in text, (
        "the doc must name the degrade seam ops/decision/journal.py::_harness_human_texts"
    )
    assert "friction" in text.lower(), "the journal-response friction tier must be named"


def test_doc_pins_no_scaffold_and_human_typed_provenance() -> None:
    text = _doc_text().lower()
    assert "no-scaffold" in text or "no scaffold" in text, (
        "the no-scaffold precondition (write only into an existing namespace) must be pinned"
    )
    assert "human-typed" in text or "human typed" in text, (
        "the human-TYPED-only provenance contract must be pinned"
    )
    # The reference provenance filters a second harness IMPORTS (the
    # injection filter is the public write-API symbol, one definition).
    assert "is_harness_injected" in text, "the harness-injection reference filter must be cited"
    assert "_is_clicked" in text, "the clicked-option reference filter must be cited"


def test_doc_pins_the_frozen_write_api_schema() -> None:
    """The frozen record schema a second harness implements byte-for-byte."""
    text = _doc_text()
    for field in ("ts", "sha256", "text"):
        assert field in text, f"the frozen record schema must name the {field!r} field"
    assert "MAX_UTTERANCE_BYTES" in text, "the per-entry cap must be named"
    assert "utterances.jsonl" in text, "the storage locator filename must be named"
    assert "repo_hash" in text and "_current_homedir" in text, (
        "the locator derivation (_current_homedir + repo_hash) must be pinned"
    )
    assert "sorted" in text.lower() and "append-only" in text.lower(), (
        "sorted-keys JSON + append-only must be pinned"
    )
    assert "codepoint" in text.lower(), "the codepoint-boundary truncation must be pinned"
    assert "fail-open" in text.lower(), "fail-open (error → clean no-op) must be pinned"


def test_utterances_all_carries_the_api_names() -> None:
    """The harness imports these six: writer, reader, locator, cap, and the
    two forms of the provenance filter (the ONE public injection-filter
    definition every conforming writer shares — never a re-derived copy)."""
    from hpc_agent.state import utterances

    for name in (
        "append_utterance",
        "read_utterances",
        "utterances_path",
        "MAX_UTTERANCE_BYTES",
        "HARNESS_INJECTION_RE",
        "is_harness_injected",
    ):
        assert name in utterances.__all__, f"{name!r} must be in state.utterances.__all__"
        assert hasattr(utterances, name), f"{name!r} must be importable from state.utterances"


def test_injection_filter_has_one_definition() -> None:
    """The reference filter is defined ONCE (state.utterances); the Claude Code
    hook and the notebook-render plugin both route through it — a re-derived
    regex copy is the drift channel this pin closes."""
    import inspect

    from hpc_agent._kernel.hooks import utterance_capture
    from hpc_agent.state import utterances

    hook_src = inspect.getsource(utterance_capture)
    assert "is_harness_injected" in hook_src, (
        "utterance_capture must route through state.utterances.is_harness_injected"
    )
    assert "re.compile" not in hook_src, (
        "utterance_capture must not carry its own filter regex (one definition)"
    )
    assert utterances.is_harness_injected("<task-notification>x")
    assert utterances.is_harness_injected("  <system-reminder> hi")
    assert not utterances.is_harness_injected("sign construction — quoting a <tag> mid-text")


def test_no_utterance_writing_verb_in_registry() -> None:
    """Lock 1 (no affordance): NO primitive is named like an utterance writer — the
    LLM must never gain a sanctioned write call (the harness writes out-of-band,
    or nothing). Mirrors ``test_no_signoff_affordance_in_registry``."""
    from tests._registry_helpers import core_only_registry

    offenders = [name for name in core_only_registry() if "utterance" in name.lower()]
    assert offenders == [], f"an utterance-writing verb leaked into the registry: {offenders}"
