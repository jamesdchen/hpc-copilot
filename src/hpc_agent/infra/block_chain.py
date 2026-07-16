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

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "SUCCESSORS",
    "WORKFLOW_OF",
    "ORDER",
    "GATED_BLOCKS",
    "ANOMALY_TERMINATORS",
    "DEADLINE_SECONDS",
    "WATCH_VERBS",
    "WATCH_BUDGET_SLACK_SEC",
    "successor_verb",
    "chain_successor",
    "workflow_of",
    "block_index",
    "next_block_hint",
    "is_gated",
    "recovery_arm_verb",
    "verb_deadline_seconds",
    "SuccessorSpecIncomplete",
    "compose_successor_spec",
    "successor_spec_sha",
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
    # RFC #362: campaign-refill is a side-spur off campaign-watch (watching_refill),
    # NOT a linear campaign touchpoint — adding it to the "campaign" chain would
    # shift the block_index positions the §4 field-change routing compares for the
    # three real touchpoints. Its own single-member family gives it a WORKFLOW_OF
    # entry + block_index(0) (so the SUCCESSORS coverage test's WORKFLOW_OF
    # membership assertion holds) without perturbing the campaign chain.
    "campaign-refill": ["campaign-refill"],
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


# ── per-verb driver deadlines ─────────────────────────────────────────────────

# How long the DRIVER lets one ``hpc-agent <verb>`` block subprocess run before
# killing it (the proving-run-#3 wedge class: an unbounded parent-side wait —
# see ``tests/contracts/test_src_subprocess_timeout_discipline.py``). These are
# generous last-resort ceilings, not schedules: a healthy block finishes long
# before its deadline, and the watch-class blocks derive theirs from the spec's
# own ``wall_clock_budget_seconds`` so the driver never undercuts a legitimate
# poll-to-terminal budget.

# Quick, journal/SSH-probe-scale blocks (resolve / snapshot / check /
# greenlight / complete): minutes suffice.
_QUICK_VERB_DEADLINE_SEC: float = 600.0
# Cluster-mutating blocks that stage, canary, or harvest (rsync + scheduler
# round-trips + payload downloads): an hour is generous.
_HEAVY_VERB_DEADLINE_SEC: float = 3600.0
# Watch-class fallback when the spec carries no budget — matches the
# ``MonitorFlowSpec.wall_clock_budget_seconds`` default (86400).
_WATCH_DEFAULT_BUDGET_SEC: float = 86400.0
# Slack added on top of a watch block's own wall-clock budget so the block's
# internal timeout terminator fires FIRST (it exits cleanly with
# ``watching_timeout``/``watch_timeout``); the driver deadline is only the
# backstop for a block that wedged past its own budget.
WATCH_BUDGET_SLACK_SEC: float = 900.0

# The watch/wait-class blocks: their runtime is the run's own poll-to-terminal
# budget, so their deadline is spec-derived (budget + slack), never a constant.
WATCH_VERBS: frozenset[str] = frozenset({"submit-s3", "status-watch", "campaign-watch"})

# The non-watch block verbs → their fixed driver deadline (seconds).
DEADLINE_SECONDS: dict[str, float] = {
    "submit-s1": _QUICK_VERB_DEADLINE_SEC,
    "submit-s2": _HEAVY_VERB_DEADLINE_SEC,
    "submit-s4": _HEAVY_VERB_DEADLINE_SEC,
    "status-snapshot": _QUICK_VERB_DEADLINE_SEC,
    "aggregate-check": _QUICK_VERB_DEADLINE_SEC,
    "aggregate-run": _HEAVY_VERB_DEADLINE_SEC,
    "campaign-greenlight": _QUICK_VERB_DEADLINE_SEC,
    "campaign-complete": _QUICK_VERB_DEADLINE_SEC,
}


def _spec_wall_clock_budget(spec: Mapping[str, Any] | None) -> float | None:
    """Extract ``wall_clock_budget_seconds`` from a block input *spec*, if any.

    The watch-class specs nest it under their embedded monitor spec
    (``submit-s3`` / ``status-watch`` → ``spec["monitor"]``); a bare
    ``monitor-flow`` spec carries it at top level. Returns ``None`` when absent
    or non-numeric — the caller falls back to the default ceiling.
    """
    if not isinstance(spec, Mapping):
        return None
    for candidate in (spec.get("monitor"), spec):
        if isinstance(candidate, Mapping):
            value = candidate.get("wall_clock_budget_seconds")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return float(value)
    return None


