"""Tests for the attention-queue collectors, ordering, and discipline (Wave B / T1).

Design: ``docs/design/attention-queue.md``. Each per-kind collector is exercised
with REAL records written through the real writers in a tmp experiment (D5 route-
through, not a mock of the predicate). The total order (D2), the ``class_order``
override semantics (T12), fleet discovery via ``repo.json`` glob (D3, non-creating),
the ``inspect.getsource`` route-through assertions (each collector calls its one
source symbol), and the read-only / watermark-neutral discipline (D6) are pinned.

Cluster-free: the journal home is redirected via ``HPC_JOURNAL_DIR``; the only
liveness dependency (``_pid_alive`` for the dead-worker scan) and the ssh-circuit
line source are monkeypatched so nothing touches a real process or breaker file.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import hpc_agent.ops.attention_queue as q
import hpc_agent.ops.recover.doctor as doctor_mod
import hpc_agent.ops.recover.net_triage as net_triage_mod
from hpc_agent.ops.attention_queue import (
    ALERT,
    AUDIT_SECTION_STALE,
    AUDIT_SECTION_UNSIGNED,
    BLOCKED,
    CAMPAIGN_PENDING,
    DEAD_WORKER,
    GREENLIGHT_UNADVANCED,
    INFORMATIONAL,
    RUN_ANOMALY,
    RUN_PARKED,
    RUN_STALLED,
    SSH_CIRCUIT_OPEN,
    VERDICT,
    AttentionItem,
    collect_alerts,
    collect_anomalies,
    collect_audits,
    collect_campaign_pending,
    collect_dead_workers,
    collect_greenlight_and_parked,
    collect_items,
    collect_queue,
    collect_ssh_circuits,
    collect_stalled,
    count_by_class,
    discover_fleet_experiments,
    order_items,
)
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import (
    load_run,
    mark_pending_decision,
    mark_seen_by_human,
    stamp_tick,
    upsert_run,
)
from hpc_agent.state.run_record import RunRecord, _current_homedir

_NOW = "2026-07-06T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _mk(exp: Path, run_id: str, *, status: str = "in_flight", **kw: object) -> RunRecord:
    rec = RunRecord(
        run_id=run_id,
        profile="prof",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path="/scratch/run",
        job_name="job",
        job_ids=["1"],
        total_tasks=10,
        submitted_at="2026-07-06T00:00:00+00:00",
        experiment_dir=str(exp),
        status=status,
        **kw,  # type: ignore[arg-type]
    )
    upsert_run(exp, rec)
    return rec


# ── per-collector: fires on a real record, passes on an empty journal ────────


def test_stalled_collector_fires(tmp_path: Path) -> None:
    _mk(tmp_path, "run-stalled")
    stamp_tick(
        "run-stalled",
        last_tick_at="2026-07-06T05:00:00+00:00",
        next_tick_due="2026-07-06T06:00:00+00:00",
        experiment_dir=tmp_path,
    )
    items = collect_stalled(tmp_path, now=_NOW)
    assert [i.kind for i in items] == [RUN_STALLED]
    item = items[0]
    assert item.item_class == BLOCKED
    assert item.scope_kind == "run"
    assert item.scope_id == "run-stalled"
    assert item.since == "2026-07-06T05:00:00+00:00"
    assert item.cluster == "hoffman2"


def test_stalled_collector_empty(tmp_path: Path) -> None:
    assert collect_stalled(tmp_path, now=_NOW) == []


def test_parked_vs_greenlight_split(tmp_path: Path) -> None:
    """find_parked_runs split by is_latest_committed_greenlight (D5 rows 1-2)."""
    # Parked, no committed y → run-parked (verdict).
    _mk(tmp_path, "run-parked")
    mark_pending_decision(
        "run-parked",
        block="submit-s2",
        workflow="submit",
        brief={},
        resume_cursor={},
        awaiting_since="2026-07-06T04:00:00+00:00",
        experiment_dir=tmp_path,
    )
    # Parked AND latest committed decision is y → greenlight-unadvanced (blocked).
    _mk(tmp_path, "run-green")
    mark_pending_decision(
        "run-green",
        block="submit-s3",
        workflow="submit",
        brief={},
        resume_cursor={},
        awaiting_since="2026-07-06T03:00:00+00:00",
        experiment_dir=tmp_path,
    )
    append_decision(
        tmp_path, scope_kind="run", scope_id="run-green", block="submit-s3", response="y"
    )

    by_id = {i.scope_id: i for i in collect_greenlight_and_parked(tmp_path, now=_NOW)}
    assert by_id["run-parked"].kind == RUN_PARKED
    assert by_id["run-parked"].item_class == VERDICT
    assert by_id["run-parked"].block == "submit-s2"
    assert by_id["run-green"].kind == GREENLIGHT_UNADVANCED
    assert by_id["run-green"].item_class == BLOCKED
    assert by_id["run-green"].block == "submit-s3"


def test_dead_worker_collector_fires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod, "_pid_alive", lambda _pid: False)
    detached = _current_homedir() / "_detached"
    detached.mkdir(parents=True, exist_ok=True)
    (detached / "submit-s4-run-crashed.lease.json").write_text(
        json.dumps(
            {
                "run_id": "run-crashed",
                "block": "submit-s4",
                "pid": 999_999_999,
                # The scan scopes the GLOBAL lease dir by the --experiment-dir
                # flag the one lease writer always stamps into the child argv.
                "argv": ["hpc-agent", "submit-s4", "--experiment-dir", str(tmp_path)],
            }
        ),
        encoding="utf-8",
    )
    items = collect_dead_workers(tmp_path, now=_NOW)
    assert [i.kind for i in items] == [DEAD_WORKER]
    item = items[0]
    assert item.item_class == BLOCKED
    assert item.scope_id == "run-crashed"
    assert item.block == "submit-s4"
    # The source predicate's OWN drafted proposal rides `action` verbatim.
    assert item.action is not None
    assert "submit-s4" in item.action
    assert "re-invoke" in item.action.lower()


def test_anomaly_collector_fires_and_excludes_superseded(tmp_path: Path) -> None:
    _mk(tmp_path, "run-failed", status="failed", last_tick_at="2026-07-06T06:00:00+00:00")
    _mk(tmp_path, "run-abandoned", status="abandoned")
    # A superseded record is a deliberate closure — never an anomaly.
    _mk(tmp_path, "run-superseded", status="abandoned", superseded_by="run-new")

    items = collect_anomalies(tmp_path, now=_NOW)
    kinds_by_id = {i.scope_id: i for i in items}
    assert set(kinds_by_id) == {"run-failed", "run-abandoned"}
    assert all(i.kind == RUN_ANOMALY and i.item_class == VERDICT for i in items)
    # The recommendation DATA (the source's own action) rides `action`.
    assert kinds_by_id["run-failed"].action == "classify-failed-tasks"
    assert kinds_by_id["run-abandoned"].action == "reconcile-journal"


def test_campaign_pending_fires_but_start_only_greenlight_yields_nothing(tmp_path: Path) -> None:
    """A campaign whose newest touchpoint is not a committed y is pending; a
    campaign whose ONLY record is the start greenlight y yields no item (D5 / the
    open-question pinned)."""
    # camp1: start y then a completion brief awaiting a response → pending.
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id="camp1",
        block="campaign-greenlight",
        response="y",
        ts="2026-07-06T01:00:00+00:00",
    )
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id="camp1",
        block="campaign-complete",
        response="please review",
        ts="2026-07-06T02:00:00+00:00",
    )
    # camp2: only the start greenlight y → latest IS a committed y → no item.
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id="camp2",
        block="campaign-greenlight",
        response="y",
    )

    items = collect_campaign_pending(tmp_path, now=_NOW)
    assert [i.scope_id for i in items] == ["camp1"]
    item = items[0]
    assert item.kind == CAMPAIGN_PENDING
    assert item.item_class == VERDICT
    assert item.block == "campaign-complete"
    assert item.since == "2026-07-06T02:00:00+00:00"


def test_alert_collector_fires(tmp_path: Path) -> None:
    from hpc_agent.state.run_record import journal_dir

    log = journal_dir(tmp_path) / "doctor.alerts.log"
    log.write_text("2026-07-06T07:00:00+00:00 driver stalled, run r — re-arm?\n", encoding="utf-8")
    items = collect_alerts(tmp_path, now=_NOW)
    assert [i.kind for i in items] == [ALERT]
    item = items[0]
    assert item.item_class == INFORMATIONAL
    assert item.scope_kind is None
    assert item.since == "2026-07-06T07:00:00+00:00"
    assert item.action is not None and "re-arm" in item.action


def test_ssh_circuit_collector_fires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    line = "ssh circuit for hoffman2: OPEN until 2026-07-06T13:00:00Z (5 failures)"
    monkeypatch.setattr(net_triage_mod, "open_circuit_lines", lambda: [line])
    items = collect_ssh_circuits(tmp_path, now=_NOW)
    assert [i.kind for i in items] == [SSH_CIRCUIT_OPEN]
    item = items[0]
    assert item.item_class == INFORMATIONAL
    assert item.scope_kind is None
    assert item.scope_id == "hoffman2"  # host parsed out of the source line
    assert item.action == line


# ── the total order (D2) ─────────────────────────────────────────────────────


def _item(kind: str, klass: str, scope_id: str, since: str | None) -> AttentionItem:
    return AttentionItem(
        kind=kind,
        item_class=klass,
        experiment_dir="/exp",
        scope_kind="run",
        scope_id=scope_id,
        since=since,
    )


def test_total_order_class_then_oldest_then_tiebreak() -> None:
    items = [
        _item(RUN_ANOMALY, VERDICT, "z", "2026-07-06T05:00:00+00:00"),
        _item(RUN_PARKED, VERDICT, "a", "2026-07-06T02:00:00+00:00"),
        _item(RUN_STALLED, BLOCKED, "s", "2026-07-06T09:00:00+00:00"),
        _item(GREENLIGHT_UNADVANCED, BLOCKED, "g", "2026-07-06T01:00:00+00:00"),
        _item(ALERT, INFORMATIONAL, "i", None),
    ]
    ordered = order_items(items)
    # blocked first (oldest-since within class), then verdict, then informational.
    assert [i.item_class for i in ordered] == [BLOCKED, BLOCKED, VERDICT, VERDICT, INFORMATIONAL]
    assert [i.scope_id for i in ordered] == ["g", "s", "a", "z", "i"]


def test_null_since_sorts_last_within_class() -> None:
    items = [
        _item(ALERT, INFORMATIONAL, "b", None),
        _item(SSH_CIRCUIT_OPEN, INFORMATIONAL, "a", "2026-07-06T03:00:00+00:00"),
    ]
    ordered = order_items(items)
    assert [i.scope_id for i in ordered] == ["a", "b"]  # dated first, null last


def test_tiebreak_is_kind_then_scope_id() -> None:
    items = [
        _item(RUN_STALLED, BLOCKED, "b", None),
        _item(RUN_STALLED, BLOCKED, "a", None),
        _item(DEAD_WORKER, BLOCKED, "z", None),
    ]
    ordered = order_items(items)
    # dead-worker < run-stalled lexicographically; within run-stalled, a < b.
    assert [(i.kind, i.scope_id) for i in ordered] == [
        (DEAD_WORKER, "z"),
        (RUN_STALLED, "a"),
        (RUN_STALLED, "b"),
    ]


def test_order_is_deterministic_under_shuffle() -> None:
    import random

    items = [
        _item(RUN_ANOMALY, VERDICT, f"r{i}", f"2026-07-06T0{i}:00:00+00:00") for i in range(1, 6)
    ]
    baseline = [i.scope_id for i in order_items(items)]
    shuffled = list(items)
    random.Random(7).shuffle(shuffled)
    assert [i.scope_id for i in order_items(shuffled)] == baseline


# ── class_order override (T12) ───────────────────────────────────────────────


def _one_of_each() -> list[AttentionItem]:
    return [
        _item(RUN_STALLED, BLOCKED, "b", "2026-07-06T01:00:00+00:00"),
        _item(RUN_PARKED, VERDICT, "v", "2026-07-06T01:00:00+00:00"),
        _item(ALERT, INFORMATIONAL, "i", "2026-07-06T01:00:00+00:00"),
    ]


def test_class_order_listed_first() -> None:
    ordered = order_items(_one_of_each(), class_order=["informational"])
    # listed class first; unlisted keep default order after.
    assert [i.item_class for i in ordered] == [INFORMATIONAL, BLOCKED, VERDICT]


def test_class_order_unknown_ignored() -> None:
    ordered = order_items(_one_of_each(), class_order=["bogus", "verdict"])
    # unknown 'bogus' ignored; verdict first; unlisted keep default after.
    assert [i.item_class for i in ordered] == [VERDICT, BLOCKED, INFORMATIONAL]


def test_class_order_unlisted_keep_default() -> None:
    ordered = order_items(_one_of_each(), class_order=["verdict"])
    assert [i.item_class for i in ordered] == [VERDICT, BLOCKED, INFORMATIONAL]


def test_within_class_rule_not_overridable_by_class_order() -> None:
    """class_order moves CLASSES only; within a class the oldest-since rule holds."""
    items = [
        _item(RUN_PARKED, VERDICT, "new", "2026-07-06T09:00:00+00:00"),
        _item(RUN_ANOMALY, VERDICT, "old", "2026-07-06T01:00:00+00:00"),
    ]
    ordered = order_items(items, class_order=["verdict"])
    assert [i.scope_id for i in ordered] == ["old", "new"]  # oldest first, unaffected


# ── route-through: each collector calls its ONE source symbol (D5) ───────────


def test_route_through_source_symbols() -> None:
    checks = {
        collect_greenlight_and_parked: ["find_parked_runs(", "is_latest_committed_greenlight("],
        collect_stalled: ["find_stalled_runs("],
        collect_dead_workers: ["scan_dead_detached_workers("],
        collect_anomalies: ["digest_run(", "ANOMALY_STATUSES", "recommendation_for("],
        collect_campaign_pending: ["latest_decision(", "is_latest_committed_greenlight("],
        collect_audits: ["audit_module("],
        collect_alerts: ["read_unacknowledged_alerts("],
        collect_ssh_circuits: ["open_circuit_lines("],
    }
    for fn, needles in checks.items():
        src = inspect.getsource(fn)
        for needle in needles:
            assert needle in src, f"{fn.__name__} must route through {needle!r} (D5 one-definition)"


# ── read-only / watermark-neutral discipline (D6) ────────────────────────────


def test_no_write_calls_in_the_package_source() -> None:
    """Source scan: the attention package writes nothing and moves no watermark.

    Checks CALL syntax (``name(``) so a docstring that merely NAMES a forbidden
    symbol in prose ("this package never calls mark_seen_by_human") is not a
    false positive — only an actual mutation call trips the guard.
    """
    import hpc_agent.ops.attention_render as render_mod

    forbidden = [
        "mark_seen_by_human(",
        "acknowledge_alerts(",
        "mark_pending_decision(",
        "mark_pending_verdict(",
        "stamp_tick(",
        "upsert_run(",
        "append_decision(",
        "record_auto_clear(",
        "record_render_receipt(",
        ".write_text(",
        ".write_bytes(",
        ".mkdir(",
        "atomic_write_json(",
    ]
    for mod in (q, render_mod):
        src = inspect.getsource(mod)
        for token in forbidden:
            assert token not in src, f"{mod.__name__} must not call {token!r} (watermark-neutral)"


def test_collect_queue_is_watermark_neutral(tmp_path: Path) -> None:
    """A write-probe: collecting moves no watermark and journals nothing."""
    from hpc_agent.state.run_record import journal_dir

    rec = _mk(tmp_path, "run-seen", last_seen_by_human_at="2026-07-06T00:00:00+00:00")
    mark_seen_by_human(rec.run_id, at="2026-07-06T00:00:00+00:00", experiment_dir=tmp_path)
    log = journal_dir(tmp_path) / "doctor.alerts.log"
    log.write_text("2026-07-06T07:00:00+00:00 an alert\n", encoding="utf-8")

    def _decision_journals() -> set[str]:
        base = tmp_path / ".hpc"
        return {str(p) for p in base.rglob("*.decisions.jsonl")} if base.exists() else set()

    before = _decision_journals()
    collect_queue(tmp_path, now=_NOW)
    after = _decision_journals()

    # The alert acknowledgment watermark was never advanced (queue is peek-only).
    assert not (journal_dir(tmp_path) / "doctor.alerts.seen").exists()
    # The attention watermark on the record is untouched.
    reloaded = load_run(tmp_path, "run-seen")
    assert reloaded is not None
    assert reloaded.last_seen_by_human_at == "2026-07-06T00:00:00+00:00"
    # No decision journal was written by the queue.
    assert before == after


# ── fleet discovery via repo.json glob (D3) ──────────────────────────────────


def test_fleet_discovery_is_non_creating_on_empty_home(tmp_path: Path) -> None:
    home = _current_homedir()  # does not exist yet
    experiments, skipped = discover_fleet_experiments()
    assert experiments == []
    assert skipped == []
    # No directory scaffolded under a fresh journal home during a fleet scan.
    assert not home.exists()


def test_fleet_discovery_finds_journaled_experiments(tmp_path: Path) -> None:
    exp_a = tmp_path / "exp_a"
    exp_b = tmp_path / "exp_b"
    exp_a.mkdir()
    exp_b.mkdir()
    _mk(exp_a, "ra")  # upsert_run → journal_dir writes repo.json for exp_a
    _mk(exp_b, "rb")
    home = _current_homedir()
    namespaces_before = {p.name for p in home.iterdir()}

    experiments, skipped = discover_fleet_experiments()
    assert skipped == []
    assert {p.resolve() for p in experiments} == {exp_a.resolve(), exp_b.resolve()}
    # Non-creating pin: discovery scaffolded no new namespace.
    assert {p.name for p in home.iterdir()} == namespaces_before


def test_fleet_discovery_skips_unreadable_and_absent(tmp_path: Path) -> None:
    home = _current_homedir()
    home.mkdir(parents=True, exist_ok=True)
    # A torn repo.json.
    torn = home / "torn_ns"
    torn.mkdir()
    (torn / "repo.json").write_text("{not json", encoding="utf-8")
    # A repo.json pointing at an experiment_dir that no longer exists.
    gone = home / "gone_ns"
    gone.mkdir()
    (gone / "repo.json").write_text(
        json.dumps({"experiment_dir": str(tmp_path / "vanished")}), encoding="utf-8"
    )

    experiments, skipped = discover_fleet_experiments()
    assert experiments == []
    refs = {s["ref"] for s in skipped}
    assert refs == {"torn_ns", "gone_ns"}


# ── the audit collector (D5 row 7) ───────────────────────────────────────────

_SRC = "# %%\n# hpc-audit-section: sec-a\nx = 1\n"
_SRC_EDITED = "# %%\n# hpc-audit-section: sec-a\nx = 2  # drifted\n"
_TMPL = "# %%\n# hpc-audit-section: sec-a\nx = 0\n"


def _setup_audit(exp: Path, *, source_text: str) -> str:
    """Write interview.json + source/template .py and return sec-a's current sha."""
    hpc = exp / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "src.py").write_text(source_text, encoding="utf-8")
    (hpc / "tmpl.py").write_text(_TMPL, encoding="utf-8")
    (exp / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "audit_id": "nb1",
                    "source": ".hpc/src.py",
                    "template": ".hpc/tmpl.py",
                }
            }
        ),
        encoding="utf-8",
    )
    return parse_percent_source(source_text).sections[0].section_sha


