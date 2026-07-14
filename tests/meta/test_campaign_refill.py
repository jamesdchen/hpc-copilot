"""Tests for the ``campaign-refill`` actor (RFC #362, Unit A).

``campaign-refill`` consumes ``campaign-advance``'s per-tick refill decision and,
for each requested slot, resolves + submits one detached ``campaign-run``
iteration. These tests mock the three composed seams
(``campaign_advance`` / ``resolve_submit_inputs`` / ``campaign_run`` — plus the
disk-reconstruction helper ``_build_iteration_resolve_spec``) at their SOURCE
modules (the actor imports them lazily inside the body), so every control-flow
branch is exercised with NO cluster, NO SSH, and — except the crash-mid-tick
self-correction test — NO journal. Every branch is exercised with the async
opt-in ON (default-off never reaches this actor; see test_watch_refill_stage).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.campaign_refill import CampaignRefillSpec
from hpc_agent._wire.workflows.campaign_run import CampaignRunResult
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsResult
from hpc_agent.meta.campaign.manifest import write_manifest
from hpc_agent.ops.campaign_refill import campaign_refill

if TYPE_CHECKING:
    from pathlib import Path

_ADV = "hpc_agent.meta.campaign.atoms.advance.campaign_advance"
_RESOLVE = "hpc_agent.ops.resolve_submit_inputs.resolve_submit_inputs"
_RUN = "hpc_agent.ops.campaign_run.campaign_run"
_BUILD = "hpc_agent.ops.campaign_refill._build_iteration_resolve_spec"


# ── fixtures / helpers ────────────────────────────────────────────────────────


def _greenlit_async_manifest(experiment_dir: Path, *, campaign_id: str, k: int = 3) -> None:
    write_manifest(
        experiment_dir,
        campaign_id=campaign_id,
        goal="tune",
        async_refill=True,
        max_in_flight=k,
        greenlit=True,
        greenlit_at="2026-07-12T00:00:00Z",
    )


def _submit_flow_dict(run_id: str) -> dict[str, Any]:
    """A minimal VALID submit-flow spec dict (what resolve's submit_spec carries)."""
    return {
        "profile": "ml",
        "cluster": "hoffman2",
        "ssh_target": "u@h",
        "remote_path": "/scratch/ml",
        "job_name": "ml_array",
        "run_id": run_id,
        "total_tasks": 1,
        "backend": "sge",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
    }


def _resolved(run_id: str) -> ResolveSubmitInputsResult:
    return ResolveSubmitInputsResult(
        stage_reached="resolved",
        needs_decision=False,
        reason="inputs resolved",
        run_id=run_id,
        cmd_sha="0" * 8,
        submit_spec=_submit_flow_dict(run_id),
    )


def _blocked(run_id: str, stage: str = "prior_run_found") -> ResolveSubmitInputsResult:
    return ResolveSubmitInputsResult(
        stage_reached=stage,
        needs_decision=True,
        reason=f"a live prior run ({run_id}) matches this cmd_sha",
        run_id=run_id,
        cmd_sha="0" * 8,
        prior_run_id=run_id,
        prior_status="in_flight",
    )


def _detached(run_id: str, pid: int) -> CampaignRunResult:
    return CampaignRunResult(
        stage_reached="detached",
        needs_decision=False,
        reason="detached",
        run_id=run_id,
        started=True,
        watch="journal",
        detached_pid=pid,
    )


# ── guard: un-greenlit refusal ────────────────────────────────────────────────


def test_refuses_ungreenlit(tmp_path: Path) -> None:
    """An async campaign that is NOT greenlit refuses — the standing-consent guard
    FIRES (greenlight is the one boundary; refills carry none)."""
    write_manifest(tmp_path, campaign_id="camp", async_refill=True, max_in_flight=3)

    with pytest.raises(errors.SpecInvalid, match="not greenlit"):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))


def test_refuses_absent_manifest(tmp_path: Path) -> None:
    """No manifest at all is a loud SpecInvalid, never a silent no-op."""
    with pytest.raises(errors.SpecInvalid, match="no manifest"):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="ghost"))


# ── guard: prior sidecar missing a required reconstruction field ───────────────

