"""Greenlight-names-target gate for the sequenced block verbs (design §2).

Before a block that ACTS on the cluster runs (``submit-s2`` stage & canary,
``submit-s3`` main-array launch, ``submit-s4`` harvest, ``aggregate-run`` reduce),
its precondition gate verifies that the human actually greenlit *this* verb:
somewhere in the run-scoped journal there is a decision that

* has ``response == "y"`` (a greenlight, not a nudge), AND
* whose ``resolved["next_block"]`` names THIS verb — the machine-computed
  ``next_block`` the predecessor block emitted, which the LLM surfaced and the
  human's ``y`` greenlit.

The gate scans the journal newest-to-oldest and passes on the FIRST such match.
It does **not** trust ``records[-1]`` in isolation: the run-scoped JSONL is
SHARED across every run touchpoint (submit S1–S4, anomaly briefs, harvest — see
``state.decision_journal`` module docstring), so any unrelated touchpoint
journaled *after* a legitimate block greenlight (a nudge, or a ``y`` naming a
different verb) would otherwise flip ``records[-1]`` and wedge a sequence the
human DID authorize. Scanning for the latest greenlight-naming-*this*-verb keeps
the guard fail-safe (it never invents a greenlight) without the wedge.

A mis-sequenced call fails loudly with :class:`errors.SpecInvalid`:

* no journaled record at all → "no journaled greenlight for <verb> — surface the
  <predecessor> brief and record the decision via append-decision";
* no greenlight names this verb and the latest exchange was a nudge
  (``response != "y"``) → the human has not greenlit yet;
* no greenlight names this verb and the latest greenlight named a *different*
  verb → both are named.

"A guard the LLM itself satisfies is not a guard" (engineering-principles.md):
prose never hardcodes the block sequence — the affordance is removed, and the
sequence is enforced HERE from the durable journal. The gate is a pure read; it
never writes.

Scope notes (verified 2026-07-03):

* The campaign driver does **NOT** route through these block verbs — it drives
  ``submit-flow`` / ``campaign_run`` directly (grep over ``meta/campaign`` finds
  no ``submit-s*`` / ``aggregate-run`` call), so the async campaign spine is
  untouched by this gate.
* No in-tree caller needs a bypass: nothing *composes* the block verbs (they are
  human-sequenced entry points), so no wire-visible bypass field is invented
  here. If a legitimate composed caller ever appears, add an explicit,
  wire-visible bypass at that call site — never a silent one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.ops.overnight import ConsumptionOutcome

# The sentinel a greenlight decision records to name the block it greenlit. The
# predecessor block computes ``next_block = {"verb": ..., ...}``; the
# append-decision that journals the human's ``y`` stores the greenlit verb under
# ``resolved[_NEXT_BLOCK_KEY]`` (the verb string, or the whole hint dict — both
# are accepted; the verb is extracted).
_NEXT_BLOCK_KEY = "next_block"

# The greenlight sentinel (decision_journal §2: ``y`` vs nudge text).
_GREENLIGHT = "y"


def _journaled_target(resolved: object) -> str | None:
    """Extract the greenlit next-block verb from a decision's ``resolved`` block.

    Accepts either the canonical string form (``resolved["next_block"] ==
    "submit-s2"``) or the whole hint dict (``{"verb": "submit-s2", ...}``) —
    whichever the journaling caller recorded.
    """
    if not isinstance(resolved, dict):
        return None
    target = resolved.get(_NEXT_BLOCK_KEY)
    if isinstance(target, dict):
        target = target.get("verb")
    return target if isinstance(target, str) else None


def assert_greenlit_target(
    experiment_dir: Path,
    *,
    run_id: str,
    verb: str,
    predecessor: str,
) -> None:
    """Refuse *verb* unless some run-scoped decision greenlit exactly it.

    *predecessor* is the human label of the brief that must have been surfaced
    and greenlit first (e.g. ``"S1"`` for ``submit-s2``) — it is named in the
    "no journaled greenlight" message so the failure is self-remediating.

    Scans the run-scoped journal newest-to-oldest and passes on the first record
    that is a greenlight (``response == "y"``) naming *verb*. A later unrelated
    touchpoint (nudge, or a ``y`` for a different verb) therefore cannot wedge a
    verb the human already greenlit — ``records[-1]`` is not this block's
    greenlight in a shared journal.

    Raises :class:`errors.SpecInvalid` when no greenlight names *verb*: the
    journal has no record at all, the latest exchange is a nudge (not a ``y``),
    or the latest greenlight names a different verb.

    NOTE (consumption is intentionally NOT enforced here — SUPERSEDED, not a
    gap): a single greenlight re-authorizes *verb* on every re-invocation, and
    that is now a DESIGNED pattern. A stale ``y`` cannot cause harm because
    (1) this gate requires the LATEST greenlight to name *verb*, so once the run
    advances (a later ``y`` names the successor) a re-invocation of the old verb
    fails here; (2) ``run_id`` dedup refuses a second cluster action for an
    already-acted run; and (3) idempotent terminal-replay
    (:mod:`hpc_agent.state.block_terminal`) makes a re-invocation with the same
    greenlight REPLAY the recorded terminal rather than re-execute. The old
    ``TODO(wave4)`` (greenlight consumption/monotonicity — refuse a re-fire by
    comparing the block's last-execution against the greenlight timestamp) would
    BREAK (3), so it is superseded by these three backstops, not deferred.

    F13 (the y-then-later-record seat) — DELIBERATELY not unified HERE. The driver
    (``block_drive.committed_greenlight_for_boundary``) and the ``block-drive`` Stop
    guard now stop at the first SAME-boundary record of either kind, so a same-boundary
    retraction nudge supersedes an earlier ``y`` on the AUTOMATED path (a mechanical
    doctor/completer tick can no longer consume a retracted greenlight — it parks). This
    gate keeps "latest-greenlight-naming-verb wins" on purpose: a later same-verb nudge
    NOT retracting is a pinned, intentional invariant
    (``test_gate_greenlight_survives_later_unrelated_touchpoints``) whose rationale is
    backstops (2)+(3) — and adding a retraction refusal ahead of the terminal replay
    (which runs AFTER this gate in ``_submit_s3_impl``) would break (3). The only residue
    is a DIRECT manual ``hpc-agent submit-s3`` invocation after a self-retraction, which
    the driver no longer reaches; closing that would require reordering the terminal-replay
    check ahead of the gate (a ``submit_blocks`` change outside this seam).
    """
    records = read_decisions(experiment_dir, "run", run_id)
    for record in reversed(records):
        if str(record.get("response") or "") != _GREENLIGHT:
            continue
        if _journaled_target(record.get("resolved")) == verb:
            return
    # No greenlight names *verb* anywhere in the journal — diagnose from the tail.
    if not records:
        raise errors.SpecInvalid(
            f"no journaled greenlight for {verb} — surface the {predecessor} brief "
            f"and record the decision via append-decision (run_id={run_id!r})."
        )
    latest = records[-1]
    response = str(latest.get("response") or "")
    if response != _GREENLIGHT:
        raise errors.SpecInvalid(
            f"{verb}: the latest decision for run {run_id!r} is a nudge, not a "
            f"greenlight (response={response!r}); re-surface the {predecessor} brief "
            "and record the human's `y` via append-decision before acting."
        )
    target = _journaled_target(latest.get("resolved"))
    raise errors.SpecInvalid(
        f"{verb}: the latest greenlight for run {run_id!r} names {target!r}, "
        f"not {verb!r} — a mis-sequenced call. Run the block the human "
        f"greenlit, or re-surface the {predecessor} brief and record a "
        f"greenlight naming {verb}."
    )


def assert_greenlit_or_consented(
    experiment_dir: Path,
    *,
    run_id: str,
    verb: str,
    predecessor: str,
    current_cmd_sha: str,
    scope_kind: str = "run",
    scope_id: str | None = None,
) -> ConsumptionOutcome | None:
    """Pass *verb* on a journaled greenlight OR a live standing consent (overnight).

    The consent-aware gate for the overnight-consumable boundaries (item 8 seam 1).
    Tries the human-greenlight path first (:func:`assert_greenlit_target`); on its
    refusal, consults the scope's STANDING CONSENT via the substrate
    (:func:`hpc_agent.ops.overnight.consume_boundary_under_consent`, the one
    definition of "live consent" — never re-derived here) and, when a live consent
    covers *verb*, RECORDS the auto-advance to the consumption ledger in the SAME
    breath (an unrecorded consumption is the laundering class) and passes.

    Returns:

    * ``None`` — a human greenlight authorized *verb* (the normal path).
    * a :class:`ConsumptionOutcome` with ``consumed=True`` — a live standing consent
      authorized *verb*; the auto-advance was ledgered (or was already ledgered for
      this identity — idempotent).

    Raises :class:`errors.SpecInvalid` when NEITHER holds — the original greenlight
    diagnosis, augmented with the consent's failing leg so the park brief names why
    the overnight consent did not carry (expired / over-cap / spec-changed / none /
    a boundary the consent does not name).
    """
    try:
        assert_greenlit_target(experiment_dir, run_id=run_id, verb=verb, predecessor=predecessor)
        return None
    except errors.SpecInvalid as greenlight_err:
        from hpc_agent.ops.overnight import consume_boundary_under_consent

        outcome = consume_boundary_under_consent(
            experiment_dir,
            scope_kind=scope_kind,
            scope_id=scope_id or run_id,
            boundary_block=verb,
            current_cmd_sha=current_cmd_sha,
        )
        if outcome.consumed:
            return outcome
        raise errors.SpecInvalid(
            f"{greenlight_err} — and no live standing consent covers {verb} "
            f"({outcome.decision.reason}): surface the {predecessor} brief for a "
            "human decision."
        ) from greenlight_err
