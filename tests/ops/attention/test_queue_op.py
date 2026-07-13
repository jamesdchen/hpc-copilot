"""The ``attention-queue`` read-only query primitive (Wave C / T4).

Single-experiment and fleet paths, the ``now`` override + its SpecInvalid guard,
the result surface (computed_at / items / counts / skipped / render), and the
non-creating discipline (a fleet scan scaffolds no namespace under a fresh home).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.attention_queue import AttentionQueueSpec
from hpc_agent.ops.attention_op import attention_queue
from hpc_agent.state.journal import stamp_tick, upsert_run
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


def _stalled(exp: Path, run_id: str) -> None:
    _mk(exp, run_id)
    stamp_tick(
        run_id,
        last_tick_at="2026-07-06T05:00:00+00:00",
        next_tick_due="2026-07-06T06:00:00+00:00",
        experiment_dir=exp,
    )


def test_single_experiment_result_surface(tmp_path: Path) -> None:
    _stalled(tmp_path, "run-stalled")
    _mk(tmp_path, "run-failed", status="failed", last_tick_at="2026-07-06T06:00:00+00:00")

    result = attention_queue(experiment_dir=tmp_path, spec=AttentionQueueSpec(now=_NOW))

    assert result.computed_at == _NOW
    assert result.render.splitlines()[0] == (
        f"attention queue · computed {_NOW} · re-run for current state"
    )
    # blocked (stalled) before verdict (anomaly).
    assert [i.item_class for i in result.items] == ["blocked", "verdict"]
    assert result.counts == {"blocked": 1, "verdict": 1}
    assert result.skipped == []


def test_bad_now_override_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        attention_queue(experiment_dir=tmp_path, spec=AttentionQueueSpec(now="not-a-date"))


def test_fleet_scan_aggregates_and_is_non_creating(tmp_path: Path) -> None:
    exp_a = tmp_path / "exp_a"
    exp_b = tmp_path / "exp_b"
    exp_a.mkdir()
    exp_b.mkdir()
    _stalled(exp_a, "a-stalled")
    _mk(exp_b, "b-failed", status="failed")

    home = _current_homedir()
    namespaces_before = {p.name for p in home.iterdir()}

    result = attention_queue(experiment_dir=exp_a, spec=AttentionQueueSpec(fleet=True, now=_NOW))
    # Items from BOTH journaled experiments are present.
    kinds = {i.kind for i in result.items}
    assert "run-stalled" in kinds and "run-anomaly" in kinds
    # Non-creating: the fleet scan scaffolded no new namespace.
    assert {p.name for p in home.iterdir()} == namespaces_before


def test_fleet_scan_on_empty_home_returns_empty(tmp_path: Path) -> None:
    result = attention_queue(experiment_dir=tmp_path, spec=AttentionQueueSpec(fleet=True, now=_NOW))
    assert result.items == []
    assert result.skipped == []
    assert "(nothing needs your attention)" in result.render


def test_skipped_namespace_surfaces_torn_repo_json(tmp_path: Path) -> None:
    home = _current_homedir()
    home.mkdir(parents=True, exist_ok=True)
    torn = home / "torn_ns"
    torn.mkdir()
    (torn / "repo.json").write_text("{not json", encoding="utf-8")

    result = attention_queue(experiment_dir=tmp_path, spec=AttentionQueueSpec(fleet=True, now=_NOW))
    assert [s.ref for s in result.skipped] == ["torn_ns"]


def test_now_omitted_stamps_current_time(tmp_path: Path) -> None:
    result = attention_queue(experiment_dir=tmp_path, spec=AttentionQueueSpec())
    # A real ISO stamp was minted (not the empty string) even with no override.
    assert result.computed_at
    assert json.dumps(result.computed_at)  # serializable str
