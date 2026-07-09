"""The built-in reference harness adapter — the kit's DEFAULT candidate (K4).

The conformance kit is a TCK: it drives a *stranger's* harness through the
:class:`~hpc_agent.conformance.adapter.HarnessAdapter` seam and earns (or
refuses) the conformance verdict. This module is the reference the kit certifies
against ITSELF — hpc-agent's own capability-1 providers, driven in-process with
no live Claude Code and no network:

* :meth:`ReferenceAdapter.write_utterance` routes text through the reference
  ``UserPromptSubmit`` capture core
  (:func:`hpc_agent._kernel.hooks.utterance_capture.capture`);
* :meth:`ReferenceAdapter.answer_question` routes an answer through the
  ``AskUserQuestion`` ``PostToolUse`` core
  (:func:`hpc_agent._kernel.hooks.answer_capture.capture`).

Both are the SAME writers + provenance filters (``is_harness_injected`` /
``_is_clicked``) a live Claude Code session runs, so "deliver *text* through
your harness's human-input channel exactly as if a human typed it" (the adapter
contract) is honored literally — the record lands via the reference writer, its
filters included.

Scope: capability 1 ONLY. This minimal reference declares the utterance-log
capability (``write_utterance``) plus the optional clicked-vs-typed provenance
channel (``answer_question``); it does NOT implement relay enforcement or
backgrounding, so the kit honestly SKIPS those with their contract-named
degraded tier. The full three-capability Claude Code / notebook-render reference
adapters are K8's ``conformance/adapters/`` subpackage; this module is the
built-in default K4's own module runs green against.

Stdlib + hpc-agent core only; pytest-free — loadable via
``--harness-adapter hpc_agent.conformance.reference_adapter:build``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.conformance.adapter import default_detect_capabilities

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ReferenceAdapter", "build"]


class ReferenceAdapter:
    """hpc-agent's own capability-1 providers behind the kit's adapter seam."""

    name = "hpc-agent-reference"

    def write_utterance(self, experiment_dir: Path, text: str) -> None:
        """Deliver *text* as if a human typed it at the ``UserPromptSubmit`` seam.

        Builds the same payload Claude Code's ``UserPromptSubmit`` command hook
        receives and calls the reference capture core, so the record (if any)
        lands through :func:`hpc_agent.state.utterances.append_utterance` with
        the reference harness-injection filter applied — never a direct write.
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

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        """The provided default detection seam (``harness-capabilities`` verb)."""
        return default_detect_capabilities(experiment_dir)


def build() -> ReferenceAdapter:
    """Zero-arg factory for ``--harness-adapter hpc_agent.conformance.reference_adapter:build``."""
    return ReferenceAdapter()
