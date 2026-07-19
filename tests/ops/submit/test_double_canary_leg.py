"""``fire_second_canary`` — the extracted canary leg fires a fresh ``-canary2``.

The determinism-fingerprint double canary reuses ``submit_flow``'s canary
submission leg (``_fire_canary``) for a SECOND canary. This proves the wrapper
mirrors the main sidecar to ``<run_id>-canary2`` (so it dispatches the SAME
command) and records it — with the scheduler/backend seam mocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def ship_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture the second-canary sidecar SHIP (finding 7) instead of SSH-ing.

    ``fire_second_canary`` ships the freshly-mirrored ``-canary2`` sidecar to the
    cluster (``push_run_sidecar``) BEFORE the qsub, since Phase 1's rsync already
    ran. Patch it at its transport home (``_fire_canary`` imports it lazily) so the
    unit tests never touch a real host, and record each call's kwargs for the
    finding-7 assertions.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        "hpc_agent.infra.transport.push_run_sidecar",
        lambda **kw: calls.append(kw),
    )
    return calls


def _spec() -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/remote",
        job_name="ml",
        run_id="run-x",
        total_tasks=4,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
    )


def test_fire_second_canary_mirrors_sidecar_and_records(
    tmp_path: Path, monkeypatch, ship_calls: list[dict]
) -> None:
    # Flag OFF (the explicit opt-out) — default-ON submit-once records the
    # canary via mint+promote, not submit_and_record.
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "0")
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

    # The main run's sidecar exists (Phase 1 wrote it); the second canary mirrors it.
    write_run_sidecar(
        tmp_path,
        run_id="run-x",
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-08T00:00:00Z",
        executor="python run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
        cluster="hoffman2",
        remote_path="/remote",
    )

    monkeypatch.setattr(sf, "build_remote_backend", lambda **_kw: object())
    monkeypatch.setattr(sf, "_make_single_array_submission", lambda *_a, **_k: ["777"])

    recorded: dict = {}

    def _fake_record(experiment_dir, *, spec, **_kw):
        recorded["run_id"] = spec.run_id
        recorded["job_ids"] = list(spec.job_ids)
        recorded["total_tasks"] = spec.total_tasks
        return (None, False)

    monkeypatch.setattr(sf, "submit_and_record", _fake_record)

    ids = sf.fire_second_canary(tmp_path, spec=_spec(), canary_run_id="run-x-canary2")

    assert ids == ["777"]
    # The -canary2 sidecar was mirrored from the main run's — SAME command, 1 task.
    sidecar = read_run_sidecar(tmp_path, "run-x-canary2")
    assert sidecar["executor"] == "python run.py --seed $SEED"
    assert sidecar["task_count"] == 1
    # It was recorded to the journal under the -canary2 id, not the first canary.
    assert recorded["run_id"] == "run-x-canary2"
    assert recorded["job_ids"] == ["777"]
    assert recorded["total_tasks"] == 1
    # Finding 7: the -canary2 sidecar was SHIPPED (Phase 1's rsync already ran, so
    # this leg must push it or the reporter reads a missing file and spins). One
    # ship, to the right run id + remote path.
    assert len(ship_calls) == 1
    assert ship_calls[0]["run_id"] == "run-x-canary2"
    assert ship_calls[0]["remote_path"] == "/remote"
    assert ship_calls[0]["ssh_target"] == "user@h"
    assert '"executor"' in ship_calls[0]["content"]  # the mirrored sidecar JSON


def test_boundary_index_selects_named_task_kwargs(
    tmp_path: Path, monkeypatch, ship_calls: list[dict]
) -> None:
    """A boundary-index canary dispatches the NAMED main task's frozen kwargs (§7.1).

    The Class-B heal re-verify samples the edge indices of a repaired range; the
    canary at index N carries the main run's task-N trial_params, not task 0's.
    """
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

    # A sweep main run with a frozen per-task manifest (one dict per task).
    write_run_sidecar(
        tmp_path,
        run_id="run-x",
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-08T00:00:00Z",
        executor="python run.py --chunk $CHUNK",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
        cluster="hoffman2",
        remote_path="/remote",
        trial_params=[{"chunk": 0}, {"chunk": 1}, {"chunk": 2}, {"chunk": 3}],
    )

    monkeypatch.setattr(sf, "build_remote_backend", lambda **_kw: object())
    monkeypatch.setattr(sf, "_make_single_array_submission", lambda *_a, **_k: ["888"])
    monkeypatch.setattr(sf, "submit_and_record", lambda *_a, **_k: (None, False))

    # Fire a canary at the LAST affected index (3) — the repaired-range boundary.
    sf.fire_second_canary(tmp_path, spec=_spec(), canary_run_id="run-x-canary-b3", boundary_index=3)

    sidecar = read_run_sidecar(tmp_path, "run-x-canary-b3")
    # 1-task probe carrying task 3's REAL kwargs (not task 0's {"chunk": 0}).
    assert sidecar["task_count"] == 1
    assert sidecar["trial_params"] == [{"chunk": 3}]


def test_default_canary_is_task0_byte_identical(
    tmp_path: Path, monkeypatch, ship_calls: list[dict]
) -> None:
    """Omitting boundary_index keeps the historical task-0 canary (byte-identical)."""
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id="run-x",
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-08T00:00:00Z",
        executor="python run.py --chunk $CHUNK",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
        cluster="hoffman2",
        remote_path="/remote",
        trial_params=[{"chunk": 0}, {"chunk": 1}, {"chunk": 2}, {"chunk": 3}],
    )

    monkeypatch.setattr(sf, "build_remote_backend", lambda **_kw: object())
    monkeypatch.setattr(sf, "_make_single_array_submission", lambda *_a, **_k: ["777"])
    monkeypatch.setattr(sf, "submit_and_record", lambda *_a, **_k: (None, False))

    sf.fire_second_canary(tmp_path, spec=_spec(), canary_run_id="run-x-canary2")

    sidecar = read_run_sidecar(tmp_path, "run-x-canary2")
    assert sidecar["trial_params"] == [{"chunk": 0}]  # task 0, exactly as before


def test_canary_forces_digests_on_even_when_main_array_is_off(
    tmp_path: Path, monkeypatch, ship_calls: list[dict]
) -> None:
    """data-trace T3: the canary IS an identity run (canary-vs-local trace-diff),
    so it forces HPC_TRACE_DIGESTS=1 even when the main array's job_env carries
    the classifier's "0" (a large main array)."""
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id="run-x",
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-08T00:00:00Z",
        executor="python run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
        cluster="hoffman2",
        remote_path="/remote",
    )

    captured: dict = {}

    def _capture(*_a, **kw):
        captured["job_env"] = dict(kw["job_env"])
        return ["777"]

    monkeypatch.setattr(sf, "build_remote_backend", lambda **_kw: object())
    monkeypatch.setattr(sf, "_make_single_array_submission", _capture)
    monkeypatch.setattr(sf, "submit_and_record", lambda *_a, **_k: (None, False))

    # Main array classified digests OFF (a large array), carried on job_env.
    spec = _spec().model_copy(update={"job_env": {"K": "v", "HPC_TRACE_DIGESTS": "0"}})
    sf.fire_second_canary(tmp_path, spec=spec, canary_run_id="run-x-canary2")

    assert captured["job_env"]["HPC_TRACE_DIGESTS"] == "1"
    assert captured["job_env"]["HPC_TASK_COUNT"] == "1"
