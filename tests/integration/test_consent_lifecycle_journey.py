"""End-to-end JOURNEY test for the overnight standing-consent lifecycle.

Every piece exercised here is unit-tested on its own
(``tests/ops/decision/test_overnight_consent.py`` for the authorship / caps /
wake / placement gates, ``tests/state/test_block_terminal.py`` for the terminal
store, the block-gate tests for the greenlight fallback). What NO unit test
does is walk ONE consent through its WHOLE life across the public seams the
real drivers compose — grant, consumption, Tier-3 clean-terminal chaining,
per-cluster cap exhaustion, spec-change death, and the morning-after
disclosure — asserting the durable journal/ledger state at every step. That
composition is exactly what the 2026-07 y-minimization bundle changed, so this
file pins it:

1. **GRANT** — the real ``append_decision`` gate refuses a consent nobody
   typed, rendering the coverage (including the ``{cluster: cap}`` per-cluster
   ceilings) plus a paste-ready grant line; pasted verbatim into the utterance
   store, that line GRANTS — the refusal → grant round-trip with the map-form
   placement, cmd_sha binding, and composed defaults (expires_at + wake).
2. **UNCONDITIONAL CONSUMPTION** — ``submit-s3`` (the one unconditional run
   boundary) consumes under the live consent via
   ``consume_boundary_under_consent``: ledgered exactly once (idempotent on
   re-entry), ``detail.cluster`` stamped for the per-cluster meter.
3. **TIER-3 CHAIN** — a DIRTY ``submit-s3`` terminal (``needs_decision`` True)
   refuses ``submit-s4`` end to end through
   ``block_gate.assert_greenlit_or_consented`` (predecessor-not-clean); a
   CLEAN terminal on the same tree flips ``predecessor_terminal_clean`` and
   the same gate consumes; ``boundary_already_ledgered`` then carries the
   evidence for a re-entry with no duplicate ledger line.
4. **CAPS ACROSS THE JOURNEY** — after consumptions carrying ``spent_budget``
   on cluster A, cluster A's ``{cluster: cap}`` ceiling refuses the next
   boundary (over-cluster-budget-cap) while cluster B still consumes — all
   through the same public seams, never by calling the meter internals.
5. **DEATH + DISCLOSURE** — the sidecar ``cmd_sha`` moves (spec change) and
   the next consumption refuses spec-changed; after the consent expires the
   morning brief still discloses every earlier consumption (the disclosure
   deliberately OUTLIVES the grant).

TOY VOCABULARY ONLY (the ``test_overnight_consent`` idiom): widget runs, never
a real domain's words. Hermetic: journal home + clusters.yaml are redirected
under ``tmp_path``; no SSH, no scheduler, no subprocess.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow
from hpc_agent.ops import overnight
from hpc_agent.ops.block_gate import assert_greenlit_or_consented
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state import decision_journal as sdj
from hpc_agent.state.block_terminal import record_terminal
from hpc_agent.state.runs import read_run_cluster, read_run_cmd_sha, write_run_sidecar
from hpc_agent.state.utterances import append_utterance, utterances_path

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

_RUN_ID = "widget-run-1"
_CMD_SHA = "a3f2c9d1beef00112233"  # the sidecar tree fingerprint the consent binds
_NEW_SHA = "deadbeefcafe99887766"  # the post-edit fingerprint that kills the consent
_CLUSTER_A = "carc"  # the cluster whose per-cluster cap the journey exhausts
_CLUSTER_B = "hoffman2"  # the cluster that must keep consuming after A refuses
_CAP_A = 10.0  # cluster A's budget ceiling — blown mid-journey (6.0 + 7.0 > 10)
# The {cluster: cap} placement map the consent records (run-queue plan §3): the
# KEY SET is the S1 membership set, the values are the per-cluster ceilings.
_PLACEMENT = {_CLUSTER_A: {"budget_cap": _CAP_A}, _CLUSTER_B: {"budget_cap": _CAP_A}}


def _iso(dt: Any) -> str:
    return str(dt.isoformat(timespec="seconds"))


@pytest.fixture
def experiment_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo dir with journal home + clusters.yaml pinned under tmp_path.

    The clusters config must declare the journey's vocabulary (carc/hoffman2)
    because the record-time placement gate validates the map form's keys
    against the ACTIVE config — the same ``_pin_clusters`` idiom as the
    consent unit tests.
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    clusters = tmp_path / "journey-clusters.yaml"
    clusters.write_text(
        "".join(f"{k}:\n  host: {k}.example.edu\n" for k in (_CLUSTER_A, _CLUSTER_B)),
        encoding="utf-8",
    )
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))
    exp = tmp_path / "exp"
    exp.mkdir()
    return exp


def _arm_wake(run_id: str) -> None:
    """A live detached status-watch lease for *run_id* (the armed wake)."""
    lease = overnight._watch_lease_path(run_id)
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")


def _write_sidecar(experiment_dir: Path, *, cmd_sha: str, cluster: str) -> None:
    """The run sidecar carrying the CURRENT identity + placement.

    ``read_run_cmd_sha`` / ``read_run_cluster`` — the exact readers the real
    gate call sites (``submit_blocks``, ``aggregate_blocks``) derive their
    ``current_*`` arguments from — read these back. Rewriting the sidecar with
    a new ``cmd_sha`` IS the spec change of step 5.
    """
    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.2.0",
        submitted_at="2026-07-29T00:00:00+00:00",
        executor="python3 src/run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="1" * 64,
        cluster=cluster,
    )


def _append_consent(experiment_dir: Path, response: str) -> Any:
    """Record the standing consent through the REAL append_decision gate.

    ``expires_at`` and ``wake`` are deliberately OMITTED so the poka-yoke
    composition seat fills (and discloses) them; ``cmd_sha`` binds the sidecar
    identity and ``placement`` is the {cluster: cap} map form.
    """
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "run",
            "scope_id": _RUN_ID,
            "block": overnight.OVERNIGHT_CONSENT_BLOCK,
            "response": response,
            "resolved": {
                "budget_cap": 50.0,  # the global ceiling — never blown in this journey
                "cmd_sha": _CMD_SHA,
                "placement": dict(_PLACEMENT),
            },
        }
    )
    return append_decision(experiment_dir=experiment_dir, spec=spec)


def _ledger(experiment_dir: Path) -> list[dict[str, Any]]:
    """The run's consumption-ledger lines — THE durable state every step asserts."""
    return overnight.read_consumption_ledger(experiment_dir, "run", _RUN_ID)


