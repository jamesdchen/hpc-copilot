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


def test_doc_pins_the_sha_canonicalization() -> None:
    """The normative sha canonicalization must be specified — a second
    implementation recomputes view/story/content shas byte-for-byte or every
    recompute lock reads drift. The section names the JSON form, the digest,
    the source-text normalization reference, and the versioned escape hatch
    (never a silent canonicalization change)."""
    text = _doc_text()
    assert "sort_keys" in text, "the JSON canonical form (sort_keys) must be pinned"
    assert "sha-256" in text.lower(), "the digest algorithm must be named"
    assert "normalize_source" in text, "the source-text normalization reference must be cited"
    assert "canon_version" in text, "the versioned escape hatch must be recorded"
    assert "8785" in text, "the deliberate divergence from RFC 8785/JCS must be recorded"


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


def test_doc_pins_capability_negotiation() -> None:
    """Capability negotiation is DETECTION, not a self-asserted manifest: the
    declaration is what code can verify, the `harness-capabilities` verb is the
    surface, it reads named seams, and the conformance kit asserts the three
    stay aligned (declared == detected == behaved)."""
    text = _doc_text()
    assert "Capability negotiation" in text, "the negotiation section must be named"
    assert "harness-capabilities" in text, "the detection verb must be named"
    assert "declaration IS what code can verify" in text, (
        "the detection-not-manifest posture must be pinned"
    )
    assert "_find_hook_entry_index" in text, (
        "the ONE canonical entry-matcher the detection reuses must be cited"
    )
    assert "declared == detected == behaved" in text, (
        "the conformance-kit alignment claim must be pinned"
    )


def test_doc_pins_mcp_elicitation_implemented() -> None:
    """MCP elicitation is a SECOND capability-1 channel, IMPLEMENTED by reference
    (2026-07-08): the doc cites the real pump/handler symbols, the clicked-option
    hazard applies (only free-text lands), the prompt MUST be code-rendered
    (never LLM-authored), the honest server flag records it, client support is
    per-session negotiation, and absent capability degrades to the hook path."""
    text = _doc_text()
    lower = text.lower()
    assert "elicitation" in lower, "the elicitation channel must be named"
    assert "specified, not implemented" not in lower, (
        "the specified-not-implemented posture retired when the pump landed"
    )
    assert "specified but not implemented" not in lower, (
        "the specified-not-implemented posture retired when the pump landed"
    )
    assert "clicked-option hazard" in lower or "clicked option" in lower, (
        "the clicked-option hazard (only free-text qualifies) must be pinned"
    )
    assert "code-rendered" in lower, (
        "the code-rendered (never LLM-authored) prompt provenance rule must be pinned"
    )
    assert "ELICITATION_SERVER_IMPLEMENTED" in text, (
        "the honest server capability flag must be named"
    )
    assert "_request_from_client" in text, (
        "the implemented-by-reference citation of the wait primitive must be present"
    )
    assert "_render_elicitation_prompt" in text, "the code-rendered prompt symbol must be cited"
    assert "_elicit_then_retry" in text, "the retry-once firing symbol must be cited"
    assert "per-session" in lower, "the per-session client-negotiation posture must be pinned"
    assert "degrades to the hook path" in lower, "the degrade-to-hook-path fallback must be pinned"


def test_doc_pins_capability_2_inspect_act_split() -> None:
    """Capability 2 splits into INSPECT (OTel GenAI semantic conventions as the
    observable-output ride) and ACT (harness hooks OR a response gateway LLM proxy
    applying verify_relay before delivery)."""
    text = _doc_text()
    assert "INSPECT" in text and "ACT" in text, "the INSPECT / ACT split must be named"
    assert "OpenTelemetry GenAI" in text or "OTel GenAI" in text, (
        "the OTel GenAI semantic-conventions inspect ride must be cited"
    )
    assert "RESPONSE GATEWAY" in text or "response gateway" in text, (
        "the response-gateway (LLM proxy) ACT implementation must be named"
    )
    assert "verify_relay" in text or "verify-relay" in text, (
        "the gateway applying verify_relay before delivery must be pinned"
    )


def test_mcp_server_elicitation_flag_is_true_and_backed() -> None:
    """The elicitation channel is implemented: the server capability flag the
    harness-capabilities verb reads is True, and — the honesty condition for the
    flip — the bidirectional machinery it asserts actually exists on the server
    class (the wait primitive, the code-rendered prompt, the retry-once firing
    site). The flag may never outrun the code."""
    from hpc_agent._kernel.extension import mcp_server

    assert mcp_server.ELICITATION_SERVER_IMPLEMENTED is True, (
        "ELICITATION_SERVER_IMPLEMENTED flipped True with the bidirectional pump; "
        "it must stay honest — False again only if the pump is removed"
    )
    assert "ELICITATION_SERVER_IMPLEMENTED" in mcp_server.__all__
    assert hasattr(mcp_server.McpServer, "_request_from_client"), (
        "the flag asserts a wait primitive that must exist"
    )
    assert hasattr(mcp_server.McpServer, "_elicit_then_retry"), (
        "the flag asserts a firing site that must exist"
    )
    assert callable(mcp_server._render_elicitation_prompt), (
        "the flag asserts a code-rendered prompt builder that must exist"
    )


def test_no_utterance_writing_verb_in_registry() -> None:
    """Lock 1 (no affordance): NO primitive is named like an utterance writer — the
    LLM must never gain a sanctioned write call (the harness writes out-of-band,
    or nothing). Mirrors ``test_no_signoff_affordance_in_registry``."""
    from tests._registry_helpers import core_only_registry

    offenders = [name for name in core_only_registry() if "utterance" in name.lower()]
    assert offenders == [], f"an utterance-writing verb leaked into the registry: {offenders}"


def test_no_agent_facing_utterance_writer_including_plugins() -> None:
    """Lock 1, extended to INSTALLED PLUGINS (adversarial review F1): a plugin verb
    can reach ``append_utterance`` too (the notebook-render ``notebook-ingest-
    signoffs`` does), and the core name-scan cannot see it. Scan the FULL registry
    (core + any installed plugin primitives) and refuse any AGENT-FACING primitive
    whose implementation module CALLS ``append_utterance`` — an agent-reachable
    utterance writer is the exact affordance the write-API lock forbids, no matter
    which lane it ships in. Non-agent-facing (human-invoked) writers are allowed:
    the ingest verb is agent_facing=False, so the human still runs it out-of-band.

    A no-op in core-only CI (no core primitive calls append_utterance); it fires
    only when a plugin that does is installed — the setting the plugin test suite
    exercises after an editable install.
    """
    import inspect

    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    offenders: list[str] = []
    for name, meta in get_registry().items():
        if not meta.agent_facing:
            continue
        module = inspect.getmodule(meta.func)
        if module is None:
            continue
        try:
            src = inspect.getsource(module)
        except (OSError, TypeError):
            continue
        if "append_utterance(" in src:
            offenders.append(name)
    assert offenders == [], (
        "an AGENT-FACING primitive reaches the utterance-log writer "
        f"(append_utterance): {offenders}. The LLM must never gain a sanctioned "
        "utterance write — make the verb agent_facing=False (human-invoked) as the "
        "notebook-ingest-signoffs plugin verb does."
    )