def verb_deadline_seconds(verb: str, spec: Mapping[str, Any] | None = None) -> float:
    """The driver-side kill deadline (seconds) for one *verb* subprocess.

    Watch-class verbs (:data:`WATCH_VERBS`) — and, conservatively, any verb this
    table does not know (e.g. the tick-loop's ``monitor-flow``/``aggregate-flow``
    steps) — get their *spec*'s own ``wall_clock_budget_seconds`` plus
    :data:`WATCH_BUDGET_SLACK_SEC`, falling back to the 24 h default budget when
    the spec carries none. Everything else reads its class deadline from
    :data:`DEADLINE_SECONDS`. Always returns a finite positive bound — this is
    the guarantee that no block-verb subprocess is ever awaited unboundedly.
    """
    if verb in WATCH_VERBS or verb not in DEADLINE_SECONDS:
        budget = _spec_wall_clock_budget(spec)
        if budget is None:
            budget = _WATCH_DEFAULT_BUDGET_SEC
        return budget + WATCH_BUDGET_SLACK_SEC
    return DEADLINE_SECONDS[verb]


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
    ("submit-s4", "detached"): None,  # brief arrives via the journal.
    # ── status family ─────────────────────────────────────────────────────────
    # status-snapshot
    ("status-snapshot", "snapshot_clean"): "status-watch",  # live run → watch (see note).
    ("status-snapshot", "snapshot_anomaly"): None,  # stalled/failed — human decides.
    # status-watch
    ("status-watch", "watch_terminal"): "submit-s4",  # complete → harvest.
    ("status-watch", "watch_timeout"): "status-watch",  # keep watching (self-loop).
    ("status-watch", "watch_anomaly"): None,  # failed/abandoned — evidence brief.
    ("status-watch", "detached"): None,  # detached child owns the poll; brief via the journal.
    # ── aggregate family ──────────────────────────────────────────────────────
    # aggregate-check
    ("aggregate-check", "ready"): "aggregate-run",  # clean → combine + reduce.
    ("aggregate-check", "not_ready"): None,  # readiness gate failed — human decides.
    ("aggregate-check", "integrity_review"): None,  # integrity issue — never auto-masked.
    # aggregate-run — end of the aggregate chain.
    ("aggregate-run", "harvested"): None,
    ("aggregate-run", "harvest_partial"): None,
    ("aggregate-run", "detached"): None,  # brief arrives via the journal.
    # ── campaign family ───────────────────────────────────────────────────────
    # campaign-greenlight
    ("campaign-greenlight", "greenlit"): "campaign-watch",  # stamped → observe.
    ("campaign-greenlight", "already_greenlit"): "campaign-watch",  # idempotent re-read → observe.
    ("campaign-greenlight", "needs_greenlight"): None,  # awaiting the once-at-start y/nudge.
    # campaign-watch
    ("campaign-watch", "watching_complete"): "campaign-complete",  # stop fired → completion.
    ("campaign-watch", "watching_healthy"): None,  # self-chains async — no boundary, no hint.
    ("campaign-watch", "watching_refill"): "campaign-refill",  # free slots → refill (#362).
    ("campaign-watch", "watching_anomaly"): None,  # loud-fail / budget halt — human decides.
    # campaign-complete — end of the campaign chain.
    ("campaign-complete", "complete"): None,
    # campaign-refill (RFC #362) — a side-spur off watch; every stage ends the
    # chain, so the next cron/loop tick re-enters via campaign-watch (one step
    # per tick). NOT in ORDER["campaign"] (it is not a linear touchpoint) and NOT
    # in GATED_BLOCKS (its own greenlight refusal is the consent check).
    ("campaign-refill", "refilled"): None,  # chain ends; next tick re-enters via campaign-watch.
    ("campaign-refill", "no_refill_needed"): None,  # advance said wait/stop/continue — noop.
    ("campaign-refill", "refill_blocked"): None,  # live-prior / scaffold escalation — human.
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


