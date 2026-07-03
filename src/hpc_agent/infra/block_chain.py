"""The single source of truth for the deterministic block SUCCESSOR (§6/§8).

``next_block`` is **re-homed** (block-drive.md §6/§8) from an LLM affordance to
the driver's internal chaining table: the sequencing moves to code, the SoT is
projected — never copied. Before this, four block modules each carried a local
``_next_block(verb, why, **spec_hint)`` helper with the successor VERB hardcoded
at every terminator call site (``ops/submit_blocks.py``, ``ops/status_blocks.py``,
``ops/aggregate_blocks.py``, ``meta/campaign/blocks.py``). This module lifts the
``(current_verb, stage_reached) -> successor_verb`` mapping out of those call
sites into one authoritative table so the ``block-drive`` driver reads a single
deterministic chaining function instead of scraping inline literals.

The four block modules keep emitting the same ``{verb, why, spec_hint}`` hint
(the human-facing rationale stays per-call); their ``_next_block`` helpers now
DELEGATE to :func:`next_block_hint`, which derives the verb from
:data:`SUCCESSORS`. So the block Result JSON is unchanged; only the SoT of the
successor verb moved here.

Terminology:

* **current_verb** — the block that just ran (e.g. ``"submit-s2"``).
* **stage_reached** — the terminator it stopped at (the ``stage_reached`` field
  on its Result; the union per family lives in ``_wire/workflows/*_blocks.py``).
* **successor** — the deterministic next block verb, or ``None`` at a genuine
  human branch / terminal (recovery has no single deterministic successor; a
  harvest terminator is the end of the chain).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SUCCESSORS",
    "WORKFLOW_OF",
    "ORDER",
    "GATED_BLOCKS",
    "successor_verb",
    "workflow_of",
    "block_index",
    "next_block_hint",
    "is_gated",
]


# ── workflow membership ───────────────────────────────────────────────────────

# The ordered block chain of each workflow family. ``block_index`` uses the
# position for §4 routing (is a changed field owned by an EARLIER / current /
# downstream block). A campaign is not a per-run linear chain but still has a
# stable touchpoint order (greenlight → watch → complete).
ORDER: dict[str, list[str]] = {
    "submit": ["submit-s1", "submit-s2", "submit-s3", "submit-s4"],
    "status": ["status-snapshot", "status-watch"],
    "aggregate": ["aggregate-check", "aggregate-run"],
    "campaign": ["campaign-greenlight", "campaign-watch", "campaign-complete"],
}

# Each block verb → its workflow family. Derived from ORDER so the two can never
# drift (a verb appears in exactly one family's chain).
WORKFLOW_OF: dict[str, str] = {
    verb: workflow for workflow, verbs in ORDER.items() for verb in verbs
}


# ── the greenlight-gated blocks ───────────────────────────────────────────────

# The block verbs whose op body calls ``ops/block_gate.assert_greenlit_target``
# before it acts on the cluster — the SINGLE SOURCE OF TRUTH for "the driver must
# PARK for a human greenlight before entering this block". Derived by grepping the
# callers of ``assert_greenlit_target``: ``ops/submit_blocks.py`` guards
# ``submit-s2`` / ``submit-s3`` / ``submit-s4`` and ``ops/aggregate_blocks.py``
# guards ``aggregate-run``. A block-drive IN-CODE chain never journals the ``y``
# these gates require, so the driver stops at the rendezvous before any member and
# lets the human greenlight it (``block_drive._chain``). ``status-watch`` and the
# ``campaign-*`` blocks are UNGATED and chain in code. The ``test_block_chain``
# suite pins this set against the live gate callers so the two cannot drift.
GATED_BLOCKS: frozenset[str] = frozenset({"submit-s2", "submit-s3", "submit-s4", "aggregate-run"})


# ── the successor table ───────────────────────────────────────────────────────

# ``(current_verb, stage_reached) -> successor_verb | None``. Populated by reading
# the actual terminators in the four block modules: every terminator that emits
# ``next_block=_next_block(...)`` yields a ``-> "<verb>"`` entry; every terminator
# that emits ``next_block=None`` (a human branch or the end of a chain) yields a
# ``-> None`` entry. Covers ALL stage_reached values for all four families.
#
# NOTE on the two runtime-gated terminators (still recorded here as their single
# deterministic successor — the driver applies the runtime guard):
#   * (status-snapshot, snapshot_clean) -> status-watch: the block emits this hint
#     ONLY when a live (non-terminal) run exists; an all-terminal / empty fleet
#     has nothing to watch and emits None. The successor WHEN there is work is
#     status-watch.
#   * (submit-s1, resolved) -> submit-s2: on the resolve leg the block passes
#     ``rr.stage_reached``; ``resolved`` chains to S2 while ``prior_run_found`` /
#     ``needs_scaffold_interview`` are human branches (below, -> None).
SUCCESSORS: dict[tuple[str, str], str | None] = {
    # ── submit family ─────────────────────────────────────────────────────────
    # submit-s1 (resolve)
    ("submit-s1", "needs_resolution"): None,  # ambiguity brief — human resolves.
    ("submit-s1", "resolved"): "submit-s2",  # clean → stage & canary.
    ("submit-s1", "prior_run_found"): None,  # resume-vs-fresh — human branch.
    ("submit-s1", "needs_scaffold_interview"): None,  # scaffold sub-interview branch.
    # submit-s2 (stage & canary)
    ("submit-s2", "canary_verified"): "submit-s3",  # green → submit & watch.
    ("submit-s2", "canary_failed"): None,  # anomaly terminator — propose a fix.
    ("submit-s2", "deduped"): None,  # run already exists — confirm resume-vs-fresh.
    ("submit-s2", "detached"): None,  # brief arrives via the journal, not this process.
    # submit-s3 (submit & watch)
    ("submit-s3", "watching_terminal"): "submit-s4",  # complete → harvest.
    ("submit-s3", "watching_timeout"): "status-watch",  # jobs may run on — keep watching.
    ("submit-s3", "watching_anomaly"): None,  # failed/abandoned — human picks recovery.
    ("submit-s3", "detached"): None,  # brief arrives via the journal.
    # submit-s4 (harvest) — end of the submit chain.
    ("submit-s4", "harvested"): None,
    ("submit-s4", "harvest_partial"): None,
    # ── status family ─────────────────────────────────────────────────────────
    # status-snapshot
    ("status-snapshot", "snapshot_clean"): "status-watch",  # live run → watch (see note).
    ("status-snapshot", "snapshot_anomaly"): None,  # stalled/failed — human decides.
    # status-watch
    ("status-watch", "watch_terminal"): "submit-s4",  # complete → harvest.
    ("status-watch", "watch_timeout"): "status-watch",  # keep watching (self-loop).
    ("status-watch", "watch_anomaly"): None,  # failed/abandoned — evidence brief.
    # ── aggregate family ──────────────────────────────────────────────────────
    # aggregate-check
    ("aggregate-check", "ready"): "aggregate-run",  # clean → combine + reduce.
    ("aggregate-check", "not_ready"): None,  # readiness gate failed — human decides.
    ("aggregate-check", "integrity_review"): None,  # integrity issue — never auto-masked.
    # aggregate-run — end of the aggregate chain.
    ("aggregate-run", "harvested"): None,
    ("aggregate-run", "harvest_partial"): None,
    # ── campaign family ───────────────────────────────────────────────────────
    # campaign-greenlight
    ("campaign-greenlight", "greenlit"): "campaign-watch",  # stamped → observe.
    ("campaign-greenlight", "already_greenlit"): "campaign-watch",  # idempotent re-read → observe.
    ("campaign-greenlight", "needs_greenlight"): None,  # awaiting the once-at-start y/nudge.
    # campaign-watch
    ("campaign-watch", "watching_complete"): "campaign-complete",  # stop fired → completion.
    ("campaign-watch", "watching_healthy"): None,  # self-chains async — no boundary, no hint.
    ("campaign-watch", "watching_anomaly"): None,  # loud-fail / budget halt — human decides.
    # campaign-complete — end of the campaign chain.
    ("campaign-complete", "complete"): None,
}


# ── lookups ───────────────────────────────────────────────────────────────────


def successor_verb(current_verb: str, stage_reached: str) -> str | None:
    """Return the deterministic successor of ``(current_verb, stage_reached)``.

    Table lookup into :data:`SUCCESSORS`. Policy for an unknown pair: return
    ``None`` — meaning "no deterministic successor; this is a human branch". This
    is deliberately lenient (no ``KeyError``) so a new or debug terminator degrades
    to "the human decides" rather than crashing the driver; the ``block_chain``
    unit test guards the table against silently dropping a KNOWN successor by
    asserting it agrees with what each block module emits.
    """
    return SUCCESSORS.get((current_verb, stage_reached))


def workflow_of(verb: str) -> str:
    """Return the workflow family ("submit"/"status"/"aggregate"/"campaign") of *verb*.

    Raises :class:`KeyError` for an unknown verb — a verb with no family is a
    programming error (a typo or an unregistered block), not a human branch.
    """
    return WORKFLOW_OF[verb]


def block_index(verb: str) -> int:
    """Return *verb*'s 0-based position within its workflow's :data:`ORDER` chain.

    Used by §4 routing to compare block positions (a changed field owned by an
    EARLIER block is a rewind; by the current block, a re-run; strictly downstream,
    advance-carrying). Raises :class:`KeyError` / :class:`ValueError` for an
    unknown verb.
    """
    return ORDER[workflow_of(verb)].index(verb)


def next_block_hint(
    current_verb: str,
    stage_reached: str,
    *,
    why: str,
    **spec_hint: Any,
) -> dict[str, Any] | None:
    """Build the ``{verb, why, spec_hint}`` next-block hint from the table.

    The shared builder the four block modules' ``_next_block`` helpers delegate to
    (design §6/§8). Looks up the deterministic successor of
    ``(current_verb, stage_reached)`` and, when one exists, returns the same hint
    dict the blocks emitted before the re-home: ``verb`` from the table, ``why``
    the caller's human-facing rationale, ``spec_hint`` the minimal next-spec
    skeleton (run_id / campaign_id / canary ids). Returns ``None`` at a terminal /
    human-branch terminator (no successor), so a caller can pass a stage through
    unconditionally and get the correct null for a branch.
    """
    verb = successor_verb(current_verb, stage_reached)
    if verb is None:
        return None
    return {"verb": verb, "why": why, "spec_hint": dict(spec_hint)}


def is_gated(verb: str) -> bool:
    """True when *verb*'s op body calls the greenlight gate (:data:`GATED_BLOCKS`).

    The SoT for the driver's park-before-gated rule: a ``block-drive`` in-code
    chain never journals the human ``y`` that ``assert_greenlit_target`` requires,
    so the driver must PARK for a greenlight before chaining into a gated block
    (``block_drive._chain``). Chains freely through every ungated block.
    """
    return verb in GATED_BLOCKS
