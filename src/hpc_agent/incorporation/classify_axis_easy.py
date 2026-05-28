"""``classify-axis-easy`` primitive — fast-path AST pattern-match for axis classification.

A read-only wrapper around
:func:`hpc_agent.experiment_kit.axis_matcher.classify_axis_easy`. The
``hpc-classify-axis`` skill calls this primitive *first*; on a confident
hit (``independent`` / ``bounded_halo`` / ``sequential``, or
``no_loop_detected`` — recorded as the terminal ``cartesian`` "no ordered
series" verdict) it skips the LLM decision tree and records directly;
only ``unclassifiable`` / ``function_not_found`` fall through to the LLM
tree.

The matcher's autonomous classification scope is narrow on purpose:
``Independent``, ``BoundedHalo`` (via a pattern library), and
``Sequential`` for unrecognized carried-state. ``Associative`` is **not**
detected autonomously — users expressing associative parallelism do so
via ``task_generator`` sweep dimensions, which the framework's
``combine-wave`` machinery already handles.

The matcher itself is stdlib-only and total — it never raises (any
parse error or unrecognised pattern surfaces as a structured envelope
``data``). The primitive therefore declares ``error_codes=[]``;
uncertainty rides in the envelope ``data``, not on an error channel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = ["classify_axis_easy"]


@primitive(
    name="classify-axis-easy",
    verb="query",
    side_effects=[],
    error_codes=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Stdlib-only AST pattern-match for a @register_run function's "
            "DataAxis. Fast path used by the hpc-classify-axis skill: returns "
            "{kind, evidence, halo_expr?, tried}. `kind` is one of "
            "independent / bounded_halo / sequential / no_loop_detected / "
            "unclassifiable / function_not_found. `halo_expr` is populated "
            "when kind == bounded_halo. The skill falls back to its LLM "
            "decision tree on unclassifiable / no_loop_detected / "
            "function_not_found; everything else is recorded directly."
        ),
        args=(
            CliArg(
                "--source-path",
                type=str,
                required=True,
                help="Path to the .py / .ipynb source containing the @register_run function.",
            ),
            CliArg(
                "--run-name",
                type=str,
                required=True,
                help="Name of the @register_run-decorated function to classify.",
            ),
        ),
    ),
    agent_facing=True,
)
def classify_axis_easy(*, source_path: str, run_name: str) -> dict[str, Any]:
    """Pattern-match the body of *run_name* in *source_path*.

    The return shape mirrors
    :class:`hpc_agent.experiment_kit.axis_matcher.MatcherResult`:

    ``{kind, evidence, halo_expr, tried}``

    where ``halo_expr`` is ``None`` unless ``kind == "bounded_halo"`` and
    ``tried`` is the ordered list of pattern checks the matcher walked
    (useful so the calling skill knows which cheap patterns were already
    ruled out before falling back to the LLM tree).
    """
    from hpc_agent.experiment_kit.axis_matcher import classify_axis_easy as _match

    result = _match(Path(source_path), run_name)
    return {
        "kind": result.kind,
        "evidence": result.evidence,
        "halo_expr": result.halo_expr,
        "tried": list(result.tried),
    }
