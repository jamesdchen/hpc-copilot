"""Decision journal — the append-only record of every ``y``/nudge exchange.

Design origin: ``docs/design/human-amplification-blocks.md`` §2. Every
human touchpoint in the fork has one shape — code digests the evidence,
the LLM drafts a proposal, the human answers with ``y`` (greenlight) or a
natural-language nudge — and **every such exchange is journaled**. The
decision record, not the chat scroll, is the source of truth for *why* a
run (or campaign) took the shape it did.

This module GENERALIZES the per-run ``verdict_history`` audit
(``state/run_record.py`` — "why a non-deterministic decision took its
branch") from failure-escalations to *every* human touchpoint (submit
briefs, canary greenlights, campaign specs, anomalies, harvest
interpretations). It is a **separate store**: it never touches
``run_record.py`` or the ``RunRecord`` JSON.

Storage locality (mirrors the ``.hpc/`` cluster-relative tree that run
sidecars and campaign scratch already live under)::

    <experiment_dir>/.hpc/runs/<run_id>.decisions.jsonl        # scope_kind="run"
    <experiment_dir>/.hpc/campaigns/<campaign_id>/decisions.jsonl  # scope_kind="campaign"
    <experiment_dir>/.hpc/scopes/<tag>.decisions.jsonl         # scope_kind="scope"
    <experiment_dir>/.hpc/notebooks/<audit_id>.decisions.jsonl # scope_kind="notebook"
    <experiment_dir>/.hpc/registrations/<registration_id>.decisions.jsonl  # "registration"
    <experiment_dir>/.hpc/packs/<pack_name>.decisions.jsonl    # scope_kind="pack"
    <experiment_dir>/.hpc/conclusions/<conclusion_id>.decisions.jsonl  # "conclusion"
    <experiment_dir>/.hpc/challenges/<challenge_id>.decisions.jsonl  # scope_kind="challenge"

One JSONL record per exchange, newest last, **append-only**: a write
never rewrites or truncates a prior record. Appends are serialized under
an advisory ``flock`` (the same lock discipline
``state/journal.py`` and ``ops/monitor/tick_log.py`` use) so concurrent
writers — an in-session agent, a slash-command surface, the campaign
driver — can't interleave bytes mid-line.

Pure I/O: no ``_wire`` import (the ``ops`` primitive layer owns the
Pydantic models and validates at the boundary), no SSH, no mapreduce.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "SCOPE_KINDS",
    "append_decision",
    "read_decisions",
    "latest_decision",
    "is_committed_greenlight_for_boundary",
    "is_latest_committed_greenlight",
    "decisions_path",
]

# Bump only on a breaking record-shape change; readers tolerate unknown
# extra keys (forward-compat) so additive fields do NOT need a bump.
SCHEMA_VERSION = 1

# The scopes a decision can belong to. A "run" decision journals the
# submit S1–S4 / anomaly / harvest touchpoints of a single run; a
# "campaign" decision journals the once-at-start spec greenlight plus the
# anomaly / completion briefs of an asynchronous campaign (design §4). A
# "scope" decision journals the lock/unlock touchpoints of a named,
# caller-tagged experiment scope (:mod:`hpc_agent.state.scopes`) — the
# substrate the scope lock state and look ledger hang off; the journal
# stores the shape, never any tag vocabulary. A "notebook" decision
# journals the audit touchpoints of an audited source module
# (``docs/design/notebook-audit.md`` D3) — sign-offs are ordinary
# append-decision records under a caller-authored ``audit_id``; the
# journal stores the shape, never any section vocabulary. A "registration"
# decision journals the deployment-boundary attestation touchpoints of a
# caller-authored ``registration_id`` (``docs/design/registration-kernel.md``
# R9) — the ``registration`` / ``registration-revoke`` records that ride
# ``append-decision`` under the R6 gate; the journal stores the shape, never any
# field/prerequisite vocabulary. It is a SIXTH kind (never coupled to a run's
# journal — a registration outlives any single run and spans dossier re-exports).
# A "pack" decision journals the bind/receipt touchpoints of a domain pack
# (``docs/design/domain-packs.md``, "The bind event") — the mechanical
# ``pack-bind`` / ``pack-receipt`` CODE attestations that ride ``append-decision``
# under a caller-authored pack ``name``; the journal stores the shape, never any
# seam/reader/pattern vocabulary. It is a SEVENTH kind; packs and the registration
# kernel took the next two slots in whichever order they landed — the kinds are
# independent (``docs/design/registration-kernel.md`` R9).
# A "conclusion" decision journals a human-authored finding — the one new record
# type of evidence memory (``docs/design/evidence-memory.md`` E-shape) — under a
# caller-authored ``conclusion_id``: the ``conclusion`` / ``conclusion-revoke``
# attestations that ride ``append-decision`` under the E-shape gate; the journal
# stores the shape, never any tag/finding vocabulary. It is an EIGHTH kind (never
# coupled to a run or campaign journal — a conclusion typically spans several and
# outlives any one of them; the R9 rationale).
# A "challenge" decision journals a human-authored, evidence-bound, sha-targeted
# attestation of DISSENT against a committed record — the missing "this is wrong"
# object (``docs/design/challenge-attestation.md`` C-shape) — under a
# caller-authored ``challenge_id``: the ``challenge`` / ``challenge-verdict`` /
# ``challenge-withdraw`` records that ride ``append-decision`` under the C-gate
# locks; the journal stores the shape, never any grounds/reasoning vocabulary. It
# is a NINTH kind, deliberately its OWN store rather than riding the target's
# journal (C-shape: a challenge may target a conclusion / registration / sign-off /
# fingerprint sample across four+ path branches, and some targets have no journal
# to ride — the R9 one-branch-per-family rule).
SCOPE_KINDS = frozenset(
    {"run", "campaign", "scope", "notebook", "registration", "pack", "conclusion", "challenge"}
)

_log = logging.getLogger(__name__)


def _validate_scope(scope_kind: str, scope_id: str) -> None:
    """Validate the ``(scope_kind, scope_id)`` pair — fail loudly.

    A primitive owns its invariants: the scope id becomes a path segment,
    so it must be filesystem-safe (same constraint ``campaign_dir`` and
    the ``run_id`` slug already enforce) or it could escape the ``.hpc/``
    tree.
    """
    if scope_kind not in SCOPE_KINDS:
        raise errors.SpecInvalid(
            f"scope_kind must be one of {sorted(SCOPE_KINDS)}; got {scope_kind!r}"
        )
    if not scope_id:
        raise errors.SpecInvalid("scope_id must be a non-empty string")
    if "/" in scope_id or "\\" in scope_id or scope_id in (".", ".."):
        raise errors.SpecInvalid(f"scope_id must be filesystem-safe; got {scope_id!r}")


def decisions_path(experiment_dir: Path, scope_kind: str, scope_id: str) -> Path:
    """Return the JSONL path for a scope's decision journal.

    Run scope lands under the per-experiment sidecar tree
    (``RepoLayout(experiment_dir).runs``); campaign scope lands inside the
    campaign's canonical scratch directory (``campaign_dir``). Both helpers
    create their parent directory idempotently — the same dir-creating
    layout access ``ops/monitor/tick_log`` makes for its ``.monitor.jsonl``
    path — so a first append into a fresh scope Just Works.

    Raises :class:`errors.SpecInvalid` on an unknown *scope_kind* or a
    non-filesystem-safe *scope_id*.
    """
    _validate_scope(scope_kind, scope_id)
    if scope_kind == "run":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).runs / f"{scope_id}.decisions.jsonl"
    if scope_kind == "scope":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).hpc / "scopes" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "notebook":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).hpc / "notebooks" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "registration":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).hpc / "registrations" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "pack":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).hpc / "packs" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "conclusion":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).hpc / "conclusions" / f"{scope_id}.decisions.jsonl"
    if scope_kind == "challenge":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).hpc / "challenges" / f"{scope_id}.decisions.jsonl"
    # scope_kind == "campaign" (validated above)
    from hpc_agent.meta.campaign.dirs import campaign_dir

    return campaign_dir(experiment_dir, scope_id) / "decisions.jsonl"


def _append_jsonl_line(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object as a line to *path* under an exclusive flock.

    Thin wrapper over the canonical JSONL-append seam
    (:func:`hpc_agent.infra.io.append_jsonl_line`) — the one definition of
    the flock + fsync + sort_keys discipline the decision journal, decision
    briefs, scope look ledger, and the guaranteed-harvest marker all share.
    Retained as the state-layer name the sibling ``state/*`` modules import.
    """
    append_jsonl_line(path, record)