def _consume(experiment_dir: Path, **overrides: Any) -> overnight.ConsumptionOutcome:
    """One consumption consultation through the public substrate seam."""
    kwargs: dict[str, Any] = {
        "scope_kind": "run",
        "scope_id": _RUN_ID,
        "boundary_block": "submit-s3",
        "current_cmd_sha": read_run_cmd_sha(experiment_dir, _RUN_ID),
        "current_placement": read_run_cluster(experiment_dir, _RUN_ID),
    }
    kwargs.update(overrides)
    return overnight.consume_boundary_under_consent(experiment_dir, **kwargs)


def _s4_gate(experiment_dir: Path, *, current_placement: str | None = None) -> Any:
    """The submit-s4 consent-aware gate, called exactly as ``submit_blocks`` calls it.

    The clean-predecessor evidence is CODE-DERIVED through the two public
    sources the real call site composes: the recorded submit-s3 terminal, or a
    prior ledgered consumption of this same boundary + identity.
    """
    cmd_sha = read_run_cmd_sha(experiment_dir, _RUN_ID)
    return assert_greenlit_or_consented(
        experiment_dir,
        run_id=_RUN_ID,
        verb="submit-s4",
        predecessor="S3",
        current_cmd_sha=cmd_sha,
        current_placement=current_placement or read_run_cluster(experiment_dir, _RUN_ID),
        clean_predecessor=(
            overnight.predecessor_terminal_clean(
                experiment_dir, _RUN_ID, "submit-s3", current_cmd_sha=cmd_sha
            )
            or overnight.boundary_already_ledgered(
                experiment_dir, "run", _RUN_ID, "submit-s4", cmd_sha
            )
        ),
    )


def _aggregate_run_gate(experiment_dir: Path, *, current_placement: str) -> Any:
    """The aggregate-run consent-aware gate, mirroring ``aggregate_blocks``."""
    cmd_sha = read_run_cmd_sha(experiment_dir, _RUN_ID)
    return assert_greenlit_or_consented(
        experiment_dir,
        run_id=_RUN_ID,
        verb="aggregate-run",
        predecessor="aggregate-check",
        current_cmd_sha=cmd_sha,
        current_placement=current_placement,
        clean_predecessor=(
            overnight.predecessor_terminal_clean(
                experiment_dir, _RUN_ID, "aggregate-check", current_cmd_sha=cmd_sha
            )
            or overnight.boundary_already_ledgered(
                experiment_dir, "run", _RUN_ID, "aggregate-run", cmd_sha
            )
        ),
    )


