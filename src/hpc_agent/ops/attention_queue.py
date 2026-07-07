"""The attention-queue item model, per-kind collectors, and the D2 total order.

Design: ``docs/design/attention-queue.md`` (Wave B / T1). The queue AGGREGATES
existing predicates (D5) and adds SELECTION + ORDERING only — any recomputation
of a verdict, a staleness, or a liveness comparison inside this package is a
defect by definition (the route-through tests pin each collector to its one
source symbol via ``inspect.getsource``).

One-definition seams (D5 table), each collector routing through the named symbol:

* ``greenlight-unadvanced`` / ``run-parked`` — ``state/index.py::find_parked_runs``
  split by ``state/decision_journal.py::is_latest_committed_greenlight`` (the same
  pair ``doctor`` and the Stop guard key on — the queue becomes the THIRD surface
  that must agree, and it calls the same symbols, so it cannot disagree).
* ``run-stalled`` — ``state/index.py::find_stalled_runs``.
* ``dead-worker`` — ``ops/recover/doctor.py::scan_dead_detached_workers``.
* ``run-anomaly`` — ``ops/status_blocks.py``'s promoted reduction
  (``digest_run`` + ``ANOMALY_STATUSES`` + ``recommendation_for``), supersession
  exclusion intact.
* ``campaign-pending`` — ``state/decision_journal.py::latest_decision`` +
  ``is_latest_committed_greenlight`` over ``scope_kind="campaign"``.
* ``audit-section-*`` — ``state/notebook_audit.py::audit_module`` (sources resolved
  by the same seam the sign-off gate uses).
* ``alert`` — ``ops/recover/notify.py::read_unacknowledged_alerts`` (peek-only).
* ``ssh-circuit-open`` — ``ops/recover/net_triage.py::open_circuit_lines``.

The D2 ordering was REVISED (user, 2026-07-08 — leverage-primary): the primary
sort key is LEVERAGE = the unblock fan-out COUNTED over the dependency edges the
journals already encode (:func:`_apply_fanout` / :func:`order_items`), never a
score. Full order: fan-out descending → class → oldest ``since`` → (kind,
scope_id). Where no encoded edge exists fan-out is 0 and the item falls through to
the class order byte-identically with the pre-revision rule.

Watermark-neutral + store-free (D6): this package moves no state — it never calls
``mark_seen_by_human`` or ``acknowledge_alerts``, and writes no file anywhere.
Pure-ish read: it reads journals via the source predicates, holds no SSH, no
``_wire`` import, and no scheduler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent.infra.time import parse_iso_utc_or_none

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "BLOCKED",
    "VERDICT",
    "INFORMATIONAL",
    "DEFAULT_CLASS_ORDER",
    "GREENLIGHT_UNADVANCED",
    "RUN_PARKED",
    "RUN_STALLED",
    "DEAD_WORKER",
    "RUN_ANOMALY",
    "CAMPAIGN_PENDING",
    "AUDIT_SECTION_UNSIGNED",
    "AUDIT_SECTION_STALE",
    "ALERT",
    "SSH_CIRCUIT_OPEN",
    "KIND_CLASS",
    "AttentionItem",
    "QueueCollection",
    "collect_greenlight_and_parked",
    "collect_stalled",
    "collect_dead_workers",
    "collect_anomalies",
    "collect_campaign_pending",
    "collect_audits",
    "collect_alerts",
    "collect_ssh_circuits",
    "collect_items",
    "order_items",
    "collect_queue",
    "count_by_class",
    "discover_fleet_experiments",
    "collect_fleet",
]

# ── the ordering classes (D2) ────────────────────────────────────────────────
BLOCKED = "blocked"
VERDICT = "verdict"
INFORMATIONAL = "informational"

#: The built-in class order: waste-before-judgment, then awareness (D2). The
#: caller may override the CLASS sequence (never the within-class rule).
DEFAULT_CLASS_ORDER: tuple[str, ...] = (BLOCKED, VERDICT, INFORMATIONAL)

# ── the item kinds (opaque strings, D1) ──────────────────────────────────────
GREENLIGHT_UNADVANCED = "greenlight-unadvanced"
RUN_PARKED = "run-parked"
RUN_STALLED = "run-stalled"
DEAD_WORKER = "dead-worker"
RUN_ANOMALY = "run-anomaly"
CAMPAIGN_PENDING = "campaign-pending"
AUDIT_SECTION_UNSIGNED = "audit-section-unsigned"
AUDIT_SECTION_STALE = "audit-section-stale"
ALERT = "alert"
SSH_CIRCUIT_OPEN = "ssh-circuit-open"

#: The one place a kind is bound to its D2 class. A new kind must name its
#: one-definition source predicate first (D5), then land here.
KIND_CLASS: dict[str, str] = {
    GREENLIGHT_UNADVANCED: BLOCKED,
    RUN_STALLED: BLOCKED,
    DEAD_WORKER: BLOCKED,
    RUN_PARKED: VERDICT,
    RUN_ANOMALY: VERDICT,
    CAMPAIGN_PENDING: VERDICT,
    AUDIT_SECTION_UNSIGNED: VERDICT,
    AUDIT_SECTION_STALE: INFORMATIONAL,
    ALERT: INFORMATIONAL,
    SSH_CIRCUIT_OPEN: INFORMATIONAL,
}


@dataclass(frozen=True)
class AttentionItem:
    """One queue item: identity + class + evidence pointer, never a score (D1).

    The subject is flattened into ``scope_kind`` / ``scope_id`` / ``block`` for a
    frozen value type; :meth:`as_dict` re-nests it into the D1 wire shape (with the
    ``class`` key). Priority lives ONLY in ``item_class`` plus D2 position — there
    is no urgency-score field by construction.
    """

    kind: str
    item_class: str
    experiment_dir: str
    scope_kind: str | None
    scope_id: str
    block: str | None = None
    cluster: str | None = None
    since: str | None = None
    action: str | None = None
    #: The D2-revision LEVERAGE key: the count of pending downstream subjects that
    #: become actionable when this one verdict clears, COUNTED over the dependency
    #: edges the journals already encode (:func:`_apply_fanout`). Never a score —
    #: where no encoded edge exists it is 0 and the item falls through to the class
    #: order (byte-identical to the pre-revision rule).
    unblocks: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The D1 wire-shaped dict (``class`` key, nested ``subject``)."""
        return {
            "kind": self.kind,
            "class": self.item_class,
            "subject": {
                "scope_kind": self.scope_kind,
                "scope_id": self.scope_id,
                "block": self.block,
            },
            "experiment_dir": self.experiment_dir,
            "cluster": self.cluster,
            "since": self.since,
            "action": self.action,
            "unblocks": self.unblocks,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class QueueCollection:
    """The raw (unordered) collection: items + fail-open skip accounting (D3)."""

    items: list[AttentionItem]
    skipped: list[dict[str, str]]


def _exp(experiment_dir: Path) -> str:
    """The stable experiment-dir string an item is stamped with (fleet mode)."""
    return str(Path(experiment_dir).resolve())


# ── run collectors (D5 rows 1-4) ─────────────────────────────────────────────


def collect_greenlight_and_parked(experiment_dir: Path, *, now: str) -> list[AttentionItem]:
    """The ``find_parked_runs`` split (D5 rows 1-2), the SAME split ``doctor`` and
    the Stop guard key on.

    A parked run whose latest committed decision IS a ``y`` greenlight is
    ``greenlight-unadvanced`` (blocked — the human already decided; a dead driver
    must be re-armed); otherwise it is ``run-parked`` (verdict — still genuinely
    awaiting the human). The greenlight test routes through the ONE predicate
    ``is_latest_committed_greenlight``; the queue never re-inlines it.
    """
    from hpc_agent.state.decision_journal import is_latest_committed_greenlight
    from hpc_agent.state.index import find_parked_runs

    exp = _exp(experiment_dir)
    items: list[AttentionItem] = []
    for hit in find_parked_runs(now, experiment_dir=experiment_dir):
        run_id = hit["run_id"]
        block = hit.get("block")
        greenlit = is_latest_committed_greenlight(experiment_dir, "run", run_id)
        kind = GREENLIGHT_UNADVANCED if greenlit else RUN_PARKED
        items.append(
            AttentionItem(
                kind=kind,
                item_class=KIND_CLASS[kind],
                experiment_dir=exp,
                scope_kind="run",
                scope_id=run_id,
                block=block,
                since=hit.get("awaiting_since"),
                evidence={
                    "block": block,
                    "workflow": hit.get("workflow"),
                    "status": hit.get("status"),
                },
            )
        )
    return items


def collect_stalled(experiment_dir: Path, *, now: str) -> list[AttentionItem]:
    """Stalled drivers (D5 row 3) via ``find_stalled_runs`` — a live run whose
    ``next_tick_due`` lapsed. Parked ≠ stalled is already encoded in the predicate.
    """
    from hpc_agent.state.index import find_stalled_runs

    exp = _exp(experiment_dir)
    items: list[AttentionItem] = []
    for hit in find_stalled_runs(now, experiment_dir):
        items.append(
            AttentionItem(
                kind=RUN_STALLED,
                item_class=KIND_CLASS[RUN_STALLED],
                experiment_dir=exp,
                scope_kind="run",
                scope_id=hit["run_id"],
                cluster=hit.get("cluster"),
                since=hit.get("last_tick_at"),
                evidence={
                    "next_tick_due": hit.get("next_tick_due"),
                    "last_tick_at": hit.get("last_tick_at"),
                    "status": hit.get("status"),
                    "ssh_target": hit.get("ssh_target"),
                },
            )
        )
    return items


def collect_dead_workers(experiment_dir: Path, *, now: str) -> list[AttentionItem]:
    """Dead detached workers (D5 row 4) via ``scan_dead_detached_workers`` — a lease
    with a dead pid and no recorded block-terminal. The source drafts the re-invoke
    proposal string, which rides ``action`` verbatim (the queue authors nothing).
    """
    from hpc_agent.ops.recover.doctor import scan_dead_detached_workers

    exp = _exp(experiment_dir)
    items: list[AttentionItem] = []
    for finding in scan_dead_detached_workers(experiment_dir, now=now):
        items.append(
            AttentionItem(
                kind=DEAD_WORKER,
                item_class=KIND_CLASS[DEAD_WORKER],
                experiment_dir=exp,
                scope_kind="run",
                scope_id=finding["run_id"],
                block=finding.get("block"),
                action=finding.get("proposal"),
                evidence={"pid": finding.get("pid"), "block": finding.get("block")},
            )
        )
    return items


def collect_anomalies(experiment_dir: Path, *, now: str) -> list[AttentionItem]:
    """Failed/abandoned runs (D5 row 5) via the promoted ``status_blocks`` reduction.

    The anomaly VERDICT — which terminal status is an anomaly, the supersession
    exclusion, the proposed next-action DATA — routes through
    ``ops/status_blocks.py::digest_run`` + ``ANOMALY_STATUSES`` +
    ``recommendation_for`` (the one definition; never re-inlined). Record discovery
    is the only thing added: no fleet ``find_failed_runs`` predicate exists (the
    in-flight scans exclude terminal runs), so the collector enumerates run records
    non-creatingly and applies the reduction. ``is_superseded`` is never an anomaly.
    """
    from hpc_agent.ops.status_blocks import ANOMALY_STATUSES, digest_run, recommendation_for

    exp = _exp(experiment_dir)
    items: list[AttentionItem] = []
    for record in _all_run_records(experiment_dir):
        row = digest_run(record)
        if row["status"] not in ANOMALY_STATUSES or row["is_superseded"]:
            continue
        recommendation = recommendation_for(row["status"])
        items.append(
            AttentionItem(
                kind=RUN_ANOMALY,
                item_class=KIND_CLASS[RUN_ANOMALY],
                experiment_dir=exp,
                scope_kind="run",
                scope_id=row["run_id"],
                cluster=row.get("cluster"),
                since=row.get("last_tick_at"),
                action=recommendation.get("action"),
                evidence={
                    "status": row["status"],
                    "recommendation": recommendation,
                    "summary": row.get("summary"),
                },
            )
        )
    return items


# ── campaign collector (D5 row 6) ────────────────────────────────────────────


def collect_campaign_pending(experiment_dir: Path, *, now: str) -> list[AttentionItem]:
    """Campaigns awaiting a verdict (D5 row 6): a discovered campaign whose newest
    journaled touchpoint is not a committed ``y``.

    No new campaign-state predicate is invented — the read routes through
    ``latest_decision`` + ``is_latest_committed_greenlight`` over ``scope_kind=
    "campaign"``, the same journal seam runs use. A campaign whose ONLY record is
    the start greenlight ``y`` correctly yields no item (its latest IS a committed
    ``y``). The record's ``block`` distinguishes a completion brief from an anomaly
    brief in the evidence. Discovery is a non-creating glob (D3).
    """
    from hpc_agent.state.decision_journal import (
        is_latest_committed_greenlight,
        latest_decision,
    )

    exp = _exp(experiment_dir)
    items: list[AttentionItem] = []
    for campaign_id in _discover_campaign_ids(experiment_dir):
        latest = latest_decision(experiment_dir, "campaign", campaign_id)
        if latest is None:
            continue
        if is_latest_committed_greenlight(experiment_dir, "campaign", campaign_id):
            continue
        block = latest.get("block")
        items.append(
            AttentionItem(
                kind=CAMPAIGN_PENDING,
                item_class=KIND_CLASS[CAMPAIGN_PENDING],
                experiment_dir=exp,
                scope_kind="campaign",
                scope_id=campaign_id,
                block=block,
                since=latest.get("ts"),
                evidence={"block": block, "response": latest.get("response")},
            )
        )
    return items


# ── audit collector (D5 row 7) ───────────────────────────────────────────────


def collect_audits(experiment_dir: Path, *, now: str) -> QueueCollection:
    """Unsigned / stale notebook-audit sections (D5 row 7) via ``audit_module``.

    The T6 reduction (``state/notebook_audit.py::audit_module``, which itself
    routes drift through the attestation kernel) is the one definition; the
    collector maps ``UNSIGNED`` → ``audit-section-unsigned`` (verdict, a graduation
    gate is blocked) and ``SIGNED_STALE`` → ``audit-section-stale`` (informational,
    an edit revoked a prior sign-off), and NEVER touches a sha. Required slugs come
    from the audit's template via the same resolver the sign-off gate uses
    (``ops/decision/verify_relay.py::_nb_resolve_sources``, itself
    ``ops/decision/journal.py::_read_interview_audited_source`` +
    ``state/audit_source.py``). An audit with no resolvable ``audited_source``
    opt-in contributes nothing and is recorded in ``skipped`` (D7 fail-safe).
    """
    from hpc_agent.ops.decision.verify_relay import _nb_resolve_sources
    from hpc_agent.state.notebook_audit import (
        SIGNED_STALE,
        UNSIGNED,
        audit_module,
    )

    exp = _exp(experiment_dir)
    items: list[AttentionItem] = []
    skipped: list[dict[str, str]] = []
    for audit_id in _discover_audit_ids(experiment_dir):
        parsed, required_slugs = _nb_resolve_sources(experiment_dir, audit_id)
        if parsed is None or required_slugs is None:
            skipped.append(
                {"ref": audit_id, "reason": "no resolvable audited_source opt-in / template"}
            )
            continue
        module = audit_module(
            experiment_dir, audit_id, source=parsed, required_slugs=required_slugs
        )
        for section in module.sections:
            if section.status == UNSIGNED:
                kind = AUDIT_SECTION_UNSIGNED
            elif section.status == SIGNED_STALE:
                kind = AUDIT_SECTION_STALE
            else:
                continue  # signed_current / auto_cleared → nothing needs attention
            items.append(
                AttentionItem(
                    kind=kind,
                    item_class=KIND_CLASS[kind],
                    experiment_dir=exp,
                    scope_kind="notebook",
                    scope_id=audit_id,
                    block=section.slug,
                    evidence={
                        "slug": section.slug,
                        "status": section.status,
                        "current_section_sha": section.current_section_sha,
                        "signed_section_sha": section.signed_section_sha,
                        "attestor": section.attestor,
                    },
                )
            )
    return QueueCollection(items=items, skipped=skipped)


# ── infra collectors (D5 rows 8-9) ───────────────────────────────────────────


def collect_alerts(experiment_dir: Path, *, now: str) -> list[AttentionItem]:
    """Unacknowledged watchdog alerts (D5 row 8) via ``read_unacknowledged_alerts``
    — peek-only (D6): the queue never advances the acknowledgment watermark.
    """
    from hpc_agent.ops.recover.notify import read_unacknowledged_alerts

    exp = _exp(experiment_dir)
    items: list[AttentionItem] = []
    for alert in read_unacknowledged_alerts(experiment_dir):
        ts = alert.get("ts", "")
        items.append(
            AttentionItem(
                kind=ALERT,
                item_class=KIND_CLASS[ALERT],
                experiment_dir=exp,
                scope_kind=None,
                scope_id=ts,
                since=ts or None,
                action=alert.get("message"),
                evidence={"ts": ts, "message": alert.get("message")},
            )
        )
    return items


def collect_ssh_circuits(experiment_dir: Path, *, now: str) -> list[AttentionItem]:
    """Open SSH circuits (D5 row 9) via ``open_circuit_lines`` — one item per
    breaker-dark host. The host is the subject id; the source line rides ``action``.
    Machine-global (not experiment-scoped), so ``scope_kind`` is null.
    """
    from hpc_agent.ops.recover.net_triage import open_circuit_lines

    exp = _exp(experiment_dir)
    items: list[AttentionItem] = []
    for line in open_circuit_lines():
        host = _circuit_host(line)
        items.append(
            AttentionItem(
                kind=SSH_CIRCUIT_OPEN,
                item_class=KIND_CLASS[SSH_CIRCUIT_OPEN],
                experiment_dir=exp,
                scope_kind=None,
                scope_id=host,
                action=line,
                evidence={"line": line},
            )
        )
    return items


# ── composition ──────────────────────────────────────────────────────────────


def collect_items(experiment_dir: Path, *, now: str) -> QueueCollection:
    """Run every collector for one experiment; return the raw items + skip accounting.

    Single-experiment scope (D3 default). The fleet widening (glob the journal home,
    recover each experiment root, run this per experiment) is the primitive's job
    (Wave C), composing this function per discovered experiment.
    """
    audits = collect_audits(experiment_dir, now=now)
    items: list[AttentionItem] = [
        *collect_greenlight_and_parked(experiment_dir, now=now),
        *collect_stalled(experiment_dir, now=now),
        *collect_dead_workers(experiment_dir, now=now),
        *collect_anomalies(experiment_dir, now=now),
        *collect_campaign_pending(experiment_dir, now=now),
        *audits.items,
        *collect_alerts(experiment_dir, now=now),
        *collect_ssh_circuits(experiment_dir, now=now),
    ]
    return QueueCollection(items=_apply_fanout(items, experiment_dir), skipped=audits.skipped)


def _resolve_class_order(class_order: Sequence[str] | None) -> tuple[str, ...]:
    """Resolve the effective class sequence (D2, the T12 semantics).

    Listed known classes first in the given order (deduped); UNKNOWN names ignored;
    unlisted classes keep the default order after them. ``None`` / empty → default.
    """
    if not class_order:
        return DEFAULT_CLASS_ORDER
    listed: list[str] = []
    for name in class_order:
        if name in DEFAULT_CLASS_ORDER and name not in listed:
            listed.append(name)
    rest = [c for c in DEFAULT_CLASS_ORDER if c not in listed]
    return (*listed, *rest)


def _since_key(since: str | None) -> tuple[int, float]:
    """Oldest-``since``-first key; a null / unparseable ``since`` sorts LAST (D2)."""
    dt = parse_iso_utc_or_none(since)
    if dt is None:
        return (1, 0.0)
    return (0, dt.timestamp())


def order_items(
    items: Sequence[AttentionItem], *, class_order: Sequence[str] | None = None
) -> list[AttentionItem]:
    """The D2-REVISED total order (user, 2026-07-08 — leverage-primary):
    **fan-out descending → class order → oldest ``since`` first → ``(kind,
    scope_id)`` tiebreak**.

    The primary key is LEVERAGE — the item's ``unblocks`` fan-out counted over the
    encoded dependency edges (:func:`_apply_fanout`) — so the verdict that unblocks
    the most pending downstream work sorts first (the queue is a STANDING TODO the
    human works from over weeks, not just an overnight digest). Where fan-out is 0
    (no encoded edge) the item falls through to the class order BYTE-IDENTICALLY
    with the pre-revision rule.

    ``class_order`` survives as the CLASS-grain override, now at the tiebreak level
    below fan-out (unknown names ignored, unlisted keep the default after —
    :func:`_resolve_class_order`). The within-class rule and the fan-out
    computation itself are FIXED and never overridable — a caller re-ranking
    individual items, or re-weighting the fan-out, would be doing prioritization
    prose, an affordance deliberately absent. Byte-reproducible for a given fleet
    state.
    """
    rank = {c: i for i, c in enumerate(_resolve_class_order(class_order))}
    default_rank = len(rank)

    def key(item: AttentionItem) -> tuple[int, int, tuple[int, float], str, str]:
        return (
            -item.unblocks,  # fan-out DESCENDING (higher leverage first)
            rank.get(item.item_class, default_rank),
            _since_key(item.since),
            item.kind,
            item.scope_id,
        )

    return sorted(items, key=key)


def collect_queue(
    experiment_dir: Path, *, now: str, class_order: Sequence[str] | None = None
) -> list[AttentionItem]:
    """Collect one experiment's items and return them in the D2 total order.

    The one-definition seat both ``status-snapshot``'s embedded ``attention`` field
    and the ``attention-queue`` verb call, so the two surfaces cannot disagree on
    ordering. Skip accounting is dropped here — the primitive uses
    :func:`collect_items` directly when it needs ``skipped``.
    """
    return order_items(collect_items(experiment_dir, now=now).items, class_order=class_order)


def count_by_class(items: Sequence[AttentionItem]) -> dict[str, int]:
    """Item count per class, in the default class order (present classes only)."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item.item_class] = counts.get(item.item_class, 0) + 1
    return {c: counts[c] for c in DEFAULT_CLASS_ORDER if c in counts}


# ── the D2-revision leverage walk: unblock fan-out over encoded edges ─────────


def _apply_fanout(items: list[AttentionItem], experiment_dir: Path) -> list[AttentionItem]:
    """Stamp each item's ``unblocks`` fan-out over the edges the journals ENCODE.

    The D2 revision (user, 2026-07-08 — leverage-primary ordering): LEVERAGE = the
    count of pending downstream subjects that become actionable when THIS one
    verdict clears, walked over dependency edges the records ALREADY encode. This
    stays inside the no-fabrication boundary because fan-out is COUNTED from record
    structure, never scored — where no encoded edge exists the fan-out is 0 and the
    item falls through to the class order (byte-identical to the pre-revision rule).

    The edges walked (the doc's revision names them):

    * ``greenlight-unadvanced`` → its run — a committed-unadvanced greenlight
      blocks its whole run; that one run is the downstream subject, so 1.
    * ``audit-section-unsigned`` / ``audit-section-stale`` → the module's
      ``passed`` gate → every run whose sidecar ``audited_source`` echo names this
      audit — the section blocks the module gate, which blocks graduation of every
      run that opted into the audit (counted via a non-creating sidecar glob).
    * ``campaign-pending`` → the campaign's remaining (non-terminal) runs — the
      pending verdict blocks them from progressing.

    Every other kind has no encoded downstream edge → 0.
    """
    from dataclasses import replace

    stamped: list[AttentionItem] = []
    for item in items:
        fan = _fanout_for(item, experiment_dir)
        stamped.append(replace(item, unblocks=fan) if fan else item)
    return stamped


def _fanout_for(item: AttentionItem, experiment_dir: Path) -> int:
    """The one dispatch from an item's kind to its encoded downstream count."""
    if item.kind == GREENLIGHT_UNADVANCED:
        return 1  # the committed-unadvanced greenlight blocks its whole run
    if item.kind in (AUDIT_SECTION_UNSIGNED, AUDIT_SECTION_STALE):
        return _count_runs_echoing_audit(experiment_dir, item.scope_id)
    if item.kind == CAMPAIGN_PENDING:
        return _count_campaign_pending_runs(experiment_dir, item.scope_id)
    return 0


def _count_runs_echoing_audit(experiment_dir: Path, audit_id: str) -> int:
    """Runs whose sidecar ``audited_source`` echo names *audit_id* (D2-rev edge).

    NON-CREATING glob of ``<experiment_dir>/.hpc/runs/*.json``: the sidecar echo of
    interview.json's ``audited_source`` opt-in (``{source, template, audit_id}``;
    notebook-audit T14) is the encoded edge from an audit to the runs that graduate
    behind it. Opaque, fail-open read — a torn/unreadable sidecar is skipped, never
    crashing the morning read; a missing runs dir counts 0.
    """
    runs = Path(experiment_dir) / ".hpc" / "runs"
    if not runs.is_dir():
        return 0
    count = 0
    for path in sorted(runs.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        echo = doc.get("audited_source") if isinstance(doc, dict) else None
        if isinstance(echo, dict) and echo.get("audit_id") == audit_id:
            count += 1
    return count


def _count_campaign_pending_runs(experiment_dir: Path, campaign_id: str) -> int:
    """The campaign's remaining (non-terminal) runs (D2-rev edge).

    Routes through ``state/index.py::find_runs_by_campaign`` (the SAME membership
    seam the campaign loop uses — the queue invents no new campaign-state
    predicate) and counts the runs not yet in a ``TERMINAL_STATUSES`` state: the
    runs a pending campaign verdict blocks from progressing. Non-creating (the
    predicate guards on the journal home existing).
    """
    from hpc_agent.state.index import find_runs_by_campaign
    from hpc_agent.state.run_record import TERMINAL_STATUSES

    return sum(
        1
        for r in find_runs_by_campaign(experiment_dir, campaign_id)
        if r.status not in TERMINAL_STATUSES
    )


# ── fleet discovery (D3) ─────────────────────────────────────────────────────


def discover_fleet_experiments() -> tuple[list[Path], list[dict[str, str]]]:
    """Every experiment this machine has journaled — via a NON-CREATING glob (D3).

    Globs the journal home for ``*/repo.json`` (never ``journal_dir``, which
    mkdirs + writes ``repo.json`` — a read must never scaffold a namespace) and
    recovers each ``experiment_dir``. Returns ``(experiment_dirs, skipped)``: a
    ``repo.json`` that is unreadable / torn, or whose ``experiment_dir`` no longer
    exists on disk, is skipped silently and counted (a wiped demo repo must never
    crash the morning read). A missing journal home yields nothing.
    """
    from hpc_agent.state.run_record import _current_homedir

    experiments: list[Path] = []
    skipped: list[dict[str, str]] = []
    home = _current_homedir()
    if not home.exists():
        return experiments, skipped
    for repo_json in sorted(home.glob("*/repo.json")):
        namespace = repo_json.parent.name
        try:
            doc = json.loads(repo_json.read_text(encoding="utf-8"))
            experiment_dir = doc["experiment_dir"]
        except (OSError, ValueError, KeyError, TypeError):
            skipped.append({"ref": namespace, "reason": "unreadable/torn repo.json"})
            continue
        if not isinstance(experiment_dir, str) or not experiment_dir:
            skipped.append({"ref": namespace, "reason": "repo.json has no experiment_dir"})
            continue
        path = Path(experiment_dir)
        if not path.exists():
            skipped.append({"ref": namespace, "reason": "experiment_dir no longer exists"})
            continue
        experiments.append(path)
    return experiments, skipped


def collect_fleet(*, now: str) -> QueueCollection:
    """Collect items across every journaled experiment (D3 fleet mode).

    Composes :func:`discover_fleet_experiments` (non-creating) with
    :func:`collect_items` per discovered experiment, accumulating both the items
    and the fail-open ``skipped`` accounting (torn namespaces plus any per-audit
    skips). The primitive orders + renders the result (Wave C); the discipline
    that no namespace is scaffolded on read lives here.
    """
    experiments, skipped = discover_fleet_experiments()
    items: list[AttentionItem] = []
    for experiment_dir in experiments:
        collection = collect_items(experiment_dir, now=now)
        items.extend(collection.items)
        skipped.extend(collection.skipped)
    return QueueCollection(items=items, skipped=skipped)


# ── non-creating discovery helpers (D3 glob discipline) ──────────────────────


def _all_run_records(experiment_dir: Path) -> list[Any]:
    """Every ``RunRecord`` under the experiment's journal namespace — NON-CREATING.

    Globs ``<journal_home>/<repo_hash>/runs/*.json`` directly (never
    ``journal_dir``, which mkdirs + writes ``repo.json``) so a read never scaffolds
    a namespace. Fail-open: an absent home / namespace / unreadable record yields
    nothing. Needed because no fleet ``find_failed_runs`` predicate exists (the
    anomaly VERDICT still routes through ``status_blocks``; only enumeration is
    added here).
    """
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.run_record import _current_homedir, repo_hash

    home = _current_homedir()
    if not home.exists():
        return []
    runs = home / repo_hash(experiment_dir) / "runs"
    if not runs.is_dir():
        return []
    records: list[Any] = []
    for path in sorted(runs.glob("*.json")):
        if path.name.endswith(".last_status.json"):
            continue
        try:
            record = load_run(experiment_dir, path.stem)
        except (OSError, ValueError):
            continue
        if record is not None:
            records.append(record)
    return records


def _discover_campaign_ids(experiment_dir: Path) -> list[str]:
    """Campaign ids with a decision journal — via a NON-CREATING glob (D3)."""
    base = Path(experiment_dir) / ".hpc" / "campaigns"
    if not base.is_dir():
        return []
    return [p.parent.name for p in sorted(base.glob("*/decisions.jsonl"))]


def _discover_audit_ids(experiment_dir: Path) -> list[str]:
    """Notebook audit ids with a decision journal — via a NON-CREATING glob (D3)."""
    suffix = ".decisions.jsonl"
    base = Path(experiment_dir) / ".hpc" / "notebooks"
    if not base.is_dir():
        return []
    ids: list[str] = []
    for path in sorted(base.glob(f"*{suffix}")):
        name = path.name
        if name.endswith(suffix):
            ids.append(name[: -len(suffix)])
    return ids


def _circuit_host(line: str) -> str:
    """The host an ``open_circuit_lines`` string names (stable prefix), else the line.

    The source format is ``"ssh circuit for <host>: ..."`` — parse the host so the
    subject id is a clean host, falling back to the whole line if the format ever
    changes (never crash the read).
    """
    prefix = "ssh circuit for "
    if line.startswith(prefix):
        return line[len(prefix) :].split(":", 1)[0].strip() or line
    return line