def chain_successor(verb: str) -> str | None:
    """Return the block immediately AFTER *verb* in its family's linear :data:`ORDER`.

    DISTINCT from :func:`successor_verb`, which keys on the runtime ``stage_reached``
    and returns ``None`` at a decision / human-branch stage. This is the STATIC
    chain-forward block — the target a human OVERRIDE greenlight names when a block
    parked a *decision* with no code-determined auto-successor.

    The motivating case (run-13 ``causal_tune_linear-de448128``): ``aggregate-check``
    parked at ``not_ready`` (its reconcile was still in flight, so the run was
    non-terminal) whose ``SUCCESSORS`` entry is ``None`` — the marker recorded
    ``next_verb=None``. But the human's ``y`` at that boundary is an override that
    greenlights the only forward move, ``aggregate-run``. The boundary predicate
    (:func:`block_drive.committed_greenlight_for_boundary`) maps a ``None`` marker
    target through here so the greenlight's ``resolved["next_block"]`` and the parked
    boundary agree on ONE target. Returns ``None`` for a verb that is last in its
    family (a genuine terminal) or not a linear touchpoint (e.g. ``campaign-refill``).
    """
    family = WORKFLOW_OF.get(verb)
    if family is None:
        return None
    order = ORDER.get(family, [])
    try:
        idx = order.index(verb)
    except ValueError:
        return None
    return order[idx + 1] if idx + 1 < len(order) else None


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
    return {"verb": verb, "why": why, "spec_hint": _complete_spec_hint(verb, dict(spec_hint))}


# ── spec_hint completeness (notebook-audit.md Addendum 8, item 13) ─────────────

# The driver passes an UNGATED successor's ``spec_hint`` VERBATIM as that block's
# input spec (``_kernel/lifecycle/block_drive._chain``). A hint that omits a
# required NESTED sub-block the successor's ``--spec`` model mandates bounces off
# that validator the instant the driver crosses the boundary — an unrecoverable
# mid-chain stall on an unattended tick (run #11: a submit-s3 spec bounced on a
# missing required ``monitor``). Where a successor requires the run identity under
# a nested sub-block, compose that shape HERE from the flat ``run_id`` the
# terminator already carries; the sub-spec's own schema defaults fill the rest
# (``MonitorFlowSpec`` needs only ``run_id``). This is IDENTITY reshaping over
# opaque caller content (engineering-principles Q1), never fabricating semantics —
# and it is idempotent: a terminator that already emits the nested shape (e.g.
# ``status-snapshot`` → ``{"monitor": {"run_id": ...}}``) is left untouched.


def _wrap_run_id_under(nested_key: str) -> Any:
    """A shaper that lifts a flat ``run_id`` hint into ``{nested_key: {run_id}}``.

    A no-op when the hint carries no ``run_id`` or already nests ``nested_key``.
    Non-``run_id`` fields the terminator passed (e.g. ``canary_run_id``) are
    preserved alongside — they stay declared fields on the successor's spec.
    """

    def _shape(spec_hint: dict[str, Any]) -> dict[str, Any]:
        run_id = spec_hint.get("run_id")
        if run_id is None or nested_key in spec_hint:
            return spec_hint
        reshaped = {k: v for k, v in spec_hint.items() if k != "run_id"}
        reshaped[nested_key] = {"run_id": run_id}
        return reshaped

    return _shape


# Successor verb → the shaper that completes its ``spec_hint`` into a shape the
# successor's spec model accepts. Only the UNGATED chained successors need this
# (a gated successor parks and runs under the human-committed ``resolved`` spec,
# not the hint — ``block_drive.run_tick`` §3): ``status-watch`` embeds the run
# identity under a required ``monitor`` sub-block, so a flat-``run_id`` terminator
# (``submit-s3`` watching_timeout, ``status-watch`` watch_timeout self-loop) would
# otherwise hand the driver a spec ``StatusWatchSpec`` rejects.
_SUCCESSOR_SPEC_SHAPERS: dict[str, Any] = {
    "status-watch": _wrap_run_id_under("monitor"),
}


