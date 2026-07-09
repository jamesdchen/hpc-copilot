"""Conformance kit K6 — negotiation: ``declared == detected == behaved``.

The kit's closing assertion (``docs/design/conformance-kit.md``, "Negotiation" +
``docs/internals/harness-contract.md``, "Capability negotiation"): a harness's
capability set is DETECTED, never a self-asserted manifest, and the three legs
must be ONE set —

* **declared** — the adapter's implemented-method set
  (:func:`~hpc_agent.conformance.adapter.declared_capabilities`);
* **detected** — what the seams observe
  (``adapter.detect_capabilities`` → the ``harness-capabilities`` verb projection
  onto the three contract nouns; ``trusted_display`` is excluded — the projection
  rule);
* **behaved** — the capabilities whose behavior the kit actually exercises.

A drift between any two is the bug the kit exists to catch: a
detected-but-not-behaved capability, or a behaved-but-not-detected one. Honest
partials are NOT failures — an undeclared capability is skipped (its module
degraded-tier), and negotiation fails only on three-way DISAGREEMENT for a
capability the harness DOES claim.

**Which detection leg is a per-harness SEAM vs a core-side CONSTANT** (the
honest-detection rule): ``backgrounding`` detection is a core-side constant
(always true — the detached-worker path is core), so the kit asserts only its
BEHAVED leg, never a per-harness detection; ``utterance-log`` and
``relay-enforcement`` are per-harness SEAMS whose ``declared`` and ``detected``
sets must AGREE.

**The elicitation leg (E7).** MCP elicitation is a second capability-1 channel,
and its negotiation is per-session: *declared* = the client's ``initialize``
``capabilities.elicitation``, *detected* = the server's per-session store
(``McpServer._client_elicitation``), *behaved* = elicitation fires only when the
bit is true (degrade-to-hook otherwise, silently). The detection leg here is the
fake-client ``initialize`` SEAM — NOT the CLI ``harness-capabilities`` probe,
which honestly reports client support as ``"per-session"`` and can never witness
a live negotiation. The duplex rig is consumed (never modified) from
``tests/_mcp_harness.py``; it is ``importorskip``-guarded so the shipped kit
module stays importable when run from the wheel outside the repo.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.conformance.adapter import (
    CAP_RELAY_ENFORCEMENT,
    CAP_UTTERANCE_LOG,
    CAPABILITIES,
    declared_capabilities,
)
from hpc_agent.conformance.test_capability_backgrounding import (
    STUB_TERMINAL_NAME,
    stub_worker_argv,
)
from hpc_agent.state.utterances import read_utterances

if TYPE_CHECKING:
    from hpc_agent.conformance.adapter import HarnessAdapter

# The per-harness SEAM capabilities whose declared and detected sets must agree.
# backgrounding is EXCLUDED: its detection is a core-side constant (always true),
# so only its behaved leg is asserted (the honest-detection rule).
SEAMS: frozenset[str] = frozenset({CAP_UTTERANCE_LOG, CAP_RELAY_ENFORCEMENT})

_BEHAVED_UTTERANCE_PROBE = "conformance negotiation behaved-leg probe"


# ─── the adapter negotiation legs (declared == detected, per seam) ───────────


def test_detected_projects_onto_contract_nouns(
    harness_adapter: HarnessAdapter, fixture_repo: Path
) -> None:
    """Detection reports ONLY the three contract nouns — never the raw four.

    The projection rule: ``harness-capabilities`` reports four capabilities, one
    of which (``trusted_display``) is always ``"unknown"`` and has no kit noun;
    the negotiation set is the projection onto ``{utterance-log,
    relay-enforcement, backgrounding}``.
    """
    detected = harness_adapter.detect_capabilities(fixture_repo)
    assert detected <= CAPABILITIES, (
        f"detect_capabilities leaked a non-contract noun: {detected - CAPABILITIES}"
    )


def test_declared_seam_caps_are_detected(
    harness_adapter: HarnessAdapter, fixture_repo: Path
) -> None:
    """A declared SEAM capability the detection MISSES is behaved-but-not-detected."""
    declared = declared_capabilities(harness_adapter) & SEAMS
    detected = harness_adapter.detect_capabilities(fixture_repo) & SEAMS
    missing = declared - detected
    assert not missing, f"declared but undetected seam capabilities: {sorted(missing)}"


def test_detected_seam_caps_are_declared(
    harness_adapter: HarnessAdapter, fixture_repo: Path
) -> None:
    """A detected SEAM capability the adapter does NOT implement is detected-but-
    not-declared — detection claiming a capability the harness cannot behave."""
    declared = declared_capabilities(harness_adapter) & SEAMS
    detected = harness_adapter.detect_capabilities(fixture_repo) & SEAMS
    extra = detected - declared
    assert not extra, f"detected but undeclared seam capabilities: {sorted(extra)}"


def test_declared_utterance_log_behaves(
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_utterance_log: None,  # noqa: ARG001 — skip-with-tier gate
) -> None:
    """behaved leg for capability 1: a written utterance round-trips through the
    reader — the write channel proves the reader accepts what it wrote."""
    harness_adapter.write_utterance(fixture_repo, _BEHAVED_UTTERANCE_PROBE)
    texts = [record["text"] for record in read_utterances(fixture_repo)]
    assert _BEHAVED_UTTERANCE_PROBE in texts, "write_utterance did not land in the reader's log"


def test_declared_relay_never_blocks_twice(
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_relay_enforcement: None,  # noqa: ARG001 — skip-with-tier gate
) -> None:
    """behaved leg for capability 2: the universal loop-safety invariant a
    conforming ACT seam MUST satisfy regardless of message — a re-entry
    (``previously_blocked=True``) never blocks again (block at most once)."""
    outcome = harness_adapter.run_enforcement_point(
        fixture_repo, "any final agent-visible message", previously_blocked=True
    )
    assert outcome.blocked is False, "a conforming relay seam must never block twice"


def test_declared_backgrounding_behaves(
    harness_adapter: HarnessAdapter,
    fixture_repo: Path,
    require_backgrounding: None,  # noqa: ARG001 — skip-with-tier gate
) -> None:
    """behaved leg for capability 3 (the core-side constant): the stub worker
    wakes the driver and the wake sees the terminal — asserted BEHAVED-only, the
    negotiation set never carries a per-harness backgrounding DETECTION."""
    handle = harness_adapter.start_background(fixture_repo, stub_worker_argv(fixture_repo))
    wake = harness_adapter.await_wake(handle, 30.0)
    assert wake.woke and wake.terminal_seen, "backgrounding declared but did not behave"


# ─── the elicitation leg (E7) — declared == detected == behaved ──────────────
#
# Envelope idioms reused from ``tests/test_mcp_elicitation_firing.py`` (the RIG,
# FakeMcpClient, is consumed from ``tests/_mcp_harness.py`` — never reinvented).


def _envelope(**kw: Any) -> str:
    return json.dumps(kw, sort_keys=True)


def _authorship_refusal() -> str:
    return _envelope(
        ok=False,
        error_code="spec_invalid",
        category="user",
        retry_safe=False,
        message="authorship evidence is missing",
        failure_features={"authorship_evidence": "missing"},
    )


class _ScriptedRunner:
    """A CliRunner that returns a fixed ``(exit, stdout, stderr)`` per call and
    records argv, so a test can count invocations (no retry on the degrade path)."""

    def __init__(self, out: str) -> None:
        self.out = out
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return (1, self.out, "")


def _append_params(experiment_dir: Path | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "scope_kind": "scope",
        "scope_id": "calib-scope-1",
        "block": "scope-unlock",
        "response": "reopen calibration for reanalysis",
        "resolved": {"scope_action": "unlock"},
    }
    args: dict[str, Any] = {"spec": spec}
    if experiment_dir is not None:
        args["experiment_dir"] = str(experiment_dir)
    return {"name": "append-decision", "arguments": args}


def _prime_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the journal home to *tmp_path*, claim the namespace, and seed one
    unrelated utterance — so the scope-unlock rationale (uncovered by the seed) is
    genuinely refused by the REAL authorship gate (the guard-can-fire posture)."""
    from hpc_agent.state.run_record import journal_dir
    from hpc_agent.state.utterances import append_utterance

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    experiment_dir = tmp_path / "repo"
    experiment_dir.mkdir()
    journal_dir(experiment_dir)
    assert append_utterance(experiment_dir, "placeholder unrelated onboarding seed") is not None
    return experiment_dir


