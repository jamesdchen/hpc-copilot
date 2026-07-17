"""Tests for the ``settle-aggregate`` workflow verb (clean-reproduction #2).

Exercises the operator-bypass table settle over a tmp experiment: the typed
human utterance is journaled with the artifact sha computed at record time; an
agent-composed utterance is REFUSED (never synthesized); an absent artifact and a
missing named run are refused; and the journaled contributing ids are authorized
by ``verify-relay`` via its normal auth-id join.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.verify_relay import VerifyRelayInput
from hpc_agent._wire.workflows.settle_aggregate import SettleAggregateInput
from hpc_agent.ops.decision.journal.verify_relay import verify_relay
from hpc_agent.ops.settle_aggregate import settle_aggregate
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar
from hpc_agent.state.utterances import append_utterance

if TYPE_CHECKING:
    from pathlib import Path

_TS = "2026-07-17T12:00:00+00:00"
_R1 = "exp-c1-11111111"
_R2 = "exp-c2-22222222"
_R0 = "exp-table-00000000"


def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="host",
        remote_path="/remote",
        job_name="job",
        job_ids=["1"],
        total_tasks=1,
        submitted_at=_TS,
        experiment_dir="/exp",
    )


def _seed_run(experiment_dir: Path, run_id: str) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha=f"cmd-{run_id}",
        hpc_agent_version="0.0.0",
        submitted_at=_TS,
        executor="exec.py",
        result_dir_template="results/{i}",
        task_count=1,
        tasks_py_sha="t",
        cluster="hoffman2",
    )
    upsert_run(experiment_dir, _record(run_id))


def _make_table(experiment_dir: Path) -> Path:
    table = experiment_dir / "_aggregated" / _R0 / "metrics_table.csv"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text("estimator,qlike\nlinear,0.42\n", encoding="utf-8")
    return table


def _seed_scene(
    experiment_dir: Path, *, log_human: str = "settle the metrics table for publication"
) -> Path:
    """Seed the contributing runs, the scope run, and the human utterance log."""
    _seed_run(experiment_dir, _R0)
    _seed_run(experiment_dir, _R1)
    _seed_run(experiment_dir, _R2)
    # The utterance log exists only after the journal namespace does (seeded above).
    append_utterance(experiment_dir, log_human)
    return _make_table(experiment_dir)


def test_typed_utterance_is_journaled_with_artifact_sha(tmp_path: Path) -> None:
    import hashlib

    table = _seed_scene(tmp_path)
    expected_sha = hashlib.sha256(table.read_bytes()).hexdigest()

    result = settle_aggregate(
        tmp_path,
        spec=SettleAggregateInput(
            run_id=_R0,
            aggregate_ref=str(table),
            derives_from=[_R1, _R2],
            utterance="settle the metrics table for publication",
        ),
    )
    assert result.stage_reached == "settled"
    assert result.artifact_sha256 == expected_sha
    assert result.contributing_run_ids == [_R1, _R2]
    assert result.authorship == "harness-captured"

    # The record is journaled under the run scope, verbatim, provenance not blessed.
    recs = read_decisions(tmp_path, "run", _R0)
    settle = [r for r in recs if r.get("block") == "settle-aggregate"]
    assert len(settle) == 1
    prov = settle[0]["provenance"]
    assert prov["artifact_sha256"] == expected_sha
    assert prov["contributing_run_ids"] == [_R1, _R2]
    assert prov["source"] == "operator-settled, provenance human-asserted"
    assert settle[0]["proposal"] == "settle the metrics table for publication"


def test_agent_composed_utterance_is_refused(tmp_path: Path) -> None:
    table = _seed_scene(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="agent-composed"):
        settle_aggregate(
            tmp_path,
            spec=SettleAggregateInput(
                run_id=_R0,
                aggregate_ref=str(table),
                derives_from=[_R1, _R2],
                utterance="xyzzy foobar quux plugh",  # shares no word with the human log
            ),
        )


def test_absent_artifact_is_refused(tmp_path: Path) -> None:
    _seed_scene(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="does not exist"):
        settle_aggregate(
            tmp_path,
            spec=SettleAggregateInput(
                run_id=_R0,
                aggregate_ref=str(tmp_path / "nope" / "ghost_table.csv"),
                derives_from=[_R1, _R2],
                utterance="settle the metrics table for publication",
            ),
        )


def test_missing_named_run_is_refused(tmp_path: Path) -> None:
    table = _seed_scene(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="no record or"):
        settle_aggregate(
            tmp_path,
            spec=SettleAggregateInput(
                run_id=_R0,
                aggregate_ref=str(table),
                derives_from=[_R1, "exp-ghost-99999999"],
                utterance="settle the metrics table for publication",
            ),
        )


def test_no_utterance_log_falls_back_to_unverified_but_still_journals(tmp_path: Path) -> None:
    # No append_utterance — the harness capture hook is not installed. The verb
    # journals the settle at the friction tier rather than silently synthesizing.
    _seed_run(tmp_path, _R0)
    _seed_run(tmp_path, _R1)
    table = _make_table(tmp_path)
    result = settle_aggregate(
        tmp_path,
        spec=SettleAggregateInput(
            run_id=_R0,
            aggregate_ref=str(table),
            derives_from=[_R1],
            utterance="any words at all — no log to check against",
        ),
    )
    assert result.authorship == "unverified-fallback"
    assert result.stage_reached == "settled"


def test_verify_relay_authorizes_the_settled_contributing_ids(tmp_path: Path) -> None:
    table = _seed_scene(tmp_path)
    # Before the settle: a relay naming the contributing runs flags them.
    relay = f"The operator table derives from runs {_R1} and {_R2}."
    before = verify_relay(
        experiment_dir=tmp_path, spec=VerifyRelayInput(run_id=_R0, relay_text=relay)
    )
    flagged_before = {m.claim for m in before.mismatches if m.kind == "run_id"}
    assert {_R1, _R2} & flagged_before, "sanity: unsettled ids should flag as unknown run-ids"

    # Settle names them → verify-relay's normal auth-id join authorizes them.
    settle_aggregate(
        tmp_path,
        spec=SettleAggregateInput(
            run_id=_R0,
            aggregate_ref=str(table),
            derives_from=[_R1, _R2],
            utterance="settle the metrics table for publication",
        ),
    )
    after = verify_relay(
        experiment_dir=tmp_path, spec=VerifyRelayInput(run_id=_R0, relay_text=relay)
    )
    flagged_after = {m.claim for m in after.mismatches if m.kind == "run_id"}
    assert not ({_R1, _R2} & flagged_after), "settled ids must be authorized via the auth-id join"
