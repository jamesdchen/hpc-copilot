"""Stdlib-only AST pattern-matcher — the fast path for ``hpc-classify-axis``.

This was originally a single 839-line module. The decomposition split it
into one ``_classifier.py`` (the dispatcher + ``MatcherResult``) plus
the ``_ast_utils`` helpers and one module per matcher under
``matchers/``. Importing the package preserves the original public API:

    >>> from hpc_agent.experiment_kit.axis_matcher import classify_axis_easy, MatcherResult
"""

from __future__ import annotations

from hpc_agent.experiment_kit.axis_matcher._classifier import (
    MatcherResult,
    classify_axis_easy,
)

__all__ = ["MatcherResult", "classify_axis_easy"]
