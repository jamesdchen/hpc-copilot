"""``settle-aggregate`` — the provenance home for an operator-bypass table.

The run-13 record-8 class (``docs/design/history/run13-findings.md`` finding 14):
a table produced OUTSIDE the sanctioned flow (the operator's direct reduce) has
no aggregate record, no harvest receipt, and its journal provenance is LOST.
``settle-run`` settles a single run's TERMINAL state; there was NO analogue for
"a table was produced outside the flow — attach provenance to it retroactively."
This verb is that analogue, extending the ``settle-run`` directed-evidence pattern
to the AGGREGATE stage.

It RECORDS, it never GATES. Given a table artifact + the runs the human claims it
derives from + a typed human utterance naming the artifact, it:

(a) validates SHAPE — the artifact exists (its sha256 is computed at record time;
    a hash is never asserted into existence), and every named run exists;
(b) refuses a SYNTHESIZED consent — the utterance must be human-authored (the same
    harness-captured utterance-log evidence tier ``append-decision``'s
    human-authorship gate uses); an agent-composed utterance is REFUSED, not
    silently accepted;
(c) journals the human's utterance as a directed decision under the run scope, the
    same ``append_decision`` sign-off shape ``settle-run`` uses — with
    ``source: "operator-settled, provenance human-asserted"``. The numbers are
    NEVER blessed.

Once journaled, ``verify-relay`` treats the named contributing ids as authorized
via its normal auth-id join (it folds a settle-aggregate record's
``contributing_run_ids`` into ``auth_ids`` exactly as it folds a run's
``campaign_id`` / ``parent_run_ids``), so a truthful relay of the operator-settled
table's run-set is no longer flagged.

This file lives at the ``ops/`` *role root* (sibling to ``settle_run.py``) because
it reads across subjects — the ``state`` journal + sidecars, the utterance log,
and the decision journal. The subject-imports lint short-circuits for role-root
files.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.settle_aggregate import (
    SettleAggregateInput,
    SettleAggregateResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    pass

__all__ = ["settle_aggregate"]

#: The journal block name for a directed aggregate settle — the ONE choke point
#: (there is no other verb writing this block), so it cannot be laundered around.
SETTLE_AGGREGATE_BLOCK = "settle-aggregate"

# Word tokenizer for the human-authorship overlap check (the decision journal's
# ``_ha_word_tokens`` intent, re-expressed): alphanumeric runs of >= 2 chars.
_WORD_RE = re.compile(r"[A-Za-z0-9]{2,}")


def _word_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(text or "")}


def _run_exists(experiment_dir: Path, run_id: str) -> bool:
    """True when a named run has EITHER a journal record OR a sidecar on disk."""
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    if load_run(experiment_dir, run_id) is not None:
        return True
    try:
        return bool(read_run_sidecar(experiment_dir, run_id))
    except (FileNotFoundError, OSError, ValueError):
        return False


def _assert_utterance_human_authored(experiment_dir: Path, utterance: str) -> str:
    """Refuse a SYNTHESIZED (agent-composed) utterance; return the authorship tier.

    The harness-captured utterance log
    (:func:`hpc_agent.state.utterances.read_utterances`, written out-of-band by the
    ``UserPromptSubmit`` hook) is the human-authored evidence tier. When it exists,
    the settle utterance's content words must OVERLAP the logged human text — an
    agent-composed utterance sharing no words with anything the human typed is
    refused (``harness-captured`` tier). When NO log exists (hook not installed /
    older session), the friction fallback accepts but discloses the
    ``unverified-fallback`` tier on the record — never a silent synthesis.

    Returns the tier string; raises :class:`errors.SpecInvalid` on refusal.
    """
    from hpc_agent.state.utterances import read_utterances

    try:
        logged = read_utterances(experiment_dir)
    except Exception:  # noqa: BLE001 — a broken log degrades to the friction tier
        logged = []
    if not logged:
        return "unverified-fallback"
    human_words: set[str] = set()
    for rec in logged:
        human_words |= _word_tokens(str(rec.get("text") or ""))
    if not (_word_tokens(utterance) & human_words):
        raise errors.SpecInvalid(
            "settle-aggregate: the utterance shares no words with any human "
            "utterance on the repo's log — it reads as agent-composed. This verb "
            "never synthesizes consent: the human must TYPE the utterance naming "
            "the artifact (captured to the utterance log), it cannot be relayed by "
            "the agent."
        )
    return "harness-captured"


@primitive(
    name="settle-aggregate",
    verb="workflow",
    composes=["append-decision"],
    side_effects=[
        SideEffect(
            "writes-journal",
            "<experiment>/.hpc/runs/<run_id>.decisions.jsonl (the directed "
            "aggregate-settle sign-off)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=False,
    cli=CliShape(
        help=(
            "Settle an operator-bypass TABLE artifact into the journal (run-13 "
            "finding 14): given the table + the runs the human claims it derives "
            "from + a typed human utterance naming the artifact, journal a directed "
            "aggregate settle. RECORDS, never gates: validates the artifact exists "
            "(sha256'd at record time) and the named runs exist, refuses an "
            "agent-composed utterance (never synthesizes consent), and journals the "
            "human's utterance with source 'operator-settled, provenance "
            "human-asserted' — the numbers are NEVER blessed. verify-relay then "
            "treats the named ids as authorized via its normal auth-id join."
        ),
        spec_arg=True,
        spec_model=SettleAggregateInput,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="settle_aggregate"),
    ),
    agent_facing=True,
)
def settle_aggregate(experiment_dir: Path, *, spec: SettleAggregateInput) -> SettleAggregateResult:
    """Journal a human-directed settle of an operator-bypass table.

    1. Refuse an ABSENT artifact (there is nothing to settle) and compute its
       sha256 over the bytes at record time.
    2. Refuse when any named ``derives_from`` run does not exist — the settle
       records a human-asserted lineage, it does not invent runs.
    3. Refuse a SYNTHESIZED utterance (the human-authorship tier); disclose the
       tier on the record.
    4. Journal the human's utterance as a directed sign-off under the run scope,
       with ``source: "operator-settled, provenance human-asserted"``.

    Raises :class:`errors.SpecInvalid` on an absent artifact, a missing named run,
    or an agent-composed utterance.
    """
    from hpc_agent.infra.time import utcnow_iso
    from hpc_agent.state.decision_journal import append_decision

    experiment_dir = Path(experiment_dir)

    # Guard 1: the artifact must exist — its sha is the record-time evidence.
    artifact = Path(spec.aggregate_ref)
    if not artifact.is_file():
        raise errors.SpecInvalid(
            f"settle-aggregate: aggregate_ref {spec.aggregate_ref!r} does not exist — "
            "a settle attaches provenance to a table that WAS produced; there is "
            "nothing to settle for an absent artifact."
        )
    try:
        artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    except OSError as exc:
        raise errors.SpecInvalid(
            f"settle-aggregate: could not read aggregate_ref {spec.aggregate_ref!r} to "
            f"compute its sha256 ({exc}) — a hash cannot be asserted into existence."
        ) from exc

    # Guard 2: every named contributing run must exist (a record or sidecar).
    contributing = [str(r) for r in spec.derives_from if str(r).strip()]
    missing = [r for r in contributing if not _run_exists(experiment_dir, r)]
    if missing:
        raise errors.SpecInvalid(
            f"settle-aggregate: named contributing run(s) {missing} have no record or "
            "sidecar — the settle records a human-asserted derives-from lineage over "
            "runs that EXIST, it does not invent runs. Name the real run ids the "
            "table derives from."
        )

    # Guard 3: the utterance must be human-authored (never synthesized).
    utterance = spec.utterance.strip()
    authorship = _assert_utterance_human_authored(experiment_dir, utterance)

    # Journal the directed evidence as a DECISION — the human's utterance is the
    # sign-off's proposal; the numbers are NEVER blessed.
    decision = append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=spec.run_id,
        block=SETTLE_AGGREGATE_BLOCK,
        response="y",
        proposal=utterance,
        resolved={
            "aggregate_ref": spec.aggregate_ref,
            "artifact_sha256": artifact_sha256,
            "contributing_run_ids": contributing,
        },
        provenance={
            "directed": True,
            "kind": "human-directed-aggregate-settle",
            "artifact_ref": spec.aggregate_ref,
            "artifact_sha256": artifact_sha256,
            "contributing_run_ids": contributing,
            "authorship": authorship,
            "source": "operator-settled, provenance human-asserted",
            "captured": spec.provenance or "human-directed",
            "recorded_at": utcnow_iso(),
        },
    )

    return SettleAggregateResult(
        stage_reached="settled",
        run_id=spec.run_id,
        aggregate_ref=spec.aggregate_ref,
        artifact_sha256=artifact_sha256,
        contributing_run_ids=contributing,
        authorship=authorship,
        decision_ts=str(decision.get("ts", "")),
        reason=(
            f"settled table {spec.aggregate_ref!r} (sha256 {artifact_sha256[:12]}…) as "
            f"operator-settled over {len(contributing)} human-asserted contributing "
            f"run(s); provenance human-asserted, numbers not blessed"
        ),
    )
