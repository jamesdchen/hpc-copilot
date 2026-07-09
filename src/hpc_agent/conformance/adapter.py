"""The harness adapter interface (D-K2) — stdlib-only, pytest-free.

A conforming harness supplies an ADAPTER that drives its three capability
providers through one seam the kit parameterizes over. The kit asserts
OUTCOMES (``EnforcementOutcome`` / ``WakeEvent``), never mechanisms — a hook-
shaped harness and a response-gateway-shaped harness certify through the SAME
adapter surface (``docs/design/conformance-kit.md``, D-K2/D-K3).

Detection-only doctrine: there is NO ``capabilities:`` manifest field on an
adapter. "Declared" == the set of Protocol methods the adapter ACTUALLY
implements (:func:`declared_capabilities`) — implementing the callable IS the
declaration. The three capability names are the contract nouns
:data:`CAPABILITIES`.

This module is importable WITHOUT pytest (the D-K1 boundary, pinned by
``tests/contracts/test_conformance_kit_boundary.py``): stdlib only.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CAPABILITIES",
    "CAP_BACKGROUNDING",
    "CAP_RELAY_ENFORCEMENT",
    "CAP_UTTERANCE_LOG",
    "DEGRADED_TIERS",
    "EnforcementOutcome",
    "HarnessAdapter",
    "WakeEvent",
    "declared_capabilities",
    "default_detect_capabilities",
    "skip_reason_for",
]

# --- the three contract nouns (docs/internals/harness-contract.md §"three
# capabilities"). These strings are the kit's capability vocabulary; the
# negotiation set (K6) is the projection of the ``harness-capabilities`` verb
# onto exactly these three.
CAP_UTTERANCE_LOG = "utterance-log"
CAP_RELAY_ENFORCEMENT = "relay-enforcement"
CAP_BACKGROUNDING = "backgrounding"

CAPABILITIES: frozenset[str] = frozenset(
    {CAP_UTTERANCE_LOG, CAP_RELAY_ENFORCEMENT, CAP_BACKGROUNDING}
)

# Which Protocol method(s) DECLARE each capability. An adapter declares a
# capability by implementing ALL of its methods (a partial harness simply omits
# them — the honest-partial posture, not a manifest opt-out).
_CAPABILITY_METHODS: dict[str, tuple[str, ...]] = {
    CAP_UTTERANCE_LOG: ("write_utterance",),
    CAP_RELAY_ENFORCEMENT: ("run_enforcement_point",),
    CAP_BACKGROUNDING: ("start_background", "await_wake"),
}

# The contract-named degraded tier each capability collapses to when ABSENT.
# The report lists a skip WITH its tier verbatim — never rounds partial up to
# conforming, never invents a tier the contract lacks (D-K6 / the boundary-drift
# "skips stay honest" flag). Wording tracks harness-contract.md's "Degrades when
# absent" clauses.
DEGRADED_TIERS: dict[str, str] = {
    CAP_UTTERANCE_LOG: "journal-response friction tier",
    CAP_RELAY_ENFORCEMENT: "verb-only relay-audit posture",
    CAP_BACKGROUNDING: "synchronous in-turn execution; correctness unaffected",
}


class EnforcementOutcome(NamedTuple):
    """The outcome of running a harness's relay ACT seam over one final message."""

    blocked: bool  # the harness forced a continuation
    reason: str | None  # itemized mismatch summary when blocked


class WakeEvent(NamedTuple):
    """The outcome of a detached-worker wake."""

    woke: bool  # the driver was re-invoked after detach
    terminal_seen: bool  # the wake observed the worker's terminal record