def test_consent_lifecycle_journey(experiment_dir: Path) -> None:
    """Grant → consume → Tier-3 chain → cap exhaustion → death → disclosure."""
    # ── step 0: the boundary exists — armed wake + a sidecar carrying the
    # identity/placement the consent binds and consumption re-derives.
    _arm_wake(_RUN_ID)
    _write_sidecar(experiment_dir, cmd_sha=_CMD_SHA, cluster=_CLUSTER_A)
    utterances_path(experiment_dir).parent.mkdir(parents=True, exist_ok=True)

    # ═══ 1. GRANT: refusal renders the coverage; the pasted line grants ═══
    # Nothing typed yet → the authorship gate REFUSES, rendering the coverage
    # inline (boundary, spec identity, the per-cluster ceilings of the map
    # form) plus the paste-ready grant line.
    with pytest.raises(errors.SpecInvalid, match="copy-paste") as exc:
        _append_consent(experiment_dir, "let the widget run advance overnight")
    refusal = str(exc.value)
    assert _RUN_ID in refusal
    assert _CMD_SHA in refusal  # the spec identity the sha-prefix leg derives from
    # The human must READ the {cluster: cap} ceilings they are consenting to.
    assert f"{_CLUSTER_A} caps: budget_cap={_CAP_A}" in refusal
    assert f"{_CLUSTER_B} caps: budget_cap={_CAP_A}" in refusal
    # Journal state: the refusal recorded NOTHING.
    assert sdj.read_decisions(experiment_dir, "run", _RUN_ID) == []

    # The paste-ready line carries every token the chat tier reads: boundary,
    # 8+ hex sha prefix, and every cluster key of the map form.
    paste_line = refusal.splitlines()[-1].strip()
    assert _RUN_ID in paste_line
    assert _CMD_SHA[:12] in paste_line
    assert _CLUSTER_A in paste_line and _CLUSTER_B in paste_line

    # Pasted verbatim into chat (the utterance store), the line GRANTS.
    append_utterance(experiment_dir, paste_line)
    result = _append_consent(experiment_dir, paste_line)
    assert result.count == 1

    # Journal state: ONE consent record binding the map form + the identity,
    # with the omitted fields COMPOSED and disclosed (poka-yoke, never a NO-GO).
    records = sdj.read_decisions(experiment_dir, "run", _RUN_ID)
    assert len(records) == 1
    consent = records[-1]
    assert consent["block"] == overnight.OVERNIGHT_CONSENT_BLOCK
    assert consent["resolved"]["cmd_sha"] == _CMD_SHA
    assert consent["resolved"]["placement"] == _PLACEMENT
    assert set(consent["resolved"]["composed_defaults"]) == {"expires_at", "wake"}
    assert consent["resolved"]["wake"]["kind"] == "status-watch"
    expires_at = consent["resolved"]["expires_at"]
    expires_dt = parse_iso_utc_or_none(expires_at)
    assert expires_dt is not None and expires_dt > utcnow()
    # The consent is LIVE against the boundary's current identity + placement.
    status = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=read_run_cmd_sha(experiment_dir, _RUN_ID),
        current_placement=read_run_cluster(experiment_dir, _RUN_ID),
    )
    assert (status.live, status.reason) == (True, "live")
    assert _ledger(experiment_dir) == []  # nothing consumed yet

    # ═══ 2. UNCONDITIONAL CONSUMPTION: submit-s3, ledgered once ═══
    # The main-array launch consumes under the live consent, carrying its real
    # cluster spend (6.0 on cluster A — under A's 10.0 ceiling).
    outcome = _consume(experiment_dir, detail={"spent_budget": 6.0})
    assert outcome.consumed is True
    assert outcome.line is not None
    assert outcome.line["detail"]["cluster"] == _CLUSTER_A  # stamped for the meter
    lines = _ledger(experiment_dir)
    assert len(lines) == 1
    assert lines[0]["consumed_block"] == "submit-s3"
    assert lines[0]["detail"]["cmd_sha"] == _CMD_SHA

    # Re-entry (a re-tick / gate replay of the same boundary + identity): still
    # authorized, but IDEMPOTENT — no second audit line to double-count.
    replay = _consume(experiment_dir, detail={"spent_budget": 6.0})
    assert replay.consumed is True
    assert replay.line is None
    assert len(_ledger(experiment_dir)) == 1

    # ═══ 3. TIER-3 CHAIN: clean terminal → submit-s4; dirty terminal parks ═══
    # 3a. A DIRTY submit-s3 terminal (the run parked a decision overnight):
    # the clean-predecessor evidence reads False and the consent-aware gate
    # refuses submit-s4 END TO END, naming the failing leg.
    record_terminal(
        experiment_dir,
        run_id=_RUN_ID,
        block="submit-s3",
        cmd_sha=_CMD_SHA,
        result_dump={"needs_decision": True, "summary": "widget main array anomaly"},
    )
    assert (
        overnight.predecessor_terminal_clean(
            experiment_dir, _RUN_ID, "submit-s3", current_cmd_sha=_CMD_SHA
        )
        is False
    )
    with pytest.raises(errors.SpecInvalid, match="predecessor-not-clean"):
        _s4_gate(experiment_dir)
    # Ledger state: the refusal ledgered nothing.
    assert [ln["consumed_block"] for ln in _ledger(experiment_dir)] == ["submit-s3"]

    # 3b. A CLEAN terminal on the SAME tree (needs_decision False, same
    # cmd_sha) flips the evidence, and the SAME gate now consumes submit-s4.
    record_terminal(
        experiment_dir,
        run_id=_RUN_ID,
        block="submit-s3",
        cmd_sha=_CMD_SHA,
        result_dump={"needs_decision": False, "summary": "widget main array clean"},
    )
    assert (
        overnight.predecessor_terminal_clean(
            experiment_dir, _RUN_ID, "submit-s3", current_cmd_sha=_CMD_SHA
        )
        is True
    )
    s4_outcome = _s4_gate(experiment_dir)
    assert s4_outcome is not None and s4_outcome.consumed is True
    assert s4_outcome.line is not None
    assert s4_outcome.line["detail"]["cluster"] == _CLUSTER_A
    assert [ln["consumed_block"] for ln in _ledger(experiment_dir)] == [
        "submit-s3",
        "submit-s4",
    ]

    # 3c. The ledgered line now carries the evidence ACROSS a process boundary:
    # even with the terminal evidence out of the picture, boundary_already_
    # ledgered re-derives clean for a re-entry — consumed, no duplicate line.
    assert (
        overnight.boundary_already_ledgered(experiment_dir, "run", _RUN_ID, "submit-s4", _CMD_SHA)
        is True
    )
    s4_replay = assert_greenlit_or_consented(
        experiment_dir,
        run_id=_RUN_ID,
        verb="submit-s4",
        predecessor="S3",
        current_cmd_sha=_CMD_SHA,
        current_placement=_CLUSTER_A,
        clean_predecessor=overnight.boundary_already_ledgered(
            experiment_dir, "run", _RUN_ID, "submit-s4", _CMD_SHA
        ),
    )
    assert s4_replay is not None and s4_replay.consumed is True
    assert s4_replay.line is None
    assert len(_ledger(experiment_dir)) == 2

    # ═══ 4. CAPS ACROSS THE JOURNEY: cluster A exhausts, cluster B consumes ═══
    # 4a. A SECOND, DISTINCT anomaly the same night (the F11 idempotency key)
    # earns its own ledger line, carrying more cluster-A spend: 6.0 + 7.0 blows
    # A's 10.0 ceiling — but only for the NEXT consultation (caps meter what
    # was already ledgered, never the line being written).
    anomaly = _consume(
        experiment_dir,
        event_kind="anomaly",
        detail={"anomaly": "widget-overload", "spent_budget": 7.0},
    )
    assert anomaly.consumed is True and anomaly.line is not None
    assert len(_ledger(experiment_dir)) == 3
    # The public meter splits the totals by the stamped cluster.
    assert overnight.consumed_spend(experiment_dir, "run", _RUN_ID) == (13.0, 0.0)
    assert overnight.consumed_spend(experiment_dir, "run", _RUN_ID, cluster=_CLUSTER_A) == (
        13.0,
        0.0,
    )
    assert overnight.consumed_spend(experiment_dir, "run", _RUN_ID, cluster=_CLUSTER_B) == (
        0.0,
        0.0,
    )

    # 4b. Cluster A now REFUSES: the aggregate-run gate (clean aggregate-check
    # terminal in hand) consults the consent on A and the per-cluster leg fires
    # — end to end through the same public gate that passed in step 3.
    record_terminal(
        experiment_dir,
        run_id=_RUN_ID,
        block="aggregate-check",
        cmd_sha=_CMD_SHA,
        result_dump={"needs_decision": False, "summary": "widget check ready"},
    )
    with pytest.raises(errors.SpecInvalid, match="over-cluster-budget-cap"):
        _aggregate_run_gate(experiment_dir, current_placement=_CLUSTER_A)
    assert len(_ledger(experiment_dir)) == 3  # the refusal ledgered nothing
    # The substrate names the same leg through the status seam.
    over_cap = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        current_placement=_CLUSTER_A,
    )
    assert (over_cap.live, over_cap.reason) == (False, "over-cluster-budget-cap")

    # 4c. Cluster B is UNTOUCHED by A's spend (the meter splits by stamp): the
    # run re-places onto B (inside the consent's bound set) and the SAME gate
    # consumes there, stamping B on the fresh line.
    _write_sidecar(experiment_dir, cmd_sha=_CMD_SHA, cluster=_CLUSTER_B)
    b_outcome = _aggregate_run_gate(experiment_dir, current_placement=_CLUSTER_B)
    assert b_outcome is not None and b_outcome.consumed is True
    assert b_outcome.line is not None
    assert b_outcome.line["detail"]["cluster"] == _CLUSTER_B
    assert [ln["consumed_block"] for ln in _ledger(experiment_dir)] == [
        "submit-s3",
        "submit-s4",
        "submit-s3",
        "aggregate-run",
    ]

    # ═══ 5. DEATH + DISCLOSURE: spec change kills; the brief outlives ═══
    # 5a. The tree moves (a code edit re-fingerprints the sidecar): the very
    # next consumption refuses spec-changed — consent dies with the spec.
    _write_sidecar(experiment_dir, cmd_sha=_NEW_SHA, cluster=_CLUSTER_B)
    assert read_run_cmd_sha(experiment_dir, _RUN_ID) == _NEW_SHA
    dead = _consume(experiment_dir, detail={"spent_budget": 1.0})
    assert dead.consumed is False
    assert dead.decision.reason == "spec-changed"
    assert len(_ledger(experiment_dir)) == 4  # the dead consent ledgered nothing

    # 5b. The consent EXPIRES (the morning boundary passes) …
    late = _iso(expires_dt + timedelta(hours=1))
    expired = overnight.standing_consent_status(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        current_cmd_sha=_CMD_SHA,
        now_iso=late,
    )
    assert (expired.live, expired.reason) == (False, "expired")

    # 5c. … and the morning brief STILL discloses every earlier consumption:
    # the disclosure deliberately outlives the grant.
    brief = overnight.morning_brief_if_any(
        experiment_dir, scope_kind="run", scope_id=_RUN_ID, now_iso=late
    )
    assert brief is not None
    assert brief["has_consent"] is True
    assert brief["consumed_count"] == 4
    assert sorted(item["consumed_block"] for item in brief["consumed"]) == [
        "aggregate-run",
        "submit-s3",
        "submit-s3",
        "submit-s4",
    ]
    # failed_at vs surfaced_at: every item's overnight latency is visible, and
    # the missing push channel (this hermetic env has no alert hook) is flagged
    # as the baked-in disclosure gap.
    for item in brief["consumed"]:
        assert item["failed_at"] is not None
        assert item["surfaced_at"] == brief["surfaced_at"]
        assert item["latency_seconds"] is not None and item["latency_seconds"] > 0
        assert item["push_available"] is False
        assert item["disclosure_gap"]
    # The distinct-anomaly line (F11) is disclosed as itself, not folded away.
    anomalies = {item["detail"].get("anomaly") for item in brief["consumed"]}
    assert "widget-overload" in anomalies
    # The brief surfaces the consent AFTER its expiry — expires_at < surfaced_at.
    brief_expires = parse_iso_utc_or_none(brief["consent"]["expires_at"])
    surfaced = parse_iso_utc_or_none(brief["surfaced_at"])
    assert brief_expires is not None and surfaced is not None
    assert brief_expires < surfaced
    # And the same reader stays quiet for a scope that never went overnight —
    # the byte-unchanged case guarding against a brief invented from nothing.
    assert (
        overnight.morning_brief_if_any(
            experiment_dir, scope_kind="run", scope_id="widget-run-2", now_iso=late
        )
        is None
    )