def test_audit_collector_unsigned_section(tmp_path: Path) -> None:
    _setup_audit(tmp_path, source_text=_SRC)
    # A discoverable notebook journal with no valid attestation → sec-a unsigned.
    nb = tmp_path / ".hpc" / "notebooks"
    nb.mkdir(parents=True, exist_ok=True)
    (nb / "nb1.decisions.jsonl").write_text(
        json.dumps({"schema_version": 1, "block": "noop"}) + "\n", encoding="utf-8"
    )

    collection = collect_audits(tmp_path, now=_NOW)
    assert collection.skipped == []
    assert [i.kind for i in collection.items] == [AUDIT_SECTION_UNSIGNED]
    item = collection.items[0]
    assert item.item_class == VERDICT
    assert item.scope_kind == "notebook"
    assert item.scope_id == "nb1"
    assert item.block == "sec-a"


def test_audit_collector_signed_then_stale(tmp_path: Path) -> None:
    sha = _setup_audit(tmp_path, source_text=_SRC)
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id="nb1",
        block="notebook-sign-off",
        response="y",
        resolved={"audit_id": "nb1", "section": "sec-a", "section_sha": sha},
    )
    # Signed at the current sha → nothing to surface.
    assert collect_audits(tmp_path, now=_NOW).items == []

    # Now the source drifts → the human sign-off is stale → informational item.
    (tmp_path / ".hpc" / "src.py").write_text(_SRC_EDITED, encoding="utf-8")
    collection = collect_audits(tmp_path, now=_NOW)
    assert [i.kind for i in collection.items] == [AUDIT_SECTION_STALE]
    assert collection.items[0].item_class == INFORMATIONAL