def append_decision(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    block: str,
    response: str,
    evidence_digest: str | dict[str, Any] | None = None,
    proposal: str | list[Any] | dict[str, Any] | None = None,
    resolved: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    attestor_id: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Append one ``y``/nudge exchange to a scope's decision journal.

    Persists exactly the fields the design §2 schema enumerates (see the
    module docstring and ``docs/primitives/append-decision.md``). *ts* is
    auto-stamped (current UTC ISO-8601) when omitted — the one field no
    caller has any business asserting. Returns the record written (the
    caller can echo it back as confirmation).

    *attestor_id* is the OPAQUE, harness-asserted multi-human actor slug of the
    session that authored this record (``docs/design/multi-human.md`` MH3/MH4).
    It is ADDITIVE and default-``None``: when ``None`` (today's single-actor
    world, or a >1-actor session whose actor did not resolve) the key is NOT
    emitted, so the on-disk record is byte-identical to before multi-human — the
    byte-identity pin. Present only when the ``ops`` gate resolved a session
    actor under a >1-actor declaration; readers tolerate the extra key
    (forward-compat, no schema bump). Core never verifies who set it.

    Append-only: this never reads-modifies-writes a prior record; a second
    call always adds a new line after the first.

    Raises :class:`errors.SpecInvalid` on a bad scope, an empty *block*, or
    an empty *response*.
    """
    _validate_scope(scope_kind, scope_id)
    if not block:
        raise errors.SpecInvalid("block must be a non-empty string (the block terminator id)")
    if not response:
        raise errors.SpecInvalid(
            "response must be a non-empty string ('y' for greenlight, or the nudge text)"
        )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts or utcnow_iso(),
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "block": block,
        "evidence_digest": evidence_digest if evidence_digest is not None else "",
        "proposal": proposal if proposal is not None else "",
        "response": response,
        "resolved": dict(resolved) if resolved else {},
        "provenance": dict(provenance) if provenance else {},
    }
    if attestor_id is not None:
        # Additive, opaque, harness-asserted (MH3): stamped ONLY when a session
        # actor resolved under a >1-actor declaration. Absent → byte-identical.
        record["attestor_id"] = attestor_id
    _append_jsonl_line(decisions_path(experiment_dir, scope_kind, scope_id), record)
    return record


def read_decisions(experiment_dir: Path, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Return every decision record for a scope, in append (chronological) order.

    Returns ``[]`` when the journal file does not exist yet (a scope with
    no recorded touchpoints). Blank lines and individually-corrupt lines
    are skipped with a warning rather than failing the whole read — one bad
    line must never strand the rest of an audit trail.

    Raises :class:`errors.SpecInvalid` on a bad scope.
    """
    _validate_scope(scope_kind, scope_id)
    path = decisions_path(experiment_dir, scope_kind, scope_id)
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("decision_journal: skipping unreadable %s (%s)", path, exc)
        return records
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.warning("decision_journal: skipping corrupt line %d in %s (%s)", lineno, path, exc)
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def latest_decision(experiment_dir: Path, scope_kind: str, scope_id: str) -> dict[str, Any] | None:
    """Return the most recent decision record for a scope, or ``None`` if empty.

    "Most recent" = the last record in append (chronological) order — the
    journal is append-only so the latest line is the current human touchpoint.
    ``None`` when the scope has no recorded decisions yet.

    Raises :class:`errors.SpecInvalid` on a bad scope.
    """
    records = read_decisions(experiment_dir, scope_kind, scope_id)
    return records[-1] if records else None


def is_latest_committed_greenlight(experiment_dir: Path, scope_kind: str, scope_id: str) -> bool:
    """True iff a scope's most recent decision is a committed ``y`` greenlight.

    UNSCOPED latest-record read: true whenever the LATEST record has
    ``response == "y"``, with no regard for WHICH boundary that ``y`` targeted.
    Correct for touchpoint-level questions — "is the human's newest word on
    this scope an approval?" (the campaign-pending queue collector, the
    relay-audit standing check). NOT sufficient for the §5
    "committed-but-unadvanced" advance detection: a consumed ``y`` is never
    removed from the journal, so after a driver consumes it and re-parks, the
    latest record is a STALE greenlight (bug-sweep #1/#23, run-12 finding 21).
    Advance detection must use :func:`is_committed_greenlight_for_boundary`,
    which the Stop guard, ``doctor``, and the attention queue's
    greenlight-unadvanced split all route through.

    Raises :class:`errors.SpecInvalid` on a bad scope.
    """
    latest = latest_decision(experiment_dir, scope_kind, scope_id)
    return latest is not None and latest.get("response") == "y"


def is_committed_greenlight_for_boundary(
    experiment_dir: Path,
    scope_kind: str,
    scope_id: str,
    *,
    next_verb: str | None,
    awaiting_since: str | None,
    block: str | None = None,
) -> bool:
    """True iff a committed greenlight for THIS parked boundary is the scope's latest word.

    The decision-journal half of the §5 "committed-but-unadvanced" predicate,
    BOUNDARY-SCOPED (bug-sweep #1/#23, run-12 finding 21): a record must be a ``y`` that
    NAMES the parked marker's ``next_verb`` and was journaled at-or-after the marker's
    ``awaiting_since`` — the single shared rule
    (:func:`hpc_agent._kernel.lifecycle.block_drive.committed_greenlight_for_boundary`) the
    ``block-drive`` Stop guard, the out-of-session ``doctor``, and the attention queue's
    greenlight-unadvanced split all key on, so no surface can read a consumed greenlight as
    a fresh one. *next_verb*/*awaiting_since* come from the run's ``pending_decision`` marker.

    F13: uses the NEWEST-FIRST scan (not a bare ``latest_decision`` read) so it agrees with
    the driver + Stop guard on BOTH directions of "y then a later record": an UNRELATED later
    record (an overnight-consent, a sign-off, another block's touchpoint) journaled after the
    greenlight does not falsely read "not committed" and stall a genuine ``y`` (it is skipped),
    while a SAME-boundary retraction nudge (*block* names the parked block, ts>=awaiting_since)
    DOES supersede the earlier ``y`` (stays parked). *block* comes from the ``pending_decision``
    marker; when omitted the supersession leg is off (skip-unrelated-only).

    Raises :class:`errors.SpecInvalid` on a bad scope.
    """
    records = read_decisions(experiment_dir, scope_kind, scope_id)
    if not records:
        return False
    # Lazy import: block_drive imports this module at top level; the shared scan is
    # pure (no journal I/O), so the one-way runtime import here keeps the single
    # definition without an import cycle.
    from hpc_agent._kernel.lifecycle.block_drive import committed_greenlight_for_boundary

    return (
        committed_greenlight_for_boundary(
            records, block=block, next_verb=next_verb, awaiting_since=awaiting_since
        )
        is not None
    )