_FIND_RUNS = "hpc_agent.state.index.find_runs_by_campaign"
_READ_SIDECAR = "hpc_agent.state.runs.read_run_sidecar"


@pytest.mark.parametrize(
    ("missing", "match"),
    [("executor", "no ``executor``"), ("result_dir_template", "no ``result_dir_template``")],
)
def test_refuses_prior_sidecar_missing_required_field(
    tmp_path: Path, missing: str, match: str
) -> None:
    """``_build_iteration_resolve_spec`` reconstructs the next iteration purely
    from the prior run's sidecar; a sidecar lacking a required field is a loud
    SpecInvalid, never a spec silently built from a placeholder/None."""
    from hpc_agent.ops.campaign_refill import _build_iteration_resolve_spec

    sidecar = {
        "profile": "ml",
        "cluster": "hoffman2",
        "remote_path": "/scratch/ml",
        "executor": "python train.py --seed $SEED",
        "result_dir_template": "results/{run_id}/{task_id}",
    }
    del sidecar[missing]
    prior = mock.Mock(run_id="ml-aaaa1111", profile="ml", cluster="hoffman2")

    with (
        mock.patch(_FIND_RUNS, return_value=[prior]),
        mock.patch(_READ_SIDECAR, return_value=sidecar),
        pytest.raises(errors.SpecInvalid, match=match),
    ):
        _build_iteration_resolve_spec(tmp_path, "camp")


# ── no-op: advance did not decide refill ──────────────────────────────────────


@pytest.mark.parametrize("decision", ["wait_in_flight", "continue", "stop_converged"])
def test_no_refill_when_advance_not_refill(tmp_path: Path, decision: str) -> None:
    """When advance decides anything other than ``refill`` the actor is a typed
    no-op carrying the decision; it never resolves or submits."""
    _greenlit_async_manifest(tmp_path, campaign_id="camp")

    with (
        mock.patch(_ADV, return_value={"decision": decision, "reason": "r", "refill_count": None}),
        mock.patch(_RESOLVE) as m_resolve,
        mock.patch(_RUN) as m_run,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))

    assert res.stage_reached == "no_refill_needed"
    assert res.needs_decision is False
    assert res.decision == decision
    assert res.refill_count == 0
    assert res.submitted == []
    m_resolve.assert_not_called()
    m_run.assert_not_called()


# ── refill: N slots, sequential, correct spec threading ───────────────────────


def test_refills_n_slots(tmp_path: Path) -> None:
    """advance decides refill_count=3 → 3 detached campaign-run submissions with
    DISTINCT run_ids, each threaded so aggregate.run_id == the resolved run_id."""
    _greenlit_async_manifest(tmp_path, campaign_id="camp")
    run_ids = ["ml-aaaa1111", "ml-bbbb2222", "ml-cccc3333"]

    with (
        mock.patch(
            _ADV,
            return_value={"decision": "refill", "reason": "free slots", "refill_count": 3},
        ),
        mock.patch(_BUILD, return_value=mock.sentinel.resolve_spec),
        mock.patch(_RESOLVE, side_effect=[_resolved(r) for r in run_ids]) as m_resolve,
        mock.patch(
            _RUN,
            side_effect=[_detached(r, 1000 + i) for i, r in enumerate(run_ids)],
        ) as m_run,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))

    assert res.stage_reached == "refilled"
    assert res.needs_decision is False
    assert res.decision == "refill"
    assert res.refill_count == 3
    # N detached submissions, distinct run handling.
    assert [s.run_id for s in res.submitted] == run_ids
    assert len({s.run_id for s in res.submitted}) == 3
    assert [s.detached_pid for s in res.submitted] == [1000, 1001, 1002]
    assert all(s.stage_reached == "detached" for s in res.submitted)

    # resolve is called N times with the SAME (built-once) resolve_spec.
    assert m_resolve.call_count == 3
    for call in m_resolve.call_args_list:
        assert call.kwargs["spec"] is mock.sentinel.resolve_spec

    # Correct spec threading: each campaign-run spec is detached and its
    # aggregate.run_id / status.monitor.run_id equal the slot's resolved run_id.
    assert m_run.call_count == 3
    for run_id, call in zip(run_ids, m_run.call_args_list, strict=True):
        crspec = call.kwargs["spec"]
        assert crspec.detach is True
        assert crspec.campaign_id == "camp"
        assert crspec.aggregate.run_id == run_id
        assert crspec.status.monitor.run_id == run_id
        assert crspec.submit.submit.submit.run_id == run_id