def test_audit_with_no_opt_in_is_skipped_not_crashed(tmp_path: Path) -> None:
    """A journal-discovered audit with no resolvable audited_source contributes
    nothing and is recorded in skipped (D7 fail-safe)."""
    nb = tmp_path / ".hpc" / "notebooks"
    nb.mkdir(parents=True, exist_ok=True)
    (nb / "orphan.decisions.jsonl").write_text(
        json.dumps({"schema_version": 1, "block": "noop"}) + "\n", encoding="utf-8"
    )
    collection = collect_audits(tmp_path, now=_NOW)
    assert collection.items == []
    assert [s["ref"] for s in collection.skipped] == ["orphan"]


# ── composition / counts ─────────────────────────────────────────────────────


def test_collect_queue_orders_and_counts(tmp_path: Path) -> None:
    _mk(tmp_path, "run-failed", status="failed")
    _mk(tmp_path, "run-stalled")
    stamp_tick(
        "run-stalled",
        last_tick_at="2026-07-06T05:00:00+00:00",
        next_tick_due="2026-07-06T06:00:00+00:00",
        experiment_dir=tmp_path,
    )
    items = collect_queue(tmp_path, now=_NOW)
    # blocked (stalled) before verdict (anomaly).
    assert [i.item_class for i in items] == [BLOCKED, VERDICT]
    assert count_by_class(items) == {BLOCKED: 1, VERDICT: 1}


