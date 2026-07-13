"""The FULL Claude Code reference adapter (K8) — all three capabilities in-process.

The conformance kit is a TCK: it drives a harness's three capability providers
through the :class:`~hpc_agent.conformance.adapter.HarnessAdapter` seam. This
module is the reference the kit certifies against ITSELF — hpc-agent's own hook
cores, driven IN-PROCESS with no live Claude Code and no network, because "the
hooks ARE the implementation under test" (``docs/design/conformance-kit.md`` D-K5):

* **capability 1 (utterance-log)** — :meth:`ClaudeCodeAdapter.write_utterance`
  builds the ``UserPromptSubmit`` payload and calls the reference capture core
  (:func:`hpc_agent._kernel.hooks.utterance_capture.capture`);
  :meth:`ClaudeCodeAdapter.answer_question` builds the ``AskUserQuestion``
  ``PostToolUse`` payload for :func:`hpc_agent._kernel.hooks.answer_capture.capture`
  — the SAME writers + provenance filters (``is_harness_injected`` / ``_is_clicked``)
  a live session runs;
* **capability 2 (relay-enforcement / ACT)** —
  :meth:`ClaudeCodeAdapter.run_enforcement_point` writes the final message as the
  trailing assistant entry of a synthetic transcript JSONL, builds the ``Stop``
  payload (``stop_hook_active`` models the re-entry), and maps
  :func:`hpc_agent._kernel.hooks.relay_audit_stop.build_hook_output` onto an
  :class:`~hpc_agent.conformance.adapter.EnforcementOutcome` (a returned block dict
  is a forced continuation; ``None`` is a pass);
* **capability 3 (backgrounding)** — :meth:`ClaudeCodeAdapter.start_background`
  launches the kit's stub worker as a detached subprocess and
  :meth:`ClaudeCodeAdapter.await_wake` reads the terminal record back through the
  ONE canonical journal locator — the durable rendezvous a woken driver reads.

Detection (:meth:`ClaudeCodeAdapter.detect_capabilities`) is by SEAM, the
Claude-Code honest-detection rule (D-K3): the ``harness-capabilities`` verb probes
the installed hook needles. The adapter materialises a settings.json declaring the
three capture/relay hooks Claude Code installs — the honest declaration, since the
adapter IS those hooks' providers — and runs the verb in-process against it,
projecting its four capabilities onto the three contract nouns (``trusted_display``
excluded — the projection rule). ``backgrounding`` is a core-side constant (always
present), so it is always in the detected set.

Stdlib + hpc-agent core only; pytest-free — loadable via
``--harness-adapter hpc_agent.conformance.adapters.claude_code:build``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from hpc_agent.conformance.adapter import (
    CAP_BACKGROUNDING,
    CAP_RELAY_ENFORCEMENT,
    CAP_UTTERANCE_LOG,
    EnforcementOutcome,
    WakeEvent,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["ClaudeCodeAdapter", "build"]


class _BackgroundHandle(NamedTuple):
    """The handle :meth:`ClaudeCodeAdapter.start_background` returns to ``await_wake``."""

    proc: subprocess.Popen[bytes]
    experiment_dir: Path


class ClaudeCodeAdapter:
    """Claude Code's own capability providers behind the kit's adapter seam.

    Declares (by implementing) all three contract capabilities plus the optional
    ``answer_question`` clicked-vs-typed channel — the FULL reference the kit
    certifies against itself.
    """

    name = "claude-code"

    # ── capability 1: the out-of-band utterance channel ─────────────────────

    def write_utterance(self, experiment_dir: Path, text: str) -> None:
        """Deliver *text* as if a human typed it at the ``UserPromptSubmit`` seam.

        Builds the same payload Claude Code's ``UserPromptSubmit`` command hook
        receives and calls the reference capture core, so the record (if any)
        lands through :func:`hpc_agent.state.utterances.append_utterance` with the
        harness-injection filter applied — never a direct write.
        """
        from hpc_agent._kernel.hooks.utterance_capture import capture

        capture({"prompt": text, "cwd": str(experiment_dir)})

    def answer_question(self, experiment_dir: Path, offered_labels: list[str], answer: str) -> None:
        """Deliver a structured-question *answer* at the ``AskUserQuestion`` seam.

        Models the ``PostToolUse`` payload Claude Code hands the answer-capture
        core: a click on an offered label is filtered out (``_is_clicked``);
        free-text residue lands. Exercises the clicked-vs-typed provenance line.
        """
        from hpc_agent._kernel.hooks.answer_capture import capture

        capture(
            {
                "tool_name": "AskUserQuestion",
                "cwd": str(experiment_dir),
                "tool_input": {
                    "questions": [{"options": [{"label": label} for label in offered_labels]}]
                },
                "tool_response": {"answers": {"answer": answer}},
            }
        )

    # ── capability 2: the relay enforcement point (ACT) ─────────────────────

    def run_enforcement_point(
        self, experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        """Run the relay-audit ``Stop`` seam over *final_message* for the cwd repo.

        The reference ACT shape (hooks): write the final message as the trailing
        assistant entry of a JSONL transcript, build the ``Stop`` payload
        (``stop_hook_active`` models the ``previously_blocked`` re-entry), and map
        :func:`~hpc_agent._kernel.hooks.relay_audit_stop.build_hook_output` — a
        returned block dict is a forced continuation; ``None`` is a pass.
        """
        from hpc_agent._kernel.hooks.relay_audit_stop import build_hook_output

        transcript = Path(experiment_dir) / "_kit_transcript.jsonl"
        content = [{"type": "text", "text": final_message}]
        line = json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": content}}
        )
        transcript.write_text(line + "\n", encoding="utf-8")
        payload = {
            "cwd": str(experiment_dir),
            "transcript_path": str(transcript),
            "stop_hook_active": previously_blocked,
        }
        out = build_hook_output(payload)
        if out is None:
            return EnforcementOutcome(blocked=False, reason=None)
        return EnforcementOutcome(blocked=True, reason=out.get("reason"))

    # ── capability 3: backgrounding / wake ──────────────────────────────────

    def start_background(self, experiment_dir: Path, argv: list[str]) -> _BackgroundHandle:
        """Launch *argv* as detached work (the kit's stub worker path).

        A bare detached subprocess — the shape a conforming ``start_background``
        must launch; the worker itself imports no hpc-agent code and meets the
        driver only at the journal-namespace rendezvous.
        """
        proc = subprocess.Popen(argv, cwd=str(experiment_dir), env=os.environ.copy())
        return _BackgroundHandle(proc, Path(experiment_dir))

    def await_wake(self, handle: _BackgroundHandle, timeout_s: float) -> WakeEvent:
        """Wait for the detached worker and read its terminal record back.

        ``woke`` is True once the worker returns within the timeout; ``terminal_seen``
        reads the rendezvous file through the ONE canonical journal locator
        (:func:`hpc_agent.state.utterances.utterances_path`), so it reflects the
        durable journal — not a private handshake.
        """
        from hpc_agent.state.utterances import utterances_path

        try:
            handle.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            return WakeEvent(woke=False, terminal_seen=False)
        terminal = utterances_path(handle.experiment_dir).parent / "stub_worker.terminal.json"
        return WakeEvent(woke=True, terminal_seen=terminal.exists())

    # ── negotiation: detection by SEAM (the hook needles the verb probes) ────

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        """Detect the contract nouns via the ``harness-capabilities`` verb.

        The Claude-Code honest-detection rule (D-K3): capability detection is by
        SEAM — the installed hook needles are what the verb probes. The adapter
        materialises a settings.json declaring the three capture/relay hooks
        Claude Code installs (honest: the adapter IS those hooks' providers), runs
        the verb in-process against it, and PROJECTS its four capabilities onto the
        three contract nouns — ``trusted_display`` excluded (no kit noun; the
        projection rule). ``backgrounding`` is a core-side constant (always true).
        """
        from hpc_agent.ops.harness_capabilities import harness_capabilities

        with (
            _claude_config_with_hooks() as claude_dir,
            _env("CLAUDE_CONFIG_DIR", str(claude_dir)),
        ):
            result = harness_capabilities(experiment_dir=Path(experiment_dir))
        caps = result.capabilities
        detected: set[str] = {CAP_BACKGROUNDING}  # core-side constant — always present
        if _present(caps.get("utterance_log")):
            detected.add(CAP_UTTERANCE_LOG)
        if _present(caps.get("relay_enforcement")):
            detected.add(CAP_RELAY_ENFORCEMENT)
        return frozenset(detected)


def _present(entry: object) -> bool:
    """True when a capability entry's ``present`` bit is truthy (never ``unknown``)."""
    value = getattr(entry, "present", None)
    return value not in (None, False, "", "unknown")


@contextmanager
def _env(key: str, value: str) -> Iterator[None]:
    """Temporarily set an environment variable, restoring the prior value."""
    prior = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior


@contextmanager
def _claude_config_with_hooks() -> Iterator[Path]:
    """A temp ``CLAUDE_CONFIG_DIR`` whose settings.json declares the capture/relay hooks.

    The three hook module paths Claude Code installs — imported as the real needle
    constants so a rename breaks loudly — are each wired into a ``hooks.<event>``
    array in exactly the shape :func:`hpc_agent.ops.harness_capabilities` probes
    (an entry whose ``hooks[].command`` mentions the module path). Materialising
    this is the adapter's HONEST declaration that Claude Code's hooks are installed:
    the adapter drives those very cores in-process, so it IS their provider.
    """
    import tempfile

    from hpc_agent.agent_assets import (
        _ANSWER_CAPTURE_NEEDLE,
        _RELAY_AUDIT_NEEDLE,
        _UTTERANCE_CAPTURE_NEEDLE,
    )

    def _entry(needle: str) -> dict[str, object]:
        return {"hooks": [{"type": "command", "command": f"{sys.executable} -m {needle}"}]}

    settings = {
        "hooks": {
            "UserPromptSubmit": [_entry(_UTTERANCE_CAPTURE_NEEDLE)],
            "PostToolUse": [_entry(_ANSWER_CAPTURE_NEEDLE)],
            "Stop": [_entry(_RELAY_AUDIT_NEEDLE)],
        }
    }
    with tempfile.TemporaryDirectory(prefix="hpc-kit-claude-") as tmp:
        claude_dir = Path(tmp)
        (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        yield claude_dir


def build() -> ClaudeCodeAdapter:
    """Zero-arg factory for ``--harness-adapter …adapters.claude_code:build``."""
    return ClaudeCodeAdapter()
