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

# The matcher kinds confident enough to record without the LLM tree — the
# "code" branch. Everything else (unclassifiable / function_not_found) abstains
# to the hpc-classify-axis LLM decision tree — the "judgement" branch.
_CONFIDENT_KINDS = frozenset({"independent", "bounded_halo", "sequential", "no_loop_detected"})
# The axis kinds the LLM tree chooses among when the matcher abstains.
_AXIS_CANDIDATES = ("independent", "bounded_halo", "sequential", "associative")


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
            "when kind == bounded_halo. The skill records a confident hit "
            "directly — including no_loop_detected as the terminal `cartesian` "
            "(no-series) verdict; only unclassifiable / function_not_found "
            "fall back to its LLM decision tree."
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
    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction, Escalation
    from hpc_agent.experiment_kit.axis_matcher import classify_axis_easy as _match

    result = _match(Path(source_path), run_name)

    # Route the verdict through the shared decision kernel so this matcher is
    # symmetric with classify-campaign-path: a confident kind resolves
    # decided_by="code"; the unclassifiable / function_not_found tail abstains
    # to judgement, where the hpc-classify-axis LLM tree picks among the axis
    # kinds. Same evaluator every decision point uses.
    def _confident(_: Any) -> CandidateAction | None:
        if result.kind in _CONFIDENT_KINDS:
            return CandidateAction(
                action=result.kind, source="catalog", rationale=f"AST matched {result.kind}"
            )
        return None

    def _to_llm_tree(_: Any) -> Escalation:
        return Escalation(
            decided_by="judgement",
            reason=f"{result.kind}: matcher abstained — the hpc-classify-axis LLM tree decides",
            candidate_actions=[
                CandidateAction(action=k, source="catalog") for k in _AXIS_CANDIDATES
            ],
        )

    decision = decide("axis_class", None, rules=[_confident], on_abstain=_to_llm_tree)
    return {
        "kind": result.kind,
        "decided_by": decision.decided_by,
        "evidence": result.evidence,
        "halo_expr": result.halo_expr,
        "tried": list(result.tried),
    }
