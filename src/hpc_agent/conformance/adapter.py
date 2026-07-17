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
    "CAP_DECISION_RENDEZVOUS",
    "CAP_RELAY_ENFORCEMENT",
    "CAP_RELAY_INSPECT",
    "CAP_SCHEDULER_FENCE",
    "CAP_STOP_HOOK_APPEND",
    "CAP_TRUSTED_DISPLAY",
    "CAP_UTTERANCE_LOG",
    "DEGRADED_TIERS",
    "DisplayOutcome",
    "EnforcementOutcome",
    "HarnessAdapter",
    "InspectionOutcome",
    "StopAppendOutcome",
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

# An OPTIONAL, WEAKER tier within capability 2 (relay/verbatim enforcement): the
# INSPECT half — OBSERVE the final agent-visible message and REPORT a
# contradiction — WITHOUT the ACT half (forcing a continuation). The contract
# splits capability 2 into INSPECT + ACT (``docs/internals/harness-contract.md``,
# "Capability 2, split: INSPECT vs ACT"): a harness that only INSPECTS (e.g. via
# OTel-GenAI telemetry) sees a contradiction but cannot stop it reaching the
# human, so the ENFORCEMENT guarantee still degrades to the verb-only posture.
# This noun is DELIBERATELY NOT in :data:`CAPABILITIES` (the conforming bar) — it
# is a disclosed weaker tier a harness may ALSO earn, never a substitute for the
# ACT bar :data:`CAP_RELAY_ENFORCEMENT`. Recording it honestly is the whole point
# (an INSPECT harness must never round up to a false ACT pass).
CAP_RELAY_INSPECT = "relay-inspection"

CAPABILITIES: frozenset[str] = frozenset(
    {CAP_UTTERANCE_LOG, CAP_RELAY_ENFORCEMENT, CAP_BACKGROUNDING}
)

# --- capabilities 6 & 7 (harness-contract.md, "Capabilities 6 & 7"). These are
# NOT part of the three-capability ``CAPABILITIES`` verdict set (the top-level
# ``conforming: harness contract v1`` line stays the three core capabilities); they
# are ADDITIVE reference-behaved batteries (contract v1.2.0) with an adapter seam a
# FOREIGN provider can later run (Wave C). Registering the nouns here lets
# ``declared_capabilities`` / ``skip_reason_for`` report them for such an adapter,
# without changing what "conforming" means today.
CAP_SCHEDULER_FENCE = "scheduler-fence"
CAP_DECISION_RENDEZVOUS = "decision-rendezvous"

# --- capabilities 4 & 5 (harness-contract.md, "Capabilities 4 & 5"). Like 6 & 7
# these are NOT part of the three-capability ``CAPABILITIES`` verdict set — they
# are ADDITIVE reference-behaved batteries (T9/T10, anti-vendor-lockout Wave D):
# the kit exercises the REAL cores (the trusted-display render-lock; the
# relay-audit Stop-hook completer) so a FOREIGN provider can later run the identical
# assertions through the adapter seam. Crucially NEITHER has a passive detection
# seam — ``harness-capabilities`` reports ``trusted_display: "unknown"`` and the
# env-declared ``stop_hook_append`` — so this landing closes the BEHAVED leg only;
# the passive-detection seam stays the honest residual (never faked into a
# self-asserted ``true``). Registering the nouns here lets ``declared_capabilities``
# / ``skip_reason_for`` report them for such an adapter, without changing what
# "conforming" means today.
CAP_TRUSTED_DISPLAY = "trusted-display"
CAP_STOP_HOOK_APPEND = "stop-hook-append"