def test_collect_items_carries_audit_skips(tmp_path: Path) -> None:
    nb = tmp_path / ".hpc" / "notebooks"
    nb.mkdir(parents=True, exist_ok=True)
    (nb / "orphan.decisions.jsonl").write_text(
        json.dumps({"block": "noop"}) + "\n", encoding="utf-8"
    )
    collection = collect_items(tmp_path, now=_NOW)
    assert [s["ref"] for s in collection.skipped] == ["orphan"]


# ── the D2-revision leverage order (fan-out primary) ─────────────────────────


def _item_u(
    kind: str, klass: str, scope_id: str, since: str | None, *, unblocks: int = 0
) -> AttentionItem:
    return AttentionItem(
        kind=kind,
        item_class=klass,
        experiment_dir="/exp",
        scope_kind="run",
        scope_id=scope_id,
        since=since,
        unblocks=unblocks,
    )


def test_fanout_is_the_primary_sort_key_over_class() -> None:
    """The D2 revision: LEVERAGE (unblocks desc) outranks the class order — a
    high-fan-out verdict sorts before a low-fan-out blocked item."""
    items = [
        _item_u(RUN_STALLED, BLOCKED, "s", "2026-07-06T01:00:00+00:00", unblocks=0),
        _item_u(AUDIT_SECTION_UNSIGNED, VERDICT, "a", "2026-07-06T09:00:00+00:00", unblocks=5),
    ]
    ordered = order_items(items)
    # unblocks 5 (verdict) sorts before unblocks 0 (blocked) — fan-out is primary.
    assert [i.scope_id for i in ordered] == ["a", "s"]


