"""The S1 MELD — the sign-off sitting carries the submit brief (prelude P2.c).

R-c (``docs/plans/expost-trust-2026-07-30.md``): *parks carry RESULTS, never
requests.* The audit's sign-off rendezvous is the human's LAST sitting before a
submit, and until now it carried only the audit view: the human signed, walked
away, and came back to a second rendezvous that asked the same "shall we run
this?" question the first one could have answered. This module is the half that
closes it — at the sign-off park the chain ALSO renders what ``submit-s1`` would
say, and fires the speculative canary so the answer is in before the ``y``.

Three things live here, all shared with the pre-existing S1 seat rather than
forked from it:

* :func:`compose_s1_preview` — the READ-ONLY S1 brief. It runs the SAME
  deterministic walk ``submit-s1`` runs (``walk-submit-ambiguities``, a pure
  function of the interview intent + the configured clusters) over the intent
  ``interview.json`` already holds. No run is minted, no sidecar is written, no
  cluster is touched: it is S1's resolve leg with its irreversible half removed.
* :func:`fire_speculative_canary` — THE one definition of the code-fired
  speculative canary (R2's Row 22 machinery, moved here from
  ``ops/submit_blocks.py`` so the S1 park and the melded park cannot drift).
  Budget-of-1, nudge-orphaning and the TTL cache are unchanged; what changed is
  WHO fires it (the chain, not an env opt-in).
* :func:`speculation_disabled` — the kill switch. ``HPC_S1_SPECULATE`` survives
  with its SENSE FLIPPED: it was the opt-in (``=1`` to enable, off by default);
  it is now the opt-OUT (``=0`` disables). The variable name is deliberately
  kept — an operator who set it to disable speculation in an emergency should
  find the same knob they read about, and a variable that silently changed
  meaning is worse than a renamed one, which is why the flip is stated in every
  place the old sense was documented.

**Why the sense flipped.** Row 22 shipped the machinery behind
``HPC_S1_SPECULATE=1``, OFF by default, and nothing ever set it — so the canary
that was supposed to be green before the human answered never fired once. An
opt-in whose only caller is a human remembering to export a variable is not a
feature; it is a documented intention. The chain now fires it at the boundary
where the ingredients exist, which is exactly the "canary fires while the human
reads" ruling. What did NOT change: the canary is still ``stop_after_canary``
(the main array is unreachable from ``submit-speculate`` by construction), still
budget-1 via the ``(cmd_sha, version)`` TTL cache, still journal-untouched, and
still best-effort — speculation is an optimization and is never load-bearing.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SPECULATE_KILL_SWITCH_ENV",
    "speculation_disabled",
    "compose_s1_preview",
    "fire_speculative_canary",
]

_log = logging.getLogger(__name__)

#: The kill switch, by name. Set to ``"0"`` to DISABLE chain-driven speculation.
SPECULATE_KILL_SWITCH_ENV = "HPC_S1_SPECULATE"


def speculation_disabled() -> bool:
    """True when the operator switched chain-driven speculation OFF.

    The ONE reader of the flipped env sense. Only the exact string ``"0"``
    disables: an unset variable, an empty one, or any other value leaves
    speculation ON, because the DEFAULT is now "speculate" and a typo must not
    silently restore the old never-fires behaviour.
    """
    return os.environ.get(SPECULATE_KILL_SWITCH_ENV) == "0"


# ── the read-only S1 preview ──────────────────────────────────────────────────


def _interview_intent(experiment_dir: Path) -> dict[str, Any] | None:
    """The persisted interview intent, or ``None`` when no interview exists yet.

    ``None`` is the ABSENT-AND-HONEST case the meld's whole contract rests on:
    before the interview runs there is no submit to preview, and inventing one
    from the audit records would be a fabricated plan attached to a sign-off.
    Reads through the ONE interview-doc reader, which is tolerant by design (a
    missing or corrupt file is skipped rather than raised).
    """
    try:
        from hpc_agent.state.interview_doc import iter_interview_docs

        for doc in iter_interview_docs(experiment_dir):
            if isinstance(doc, dict) and doc:
                return doc
    except Exception:  # noqa: BLE001 — the preview is disclosure, never load-bearing
        return None
    return None


def _walk_inputs(intent: dict[str, Any]) -> dict[str, Any]:
    """Project the persisted intent onto ``walk-submit-ambiguities``' inputs.

    Deliberately the SAME projection ``ops/memory/interview._compose_submit_walk``
    performs at the chain's exit edge — reading the same fields out of the same
    record — so the preview the human reads at sign-off and the spec the chain
    actually runs one hop later cannot describe different experiments. Nothing is
    defaulted here: a field the intent does not carry is simply absent, and the
    walk surfaces it as the ambiguity it is.
    """
    walk: dict[str, Any] = {
        "tasks_py_present": True,
        "entry_point_resolved": "entry_point" in intent,
    }
    goal = intent.get("goal")
    if isinstance(goal, str) and goal:
        walk["goal"] = goal
    task_generator = intent.get("task_generator")
    if isinstance(task_generator, dict) and task_generator:
        walk["task_generator"] = task_generator
    cluster_target = intent.get("cluster_target")
    if isinstance(cluster_target, dict) and isinstance(cluster_target.get("cluster"), str):
        walk["cluster"] = cluster_target["cluster"]
    try:
        from hpc_agent.infra.clusters import load_clusters_config

        loaded = load_clusters_config()
        walk["configured_clusters"] = (
            sorted(str(key) for key in loaded) if isinstance(loaded, dict) else []
        )
    except Exception:  # noqa: BLE001 — an unreadable config surfaces as an ambiguity
        walk["configured_clusters"] = []
    return walk


def compose_s1_preview(experiment_dir: Path) -> dict[str, Any] | None:
    """The OPTIONAL S1 brief melded into the audit sign-off park, or ``None``.

    ``None`` — and therefore NO ``s1_preview`` key in the sign-off brief — in
    exactly one case: no ``interview.json`` exists yet, so there is no submit to
    preview. That silence is the honest answer; the alternative (a preview
    assembled from the audit records) would put a plan the human never authored
    beside a signature request.

    When an interview DOES exist the preview carries:

    * ``walk`` — S1's own deterministic ambiguity walk, run READ-ONLY. Its
      ``resolved`` map and its ambiguities (each with the ``safe_default`` S1
      surfaces as a RECOMMENDATION, never auto-applied) are what the S1 brief
      would show. Nothing is minted or written by running it.
    * ``clean`` — whether the walk found nothing to ask, i.e. whether the S1
      rendezvous after this one would have had a question at all.
    * ``standing_consent`` — the R-a disclosure: what the standing grant means
      (the D1 bar, from the grant vocabulary's own home) and where the bindable
      grant LINE appears. The line itself is NOT rendered here, because a
      standing consent binds to a run identity (``cmd_sha``) that does not exist
      until the resolve leg mints it — offering an unbindable line would offer a
      grant that fails its own gate.

    Never raises: the whole block is a disclosure on a sign-off park, and a park
    that crashed because a preview could not be assembled would be strictly worse
    than one that says so.
    """
    intent = _interview_intent(experiment_dir)
    if intent is None:
        return None
    try:
        from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
        from hpc_agent.ops.walk_submit_ambiguities import walk_submit_ambiguities

        walk = walk_submit_ambiguities(
            spec=WalkSubmitAmbiguitiesInput.model_validate(_walk_inputs(intent))
        )
    except Exception as exc:  # noqa: BLE001 — the preview never fails the park
        return {
            "checked": False,
            "reason": (
                "an interview.json exists but the read-only submit walk could not be "
                f"composed from it ({exc}); submit-s1 re-runs the walk for real."
            ),
        }
    ambiguities = [dict(a) for a in walk.ambiguities]
    preview: dict[str, Any] = {
        "checked": True,
        "source": (
            "submit-s1's resolve leg, run READ-ONLY at this boundary: the same "
            "walk-submit-ambiguities S1 runs, over the intent interview.json "
            "already holds. No run_id was minted, no sidecar written, no cluster "
            "touched."
        ),
        "clean": not ambiguities,
        "resolved": dict(walk.resolved),
        "ambiguities": ambiguities,
        "standing_consent": {
            "bar": _standing_consent_bar(),
            "where": (
                "the bindable grant line is rendered at the submit-s1 park, where "
                "the run identity (cmd_sha) a standing consent must bind exists. "
                "It is disclosed HERE so the envelope is not a surprise there."
            ),
        },
    }
    if intent.get("cmd_sha"):
        preview["interview_cmd_sha12"] = str(intent["cmd_sha"])[:12]
    return preview


def _standing_consent_bar() -> str:
    """The D1 bar, read from the grant vocabulary's ONE home (never restated)."""
    from hpc_agent.ops.overnight import STANDING_CONSENT_BAR

    return STANDING_CONSENT_BAR


# ── the code-fired speculative canary (Row 22, re-homed) ──────────────────────


def fire_speculative_canary(
    experiment_dir: Path, *, run_id: str | None, brief: Any, stage_reached: str | None
) -> dict[str, Any]:
    """Fire S2's canary EARLY, in code, and report what happened (never raise).

    THE one definition (Row 22, moved out of ``ops/submit_blocks`` so the S1 park
    and the melded audit park cannot drift). Fires ONLY on a CLEAN ``resolved``
    boundary that MINTED a run_id and BUILT a submit-flow spec — an ambiguous walk
    or the PRE-RESOLVE boundary SKIP, the same skip rule the ``hpc-submit`` skill
    applies (required ambiguities are genuine human judgment, never speculated
    over).

    The fired verb is ``submit-speculate``, which composes
    ``submit-and-verify(stop_after_canary=True)`` — unstrippable on this path, the
    main array is unreachable from that verb. Its spec is composed by the run-14
    #4 composer (``block_chain.compose_successor_spec("submit-s2", …)``), the SAME
    code that materializes the S1→S2 successor, so the speculated spec is
    byte-identical to the one the human's ``y`` will run.

    ONE speculation-state definition: budget-of-1 and nudge-invalidation both come
    free from the ``(cmd_sha, version)`` canary TTL cache, which
    ``submit-speculate`` consults before firing. A nudge that moves ``cmd_sha``
    ORPHANS the speculative canary (cache miss → S2 re-canaries); the orphan
    drains naturally and nothing is cancelled. Journal-untouched: the detached
    launch writes only the ``_detached/`` handle.

    Returns a DISCLOSURE dict describing the outcome (``fired`` plus a stated
    ``reason``) so the brief can say what happened rather than leaving the human
    to infer it from silence. Never raises.
    """
    if speculation_disabled():
        return {
            "fired": False,
            "reason": (
                f"chain-driven speculation is disabled by {SPECULATE_KILL_SWITCH_ENV}=0 "
                "(the kill switch). The canary runs inside submit-s2 as usual."
            ),
        }
    if stage_reached != "resolved" or not run_id:
        return {
            "fired": False,
            "reason": (
                "no speculative canary: this boundary has no resolved run to canary "
                "(an ambiguous walk or the PRE-RESOLVE boundary). Required "
                "ambiguities are human judgment and are never speculated over."
            ),
        }
    resolve = brief.get("resolve") if isinstance(brief, dict) else None
    submit_flow = resolve.get("submit_spec") if isinstance(resolve, dict) else None
    if not isinstance(submit_flow, dict):
        return {
            "fired": False,
            "reason": (
                "no speculative canary: the boundary carries no built submit-flow "
                "spec, so there is nothing to canary without authoring one."
            ),
        }
    try:
        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached
        from hpc_agent.infra.block_chain import compose_successor_spec

        composed = compose_successor_spec(
            "submit-s2",
            spec_hint={"run_id": run_id},
            result_brief=brief if isinstance(brief, dict) else {},
        )
        launch_submit_block_detached(
            verb="submit-speculate",
            experiment_dir=str(experiment_dir),
            spec={"submit": composed["submit"], "detach": False},
        )
    except Exception as exc:  # noqa: BLE001 — speculation is never load-bearing
        _log.info(
            "speculative canary at the %s boundary for %s skipped (best-effort)",
            stage_reached,
            run_id,
            exc_info=True,
        )
        return {"fired": False, "reason": f"speculative canary could not be launched ({exc})."}
    return {
        "fired": True,
        "run_id": run_id,
        "reason": (
            "speculative canary launched in a detached worker while you read this "
            "brief — a plain `y` finds submit-s2 reusing a green canary. Budget is "
            "1 per (cmd_sha, version): a nudge that changes the spec orphans it and "
            "S2 re-canaries."
        ),
    }
