"""Live-conformance boundary contracts (``docs/design/live-conformance.md``).

The enforcement rows mechanized (T10). Live conformance is Shewhart's chart rebuilt
on attestations: the chart JUDGES, the operator ADJUSTS — the substrate OBSERVES,
JUDGES, and ROUTES, and NEVER actuates. These pins hold the load-bearing lines no
lint can, the **no-actuation pin first** (the agency boundary, mechanized):

* **No actuation affordance** — the ONLY conformance mutate verb is
  ``conformance-record`` (sole side effect: one ledger append); no core conformance
  module reaches a broker/instrument/external system; a nonconforming window changes
  NO registration status (the registration journal is byte-identical).
* **Route through the ONE kernels** — the append binds via ``state/attestation.py``;
  the comparator is ``judge_window`` (never a re-inlined envelope).
* **Sealed, fixed baseline** — a baseline path/sha not sealed in the dossier is
  refused at registration; live receipts change no baseline byte.
* **No control RULES** — 8 consecutive inside points stay ``conforming``.
* **Derived verdicts** — the query is watermark-neutral + store-free.
* **No verdict verb; record verb is not agent-facing.**
* **No market vocabulary** — the fixtures are the instrument-QC toy only.
* **The horizon is a timestamp comparison** — no duration arithmetic in core.

TOY VOCABULARY ONLY: a fake ``sensor-7`` calibration. Never trading vocabulary.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent._wire.queries.conformance_status import ConformanceStatusSpec
from hpc_agent.ops import attention_queue as aq
from hpc_agent.ops.conformance.status_op import conformance_status
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state import conformance
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.registration import reduce_registration
from tests.fixtures import toy_conformance as tc

_SRC_ROOT = Path(inspect.getfile(conformance)).parents[1]  # hpc_agent/
_CONFORMANCE_MODULES = [
    _SRC_ROOT / "state" / "conformance.py",
    _SRC_ROOT / "state" / "conformance_store.py",
    _SRC_ROOT / "ops" / "conformance" / "record_op.py",
    _SRC_ROOT / "ops" / "conformance" / "status_op.py",
    _SRC_ROOT / "ops" / "conformance_render.py",
]


def _registry() -> dict:
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    return dict(get_registry())


# ── (1) NO ACTUATION AFFORDANCE — first-class (the agency boundary mechanized) ─


def test_only_conformance_mutate_verb_is_record_with_a_single_append() -> None:
    """The ONLY conformance mutate verb is ``conformance-record``; its sole side
    effect is the one ledger append — no halt/pause/recalibrate/deploy affordance."""
    registry = _registry()
    mutating = [
        name
        for name, meta in registry.items()
        if "conformance" in name.lower() and meta.verb != "query"
    ]
    assert mutating == ["conformance-record"], (
        f"the only conformance mutate verb must be conformance-record; found {mutating}"
    )
    rec = registry["conformance-record"]
    assert rec.verb == "mutate"
    assert len(rec.side_effects) == 1  # exactly one side effect
    assert rec.side_effects[0].kind == "file_write"
    # No actuation-shaped verb name exists anywhere in the registry.
    for name in registry:
        low = name.lower()
        for banned in ("halt", "pause", "recalibrate", "deploy", "cancel-order", "actuate"):
            assert not (banned in low and "conformance" in low), (
                f"an actuation affordance appeared: {name!r}"
            )


def test_conformance_modules_reach_no_external_system() -> None:
    """No core conformance module imports a network / subprocess / device client —
    core receives an already-reduced opaque payload; the emitter is caller machinery."""
    banned_modules = {
        "socket",
        "subprocess",
        "requests",
        "urllib",
        "http",
        "httpx",
        "paramiko",
        "asyncssh",
        "ftplib",
        "smtplib",
        "telnetlib",
    }
    offenders: list[str] = []
    for path in _CONFORMANCE_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in banned_modules:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] in banned_modules
            ):
                offenders.append(f"{path.name}: from {node.module}")
    assert not offenders, f"a conformance module reaches an external system: {offenders}"


def test_no_credential_field_in_conformance_modules() -> None:
    """No conformance module names a credential field — core holds no secret to any
    external system (the emitter owns all domain I/O, arms-length forever)."""
    banned = ("password", "api_key", "apikey", "secret", "credential", "access_token")
    offenders: list[str] = []
    for path in _CONFORMANCE_MODULES:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            low = line.lower()
            if low.lstrip().startswith("#") or "never" in low:  # rule-statement / comment lines
                continue
            for word in banned:
                if word in low:
                    offenders.append(f"{path.name}:{lineno}: {word}")
    assert not offenders, f"a credential field appeared in a conformance module: {offenders}"


def test_nonconforming_window_leaves_registration_journal_byte_identical(tmp_path: Path) -> None:
    """The behavioral no-actuation pin: recording a nonconforming window + judging it
    writes ONLY the ledger — the registration decision journal is byte-identical."""
    tc.build_substrate(tmp_path)
    tc.register(tmp_path)
    journal = tmp_path / ".hpc" / "registrations" / f"{tc.REG_ID}.decisions.jsonl"
    before = journal.read_bytes()

    for reading, ts in [(25.0, "2026-06-01"), (25.1, "2026-06-02"), (24.8, "2026-06-03")]:
        tc.record(tmp_path, reading=reading, observed_at=f"{ts}T00:00:00Z")
    status = conformance_status(
        experiment_dir=tmp_path, spec=ConformanceStatusSpec(registration_id=tc.REG_ID, last_n=3)
    )
    items = aq.collect_conformance(tmp_path, now="2026-07-08T00:00:00Z")

    assert status.overall == conformance.NONCONFORMING  # the FINDING fired
    assert [i.kind for i in items] == [aq.CONFORMANCE_NONCONFORMING]
    assert journal.read_bytes() == before  # the registration is byte-untouched


# ── (2) route-through the ONE kernels ─────────────────────────────────────────


def test_append_binds_through_the_attestation_kernel() -> None:
    """The observation append routes through ``state/attestation.py::bind`` — the
    payload sha is server-recomputed, never trusted from the wire."""
    from hpc_agent.state import conformance_store

    assert "attestation.bind(" in inspect.getsource(conformance_store.append_observation)


def test_comparator_is_the_one_definition() -> None:
    """Every conformance consumer routes through ``judge_window`` — never a re-inlined
    envelope. The comparator itself uses the shared order-statistics helper."""
    assert "judge_window(" in inspect.getsource(aq.collect_conformance)
    assert "judge_window(" in inspect.getsource(conformance_status)
    # judge_window never re-inlines min/max — it routes through the shared helper.
    src = inspect.getsource(conformance._order_statistics_envelope)
    assert "order_statistics_envelope(" in src


# ── (3) sealed, fixed baseline (no admission path) ────────────────────────────


def test_baseline_not_sealed_in_dossier_refused_at_registration(tmp_path: Path) -> None:
    """A baseline sha that no dossier entry carries is refused at the registration
    append — the limits derive from evidence INSIDE the sealed dossier."""
    tc.build_substrate(tmp_path)
    # Declare a baseline sha that no dossier entry carries (a file swapped after
    # sign-off would produce exactly this): the gate recomputes the live signature
    # and refuses, since the declared sha is sealed nowhere.
    with pytest.raises(Exception, match="NOT sealed in the dossier"):
        tc.register(tmp_path, baseline_sha_override="f" * 64)


def test_recording_receipts_changes_no_baseline_byte(tmp_path: Path) -> None:
    """The sealed baseline is FIXED: recording live receipts writes the ledger, never
    the baseline artifact — no admission path (re-baselining is re-registration)."""
    tc.build_substrate(tmp_path)
    tc.register(tmp_path)
    baseline = tmp_path / f"_aggregated/{tc.RUN_ID}/calibration.json"
    before = baseline.read_bytes()
    for reading, ts in [(25.0, "2026-06-01"), (25.1, "2026-06-02"), (24.8, "2026-06-03")]:
        tc.record(tmp_path, reading=reading, observed_at=f"{ts}T00:00:00Z")
    assert baseline.read_bytes() == before  # the sealed envelope never moves


# ── (4) no control RULES in core ──────────────────────────────────────────────


def test_eight_consecutive_inside_points_stay_conforming() -> None:
    """No Western-Electric-style run rule: 8 consecutive near-limit points INSIDE the
    envelope stay ``conforming`` — sequential policy is caller territory."""
    baseline = conformance.parse_baseline_rows(tc.BASELINE_ROWS)
    declaration = conformance.validate_declaration(
        {"baseline": {"path": "b", "sha256": "s"}, "keys": [tc.KEY], "min_window_n": 3}
    )
    # 8 readings, all comfortably inside [20.0, 21.0], the same run direction.
    window = [
        {"payload": {tc.KEY: 20.4 + i * 0.01}, "observed_at": f"2026-05-0{i + 1}T00:00:00Z"}
        for i in range(8)
    ]
    report = conformance.judge_window(baseline, window, declaration, now="2026-07-08T00:00:00Z")
    assert report.tier == conformance.CONFORMING
    assert all(kv.tier_reason == conformance.WITHIN_ENVELOPE for kv in report.keys)


# ── (5) derived verdicts — watermark-neutral + store-free ─────────────────────


def test_conformance_status_query_is_read_only() -> None:
    """``conformance-status`` is verb=query, no side effects (the derived-verdict seat)."""
    spec = _registry()["conformance-status"]
    assert spec.verb == "query"
    assert list(spec.side_effects) == []


def test_status_query_creates_and_mutates_nothing(tmp_path: Path) -> None:
    """The write-probe: a status read leaves the experiment tree byte-identical."""
    tc.build_substrate(tmp_path)
    tc.register(tmp_path)
    for reading, ts in [(20.4, "2026-05-01"), (20.6, "2026-05-02"), (20.3, "2026-05-03")]:
        tc.record(tmp_path, reading=reading, observed_at=f"{ts}T00:00:00Z")

    def _snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(tmp_path)): p.read_bytes()
            for p in sorted(tmp_path.rglob("*"))
            if p.is_file()
        }

    before = _snapshot()
    conformance_status(
        experiment_dir=tmp_path, spec=ConformanceStatusSpec(registration_id=tc.REG_ID, last_n=3)
    )
    assert _snapshot() == before  # nothing created, nothing marked seen


# ── (6) no verdict verb; the record verb is not agent-facing ──────────────────


def test_no_conformance_resolve_halt_or_baseline_verb() -> None:
    """No conformance-resolve / -halt / -baseline verb — the human verdict is
    append-decision, and re-baselining is re-registration (the no-unlock-verb doctrine)."""
    for name in _registry():
        low = name.lower()
        assert name not in ("conformance-resolve", "conformance-halt", "conformance-baseline")
        if "conformance" in low:
            assert not any(w in low for w in ("resolve", "halt", "baseline", "verdict"))


def test_record_verb_is_not_agent_facing() -> None:
    """``conformance-record`` is ``agent_facing=False`` — an agent authoring the outcome
    stream that judges its own registration is the receipt-laundering class."""
    assert _registry()["conformance-record"].agent_facing is False
    assert _registry()["conformance-status"].agent_facing is True  # the read seat IS agent-facing


# ── (7) no market vocabulary anywhere ─────────────────────────────────────────


def test_fixtures_use_no_market_vocabulary() -> None:
    """No market/trading word lands in the conformance FIXTURE — the instrument-QC toy
    (``sensor-7`` calibration) is the ONLY fixture domain. Field-NAME vocabulary in the
    wire schemas is pinned separately by tests/_wire/test_conformance_wire.py (where
    ``baseline`` is a PERMITTED SPC mechanism noun); core-source English/math words
    like "order statistics" are not market vocabulary and are not scanned here."""
    banned = (
        "fill",
        "order",
        "position",
        "pnl",
        "trade",
        "venue",
        "ticker",
        "price",
        "portfolio",
        "harxhar",
        "quant",
    )
    path = Path(inspect.getfile(tc))
    offenders: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        low = line.lower()
        if "never" in low:  # a rule-statement line names the boundary, never crosses it
            continue
        tokens = set(low.split("#")[0].replace("_", " ").replace(".", " ").split())
        for word in banned:
            if word in tokens:
                offenders.append(f"{path.name}:{lineno}: {word}")
    assert not offenders, f"a market word landed in the conformance fixture: {offenders}"


# ── (8) the horizon is a timestamp comparison — no duration arithmetic ────────


def test_horizon_does_no_duration_arithmetic() -> None:
    """Core names no period and computes no cadence — the horizon is a bare timestamp
    comparison (no ``timedelta`` / ``days=`` / seconds arithmetic in the horizon code)."""
    from hpc_agent.state import registration as reg

    src = inspect.getsource(reg._horizon_lapsed) + inspect.getsource(reg._effective_horizon)
    for banned in ("timedelta", "days=", "weeks=", "hours=", "* 86400", "* 3600"):
        assert banned not in src, f"the horizon code computes a duration ({banned})"


# ── the full instrument-QC loop (T10 scenario) ────────────────────────────────


def test_full_instrument_qc_loop(tmp_path: Path) -> None:
    """The whole loop end to end: register-with-declaration → conforming stream →
    status conforming → drifted readings → nonconforming FINDING (status
    byte-unchanged) → human verdict → queue clears → horizon lapses →
    stale(horizon-lapsed) → re-registration embeds the ledger (the T9 noun)."""
    from hpc_agent.ops import export_dossier

    tc.build_substrate(tmp_path)
    tc.register(tmp_path)

    # (a) a conforming stream → status conforming.
    for reading, ts in [(20.4, "2026-05-01"), (20.6, "2026-05-02"), (20.3, "2026-05-03")]:
        tc.record(tmp_path, reading=reading, observed_at=f"{ts}T00:00:00Z")
    assert (
        conformance_status(
            experiment_dir=tmp_path,
            spec=ConformanceStatusSpec(registration_id=tc.REG_ID, last_n=3),
        ).overall
        == conformance.CONFORMING
    )

    # (b) drifted readings → a nonconforming FINDING; the queue surfaces it.
    last_sha = ""
    for reading, ts in [(25.0, "2026-06-01"), (25.1, "2026-06-02"), (24.8, "2026-06-03")]:
        last_sha = tc.record(tmp_path, reading=reading, observed_at=f"{ts}T00:00:00Z")
    assert [i.kind for i in aq.collect_conformance(tmp_path, now="2026-07-08T00:00:00Z")] == [
        aq.CONFORMANCE_NONCONFORMING
    ]

    # (c) a human verdict via append-decision → the queue item clears.
    append_decision(
        experiment_dir=tmp_path,
        spec=AppendDecisionInput(
            scope_kind="registration",
            scope_id=tc.REG_ID,
            block="conformance-verdict",
            response=f"conformance-verdict for {tc.REG_ID} — receipt {last_sha[:8]} real drift",
            resolved={
                "registration_id": tc.REG_ID,
                "cites": [last_sha],
                "note": "the instrument drifted out of calibration; recalibrate before re-register",
            },
        ),
    )
    assert aq.collect_conformance(tmp_path, now="2026-07-08T00:00:00Z") == []  # cleared

    # (d) horizon lapses → stale(horizon-lapsed). (Isolated on the recorded dossier sha:
    # the live dossier ALSO drifted once the ledger accrued — a separate axis; here we
    # pin the TIME-based cause.)
    records = read_decisions(tmp_path, "registration", tc.REG_ID)
    winner = reduce_registration(records, registration_id=tc.REG_ID, live_dossier_sha=None).winner
    assert winner is not None
    lapsed = reduce_registration(
        records,
        registration_id=tc.REG_ID,
        live_dossier_sha=winner["dossier_sha"],
        now="2028-01-01T00:00:00Z",
    )
    assert lapsed.status == "stale"
    assert lapsed.stale_cause == "horizon-lapsed"

    # (e) re-registration embeds the ledger: the dossier now seals the live record.
    sig = export_dossier.compute_dossier_signature(tmp_path, tc.RUN_ID)
    assert any(e["source"] == "live-conformance" for e in sig.entries)