def _complete_spec_hint(successor: str, spec_hint: dict[str, Any]) -> dict[str, Any]:
    """Reshape ``spec_hint`` into a shape *successor*'s ``--spec`` model accepts.

    The completeness half of the successor table: ``next_block_hint`` routes every
    hint through here so an ungated in-code chain never hands the driver a spec its
    successor's validator would bounce. Pinned by
    ``tests/contracts/test_spec_hint_completeness.py``.
    """
    shaper = _SUCCESSOR_SPEC_SHAPERS.get(successor)
    return shaper(spec_hint) if shaper is not None else spec_hint


# ── materialized successor specs (run-14 finding #4: no LLM over-authoring) ─────
#
# Run-14 #4 (USER-NAMED): every boundary must MATERIALIZE its complete successor
# spec as a FILE, so a CLI fallback = invoke-the-file, never re-author it. The
# motivating failures were the S1→S2 re-transcription (the agent hand-rebuilt the
# submit spec at the boundary and dropped fields) and the submit-speculate shape
# failure (migrate-remainder-2026-07-16/SPEC.md precedent notes). The composer
# below builds a successor's COMPLETE input spec IN CODE by REUSING what the
# predecessor already produced — never fabricating a caller-owned field.
#
# THE composition doctrine (enforcement rows 14–16):
#   * Row 14 — the composer NEVER fills a REQUIRED_CALLER_FIELD. A boundary that
#     cannot source a required caller-owned sub-spec (the run's ``submit`` /
#     ``goal`` / ``task_generator``) RAISES :class:`SuccessorSpecIncomplete`; the
#     caller then materializes NOTHING (refuse, never fabricate — the
#     ``ops/submit/field_partition`` anti-pattern this exists to kill). Mirrors
#     ``overnight.py``'s "cmd_sha is NEVER composed".
#   * Row 16 — the composed spec is sha-stamped at park (:func:`successor_spec_sha`
#     over the byte-stable sorted-keys JSON); consumption recomputes and refuses on
#     drift (the R3 leg — NOT built here). Same inputs → same bytes → same sha.


class SuccessorSpecIncomplete(Exception):
    """A complete successor spec cannot be composed without fabricating a caller field.

    Row 14: the composer reuses what the predecessor produced and derives identity
    sub-shapes (``monitor`` from ``run_id``), but a REQUIRED_CALLER_FIELD it cannot
    source — the run's ``submit`` sub-spec, ``goal`` / ``task_generator`` — is never
    invented. When one is missing the composer raises this; the materialization
    caller refuses (no file written), so the run-14 #4 over-authoring class cannot
    survive through a fabricated-default back door.
    """

    def __init__(self, successor: str, missing: str) -> None:
        self.successor = successor
        self.missing = missing
        super().__init__(
            f"cannot compose a complete {successor!r} spec: {missing!r} is a caller-owned "
            "field the composer must not fabricate (run-14 #4) — refuse, do not materialize."
        )


def _compose_submit_s2_spec(
    spec_hint: dict[str, Any], predecessor_spec: dict[str, Any], result_brief: dict[str, Any]
) -> dict[str, Any]:
    """S1→S2: reuse the ``submit-and-verify`` spec S1's resolve leg BUILT (never re-author).

    The SubmitFlowSpec is composed by ``resolve-submit-inputs`` and rides the S1
    result brief (``brief["resolve"]["submit_spec"]``) — the exact S1→S2
    re-transcription case run-14 #4 names. A boundary whose resolve leg has not run
    (the PRE-RESOLVE brief, run_id unminted) carries no ``submit_spec`` → refuse.
    """
    resolve = result_brief.get("resolve")
    submit_flow = resolve.get("submit_spec") if isinstance(resolve, dict) else None
    if not isinstance(submit_flow, dict):
        raise SuccessorSpecIncomplete("submit-s2", "submit")
    return {"submit": {"submit": submit_flow}}