def test_resolve_and_submit_interleave_per_slot(tmp_path: Path) -> None:
    """RFC E4/E5: each slot's campaign_run is spawned IMMEDIATELY after its
    resolve — resolve/submit interleave (resolve, run, resolve, run, ...), never
    batch-all-resolves-then-submit — so the sidecar write lands between slots."""
    _greenlit_async_manifest(tmp_path, campaign_id="camp")
    run_ids = ["ml-aaaa1111", "ml-bbbb2222"]
    order: list[str] = []

    def _resolve_se(*_a: Any, **_k: Any) -> ResolveSubmitInputsResult:
        r = run_ids[len([o for o in order if o == "resolve"])]
        order.append("resolve")
        return _resolved(r)

    def _run_se(*_a: Any, **_k: Any) -> CampaignRunResult:
        order.append("run")
        return _detached("x", 1)

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=mock.sentinel.resolve_spec),
        mock.patch(_RESOLVE, side_effect=_resolve_se),
        mock.patch(_RUN, side_effect=_run_se),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))

    assert order == ["resolve", "run", "resolve", "run"]


# ── per-slot distinctness: HPC_CAMPAIGN_ID is exported around the resolve loop ─


def test_exports_campaign_id_env_during_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async scaffold's per-slot distinctness reads the campaign id from
    ``HPC_CAMPAIGN_ID`` at materialization time. The actor must EXPORT it from
    ``spec.campaign_id`` (not trust the ambient shell), so ``_submitted_count``
    advances per slot and each asks a distinct trial. Capture it inside the
    resolve call; assert it is the campaign id and is RESTORED (here: popped) after."""
    monkeypatch.delenv("HPC_CAMPAIGN_ID", raising=False)
    _greenlit_async_manifest(tmp_path, campaign_id="camp")
    seen: list[str | None] = []

    def _resolve_se(*_a: Any, **_k: Any) -> ResolveSubmitInputsResult:
        import os

        seen.append(os.environ.get("HPC_CAMPAIGN_ID"))
        return _resolved(f"ml-{len(seen):04d}0000")

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=mock.sentinel.resolve_spec),
        mock.patch(_RESOLVE, side_effect=_resolve_se),
        mock.patch(_RUN, side_effect=lambda *a, **k: _detached("x", 1)),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))

    assert res.stage_reached == "refilled"
    assert seen == ["camp", "camp"]  # exported for every slot's resolve
    import os

    assert "HPC_CAMPAIGN_ID" not in os.environ  # restored (was unset) after the tick


def test_restores_preexisting_campaign_id_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing ambient ``HPC_CAMPAIGN_ID`` is overridden to ``spec.campaign_id``
    for the resolve (correctness: refill this campaign, not whatever the shell drove)
    and RESTORED to its prior value afterward."""
    monkeypatch.setenv("HPC_CAMPAIGN_ID", "other-campaign")
    _greenlit_async_manifest(tmp_path, campaign_id="camp")
    seen: list[str | None] = []

    def _resolve_se(*_a: Any, **_k: Any) -> ResolveSubmitInputsResult:
        import os

        seen.append(os.environ.get("HPC_CAMPAIGN_ID"))
        return _resolved("ml-aaaa1111")

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
        mock.patch(_BUILD, return_value=mock.sentinel.resolve_spec),
        mock.patch(_RESOLVE, side_effect=_resolve_se),
        mock.patch(_RUN, side_effect=lambda *a, **k: _detached("x", 1)),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))

    import os

    assert seen == ["camp"]
    assert os.environ["HPC_CAMPAIGN_ID"] == "other-campaign"  # ambient value restored


# ── blocked mid-loop ──────────────────────────────────────────────────────────


