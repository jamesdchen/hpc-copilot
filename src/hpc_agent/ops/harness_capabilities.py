"""``harness-capabilities`` — detect the harness capability set, as code sees it.

A read-only ``query`` primitive: LSP-style capability NEGOTIATION for the harness
contract (``docs/internals/harness-contract.md``, "Capability negotiation"). It
DETECTS and reports the contract capabilities as code can OBSERVE them —
never a self-asserted manifest:

1. **The out-of-band utterance log.** Whether a log already exists for this repo —
   the unsuffixed ``utterances.jsonl`` OR any actor-suffixed
   ``utterances.<actor>.jsonl`` (non-creating read via
   :mod:`hpc_agent.state.utterances`; multi-human MH2 attributes capture to the
   suffixed locator, so an actor-only regime leaves the unsuffixed file absent),
   plus which
   input channels are installed in ``~/.claude/settings.json`` (the utterance- and
   answer-capture hooks, matched by their :mod:`hpc_agent.agent_assets` module-path
   needles), plus whether the MCP elicitation SERVER code is implemented (the
   :data:`~hpc_agent._kernel.extension.mcp_server.ELICITATION_SERVER_IMPLEMENTED`
   flag — the honest thing a separate-process probe can report; client support is
   negotiated per session at ``initialize`` and is unknown from this probe).
2. **Relay/verbatim enforcement.** Whether the relay-audit ``Stop`` hook is
   installed (its needle).
3. **Backgrounding / wake.** Always present — the detached-worker machinery is
   core-side — with the watchdog alert-delivery hook's presence reported honestly.
4. **Trusted display.** ``"unknown"`` — the trusted-render capability has no
   detection seam yet (the honest non-answer, not an asserted ``true``).

The result pairs each capability's detected report with the exact tier its absence
degrades to (the contract's named friction tiers, quoted). This is
detection-as-negotiation: the declaration IS what code can verify. The conformance
kit (planned separately) asserts declared == detected == behaved.

Fail-open throughout: an unreadable / absent ``settings.json`` degrades to "no
channels detected", never an exception — a broken config read must not wedge a
read-only probe. Pure local read: no SSH, no scheduler, no write, no state moved.

This file lives at the ``ops/`` *role root* (sibling to ``export_dossier.py`` /
``attention_op.py``, NOT inside a subject package) because it reads across
subjects — the harness-config needles in :mod:`hpc_agent.agent_assets`, the
utterance-log locator in :mod:`hpc_agent.state.utterances`, and the MCP server's
elicitation flag. The subject-imports lint short-circuits for role-root files, so
the cross-subject reads here are allowed by construction.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.harness_capabilities import (
    CapabilityEntry,
    HarnessCapabilitiesResult,
    HarnessCapabilitiesSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef

__all__ = [
    "HARNESS_CONTRACT_VERSION",
    "detect_stop_hook_append",
    "detect_stop_hook_append_on_block",
    "harness_capabilities",
]

# The ONE home of the harness-contract SemVer (conformance-kit D-K6/K10). This
# constant is the single source of truth for three surfaces pinned equal by
# ``tests/contracts/test_harness_contract.py``: the ``harness-capabilities``
# result field below, the ``docs/internals/harness-contract.md`` version line,
# and the conformance kit's stamped verdict
# (``hpc_agent.conformance.report.CONTRACT_VERSION``, which imports THIS). It
# lives beside the verb (not in the kit) because the verb reports it to any
# harness at negotiation time, and the kit does not ship in every install. SemVer
# posture (harness-contract.md "Contract version"): within major 1 the contract
# is ADDITIVE-ONLY; the sha canonicalization is the canonical major trigger.
HARNESS_CONTRACT_VERSION = "1.1.0"

_log = logging.getLogger(__name__)

#: The env markers a conformance-proven harness sets to ACTIVATE the Stop-hook
#: append channel (capability 5, ``stop-hook-append``; D1 of
#: ``docs/design/stop-hook-completer.md``). Unlike capabilities 1–3 there is no
#: passive install seam yet — a hook ``systemMessage`` has ZERO evidence in this
#: repo (``trusted_display`` sits at ``"unknown"`` for the same no-seam reason),
#: so the channel is DECLARED explicitly, and only after a harness's conformance
#: probe confirms it displays a ``systemMessage``. Two markers because the D1
#: probe covers TWO output shapes (display may differ between them):
#: ``HPC_STOP_HOOK_APPEND`` — ``systemMessage`` on a PROCEEDING stop; and
#: ``HPC_STOP_HOOK_APPEND_ON_BLOCK`` — ``systemMessage`` combined with
#: ``decision:"block"``. Absent/unknown → the completer degrades to the REJECTOR
#: (block-once bounce), never to silence.
_STOP_HOOK_APPEND_ENV = "HPC_STOP_HOOK_APPEND"
_STOP_HOOK_APPEND_ON_BLOCK_ENV = "HPC_STOP_HOOK_APPEND_ON_BLOCK"


def _read_tristate_env(var: str) -> bool | str:
    """A tri-state env probe: ``True`` / ``False`` when set, ``"unknown"`` absent.

    The honest non-answer posture: an unset marker reads ``"unknown"`` (no seam
    confirmed), never ``False`` — a caller distinguishes "declared absent" from
    "never probed". Truthy: ``1/true/yes/on``; falsey: ``0/false/no/off``.
    """
    raw = (os.environ.get(var) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return "unknown"


def detect_stop_hook_append() -> bool | str:
    """Whether the harness DISPLAYS a hook ``systemMessage`` on a PROCEEDING stop.

    Capability 5's proceeding-shape bit (D1). This is the seam the Stop-hook
    completer (:mod:`hpc_agent._kernel.hooks.relay_audit_stop`) gates on: only
    when this reads ``True`` does the completer append owed artifacts /
    corrections instead of bouncing. ``"unknown"`` (the default — no env marker,
    no conformance probe yet) and ``False`` both keep the REJECTOR. See
    :data:`_STOP_HOOK_APPEND_ENV`.
    """
    return _read_tristate_env(_STOP_HOOK_APPEND_ENV)


def detect_stop_hook_append_on_block() -> bool | str:
    """Whether the harness displays a ``systemMessage`` on a BLOCKED stop (D1).

    The mixed-class bit (D2's discharge-gating). When this is NOT ``True``, a
    stop that also blocks (a poisoned-decision violation) DEFERS its completions
    to the post-continuation stop rather than riding them on a possibly-swallowed
    blocked ``systemMessage`` — the discharge-gated-on-confirmed-display rule.
    See :data:`_STOP_HOOK_APPEND_ON_BLOCK_ENV`.
    """
    return _read_tristate_env(_STOP_HOOK_APPEND_ON_BLOCK_ENV)


def _claude_dir() -> Path:
    """Resolve the harness config dir the same way Claude Code does.

    ``CLAUDE_CONFIG_DIR`` env override (the documented relocation knob) if set and
    non-empty, else ``~/.claude`` (:func:`hpc_agent.agent_assets.DEFAULT_CLAUDE_DIR`).
    Non-creating — this is a pure read.
    """
    override = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    from hpc_agent.agent_assets import DEFAULT_CLAUDE_DIR

    return DEFAULT_CLAUDE_DIR()


def _read_settings() -> dict[str, Any]:
    """Return the parsed ``<claude_dir>/settings.json`` object, or ``{}`` — fail-open.

    A missing file, an unreadable file, or a non-object payload all degrade to an
    empty dict (no channels detected), never an exception: a broken harness-config
    read must not wedge this probe, and "cannot read config" must land as the
    honest weaker tier, not a crash.
    """
    settings_path = _claude_dir() / "settings.json"
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _log.warning("harness-capabilities: unreadable settings.json (%s)", exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _needle_installed(settings: dict[str, Any], needle: str) -> bool:
    """True when a hook whose command mentions *needle* is wired into settings.json.

    Scans every ``hooks.<event>`` array (the events differ per hook — Stop,
    PostToolUse, UserPromptSubmit, SessionStart) and reuses the ONE canonical
    entry-matcher :func:`hpc_agent.agent_assets._find_hook_entry_index` (module-path
    match, so a moved-venv reinstall still detects the same entry) — never a
    re-derived scan. Fail-open on any shape surprise.
    """
    from hpc_agent.agent_assets import _find_hook_entry_index

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if isinstance(entries, list) and _find_hook_entry_index(entries, needle) is not None:
            return True
    return False


@primitive(
    name="harness-capabilities",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Detect and report the harness capability set as CODE can observe it "
            "(LSP-style negotiation for the harness contract). Reports the "
            "contract capabilities — the out-of-band utterance log (installed "
            "capture hooks + this repo's log presence + MCP elicitation flag), "
            "relay/verbatim enforcement (the relay-audit Stop hook), backgrounding "
            "(core-side, always present; watchdog hook reported honestly), "
            "trusted display (unknown — no detection seam yet), and the Stop-hook "
            "append channel (unknown until a conformance probe activates it) — each "
            "with the named tier its absence degrades to. Read-only, no SSH, "
            "fail-open on an unreadable settings.json. The declaration IS what "
            "code can verify."
        ),
        spec_arg=True,
        spec_required=False,
        experiment_dir_arg=True,
        spec_model=HarnessCapabilitiesSpec,
        schema_ref=SchemaRef(input="harness_capabilities"),
    ),
    agent_facing=True,
)
def harness_capabilities(
    *, experiment_dir: Path, spec: HarnessCapabilitiesSpec | None = None
) -> HarnessCapabilitiesResult:
    """Detect the harness-contract capabilities for *experiment_dir*.

    Pure observation: reads ``settings.json`` (fail-open), checks the utterance
    log's namespace for this repo (non-creating), and consults the MCP elicitation
    flag. Every ``present`` bit is something code verified; ``trusted_display`` is
    ``"unknown"`` because it has no detection seam. The ``spec`` is empty
    (``extra="forbid"`` still rejects a bogus key), so it is optional here.
    """
    from hpc_agent._kernel.extension.mcp_server import ELICITATION_SERVER_IMPLEMENTED
    from hpc_agent.agent_assets import (
        _ALERT_COUNT_NEEDLE,
        _ANSWER_CAPTURE_NEEDLE,
        _RELAY_AUDIT_NEEDLE,
        _UTTERANCE_CAPTURE_NEEDLE,
    )
    from hpc_agent.state.utterances import utterances_path

    experiment_dir = Path(experiment_dir)
    settings = _read_settings()

    # Capability 1 — the out-of-band human-utterance log.
    utterance_capture = _needle_installed(settings, _UTTERANCE_CAPTURE_NEEDLE)
    answer_capture = _needle_installed(settings, _ANSWER_CAPTURE_NEEDLE)
    # Presence probe: the unsuffixed log OR any actor-suffixed log
    # (``utterances.<actor>.jsonl``, MH2 consequence 1). Under an actor-only
    # capture regime the unsuffixed file never exists while attributed logs
    # sit beside it, so a bare ``utterances.jsonl`` check would report the
    # capability absent. Non-creating throughout: a glob over a missing
    # namespace dir yields nothing and scaffolds nothing (the no-scaffold rule).
    try:
        base = utterances_path(experiment_dir)  # non-creating
        log_present = base.exists() or any(base.parent.glob("utterances.*.jsonl"))
    except OSError:
        log_present = False

    utterance_log = CapabilityEntry(
        # The write channel (the UserPromptSubmit capture hook) is what earns the
        # full-strength authorship tier; its presence is the capability's `present`.
        present=utterance_capture,
        channel=(
            "UserPromptSubmit utterance-capture hook -> state.utterances write API "
            "(out-of-band; the harness, not the model, is the writer)"
        ),
        evidence={
            "utterance_capture_hook": utterance_capture,
            "answer_capture_hook": answer_capture,
            # Elicitation splits into what code can verify vs. what it cannot: the
            # SERVER code capability is a real, separate-process-observable bit; the
            # CLIENT support is negotiated per session at MCP `initialize` and is
            # unknown from this probe (say unknown, not yes — the honesty posture).
            "elicitation_server": ELICITATION_SERVER_IMPLEMENTED,
            "elicitation_client": "per-session",
            "log_present_for_repo": log_present,
        },
    )

    # Capability 2 — the relay/verbatim enforcement point.
    relay_installed = _needle_installed(settings, _RELAY_AUDIT_NEEDLE)
    relay_enforcement = CapabilityEntry(
        present=relay_installed,
        channel="relay-audit Stop hook -> verify-relay on the final agent-visible message",
        evidence={"relay_audit_stop_hook": relay_installed},
    )

    # Capability 3 — backgrounding / wake. The detached-worker machinery is
    # core-side (S2/S3/S4 detach, campaign reconcile self-chaining, wait-detached),
    # so it is always present; the watchdog alert-DELIVERY hook is reported
    # honestly (detection without delivery is silence — proving-run #3).
    watchdog_alert = _needle_installed(settings, _ALERT_COUNT_NEEDLE)
    backgrounding = CapabilityEntry(
        present=True,
        channel="core detached-worker machinery (detach + wait-detached rendezvous)",
        evidence={
            "detached_machinery": "core",
            "watchdog_alert_hook": watchdog_alert,
        },
    )

    # Capability 4 — trusted display. No detection seam yet (the render-file
    # capability has no observable install marker); the honest non-answer.
    trusted_display = CapabilityEntry(
        present="unknown",
        channel="none — the trusted-render capability has no detection seam yet",
        evidence={
            "note": (
                "no code-observable install marker for a trusted display surface; "
                "reported unknown rather than asserted"
            )
        },
    )

    # Capability 5 — the Stop-hook APPEND channel (D1 of the stop-hook completer).
    # Tri-state like trusted_display: no passive install seam exists, so it reads
    # "unknown" until a conforming harness's conformance probe confirms it displays
    # a hook ``systemMessage`` and activates it via the env markers. Absent/unknown
    # → the relay-audit Stop hook stays the REJECTOR (block-once), never silence.
    append_proceeding = detect_stop_hook_append()
    append_on_block = detect_stop_hook_append_on_block()
    stop_hook_append = CapabilityEntry(
        present=append_proceeding,
        channel=(
            "hook systemMessage display -> the relay-audit Stop hook COMPLETES "
            "(code-appends the owed artifact / correction) instead of bouncing"
        ),
        evidence={
            "append_on_proceeding": append_proceeding,
            "append_on_block": append_on_block,
            "activation_markers": [_STOP_HOOK_APPEND_ENV, _STOP_HOOK_APPEND_ON_BLOCK_ENV],
            "note": (
                "no passive install seam yet (like trusted_display); a conforming "
                "harness activates the append channel explicitly once its "
                "systemMessage-display conformance probe confirms it. Absent/unknown "
                "-> the completer degrades to the rejector, never to silence."
            ),
        },
    )

    return HarnessCapabilitiesResult(
        capabilities={
            "utterance_log": utterance_log,
            "relay_enforcement": relay_enforcement,
            "backgrounding": backgrounding,
            "trusted_display": trusted_display,
            "stop_hook_append": stop_hook_append,
        },
        tier_consequences={
            "utterance_log": (
                "absent -> the authorship / scope-unlock / notebook-sign-off gates "
                "fall back to the JOURNAL-RESPONSE FRICTION TIER at the seam "
                "ops/decision/journal.py::_harness_human_texts returning None: the "
                "evidence source becomes agent-authored journal `response` fields, "
                "so a determined agent could still fabricate a human quote (the "
                "named weaker tier, not a uniform claim)."
            ),
            "relay_enforcement": (
                "absent -> the relay audit reverts to the VERB-ONLY posture: an "
                "unaudited relay can reach the human ('running' relayed while the "
                "journal said 'failed'). No wedge — just the weaker guarantee."
            ),
            "backgrounding": (
                "absent -> the blocks collapse to synchronous, in-turn execution; "
                "correctness is unaffected (the journal is still the source of "
                "truth), only the wall-clock ergonomics degrade."
            ),
            "trusted_display": (
                "unknown -> the trusted-render capability cannot be verified from "
                "code; no tier claim is made either way."
            ),
            "stop_hook_append": (
                "absent/unknown -> the relay-audit Stop hook stays the REJECTOR: an "
                "owed terminal verdict or a contradicted claim is re-relayed by the "
                "MODEL (the block-once bounce), never code-appended. No wedge; the "
                "completer path is dark until the append channel is confirmed."
            ),
        },
        harness_contract_version=HARNESS_CONTRACT_VERSION,
    )
