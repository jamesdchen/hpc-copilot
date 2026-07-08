"""``harness-capabilities`` — detect the harness capability set, as code sees it.

A read-only ``query`` primitive: LSP-style capability NEGOTIATION for the harness
contract (``docs/internals/harness-contract.md``, "Capability negotiation"). It
DETECTS and reports the four contract capabilities as code can OBSERVE them —
never a self-asserted manifest:

1. **The out-of-band utterance log.** Whether the log namespace already exists for
   this repo (non-creating read via :mod:`hpc_agent.state.utterances`), plus which
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

This file lives at the ``ops/`` *role root* (sibling to ``notebook_status.py`` /
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

__all__ = ["harness_capabilities"]

_log = logging.getLogger(__name__)


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
            "(LSP-style negotiation for the harness contract). Reports the four "
            "contract capabilities — the out-of-band utterance log (installed "
            "capture hooks + this repo's log presence + MCP elicitation flag), "
            "relay/verbatim enforcement (the relay-audit Stop hook), backgrounding "
            "(core-side, always present; watchdog hook reported honestly), and "
            "trusted display (unknown — no detection seam yet) — each with the "
            "named tier its absence degrades to. Read-only, no SSH, fail-open on an "
            "unreadable settings.json. The declaration IS what code can verify."
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
    """Detect the four harness-contract capabilities for *experiment_dir*.

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
    try:
        log_present = utterances_path(experiment_dir).exists()  # non-creating
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

    return HarnessCapabilitiesResult(
        capabilities={
            "utterance_log": utterance_log,
            "relay_enforcement": relay_enforcement,
            "backgrounding": backgrounding,
            "trusted_display": trusted_display,
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
        },
    )
