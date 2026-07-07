"""Detach-by-contract seams on the submit block verbs (design §3).

With ``detach=True`` (the default) a scheduler-bound block returns IMMEDIATELY
after spawning a durable detached worker — it never holds the chat. These pin:

* the handle envelope (started / watch=journal / detached_pid, stage=detached);
* the ordering PROOF — gate → (drift) → detach: a gate/drift failure surfaces
  SYNCHRONOUSLY to the caller, never inside a detached child that already
  launched work;
* the child's spec carries ``detach=False`` so it runs synchronously (no fork
  storm);
* ``detach=False`` still runs the current in-process path (tests / CI).

Cluster-free: the detached launcher is patched at its source module, so nothing
is actually spawned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

import hpc_agent.ops.submit_blocks as blocks
import hpc_agent.ops.submit_speculate as speculate
from hpc_agent import errors
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec, SubmitS3Spec, SubmitS4Spec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources
from hpc_agent._wire.workflows.submit_speculate import SubmitSpeculateSpec
from tests.ops._block_fixtures import greenlight

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "ml_run_abcd1234"
_LAUNCH_PATH = "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached"


class _FakeLaunch:
    run_id = _RUN_ID
    pid = 4242
    log_path = "/x/detached.log"


def _greenlight(experiment_dir: Path, verb: str) -> None:
    greenlight(experiment_dir, verb, run_id=_RUN_ID)


def _submit_flow_spec() -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=_RUN_ID,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        canary=True,
        job_env={"K": "v"},
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )


def _sv_spec() -> SubmitAndVerifySpec:
    return SubmitAndVerifySpec(submit=_submit_flow_spec(), poll_interval_sec=1, wait_budget_sec=5)


# ── S2: gate → detach ────────────────────────────────────────────────────────


def test_s2_detaches_by_default_after_gate(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s2")
    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(blocks, "submit_and_verify") as m_sv,
    ):
        result = blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec()))  # detach default

    # Detached: the synchronous canary path never ran.
    m_sv.assert_not_called()
    m_launch.assert_called_once()
    # The child gets the SAME verb with detach forced off.
    assert m_launch.call_args.kwargs["verb"] == "submit-s2"
    assert m_launch.call_args.kwargs["spec"]["detach"] is False
    # Handle envelope.
    assert result.stage_reached == "detached"
    assert result.started is True
    assert result.watch == "journal"
    assert result.detached_pid == 4242
    assert result.needs_decision is False
    assert result.next_block is None


def test_s2_gate_fires_before_detach(tmp_path: Path) -> None:
    """Ordering proof: no greenlight → the gate raises SYNCHRONOUSLY and the
    detached launcher is NEVER reached (never a gate failure inside the child)."""
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        pytest.raises(errors.SpecInvalid, match="no journaled greenlight"),
    ):
        blocks.submit_s2(tmp_path, spec=SubmitS2Spec(submit=_sv_spec()))
    m_launch.assert_not_called()


# ── S3: gate → drift → detach ────────────────────────────────────────────────


def _s3_spec() -> SubmitS3Spec:
    return SubmitS3Spec(
        submit=_sv_spec(),
        canary_run_id=f"{_RUN_ID}_canary",
        canary_job_ids=["1"],
        monitor=MonitorFlowSpec(run_id=_RUN_ID),
        invocation_argv="monitor-hpc --run-id " + _RUN_ID,
    )


def test_s3_ordering_is_gate_then_drift_then_detach(tmp_path: Path, monkeypatch) -> None:
    _greenlight(tmp_path, "submit-s3")
    calls: list[str] = []

    # Canary-validated gate: disable the cache so _assert_canary_verified is a
    # no-op (its own bounds), isolating the ordering assertion to drift→detach.
    monkeypatch.setattr("hpc_agent.state.canary_cache.cache_disabled", lambda: True)

    def _drift(*_a: Any, **_k: Any) -> None:
        calls.append("drift")

    def _launch(*_a: Any, **_k: Any):
        calls.append("detach")
        return _FakeLaunch()

    with (
        mock.patch("hpc_agent.ops.submit_and_verify._assert_no_post_greenlight_drift", _drift),
        mock.patch(_LAUNCH_PATH, side_effect=_launch),
        mock.patch.object(blocks, "launch_main_array") as m_main,
        mock.patch.object(blocks, "monitor_flow") as m_mon,
    ):
        result = blocks.submit_s3(tmp_path, spec=_s3_spec())

    # The main-array launch + monitor never ran in-process — they ride the child.
    m_main.assert_not_called()
    m_mon.assert_not_called()
    # Drift guard ran BEFORE the detach (gate → drift → detach).
    assert calls == ["drift", "detach"]
    assert result.stage_reached == "detached"
    assert result.started is True
    assert result.detached_pid == 4242


def test_s3_drift_fires_before_detach(tmp_path: Path, monkeypatch) -> None:
    """A post-greenlight tree drift raises SYNCHRONOUSLY — never inside a child
    that already launched the full array."""
    _greenlight(tmp_path, "submit-s3")
    monkeypatch.setattr("hpc_agent.state.canary_cache.cache_disabled", lambda: True)

    def _drift(*_a: Any, **_k: Any) -> None:
        raise errors.SpecInvalid("tasks.py/executor drifted since the canary greenlight")

    with (
        mock.patch("hpc_agent.ops.submit_and_verify._assert_no_post_greenlight_drift", _drift),
        mock.patch(_LAUNCH_PATH) as m_launch,
        pytest.raises(errors.SpecInvalid, match="drifted"),
    ):
        blocks.submit_s3(tmp_path, spec=_s3_spec())
    m_launch.assert_not_called()


# ── S4: gate → detach ────────────────────────────────────────────────────────


def test_s4_detaches_by_default_after_gate(tmp_path: Path) -> None:
    _greenlight(tmp_path, "submit-s4")
    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(blocks, "aggregate_flow") as m_agg,
    ):
        result = blocks.submit_s4(
            tmp_path, spec=SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
        )  # detach default

    # Detached: the synchronous harvest path never ran.
    m_agg.assert_not_called()
    m_launch.assert_called_once()
    # The child gets the SAME verb with detach forced off.
    assert m_launch.call_args.kwargs["verb"] == "submit-s4"
    assert m_launch.call_args.kwargs["spec"]["detach"] is False
    # Handle envelope.
    assert result.stage_reached == "detached"
    assert result.started is True
    assert result.watch == "journal"
    assert result.detached_pid == 4242
    assert result.needs_decision is False
    assert result.next_block is None


def test_s4_gate_fires_before_detach(tmp_path: Path) -> None:
    """Ordering proof: no greenlight → the gate raises SYNCHRONOUSLY and the
    detached launcher is NEVER reached (never a gate failure inside the child)."""
    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        pytest.raises(errors.SpecInvalid, match="no journaled greenlight"),
    ):
        blocks.submit_s4(tmp_path, spec=SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID)))
    m_launch.assert_not_called()


def _scoped_sidecar(experiment_dir: Path, *, scopes: list[str]) -> None:
    """Write a sidecar carrying *scopes* so the S4 scope gate has tags to read."""
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha="deadbeef",
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=10,
        tasks_py_sha="",
        scopes=scopes,
    )


def test_s4_locked_scope_refuses_before_detach(tmp_path: Path) -> None:
    """Ordering proof (rigor-primitives T3): a LOCKED evidence-scope refuses
    SYNCHRONOUSLY in the parent — the detached launcher is NEVER reached, so the
    refusal can never hide inside a detached child's log."""
    from hpc_agent.state.scopes import record_lock

    _greenlight(tmp_path, "submit-s4")
    _scoped_sidecar(tmp_path, scopes=["holdout"])
    record_lock(tmp_path, "holdout", reason="reserve this look")

    with (
        mock.patch(_LAUNCH_PATH) as m_launch,
        pytest.raises(errors.ScopeLocked, match="holdout"),
    ):
        blocks.submit_s4(tmp_path, spec=SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID)))
    m_launch.assert_not_called()