def test_greenlight_blocking_a_run_outranks_a_lone_unsigned_section() -> None:
    """A committed-unadvanced greenlight (fan-out 1: unblocks its run) outranks a
    lone unsigned section named by no sidecar (fan-out 0)."""
    items = [
        _item_u(AUDIT_SECTION_UNSIGNED, VERDICT, "sec", "2026-07-06T01:00:00+00:00", unblocks=0),
        _item_u(GREENLIGHT_UNADVANCED, BLOCKED, "run-g", "2026-07-06T09:00:00+00:00", unblocks=1),
    ]
    ordered = order_items(items)
    assert [i.kind for i in ordered] == [GREENLIGHT_UNADVANCED, AUDIT_SECTION_UNSIGNED]


def test_audit_named_by_two_sidecars_outranks_one_named_by_none() -> None:
    """An audit whose module gate blocks two runs (fan-out 2) outranks an audit
    named by no sidecar (fan-out 0), within the same class."""
    items = [
        _item_u(AUDIT_SECTION_UNSIGNED, VERDICT, "lonely", "2026-07-06T01:00:00+00:00", unblocks=0),
        _item_u(AUDIT_SECTION_UNSIGNED, VERDICT, "busy", "2026-07-06T09:00:00+00:00", unblocks=2),
    ]
    ordered = order_items(items)
    assert [i.scope_id for i in ordered] == ["busy", "lonely"]