# Which Protocol method(s) DECLARE each capability. An adapter declares a
# capability by implementing ALL of its methods (a partial harness simply omits
# them — the honest-partial posture, not a manifest opt-out).
_CAPABILITY_METHODS: dict[str, tuple[str, ...]] = {
    CAP_UTTERANCE_LOG: ("write_utterance",),
    CAP_RELAY_ENFORCEMENT: ("run_enforcement_point",),
    CAP_BACKGROUNDING: ("start_background", "await_wake"),
    # The OPTIONAL weaker INSPECT tier — declared by implementing ``inspect_relay``
    # (observe + report) WITHOUT ``run_enforcement_point`` (the ACT bar). An
    # existing adapter that implements neither is byte-unchanged: it declares
    # neither noun.
    CAP_RELAY_INSPECT: ("inspect_relay",),
    # Capabilities 6 & 7 — declared by implementing the optional adapter method;
    # the reference batteries fall back to the real core when a method is absent.
    CAP_SCHEDULER_FENCE: ("run_scheduler_fence",),
    CAP_DECISION_RENDEZVOUS: ("run_decision_rendezvous",),
    # Capabilities 4 & 5 — same additive shape (T9/T10). A foreign provider
    # declares by implementing the optional method; the reference battery falls
    # back to the real render-lock / completer core otherwise.
    CAP_TRUSTED_DISPLAY: ("run_trusted_display",),
    CAP_STOP_HOOK_APPEND: ("run_stop_hook_append",),
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
    # The INSPECT tier's own "absent" degrade: with no telemetry-inspection
    # channel the audit cannot even OBSERVE the final message out-of-band, so it
    # collapses to the same verb-only posture the ACT bar does. Named so a skip
    # line for this optional tier stays honest; the ACT bar's absence is disclosed
    # SEPARATELY under CAP_RELAY_ENFORCEMENT.
    CAP_RELAY_INSPECT: "verb-only relay-audit posture (no telemetry-inspection channel)",
    # Capabilities 6 & 7 tiers track harness-contract.md's "Degrades when absent"
    # clauses (prose-only fence / doctor-tick-backstop-only rendezvous).
    CAP_SCHEDULER_FENCE: "prose-only scheduler-mutation guard",
    CAP_DECISION_RENDEZVOUS: "doctor-tick-backstop-only rendezvous",
    # Capabilities 4 & 5 tiers track harness-contract.md's degrade clauses: a
    # model-carried display with no code-proven verbatim surface; the rejector
    # block-once bounce with no code-appended completion.
    CAP_TRUSTED_DISPLAY: "model-carried display; no code-proven verbatim surface",
    CAP_STOP_HOOK_APPEND: "rejector block-once bounce; no code-appended completion",
}


class EnforcementOutcome(NamedTuple):
    """The outcome of running a harness's relay ACT seam over one final message."""

    blocked: bool  # the harness forced a continuation
    reason: str | None  # itemized mismatch summary when blocked


class InspectionOutcome(NamedTuple):
    """The outcome of a harness's relay INSPECT seam over one final message.

    The weaker half of capability 2: the harness OBSERVED the final message and
    REPORTS whether it contradicts the journal — but never ACTS (never forces a
    continuation). ``detected`` is the observe-and-report verdict; ``report`` is
    the telemetry line the harness emitted (a span/event summary), carried so the
    kit can confirm the INSPECT harness disclosed rather than enforced.
    """

    detected: bool  # a contradiction was observed and reported
    report: str | None  # the telemetry the harness emitted (never a block)


class WakeEvent(NamedTuple):
    """The outcome of a detached-worker wake."""

    woke: bool  # the driver was re-invoked after detach
    terminal_seen: bool  # the wake observed the worker's terminal record


class DisplayOutcome(NamedTuple):
    """The outcome of running a harness's trusted-display seam over one code-rendered payload."""

    #: the exact bytes the surface shows the human.
    displayed: str
    #: the content address the displayed artifact binds (its header ``view_sha``).
    bound_view_sha: str | None
    #: the artifact lives at its ``view_sha`` content address (a forged binding is False).
    content_addressed: bool