def test_elicitation_declared_detected_behaved_when_client_supports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DECLARED (client ``initialize`` elicitation) == DETECTED (the per-session
    store) == BEHAVED (an authorship-refused ``append-decision`` fires
    ``elicitation/create`` and captures the typed sign-off)."""
    harness = pytest.importorskip("tests._mcp_harness")
    from hpc_agent._kernel.extension import mcp_server as mcp
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives
    from hpc_agent.state.utterances import read_utterances as _read

    register_primitives()
    experiment_dir = _prime_namespace(tmp_path, monkeypatch)
    server = mcp.McpServer(
        registry=get_registry(), allow_mutations=True, catalog="curated", runner=None
    )
    typed = "reopen calibration for reanalysis of the drift"
    with harness.FakeMcpClient(server) as client:
        client.initialize(elicitation=True)  # DECLARED
        assert server._client_elicitation is True  # DETECTED (per-session store)
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": _append_params(experiment_dir),
            }
        )
        req = client.recv(timeout=60.0)  # BEHAVED: elicitation fired
        assert req["method"] == "elicitation/create"
        client.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"action": "accept", "content": {"utterance": typed}},
            }
        )
        resp = client.recv(timeout=60.0)
    structured = resp["result"]["structuredContent"]
    assert structured["elicitation"] == "captured"
    assert structured["sha256"] == hashlib.sha256(typed.encode("utf-8")).hexdigest()
    assert _read(experiment_dir)[-1]["text"] == typed


def test_elicitation_absent_when_client_silent() -> None:
    """WITHOUT the declaration: the per-session store is False, NO outbound
    elicitation fires, and the refusal returns directly — the hook-tier degrade
    (behaved-absent). declared == detected == behaved, all three absent."""
    harness = pytest.importorskip("tests._mcp_harness")
    from hpc_agent._kernel.extension import mcp_server as mcp
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    runner = _ScriptedRunner(_authorship_refusal())
    server = mcp.McpServer(
        registry=get_registry(), allow_mutations=True, catalog="curated", runner=runner
    )
    with harness.FakeMcpClient(server) as client:
        client.initialize(elicitation=False)  # NOT declared
        assert server._client_elicitation is False  # detected absent
        client.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": _append_params()})
        resp = client.recv(timeout=10.0)
    assert "method" not in resp, "an elicitation/create fired despite no client declaration"
    structured = resp["result"]["structuredContent"]
    assert structured["ok"] is False
    assert "elicitation" not in structured  # the original refusal, unchanged
    assert len(runner.calls) == 1  # no retry — the degrade path


def test_harness_capabilities_reports_elicitation_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The separate-process probe stays HONEST: it reports the server-side bit it
    can verify (``elicitation_server`` True) and refuses to assert client support
    it cannot witness (``elicitation_client`` ``"per-session"``, not ``yes``)."""
    from hpc_agent.ops.harness_capabilities import harness_capabilities

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    result = harness_capabilities(experiment_dir=tmp_path / "repo")
    evidence = result.capabilities["utterance_log"].evidence
    assert evidence["elicitation_server"] is True
    assert evidence["elicitation_client"] == "per-session"


# Referenced so a stale rename of the rendezvous constant fails loudly here too
# (the mirror and the reference adapter both key off this name).
assert STUB_TERMINAL_NAME == "stub_worker.terminal.json"