def test_fanout_zero_falls_through_to_class_order_byte_identically() -> None:
    """With every fan-out 0 (no encoded edge), the order is byte-identical to the
    pre-revision class-primary rule (class → oldest-since → tiebreak)."""
    items = [
        _item_u(RUN_ANOMALY, VERDICT, "z", "2026-07-06T05:00:00+00:00"),
        _item_u(RUN_PARKED, VERDICT, "a", "2026-07-06T02:00:00+00:00"),
        _item_u(RUN_STALLED, BLOCKED, "s", "2026-07-06T09:00:00+00:00"),
        _item_u(GREENLIGHT_UNADVANCED, BLOCKED, "g", "2026-07-06T01:00:00+00:00"),
        _item_u(ALERT, INFORMATIONAL, "i", None),
    ]
    ordered = order_items(items)
    assert [i.item_class for i in ordered] == [BLOCKED, BLOCKED, VERDICT, VERDICT, INFORMATIONAL]
    assert [i.scope_id for i in ordered] == ["g", "s", "a", "z", "i"]


# ── the fan-out walk stamps unblocks over the encoded edges (collect_items) ──


def test_collect_items_stamps_greenlight_fanout_one(tmp_path: Path) -> None:
    _mk(tmp_path, "run-green")
    mark_pending_decision(
        "run-green",
        block="submit-s3",
        workflow="submit",
        brief={},
        resume_cursor={},
        awaiting_since="2026-07-06T03:00:00+00:00",
        experiment_dir=tmp_path,
    )
    append_decision(
        tmp_path, scope_kind="run", scope_id="run-green", block="submit-s3", response="y"
    )
    items = {i.scope_id: i for i in collect_items(tmp_path, now=_NOW).items}
    assert items["run-green"].kind == GREENLIGHT_UNADVANCED
    assert items["run-green"].unblocks == 1  # the greenlight unblocks its whole run


