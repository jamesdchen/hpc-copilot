"""Per-pattern matchers for the axis classifier.

Each matcher lives in its own module and exports its public ``_match_*``
entry point plus any pattern-specific helpers. The dispatcher
(:func:`hpc_agent.experiment_kit.axis_matcher.classify_axis_easy`)
imports each matcher and calls them in pattern-priority order.

This package is intentionally underscore-aware: every name here is
internal to the matcher subsystem; nothing should be reached for from
outside ``axis_matcher``.
"""

from __future__ import annotations

__all__: list[str] = []