class HarnessAdapter(Protocol):
    """The seam a conforming harness implements so the kit can drive it.

    The kit calls ONLY these methods; a harness maps each onto its own
    capability provider (hooks, a response gateway, an MCP elicitation channel,
    a notebook render, ...). ``answer_question`` is OPTIONAL — the kit skips the
    clicked-vs-typed provenance assertions when it is absent.
    """

    name: str  # the harness's published name (report identity)

    # --- capability 1: the out-of-band utterance channel ---
    def write_utterance(self, experiment_dir: Path, text: str) -> None:
        """Deliver *text* through YOUR harness's human-input channel
        end-to-end, exactly as if a human typed it — so the record (if any)
        lands via your writer, filters included. The kit never writes the
        log directly; it drives your channel and reads the log back through
        ``state/utterances.py::read_utterances``."""

    # --- capability 2: the relay enforcement point (ACT) ---
    def run_enforcement_point(
        self, experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        """Run YOUR enforcement seam over *final_message* as the final
        agent-visible text for the cwd repo. Hook-shaped harnesses replay
        their Stop seam; gateway-shaped harnesses run their pre-delivery
        ``verify_relay`` pass. ``previously_blocked=True`` models the
        ``stop_hook_active`` re-entry — a conforming seam NEVER blocks twice."""

    # --- capability 3: backgrounding / wake ---
    def start_background(self, experiment_dir: Path, argv: list[str]) -> Any: ...
    def await_wake(self, handle: Any, timeout_s: float) -> WakeEvent: ...

    # --- optional; kit skips the matching assertions when absent ---
    def answer_question(self, experiment_dir: Path, offered_labels: list[str], answer: str) -> None:
        """Drive YOUR structured-question channel (the AskUserQuestion /
        MCP-elicitation analog) with *answer* against *offered_labels* —
        exercises the clicked-vs-typed provenance line (``_is_clicked``)."""

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        """Default implementation (provided): invoke the core
        ``harness-capabilities`` verb via the CLI in the harness's
        environment and return its detected set. Adapters may delegate to
        :func:`default_detect_capabilities`."""


def declared_capabilities(adapter: object) -> frozenset[str]:
    """The set of contract capabilities *adapter* DECLARES by implementation.

    A capability is declared iff every method in :data:`_CAPABILITY_METHODS`
    for it is a callable attribute of *adapter*. This is the whole "declared"
    leg of ``declared == detected == behaved`` — no manifest, no self-report.
    """
    declared: set[str] = set()
    for capability, methods in _CAPABILITY_METHODS.items():
        if all(callable(getattr(adapter, method, None)) for method in methods):
            declared.add(capability)
    return frozenset(declared)


def skip_reason_for(capability: str) -> str:
    """The skip reason for a capability the adapter does not implement.

    Carries the contract-named degraded tier VERBATIM (the report never rounds
    partial up to conforming; a skip is honest about what degraded, D-K6).
    """
    tier = DEGRADED_TIERS[capability]
    return f"adapter does not implement capability {capability!r} — degraded tier: {tier}"


def default_detect_capabilities(
    experiment_dir: Path, *, cli: Sequence[str] = ("hpc-agent",)
) -> frozenset[str]:
    """Provided default for :meth:`HarnessAdapter.detect_capabilities`.

    Invokes the read-only ``harness-capabilities`` verb (``ops/harness_capabilities.py``)
    in the harness's environment and PROJECTS its report onto the three kit
    nouns (the ``trusted_display`` capability is always excluded — there is no
    kit noun for it; the projection rule, D-K3). Any failure degrades to the
    empty set — this is a best-effort detection seam that K6's negotiation
    module refines and asserts against ``declared == detected == behaved``.

    Stdlib-only (``subprocess`` + ``json``); never raises into a caller.
    """
    import json
    import subprocess  # noqa: S404 - launching the harness's own CLI, offline

    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            [*cli, "harness-capabilities"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(experiment_dir),
            timeout=120,
            check=False,
        )
        payload = json.loads(proc.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return frozenset()
    detected: set[str] = set()
    caps = payload.get("result", payload) if isinstance(payload, dict) else {}
    if not isinstance(caps, dict):
        return frozenset()
    # Best-effort projection: any truthy signal for a contract noun counts.
    # K6 owns the exact verb-key -> noun mapping (the E3-a-reshaped result).
    for noun in CAPABILITIES:
        key = noun.replace("-", "_")
        if _truthy_capability(caps.get(key)) or _truthy_capability(caps.get(noun)):
            detected.add(noun)
    return frozenset(detected)


def _truthy_capability(value: object) -> bool:
    """A capability signal counts as detected unless it is falsey/``unknown``."""
    return value not in (None, False, "", "unknown", "absent", "none")
