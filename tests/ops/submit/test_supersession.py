"""Supersession conduct — the proving-run-#4 scope-hop escape hatch is closed.

Findings e/g/h (2026-07-05): the agent minted a NEW run_id
(``pi-estimation-h2-e2cddfb7``) for the SAME code identity as a live prior
attempt (``pi-estimation-e2cddfb7``) without closing it — fresh run_id = fresh
lease, and every rule-9/lease gate forgot. The gate under test
(:mod:`hpc_agent.ops.supersession`) makes that act either a structured
refusal or an explicit, closure-triggering ``supersedes`` declaration.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from hpc_agent import errors
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops import supersession
from hpc_agent.ops.supersession import (
    apply_supersession_gate,
    find_live_siblings,
    stamp_supersedes_on_new,
)
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

SHA = "a" * 64
OTHER_SHA = "b" * 64


@pytest.fixture(autouse=True)
def _journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _record(
    tmp_path: Path,
    run_id: str,
    *,
    sha: str | None = SHA,
    status: str = "in_flight",
    job_ids: list[str] | None = None,
    cluster: str = "hoffman2",
) -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        profile="p",
        cluster=cluster,
        ssh_target="u@h",
        remote_path="/scratch/x",
        job_name="j",
        job_ids=job_ids if job_ids is not None else ["101"],
        total_tasks=4,
        submitted_at="2026-07-05T00:00:00+00:00",
        experiment_dir=str(tmp_path),
        status=status,
        job_env={"HPC_CMD_SHA": sha} if sha else {},
    )
    upsert_run(tmp_path, record)
    return record


def _spec(run_id: str, *, sha: str | None = SHA, supersedes: str | None = None) -> SubmitFlowSpec:
    job_env = {"EXECUTOR": "run_task.sh"}
    if sha:
        job_env["HPC_CMD_SHA"] = sha
    return SubmitFlowSpec(
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/scratch/x",
        job_name="j",
        run_id=run_id,
        total_tasks=4,
        backend="sge",
        script=".hpc/templates/cpu_array.sh",
        job_env=job_env,
        canary=False,
        supersedes=supersedes,
    )


# ── refusal: the run-#4 shape ────────────────────────────────────────────────


def test_refusal_fires_on_live_sibling_same_cmd_sha(tmp_path: Path) -> None:
    """New run_id + live in_flight sibling with the same cmd_sha → refuse,
    naming the sibling, its state, cluster, and the two sanctioned exits."""
    _record(tmp_path, "pi-estimation-e2cddfb7")

    with pytest.raises(errors.SiblingRunLive) as exc_info:
        apply_supersession_gate(tmp_path, _spec("pi-estimation-h2-e2cddfb7"))

    msg = str(exc_info.value)
    assert "pi-estimation-e2cddfb7" in msg
    assert "in_flight since 2026-07-05T00:00:00+00:00" in msg
    assert "hoffman2" in msg
    # The two sanctioned exits.
    assert "kill --run-id pi-estimation-e2cddfb7" in msg
    assert '"supersedes": "pi-estimation-e2cddfb7"' in msg


def test_refusal_names_live_detached_lease_pid(tmp_path: Path) -> None:
    """A live (run_id, block) worker lease on the sibling is named in the refusal."""
    from hpc_agent.state.run_record import _current_homedir

    _record(tmp_path, "old-run")
    detached = _current_homedir() / "_detached"
    detached.mkdir(parents=True, exist_ok=True)
    # Our own pid is definitionally alive.
    (detached / "submit-s2-old-run.lease.json").write_text(
        f'{{"run_id": "old-run", "block": "submit-s2", "pid": {os.getpid()}}}',
        encoding="utf-8",
    )

    with pytest.raises(errors.SiblingRunLive) as exc_info:
        apply_supersession_gate(tmp_path, _spec("new-run"))
    assert f"pid {os.getpid()}" in str(exc_info.value)
    assert "submit-s2" in str(exc_info.value)


def test_refusal_fires_on_live_canary_of_prior_run_via_sidecar(tmp_path: Path) -> None:
    """Finding e: an orphaned live ``-canary`` of a PRIOR run trips too — its
    identity comes from the mirrored sidecar (canary records carry no job_env)."""
    from hpc_agent.state.runs import write_run_sidecar

    _record(tmp_path, "old-run-canary", sha=None)  # empty job_env, like real canaries
    write_run_sidecar(
        tmp_path,
        run_id="old-run-canary",
        cmd_sha=SHA,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-05T00:00:00+00:00",
        executor="run_task.sh",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="t" * 64,
    )

    with pytest.raises(errors.SiblingRunLive):
        apply_supersession_gate(tmp_path, _spec("new-run"))


# ── supersedes: the sanctioned exit ─────────────────────────────────────────


def _fake_kill_factory(calls: list[dict[str, Any]], *, still_alive: list[str] | None = None):
    def _fake(experiment_dir: Path, *, run_id: str, scheduler: str) -> dict[str, Any]:
        calls.append({"run_id": run_id, "scheduler": scheduler})
        return {
            "run_id": run_id,
            "still_alive_job_ids": list(still_alive or []),
            "confirmed_gone_job_ids": [],
        }

    return _fake


def test_supersedes_journals_link_closes_old_and_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "old-run")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(supersession, "_invoke_kill", _fake_kill_factory(calls))

    summary = apply_supersession_gate(tmp_path, _spec("new-run", supersedes="old-run"))

    # Closure was requested through the kill machinery, then the record was
    # settled abandoned via the centralized transition with the reason recorded.
    assert calls == [{"run_id": "old-run", "scheduler": "sge"}]
    assert summary is not None
    assert summary["superseded_run_id"] == "old-run"
    assert "old-run" in summary["closed"]
    assert summary["pending_closure"] == []

    old = load_run(tmp_path, "old-run")
    assert old is not None
    assert old.status == "abandoned"
    assert old.superseded_by == "new-run"
    assert old.superseded_at
    assert old.last_status["verdict_reason"] == "superseded_by=new-run"
    assert old.pending_closure == {}

    # Forward link after the new record lands: queryable in both directions.
    _record(tmp_path, "new-run")
    stamp_supersedes_on_new(tmp_path, new_run_id="new-run", old_run_id="old-run")
    new = load_run(tmp_path, "new-run")
    assert new is not None
    assert new.supersedes == "old-run"


def test_supersedes_closes_the_canary_pairing_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding g: the old run's worker AND canary were orphaned — closure must
    target the #258 ``-canary`` pairing alongside the named run."""
    _record(tmp_path, "old-run")
    _record(tmp_path, "old-run-canary", sha=None, job_ids=["102"])
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(supersession, "_invoke_kill", _fake_kill_factory(calls))

    apply_supersession_gate(tmp_path, _spec("new-run", supersedes="old-run"))

    assert [c["run_id"] for c in calls] == ["old-run", "old-run-canary"]
    for rid in ("old-run", "old-run-canary"):
        rec = load_run(tmp_path, rid)
        assert rec is not None
        assert rec.status == "abandoned"
        assert rec.superseded_by == "new-run"