class StopAppendOutcome(NamedTuple):
    """The outcome of running a harness's Stop-hook APPEND channel over one owed/poisoned stop."""

    #: the code-appended ``systemMessage`` the surface DISPLAYS (None = the rejector degrade).
    system_message: str | None
    #: the stop ALSO forced a continuation (``decision:"block"``) — the mixed class (D1 shape B).
    blocked: bool


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

    # --- capability 2 (INSPECT half); OPTIONAL — the weaker observe-only tier ---
    def inspect_relay(self, experiment_dir: Path, final_message: str) -> InspectionOutcome:
        """OBSERVE *final_message* and REPORT whether it contradicts the journal —
        WITHOUT forcing a continuation (the INSPECT half of capability 2). A
        harness that provides this but NOT ``run_enforcement_point`` (e.g. an
        OTel-GenAI telemetry stream) earns the weaker :data:`CAP_RELAY_INSPECT`
        tier; the ACT bar :data:`CAP_RELAY_ENFORCEMENT` still degrades to its
        verb-only posture. The kit skips the matching assertions when absent."""

    # --- capability 3: backgrounding / wake ---
    def start_background(self, experiment_dir: Path, argv: list[str]) -> Any: ...
    def await_wake(self, handle: Any, timeout_s: float) -> WakeEvent: ...

    # --- capability 6 (scheduler-write fence) — OPTIONAL (additive, v1.2.0) ---
    def run_scheduler_fence(self, command: str) -> EnforcementOutcome:
        """Run YOUR pre-execution command fence over *command* — the seam that
        INSPECTS a shell command the agent is about to run and REFUSES it when it
        would EXECUTE a mutating scheduler verb (``qsub``/``sbatch``/``qdel``/…) in
        command position (including wrapped / transport forms like
        ``bash -c 'qsub …'`` or ``ssh host qdel``), while letting a mere mention
        (``grep qsub``), a read-only probe (``qstat``), and the ``hpc-agent`` CLI
        itself through. ``blocked=True`` is a refusal (``reason`` names the fenced
        verb); ``blocked=False`` is a pass. When absent, the reference battery falls
        back to the real fence core (a FOREIGN proof stays owed — Wave C)."""

    # --- capability 7 (decision-rendezvous commit-then-continue) — OPTIONAL ---
    def run_decision_rendezvous(
        self, experiment_dir: Path, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        """Run YOUR turn-final seam over the cwd repo's journal — the seam that
        reads the decision journal and FORCES ONE continuation when a human
        greenlight is committed but the driver has not advanced past the parked
        boundary, and stays SILENT while the driver is merely awaiting the human.
        ``blocked=True`` forced a continuation; ``blocked=False`` let the stop
        proceed. ``previously_blocked=True`` models the ``stop_hook_active``
        re-entry — a conforming seam NEVER forces twice. When absent, the reference
        battery falls back to the real rendezvous core (FOREIGN proof owed — Wave C)."""

    # --- capability 4 (trusted display) — OPTIONAL (additive, T9; kit-behaved) ---
    def run_trusted_display(
        self, experiment_dir: Path, *, audit_id: str, view: Any
    ) -> DisplayOutcome:
        """Run YOUR trusted-display surface over a KIT-CHOSEN code-rendered payload
        (*view*) — the surface that DISPLAYS a code-authored artifact to the human so
        code can PROVE the human saw it VERBATIM, uncorrupted by the model. Emit the
        payload to your content-addressed display and report what it shows:
        ``displayed`` is the exact bytes the surface shows, ``bound_view_sha`` is the
        content address the artifact binds, ``content_addressed`` is whether the
        artifact actually lives at that address (a forged binding is False). The kit
        compares ``displayed`` byte-for-byte against the known payload and checks the
        address integrity; a model-substituted or non-content-addressed display is
        FAILED. When absent, the reference battery falls back to the real render-lock
        core (a FOREIGN proof stays owed). NOTE (T9 residual): NO passive install
        marker exists for a trusted-display surface — ``harness-capabilities``
        honestly reports ``trusted_display: "unknown"`` — so this seam is the BEHAVED
        leg only; the passive-detection seam is the still-owed follow-on, never faked
        into a self-asserted ``true``."""

    # --- capability 5 (Stop-hook append channel) — OPTIONAL (additive, T10) ---
    def run_stop_hook_append(
        self, experiment_dir: Path, *, on_block: bool = False
    ) -> StopAppendOutcome:
        """Run YOUR turn-final APPEND channel over an owed/poisoned stop — the seam
        that lets deterministic code APPEND what it holds (an owed terminal verdict, a
        trusted render, a rule-10 correction) to the human via a hook
        ``systemMessage`` and PROCEED, instead of bouncing the model into re-relaying
        it. The D1 two-shape probe: ``on_block=False`` MUST display a bare
        ``systemMessage`` on a PROCEEDING stop (``blocked=False``); ``on_block=True``
        MUST display a ``systemMessage`` COMBINED with a ``decision:"block"`` poisoned
        bounce (``blocked=True``) — display may differ between the two shapes.
        ``system_message`` is the appended text the surface shows (None = the REJECTOR
        degrade, nothing appended). When absent, the reference battery falls back to
        the real relay-audit completer core (a FOREIGN proof stays owed)."""

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