def test_s4_unlocked_scope_detaches(tmp_path: Path) -> None:
    """The companion: a scope present but UNLOCKED (lock then a journaled
    unlock) passes the gate and the block detaches normally."""
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.scopes import record_lock

    _greenlight(tmp_path, "submit-s4")
    _scoped_sidecar(tmp_path, scopes=["holdout"])
    record_lock(tmp_path, "holdout", reason="reserve this look")
    # Journaled unlock — the newest lock/unlock record decides, so this reads
    # unlocked while the lock history stays on disk (append-only).
    append_decision(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-lock",
        response="release",
        resolved={"scope_action": "unlock"},
    )

    with mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch:
        result = blocks.submit_s4(
            tmp_path, spec=SubmitS4Spec(aggregate=AggregateFlowSpec(run_id=_RUN_ID))
        )

    m_launch.assert_called_once()
    assert result.stage_reached == "detached"
    assert result.started is True


def test_s4_detached_launcher_digs_run_id_from_aggregate(tmp_path: Path) -> None:
    """The S4 spec carries its run_id at ``aggregate.run_id`` (not the S2/S3
    ``submit.submit.run_id`` shape) — the launcher's handle must still name it."""
    from hpc_agent._kernel.lifecycle import detached

    captured: dict[str, Any] = {}

    def _capture(*, run_id: str, block: str, argv: list[str], log_path: Any, cwd: str):  # noqa: ANN401
        captured["run_id"] = run_id
        captured["block"] = block
        return _FakeLaunch()

    with mock.patch.object(detached, "_spawn_detached", _capture):
        detached.launch_submit_block_detached(
            verb="submit-s4",
            experiment_dir=str(tmp_path),
            spec={"aggregate": {"run_id": _RUN_ID}, "detach": False},
        )

    assert captured["run_id"] == _RUN_ID
    assert captured["block"] == "submit-s4"


