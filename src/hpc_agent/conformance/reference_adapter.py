"""The built-in capability-1 reference adapter — a COMPAT SHIM over K8's package.

K4 shipped this module as the kit's minimal built-in default. K8 promoted the
FULL reference into :mod:`hpc_agent.conformance.adapters.claude_code`, so this
module is now a thin, backward-compatible façade: :class:`ReferenceAdapter`
DELEGATES its capability-1 methods (``write_utterance`` / ``answer_question``) to
one :class:`~hpc_agent.conformance.adapters.claude_code.ClaudeCodeAdapter`
instance — the single, promoted definition of "drive the hook cores in-process"
— and deliberately implements NOTHING else, so it declares exactly
``{utterance-log}`` (plus the optional clicked-vs-typed channel). The kit honestly
SKIPS relay enforcement and backgrounding for it, with their contract-named
degraded tiers.

Prefer the named adapters for new work:
``--harness-adapter hpc_agent.conformance.adapters.claude_code:build`` (the full
reference) or ``…adapters.notebook_render:build`` (the partial second harness).
This shim stays importable so K4's mirror unit tests keep certifying the
capability-1 battery against the built-in default.

Stdlib + hpc-agent core only; pytest-free — loadable via
``--harness-adapter hpc_agent.conformance.reference_adapter:build``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.conformance.adapter import default_detect_capabilities
from hpc_agent.conformance.adapters.claude_code import ClaudeCodeAdapter

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ReferenceAdapter", "build"]


class ReferenceAdapter:
    """Capability-1 façade over :class:`ClaudeCodeAdapter` (utterance-log only)."""

    name = "hpc-agent-reference"

    def __init__(self) -> None:
        # The promoted definition: capability-1 writes route through the SAME
        # in-process hook cores the full Claude Code adapter drives — never a
        # second copy of the capture logic (D-K5 "promote, don't duplicate").
        self._full = ClaudeCodeAdapter()

    def write_utterance(self, experiment_dir: Path, text: str) -> None:
        """Deliver *text* as if a human typed it (``UserPromptSubmit`` capture core)."""
        self._full.write_utterance(experiment_dir, text)

    def answer_question(self, experiment_dir: Path, offered_labels: list[str], answer: str) -> None:
        """Deliver a structured-question *answer* (``AskUserQuestion`` capture core)."""
        self._full.answer_question(experiment_dir, offered_labels, answer)

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        """The provided default detection seam (``harness-capabilities`` verb)."""
        return default_detect_capabilities(experiment_dir)


def build() -> ReferenceAdapter:
    """Zero-arg factory for ``--harness-adapter hpc_agent.conformance.reference_adapter:build``."""
    return ReferenceAdapter()