def _compose_submit_s3_spec(
    spec_hint: dict[str, Any], predecessor_spec: dict[str, Any], result_brief: dict[str, Any]
) -> dict[str, Any]:
    """S2→S3: reuse S2's ``submit`` sub-spec + derive the ``monitor`` shape from run_id.

    THE proof case. ``SubmitS3Spec`` requires ``submit`` (the SAME submit-and-verify
    spec S2 ran — pulled VERBATIM from the predecessor's own input spec, never
    re-authored) and ``monitor`` (a ``MonitorFlowSpec`` whose only required field is
    ``run_id`` — the identity reshape ``_wrap_run_id_under`` encodes). ``invocation_argv``
    is agent-known and now OPTIONAL on the model, so the composer omits it (never
    fabricates it). The canary ids ride the hint.
    """
    submit = predecessor_spec.get("submit")
    if not isinstance(submit, dict):
        raise SuccessorSpecIncomplete("submit-s3", "submit")
    run_id = spec_hint.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise SuccessorSpecIncomplete("submit-s3", "run_id")
    composed: dict[str, Any] = {"submit": submit, "monitor": {"run_id": run_id}}
    for key in ("canary_run_id", "canary_job_ids"):
        value = spec_hint.get(key)
        if value is not None:
            composed[key] = value
    return composed


def _compose_submit_s4_spec(
    spec_hint: dict[str, Any], predecessor_spec: dict[str, Any], result_brief: dict[str, Any]
) -> dict[str, Any]:
    """S3→S4: derive the ``aggregate`` shape from the run identity.

    ``SubmitS4Spec`` requires ``aggregate`` (an ``AggregateFlowSpec`` whose only
    required field is ``run_id``). No caller-owned field is fabricated — the harvest
    reads the run's own outputs — so a run_id is all the composer needs.
    """
    run_id = spec_hint.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise SuccessorSpecIncomplete("submit-s4", "run_id")
    return {"aggregate": {"run_id": run_id}}


def _compose_aggregate_run_spec(
    spec_hint: dict[str, Any], predecessor_spec: dict[str, Any], result_brief: dict[str, Any]
) -> dict[str, Any]:
    """aggregate-check→aggregate-run: the check already emits the nested ``aggregate`` hint.

    ``aggregate-check`` composes ``spec_hint={"aggregate": {"run_id": ...}}`` at its
    ``ready`` terminator, so the complete spec is the hint itself. Fall back to
    deriving ``aggregate`` from a flat ``run_id`` when a caller passed one.
    """
    agg = spec_hint.get("aggregate")
    if isinstance(agg, dict) and agg.get("run_id"):
        return {"aggregate": dict(agg)}
    run_id = spec_hint.get("run_id")
    if isinstance(run_id, str) and run_id:
        return {"aggregate": {"run_id": run_id}}
    raise SuccessorSpecIncomplete("aggregate-run", "aggregate.run_id")


# Gated successor verb → the composer that builds its COMPLETE input spec from the
# predecessor's own products (input spec + result brief) + code-derived identity
# shapes. An ungated successor is absent here: its ``spec_hint`` is already the
# complete shaped spec (``_complete_spec_hint`` ran at ``next_block_hint`` time), so
# :func:`compose_successor_spec` returns it verbatim.
_GATED_SPEC_COMPOSERS: dict[str, Any] = {
    "submit-s2": _compose_submit_s2_spec,
    "submit-s3": _compose_submit_s3_spec,
    "submit-s4": _compose_submit_s4_spec,
    "aggregate-run": _compose_aggregate_run_spec,
}