def test_unreachable_closure_records_pending_marker_and_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "old-run", job_ids=["7001", "7002"])

    def _unreachable(experiment_dir: Path, *, run_id: str, scheduler: str) -> dict[str, Any]:
        raise errors.SshCircuitOpen("circuit open to hoffman2 until 12:00")

    monkeypatch.setattr(supersession, "_invoke_kill", _unreachable)

    summary = apply_supersession_gate(tmp_path, _spec("new-run", supersedes="old-run"))

    assert summary is not None
    assert len(summary["pending_closure"]) == 1
    marker = summary["pending_closure"][0]
    assert marker["run_id"] == "old-run"
    assert marker["job_ids"] == ["7001", "7002"]
    assert "SshCircuitOpen" in marker["reason"]

    old = load_run(tmp_path, "old-run")
    assert old is not None
    # Watchdog stops re-flagging it (no longer in_flight), evidence durable.
    assert old.status == "abandoned"
    assert old.superseded_by == "new-run"
    assert old.pending_closure["job_ids"] == ["7001", "7002"]
    assert old.last_status["verdict_reason"] == "superseded_by=new-run"


def test_partial_kill_records_unconfirmed_job_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cancel affordance / partial verify: unconfirmed ids land in the marker."""
    _record(tmp_path, "old-run", job_ids=["7001", "7002"])
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        supersession, "_invoke_kill", _fake_kill_factory(calls, still_alive=["7002"])
    )

    summary = apply_supersession_gate(tmp_path, _spec("new-run", supersedes="old-run"))

    assert summary is not None
    assert summary["pending_closure"][0]["job_ids"] == ["7002"]
    old = load_run(tmp_path, "old-run")
    assert old is not None
    assert old.pending_closure["job_ids"] == ["7002"]


def test_supersedes_does_not_blanket_amnesty_other_live_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "old-run")
    _record(tmp_path, "third-run")
    monkeypatch.setattr(supersession, "_invoke_kill", _fake_kill_factory([]))

    with pytest.raises(errors.SiblingRunLive) as exc_info:
        apply_supersession_gate(tmp_path, _spec("new-run", supersedes="old-run"))
    assert "third-run" in str(exc_info.value)
    assert "old-run'" not in str(exc_info.value)  # the covered one is not re-named


def test_supersedes_unknown_run_id_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="no journal record"):
        apply_supersession_gate(tmp_path, _spec("new-run", supersedes="never-existed"))


def test_supersedes_terminal_run_links_without_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Superseding an already-terminal run journals the link and touches nothing."""
    _record(tmp_path, "old-run", status="failed")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(supersession, "_invoke_kill", _fake_kill_factory(calls))

    summary = apply_supersession_gate(tmp_path, _spec("new-run", supersedes="old-run"))

    assert calls == []
    assert summary is not None and "old-run" in summary["closed"]
    old = load_run(tmp_path, "old-run")
    assert old is not None
    assert old.status == "failed"  # verdict untouched — only the link recorded
    assert old.superseded_by == "new-run"