def test_slot_blocked_mid_loop(tmp_path: Path) -> None:
    """A slot that resolves to prior_run_found stops the loop: stage
    refill_blocked, needs_decision, blocked populated, only the prior slots
    submitted (the 2nd resolve breaks before its campaign_run)."""
    _greenlit_async_manifest(tmp_path, campaign_id="camp")

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 3}),
        mock.patch(_BUILD, return_value=mock.sentinel.resolve_spec),
        mock.patch(
            _RESOLVE,
            side_effect=[_resolved("ml-aaaa1111"), _blocked("ml-bbbb2222")],
        ) as m_resolve,
        mock.patch(_RUN, side_effect=[_detached("ml-aaaa1111", 1000)]) as m_run,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))

    assert res.stage_reached == "refill_blocked"
    assert res.needs_decision is True
    assert len(res.submitted) == 1
    assert res.submitted[0].run_id == "ml-aaaa1111"
    assert len(res.blocked) == 1
    assert res.blocked[0].stage == "prior_run_found"
    assert res.blocked[0].run_id == "ml-bbbb2222"
    # The loop broke: resolve called twice (2nd blocked), run called once.
    assert m_resolve.call_count == 2
    assert m_run.call_count == 1


# ── crash-mid-tick self-correction (real advance over a synthetic journal) ─────


def _seed_iteration(experiment_dir: Path, *, run_id: str, campaign_id: str, status: str) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        hpc_agent_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python train.py --seed $SEED",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=1,
        tasks_py_sha="0" * 12,
        campaign_id=campaign_id,
        profile="ml",
        cluster="hoffman2",
        remote_path="/u/scratch/exp",
    )
    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/scratch/exp",
            job_name="ml",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment_dir.resolve()),
            campaign_id=campaign_id,
            status=status,
        ),
    )


def test_crash_mid_tick_self_corrects_via_shrunk_refill_count(
    journal_home: Path, tmp_path: Path
) -> None:
    """Simulate a prior partial tick that submitted 2 of 3 slots (2 sidecars now
    in-flight). Re-ticking with REAL campaign-advance recomputes refill_count from
    the journal: K=3 pool with 2 in flight → refill_count SHRINKS to 1, so exactly
    one more slot submits. No cursor, no new state file — the pool self-corrects."""
    _greenlit_async_manifest(tmp_path, campaign_id="camp", k=3)
    # The residue of a partial tick: 2 iterations already in flight.
    _seed_iteration(tmp_path, run_id="ml-slot0", campaign_id="camp", status="in_flight")
    _seed_iteration(tmp_path, run_id="ml-slot1", campaign_id="camp", status="in_flight")

    with (
        mock.patch(_BUILD, return_value=mock.sentinel.resolve_spec),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-slot2")]) as m_resolve,
        mock.patch(_RUN, side_effect=[_detached("ml-slot2", 2000)]) as m_run,
    ):
        # NOTE: campaign_advance is REAL here — it reads the 2 in-flight records.
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))

    assert res.stage_reached == "refilled"
    assert res.decision == "refill"
    assert res.refill_count == 1  # 3 - 2 in-flight (had shrunk from a full-pool 3).
    assert len(res.submitted) == 1
    assert m_resolve.call_count == 1
    assert m_run.call_count == 1


def test_full_pool_waits_not_refills(journal_home: Path, tmp_path: Path) -> None:
    """Contrast to the shrink test: when the pool is already full (K=2, 2 in
    flight) REAL advance decides wait_in_flight, so the actor is a no-op — this is
    the terminal of the self-correction (the last partial slot never over-submits)."""
    _greenlit_async_manifest(tmp_path, campaign_id="camp", k=2)
    _seed_iteration(tmp_path, run_id="ml-slot0", campaign_id="camp", status="in_flight")
    _seed_iteration(tmp_path, run_id="ml-slot1", campaign_id="camp", status="in_flight")

    with (
        mock.patch(_RESOLVE) as m_resolve,
        mock.patch(_RUN) as m_run,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="camp"))

    assert res.stage_reached == "no_refill_needed"
    assert res.decision == "wait_in_flight"
    m_resolve.assert_not_called()
    m_run.assert_not_called()