def compose_successor_spec(
    successor: str,
    *,
    spec_hint: Mapping[str, Any],
    predecessor_spec: Mapping[str, Any] | None = None,
    result_brief: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose *successor*'s COMPLETE input spec in code (run-14 #4 materialization).

    For a GATED successor (:data:`_GATED_SPEC_COMPOSERS`) the composer reuses the
    predecessor's own products — its input ``spec`` and the result ``brief`` — plus
    code-derived identity shapes, never fabricating a caller-owned field (Row 14 —
    a boundary that cannot source one raises :class:`SuccessorSpecIncomplete`). For
    an UNGATED successor the ``spec_hint`` is already the complete shaped spec, so it
    is returned verbatim. Pure: computes the spec dict, writes nothing (the
    materialization I/O + validation live in the block-drive caller).
    """
    composer = _GATED_SPEC_COMPOSERS.get(successor)
    if composer is None:
        return dict(spec_hint)
    composed: dict[str, Any] = composer(
        dict(spec_hint), dict(predecessor_spec or {}), dict(result_brief or {})
    )
    return composed


def successor_spec_sha(spec: Mapping[str, Any]) -> str:
    """A byte-stable identity for a composed successor *spec* (Row 16 sha-pin).

    SHA-256 over the sorted-keys JSON — the SAME serialization
    :func:`hpc_agent.infra.io.atomic_write_json` writes, so the sha stamped at park
    is exactly the sha a consumer recomputes over the materialized file's bytes.
    Same inputs → same bytes → same sha: the byte-stability the R3 drift-refuse leg
    builds on (that recompute-and-refuse is NOT this function's job — it only stamps).
    """
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def is_gated(verb: str) -> bool:
    """True when *verb*'s op body calls the greenlight gate (:data:`GATED_BLOCKS`).

    The SoT for the driver's park-before-gated rule: a ``block-drive`` in-code
    chain never journals the human ``y`` that ``assert_greenlit_target`` requires,
    so the driver must PARK for a greenlight before chaining into a gated block
    (``block_drive._chain``). Chains freely through every ungated block.
    """
    return verb in GATED_BLOCKS


# ── the anomaly recovery arms ──────────────────────────────────────────────────

# The block terminators where recovery is a genuine HUMAN branch — ``SUCCESSORS``
# maps each to ``None`` (a bare ``y`` has no deterministic successor: the human's
# nudge names the recovery). Kept as a NAMED set (not re-scraped from SUCCESSORS)
# so ``recovery_arm_verb`` is explicit about WHERE a delta-selected arm applies,
# and so a future terminator that gains an arm is a one-line, auditable edit.
ANOMALY_TERMINATORS: frozenset[tuple[str, str]] = frozenset(
    {
        ("submit-s2", "canary_failed"),
        ("submit-s3", "watching_anomaly"),
    }
)

# Recovery-arm verb keyed by the DELTA's target field (design §4.1: "the route is
# a function of the spec — the delta's target field selects the arm, computed in
# code, never a verb the model picks"). A ``cluster`` delta selects ``retarget-run``
# (proving-run-5 wave 5.2) — the one composite that supersedes the failed attempt,
# re-resolves under a NEW run_name + the new cluster, and re-canaries. Other
# recovery deltas (resume / kill / fix-and-retry) stay human branches (``None``)
# until their arm lands. Kept SEPARATE from ``SUCCESSORS`` on purpose: an arm is
# NOT a deterministic successor of the STAGE (a bare ``y`` at an anomaly has no
# successor) — it is selected only when the human's nudge names the recovery, so
# folding it into SUCCESSORS would wrongly auto-chain every anomaly to retarget.
_RECOVERY_ARM_BY_FIELD: dict[str, str] = {"cluster": "retarget-run"}


def recovery_arm_verb(
    current_verb: str, stage_reached: str, delta_fields: Iterable[str]
) -> str | None:
    """The recovery-arm verb for a nudge DELTA at an anomaly terminator, else None.

    Design §4.1: at an anomaly terminator (:data:`ANOMALY_TERMINATORS` —
    ``canary_failed`` / ``watching_anomaly``) a nudge that names an anomaly
    recovery routes to the recovery arm; the route is a FUNCTION OF THE SPEC — the
    delta's target field selects the arm, computed HERE in code, never a verb the
    model picks. A delta touching ``cluster`` selects ``retarget-run`` (supersede →
    re-resolve under a new run_name → re-canary). Returns ``None`` when the pair is
    not an anomaly terminator, or when the delta names no field that maps to an arm
    (a genuine human branch — resubmit / kill / fix).

    This is the SoT the ``hpc-submit`` skill consults at the rendezvous (mirroring
    how a spec-changing nudge routes through ``revise-resolved``): the LLM names the
    delta; code maps the delta to the arm. It is deliberately kept off the driver's
    deterministic ``SUCCESSORS`` auto-chain — a bare ``y`` at an anomaly has no
    successor, so the arm fires only when the human's nudge selects it.
    """
    if (current_verb, stage_reached) not in ANOMALY_TERMINATORS:
        return None
    for field in delta_fields:
        arm = _RECOVERY_ARM_BY_FIELD.get(field)
        if arm is not None:
            return arm
    return None