# ── speculate: dedup → detach ────────────────────────────────────────────────


def test_speculate_detaches_by_default(tmp_path: Path) -> None:
    # job_env has no HPC_CMD_SHA → cache key None → a fresh canary would fire.
    with (
        mock.patch(_LAUNCH_PATH, return_value=_FakeLaunch()) as m_launch,
        mock.patch.object(speculate, "submit_and_verify") as m_sv,
    ):
        result = speculate.submit_speculate(
            tmp_path, spec=SubmitSpeculateSpec(submit=_sv_spec())
        )  # detach default True

    m_sv.assert_not_called()
    m_launch.assert_called_once()
    assert m_launch.call_args.kwargs["verb"] == "submit-speculate"
    assert m_launch.call_args.kwargs["spec"]["detach"] is False
    assert result.started is True
    assert result.speculated is True
    assert result.watch == "journal"
    assert result.detached_pid == 4242


def test_speculate_noop_when_fresh_does_not_detach(tmp_path: Path, monkeypatch) -> None:
    """The budget/dedup no-op runs synchronously BEFORE any detach — a
    validated-fresh canary never spawns a worker."""
    submit = _submit_flow_spec().model_copy(update={"job_env": {"HPC_CMD_SHA": "deadbeef"}})
    spec = SubmitSpeculateSpec(
        submit=SubmitAndVerifySpec(submit=submit, poll_interval_sec=1, wait_budget_sec=5)
    )
    monkeypatch.setattr("hpc_agent.state.canary_cache.cache_disabled", lambda: False)
    monkeypatch.setattr(
        "hpc_agent.state.canary_cache.is_canary_validated_fresh", lambda *_a, **_k: True
    )
    with mock.patch(_LAUNCH_PATH) as m_launch:
        result = speculate.submit_speculate(tmp_path, spec=spec)
    m_launch.assert_not_called()
    assert result.speculated is False
    assert result.started is False


class TestDetachedWorkerBindsToRunningInterpreter:
    """The detached worker must run the SAME install as the parent that spawned
    it — ``sys.executable -m hpc_agent``, never a bare ``hpc-agent`` PATH lookup.

    A bare PATH ``hpc-agent`` is an independent console-script that can resolve
    to a DIFFERENT install (stale wheel, unactivated conda env, editable-dev
    multi-venv). In that case the detached worker silently runs the wrong code,
    or — when the block verbs are absent there — dies immediately with
    ``unknown command 'submit-s2'``. Surfaced by the first Hoffman2 proving run.
    """

    def test_submit_block_argv_uses_sys_executable_dash_m(self, tmp_path: Path) -> None:
        import sys

        from hpc_agent._kernel.lifecycle import detached

        submit = _submit_flow_spec().model_copy(update={"canary": True})
        spec = SubmitAndVerifySpec(
            submit=submit.model_copy(update={}), poll_interval_sec=1, wait_budget_sec=5
        )
        captured: dict[str, Any] = {}

        def _capture(*, run_id: str, block: str, argv: list[str], log_path: Any, cwd: str):  # noqa: ANN401
            captured["argv"] = argv
            captured["block"] = block
            return _FakeLaunch()

        with mock.patch.object(detached, "_spawn_detached", _capture):
            detached.launch_submit_block_detached(
                verb="submit-s2",
                experiment_dir=str(tmp_path),
                spec={"submit": spec.model_dump(mode="json"), "detach": False},
            )

        argv = captured["argv"]
        assert argv[:3] == [sys.executable, "-m", "hpc_agent"]
        assert argv[3] == "submit-s2"
        assert "hpc-agent" not in argv[:1]  # never a bare PATH console-script

    def test_explicit_bin_still_overrides(self) -> None:
        from hpc_agent._kernel.lifecycle.detached import _agent_launch_prefix

        assert _agent_launch_prefix("my-stub-bin") == ["my-stub-bin"]

    def test_default_prefix_is_running_interpreter(self) -> None:
        import sys

        from hpc_agent._kernel.lifecycle.detached import _agent_launch_prefix

        assert _agent_launch_prefix(None) == [sys.executable, "-m", "hpc_agent"]
        assert _agent_launch_prefix("") == [sys.executable, "-m", "hpc_agent"]