def test_collect_items_stamps_audit_fanout_from_sidecar_echoes(tmp_path: Path) -> None:
    _setup_audit(tmp_path, source_text=_SRC)
    nb = tmp_path / ".hpc" / "notebooks"
    nb.mkdir(parents=True, exist_ok=True)
    (nb / "nb1.decisions.jsonl").write_text(
        json.dumps({"schema_version": 1, "block": "noop"}) + "\n", encoding="utf-8"
    )
    # Run sidecars echo audited_source.audit_id; only PENDING (non-terminal,
    # non-superseded) runs count toward the fan-out (F4).
    runs = tmp_path / ".hpc" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for rid, audit in (("r1", "nb1"), ("r2", "nb1"), ("r3", "other")):
        (runs / f"{rid}.json").write_text(
            json.dumps({"audited_source": {"audit_id": audit}}), encoding="utf-8"
        )
    # F4: a TERMINAL echoing run (r4) and a SUPERSEDED one (r5) must NOT inflate
    # the count — the echo is stamped after graduation, so historical usage would
    # otherwise grow the leverage forever.
    for rid in ("r4", "r5"):
        (runs / f"{rid}.json").write_text(
            json.dumps({"audited_source": {"audit_id": "nb1"}}), encoding="utf-8"
        )
    # Journal records carry the status the fan-out filter reads.
    _mk(tmp_path, "r1", status="in_flight")
    _mk(tmp_path, "r2", status="in_flight")
    _mk(tmp_path, "r3", status="in_flight")
    _mk(tmp_path, "r4", status="complete")  # terminal → excluded
    _mk(tmp_path, "r5", status="in_flight", superseded_by="r6")  # superseded → excluded

    items = [i for i in collect_items(tmp_path, now=_NOW).items if i.scope_kind == "notebook"]
    assert items and items[0].kind == AUDIT_SECTION_UNSIGNED
    # The module gate blocks the two PENDING runs that opted into nb1 (r3 opted
    # into another audit; r4 is terminal; r5 is superseded).
    assert items[0].unblocks == 2


def test_collect_items_stamps_campaign_fanout_from_remaining_runs(tmp_path: Path) -> None:
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id="camp1",
        block="campaign-complete",
        response="please review",
        ts="2026-07-06T02:00:00+00:00",
    )
    # Two non-terminal runs belong to camp1; one complete run does not count.
    _mk(tmp_path, "cr1", status="in_flight", campaign_id="camp1")
    _mk(tmp_path, "cr2", status="in_flight", campaign_id="camp1")
    _mk(tmp_path, "cr3", status="complete", campaign_id="camp1")
    items = {i.scope_id: i for i in collect_items(tmp_path, now=_NOW).items}
    assert items["camp1"].kind == CAMPAIGN_PENDING
    assert items["camp1"].unblocks == 2  # the two remaining (non-terminal) runs


def test_fanout_walk_routes_through_campaign_membership_predicate() -> None:
    """The campaign fan-out counts via the ONE membership seam, never a re-inlined
    scan (D5 / the no-fabrication boundary — fan-out is COUNTED, not scored)."""
    src = inspect.getsource(q._count_campaign_pending_runs)
    assert "find_runs_by_campaign(" in src
    assert "TERMINAL_STATUSES" in src
