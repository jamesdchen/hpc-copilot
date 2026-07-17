"""Pydantic models for the ``settle-run`` workflow primitive.

``settle-run`` is run-12 finding 25 (``docs/design/history/run12-findings.md``
§25): the human-directed terminal settle. Closing run 12 required journal surgery
— with completion proven by TWO independent sources (a foreground reporter RC=0
over all 2700 tasks; the result tree verified on disk) but the framework path
structurally unable to finish on the old wheel, the human directed a settle by
hand-editing the run record (status → complete). It worked, but it BYPASSED
``harvest_on_terminal`` (no summary pull, no transition stamp) and carried prose
evidence instead of typed counts.

The generator (upstream-fixes G2): state transitions reachable only through
inference-from-probes — every legitimate human override becomes surgery. The fix
is that every transition the system can make on PROBED evidence must also be
makeable on DIRECTED evidence through the SAME machinery. ``settle-run``:

(a) takes an evidence statement + optional artifact refs + optional typed counts;
(b) journals it as a DECISION (a sign-off — the directed evidence + its
    provenance, so the settle carries a trail, not a silent hand-edit);
(c) sets the terminal status via the SAME ``mark_run`` the probe path uses;
(d) runs the SAME receipt-gated ``harvest_on_terminal`` the automatic path runs
    (summary pull + transition stamp): the harvest fires on a status TRANSITION
    OR — absent a transition — as a journal-evidence BACKSTOP when the run is
    terminal with NO harvest receipt (a session-death between ``mark_run`` and the
    harvest), never solely on in-process transition state (the sibling of
    reconcile's ``_harvest_if_owed``). So a directed settle is byte-for-byte the
    same lifecycle event as a probed one, and it self-heals a dropped harvest.

I/O contracts:

* Input: ``schemas/settle_run.input.json`` (from ``SettleRunInput``).
* Output: ``schemas/settle_run.output.json`` (from ``SettleRunResult``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SettleRunInput(BaseModel):
    """Inputs to ``settle-run``: the run + the DIRECTED terminal evidence.

    ``run_id`` is the run to settle. ``status`` is the terminal state the directed
    evidence proves — one of ``complete`` / ``failed`` / ``abandoned`` (a
    non-terminal status is refused). ``evidence`` is the human's directed evidence
    statement and is REQUIRED — a directed settle with no evidence is refused
    (the whole point is DIRECTED evidence, not a bare status flip). ``artifact_refs``
    optionally corroborates (result-tree paths, a reporter log). ``task_counts``
    optionally supplies the typed counts finding 25 said the prose hand-edit
    lacked (e.g. ``{"complete": 2700, "failed": 0, "total": 2700}``). ``provenance``
    optionally names how the evidence was captured.
    """

    model_config = ConfigDict(extra="forbid", title="settle-run input spec")

    run_id: str = Field(min_length=1, description="The run to settle on directed evidence.")
    status: str = Field(
        min_length=1,
        description=(
            "The terminal status the directed evidence proves — one of 'complete', "
            "'failed', 'abandoned'. A non-terminal status is refused by the verb "
            "(settle-run only sets a TERMINAL state)."
        ),
    )
    evidence: str = Field(
        min_length=1,
        description=(
            "The directed evidence statement (REQUIRED) — what proves this terminal "
            "state (e.g. 'foreground reporter RC=0 all-2700; result tree verified on "
            "disk'). Journaled as the sign-off's proposal."
        ),
    )
    artifact_refs: list[str] | None = Field(
        default=None,
        description="Optional corroborating artifact refs (result-tree paths, a reporter log).",
    )
    task_counts: dict[str, int] | None = Field(
        default=None,
        description=(
            "Optional typed counts recorded in last_status (the counts the prose "
            "hand-edit lacked) — e.g. {'complete': 2700, 'failed': 0, 'total': 2700}."
        ),
    )
    provenance: str | None = Field(
        default=None,
        description="Optional note on how the directed evidence was captured (default 'human-directed').",
    )


class SettleRunResult(BaseModel):
    """The settle outcome — the journaled sign-off + the terminal transition.

    ``stage_reached`` says what ACTUALLY happened (no silent re-interpretation of a
    backstop as a plain transition):

    * ``settled`` — the status actually transitioned (the common case: a stuck
      in_flight run → terminal) and the harvest fired.
    * ``harvest_backstopped`` — the run was ALREADY terminal but carried NO harvest
      receipt (a session-death between ``mark_run`` and the harvest dropped the
      guaranteed harvest), so the harvest re-fired via the journal-evidence backstop
      — NOT a status transition. The sibling of reconcile's ``_harvest_if_owed``.
    * ``already_terminal`` — the run already carried the target status AND its harvest
      receipt was on the ledger: a true idempotent no-op, the harvest is NOT re-fired.

    ``harvested`` is True exactly when ``harvest_on_terminal`` ran — on a transition
    (``settled``) OR the no-receipt backstop (``harvest_backstopped``); ``harvest``
    carries its marker (finding 25's requirement: the same summary pull + transition
    stamp the automatic path produces). ``decision_ts`` is the journaled sign-off's
    timestamp.
    """

    model_config = ConfigDict(extra="forbid", title="settle-run output data")

    stage_reached: Literal["settled", "harvest_backstopped", "already_terminal"] = Field(
        description=(
            "What actually happened: 'settled' (status transitioned + harvested), "
            "'harvest_backstopped' (already terminal with NO harvest receipt — the "
            "guaranteed harvest re-fired via the journal-evidence backstop, not a "
            "transition), or 'already_terminal' (already terminal, receipt present — "
            "an idempotent no-op)."
        ),
    )
    run_id: str = Field(description="The settled run.")
    status: str = Field(description="The terminal status now recorded.")
    prior_status: str = Field(description="The run's status before the settle.")
    harvested: bool = Field(
        description=(
            "True exactly when harvest_on_terminal ran — on a status transition OR "
            "the terminal-with-no-receipt journal-evidence backstop."
        ),
    )
    harvest: dict = Field(
        default_factory=dict,
        description="The harvest_on_terminal marker (summary pull + transition stamp), when it ran.",
    )
    decision_ts: str = Field(
        description="Timestamp of the journaled directed-settle sign-off (the provenance trail).",
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the settle outcome.",
    )