def test_supersedes_own_run_id_refused_at_the_model(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="PRIOR sibling"):
        _spec("new-run", supersedes="new-run")
    with pytest.raises(ValidationError, match="PRIOR sibling"):
        _spec("new-run", supersedes="new-run-canary")


# ── no false trips ───────────────────────────────────────────────────────────


def test_no_trip_on_very_first_submit(tmp_path: Path) -> None:
    assert apply_supersession_gate(tmp_path, _spec("first-run")) is None


def test_no_trip_on_same_run_canary_pairing(tmp_path: Path) -> None:
    """Two-phase canary gate: main ``X`` submits while ``X-canary`` is live."""
    _record(tmp_path, "new-run-canary")  # same SHA, in_flight
    assert apply_supersession_gate(tmp_path, _spec("new-run")) is None


@pytest.mark.parametrize("status", ["complete", "failed", "abandoned"])
def test_no_trip_on_terminal_sibling(tmp_path: Path, status: str) -> None:
    _record(tmp_path, "old-run", status=status)
    assert apply_supersession_gate(tmp_path, _spec("new-run")) is None


def test_no_trip_on_different_cmd_sha(tmp_path: Path) -> None:
    _record(tmp_path, "old-run", sha=OTHER_SHA)
    assert apply_supersession_gate(tmp_path, _spec("new-run")) is None


def test_no_trip_when_spec_identity_unknown(tmp_path: Path) -> None:
    _record(tmp_path, "old-run")
    assert apply_supersession_gate(tmp_path, _spec("new-run", sha=None)) is None


def test_no_trip_when_sibling_identity_unknown(tmp_path: Path) -> None:
    _record(tmp_path, "old-run", sha=None)  # no job_env sha, no sidecar
    assert apply_supersession_gate(tmp_path, _spec("new-run")) is None


def test_find_live_siblings_matches_run4_shape(tmp_path: Path) -> None:
    _record(tmp_path, "pi-estimation-e2cddfb7")
    siblings = find_live_siblings(tmp_path, run_id="pi-estimation-h2-e2cddfb7", identity=SHA)
    assert [s.run_id for s in siblings] == ["pi-estimation-e2cddfb7"]


def test_pre_supersession_record_loads_with_defaults(tmp_path: Path) -> None:
    """Back-compat: a record written before the supersession fields existed
    loads with harmless empty defaults (from_dict filters to known fields)."""
    record = _record(tmp_path, "old-run")
    payload = {
        k: v
        for k, v in record.to_dict().items()
        if k not in ("superseded_by", "superseded_at", "supersedes", "pending_closure")
    }
    loaded = RunRecord.from_dict(payload)
    assert loaded.superseded_by == ""
    assert loaded.superseded_at is None
    assert loaded.supersedes == ""
    assert loaded.pending_closure == {}
