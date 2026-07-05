"""Tests for the ``resolve-submit-inputs`` composite.

The composite chains four laptop-side atoms (compute-run-id, find-prior-run,
build-tasks-py, build-submit-spec) and branches on tasks.py presence + the
find-prior-run resume contract. These tests mock each atom at the
``resolve_submit_inputs`` module seam and exercise every ``stage_reached``
path — no cluster, no journal, ``tmp_path`` for the experiment dir.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.actions.build_tasks_py import BuildTasksPyInput
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec

if TYPE_CHECKING:
    from pathlib import Path

_SEAM = "hpc_agent.ops.resolve_submit_inputs"


def _submit_input() -> BuildSubmitSpecInput:
    return BuildSubmitSpecInput(
        profile="ridge",
        cluster="h2",
        ssh_target="me@login.h2",
        remote_path="/scratch/me/exp",
        run_id="ridge-abcd1234",
        cmd_sha="a" * 64,
        total_tasks=2,
        backend="sge",
    )


def _sidecar_input() -> WriteRunSidecarInput:
    return WriteRunSidecarInput(
        run_id="ridge-placeholder",  # overridden by compute-run-id inside the composite
        cmd_sha="0" * 8,  # placeholder; overridden too
        executor="python -m src.ridge --alpha $alpha",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
    )


def _sidecar_ret() -> dict[str, Any]:
    return {"path": "/tmp/exp/.hpc/runs/ridge-abcd1234.json"}


def _build_tasks_input() -> BuildTasksPyInput:
    return BuildTasksPyInput(
        axes=[{"name": "exp_alpha", "values": [0.1, 1.0]}],  # type: ignore[list-item]
        flags_by_executor={"src.ridge": [{"name": "alpha", "type": "float"}]},  # type: ignore[list-item]
    )


def _spec(build_tasks: BuildTasksPyInput | None = None) -> ResolveSubmitInputsSpec:
    return ResolveSubmitInputsSpec(
        run_name="ridge",
        submit=_submit_input(),
        sidecar=_sidecar_input(),
        build_tasks=build_tasks,
    )


def _cr() -> dict[str, Any]:
    return {
        "run_id": "ridge-abcd1234",
        "cmd_sha": "a" * 64,
        "trial_tokens": None,
        "trial_params": [{"alpha": 0.1}, {"alpha": 1.0}],
        # The authoritative task count compute-run-id materialized (== len of
        # trial_params). resolve-submit-inputs cross-checks the agent-authored
        # submit.total_tasks / sidecar.task_count against this.
        "total": 2,
    }


def _spec_with_counts(*, submit_total: int, sidecar_count: int) -> ResolveSubmitInputsSpec:
    """A spec whose agent-authored task counts are overridable per-test, so a
    mismatch against compute-run-id's true count (``_cr()`` total == 2) can be
    exercised."""
    return ResolveSubmitInputsSpec(
        run_name="ridge",
        submit=_submit_input().model_copy(update={"total_tasks": submit_total}),
        sidecar=_sidecar_input().model_copy(update={"task_count": sidecar_count}),
    )


def _fp(
    *,
    found: bool = False,
    is_orphan: bool = False,
    status: str | None = None,
    prior_run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "found": found,
        "prior_run_id": prior_run_id,
        "is_orphan": is_orphan,
        "status": status,
        "age_sec": None,
        "profile": None,
        "cluster": None,
        "job_ids": [],
        "campaign_id": None,
        "submitted_at": None,
    }


def _touch_tasks_py(experiment_dir: Path) -> None:
    hpc = experiment_dir / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text("# stub\n", encoding="utf-8")


def test_resolved_builds_submit_spec(tmp_path: Path) -> None:
    """tasks.py present, no prior → resolved, carries the built submit spec."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    built = {"profile": "ridge", "run_id": "ridge-abcd1234", "total_tasks": 2}
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)) as fp,
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built) as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()) as ws,
        mock.patch(f"{_SEAM}.build_tasks_py") as bt,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False
    assert res.run_id == "ridge-abcd1234"
    assert res.cmd_sha == "a" * 64
    assert res.submit_spec == built
    assert res.sidecar_path == "/tmp/exp/.hpc/runs/ridge-abcd1234.json"
    assert res.prior_run_id is None
    fp.assert_called_once()
    bs.assert_called_once()
    ws.assert_called_once()  # per-run sidecar written on the resolved path (#171)
    # compute-run-id values are injected into BOTH downstream inputs, overriding
    # the placeholders the caller passed — so the built spec + sidecar match the
    # reported run_id.
    assert bs.call_args.kwargs["spec"].run_id == "ridge-abcd1234"
    assert ws.call_args.kwargs["spec"].run_id == "ridge-abcd1234"
    assert ws.call_args.kwargs["spec"].cmd_sha == "a" * 64
    # compute-run-id is authoritative for the per-task round-trip: its
    # trial_tokens AND trial_params (the cmd_sha pre-image, persisted for
    # provenance) are injected onto the sidecar spec, not hand-threaded.
    assert ws.call_args.kwargs["spec"].trial_tokens is None
    assert ws.call_args.kwargs["spec"].trial_params == [{"alpha": 0.1}, {"alpha": 1.0}]
    # No interview.json here → the caller-supplied executor stands (the
    # deterministic override is a no-op on the canonical no-interview path).
    assert ws.call_args.kwargs["spec"].executor == "python -m src.ridge --alpha $alpha"
    bt.assert_not_called()  # tasks.py present → no scaffold


def test_resolved_overrides_executor_from_materialized_interview(tmp_path: Path) -> None:
    """When interview.json materialized a per-task executor_cmd (a python_module's
    run-module dispatch), CODE resolves it and it WINS over the caller-supplied
    sidecar.executor — executor selection never rides on the LLM, so the agent
    can't divine a broken `python3 -m <module>` (the ridge_imp exit-127 class)."""
    import json as _json

    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    materialized_cmd = "python3 -m hpc_agent.executor_cli run-module my_pkg.train:main"
    (tmp_path / "interview.json").write_text(
        _json.dumps(
            {
                "_materialized": {
                    "entry_point": {
                        "kind": "python_module",
                        "module": "my_pkg.train",
                        "function": "main",
                        "executor_cmd": materialized_cmd,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value={"x": 1}),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()) as ws,
        mock.patch(f"{_SEAM}.build_tasks_py"),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    # The caller passed executor="python -m src.ridge --alpha $alpha"; the
    # materialized run-module command overrides it deterministically.
    assert ws.call_args.kwargs["spec"].executor == materialized_cmd


def test_materialized_executor_cmd_reads_run_module_from_a_real_interview(tmp_path: Path) -> None:
    """End-to-end seam: a real `record_interview` for a python_module entry writes
    a run-module `executor_cmd`, and `_materialized_executor_cmd` reads it back —
    so resolve-submit-inputs gets the deterministic run-module command from a REAL
    interview, not a hand-crafted interview.json. Pins the producer↔consumer
    contract the run-module work created end-to-end (previously each side was
    tested alone: the interview emits it; the reader reads it)."""
    import json as _json

    from hpc_agent._wire.actions.interview import InterviewSpec
    from hpc_agent.ops.memory.interview import record_interview
    from hpc_agent.ops.resolve_submit_inputs import _materialized_executor_cmd

    # tasks.py the interview validates task_count against (3 tasks).
    tasks = tmp_path / ".hpc" / "tasks.py"
    tasks.parent.mkdir(parents=True, exist_ok=True)
    tasks.write_text(
        '_TASKS = [{"seed": 0}, {"seed": 1}, {"seed": 2}]\n'
        "def total(): return len(_TASKS)\n"
        "def resolve(i): return _TASKS[i]\n",
        encoding="utf-8",
    )
    # An importable module with `main` so the python_module intake validation
    # passes (campaign_dir is on sys.path during intake, #178).
    (tmp_path / "pm_e2e.py").write_text(
        "def main(seed: int = 0) -> dict:\n    return {'seed': seed}\n", encoding="utf-8"
    )
    intent = {
        "goal": "e2e seam",
        "task_count": 3,
        "produced_by": {"kind": "human", "operator": "test"},
        "entry_point": {"kind": "python_module", "module": "pm_e2e", "function": "main"},
    }
    record_interview(InterviewSpec.model_validate(intent), campaign_dir=tmp_path)

    # The reader resolve-submit-inputs uses returns the run-module command the
    # interview materialized — no LLM, no hand-crafted JSON.
    assert (
        _materialized_executor_cmd(tmp_path)
        == "python3 -m hpc_agent.executor_cli run-module pm_e2e:main"
    )
    # And it is actually persisted in interview.json's materialized block.
    doc = _json.loads((tmp_path / "interview.json").read_text(encoding="utf-8"))
    assert doc["_materialized"]["entry_point"]["executor_cmd"].endswith("run-module pm_e2e:main")


def test_terminal_failed_prior_is_not_live_proceeds_fresh(tmp_path: Path) -> None:
    """A failed/abandoned record (#276) is forensic, not live → resolved."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    built = {"profile": "ridge"}
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, status="failed", prior_run_id="ridge-dead0000"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False
    assert res.submit_spec == built


def test_live_prior_complete_escalates(tmp_path: Path) -> None:
    """A live complete prior → prior_run_found, resume-vs-fresh is the user's call."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, status="complete", prior_run_id="ridge-abcd1234"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "prior_run_found"
    assert res.needs_decision is True
    assert res.prior_run_id == "ridge-abcd1234"
    assert res.prior_status == "complete"
    assert res.submit_spec is None
    assert res.sidecar_path is None
    bs.assert_not_called()  # stopped before building the spec
    ws.assert_not_called()  # and before writing the sidecar


def test_live_prior_in_flight_escalates(tmp_path: Path) -> None:
    """An in_flight prior also blocks (a timed-out run stays in_flight)."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, status="in_flight", prior_run_id="ridge-abcd1234"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec"),
        mock.patch(f"{_SEAM}.write_run_sidecar"),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "prior_run_found"
    assert res.needs_decision is True
    assert res.prior_status == "in_flight"


def test_orphan_prior_is_not_live_proceeds_fresh(tmp_path: Path) -> None:
    """A half-baked orphan sidecar is not a real prior → resolved."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_SEAM}.find_prior_run",
            return_value=_fp(found=True, is_orphan=True, status="complete"),
        ),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value={"ok": 1}),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False


def _canary_record(
    tmp_path: Path,
    run_id: str,
    *,
    status: str,
    cluster: str = "discovery",
    job_ids: list[str] | None = None,
) -> None:
    """Upsert a ``<run_id>`` journal RunRecord in tmp_path's journal home.

    Mirrors tests/ops/submit/test_supersession.py's ``_record`` idiom — the
    canary sub-record is an ordinary RunRecord whose run_id ends in ``-canary``.
    """
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        tmp_path,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster=cluster,
            ssh_target="u@h",
            remote_path="/scratch/x",
            job_name="j",
            job_ids=job_ids if job_ids is not None else ["501"],
            total_tasks=1,
            submitted_at="2026-07-05T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status=status,
        ),
    )


def test_live_canary_only_attempt_escalates_with_cluster(tmp_path: Path, monkeypatch: Any) -> None:
    """An attempt that died pre-main-submit leaves only a LIVE <run_id>-canary
    sub-record (main array never launched) — invisible to cmd_sha resume-detection.
    resolve-submit-inputs must still surface prior_run_found, carrying the canary's
    cluster + status so the human meets the retarget fork at S1, not the S2 backstop."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    _touch_tasks_py(tmp_path)
    # compute-run-id yields run_id "ridge-abcd1234"; its live canary sibling.
    _canary_record(tmp_path, "ridge-abcd1234-canary", status="in_flight", cluster="discovery")
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        # cmd_sha resume-detection finds nothing (the main record never existed).
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "prior_run_found"
    assert res.needs_decision is True
    assert res.prior_run_id == "ridge-abcd1234-canary"
    assert res.prior_status == "in_flight"
    assert res.prior_cluster == "discovery"  # the retarget-fork brief field
    assert res.submit_spec is None
    assert res.sidecar_path is None
    bs.assert_not_called()  # stopped before building the spec
    ws.assert_not_called()  # and before writing the sidecar


def test_terminal_canary_only_attempt_proceeds_fresh(tmp_path: Path, monkeypatch: Any) -> None:
    """A TERMINAL canary-only sub-record (failed/complete) with no live lease is a
    corpse (#276 spirit) — it must NOT block re-resolve. Clean resolve unchanged."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    _touch_tasks_py(tmp_path)
    _canary_record(tmp_path, "ridge-abcd1234-canary", status="failed", cluster="discovery")
    built = {"profile": "ridge"}
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built) as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()) as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False
    assert res.submit_spec == built
    bs.assert_called_once()
    ws.assert_called_once()


def test_live_detached_lease_only_escalates(tmp_path: Path, monkeypatch: Any) -> None:
    """Even with NO canary journal record, a live detached S2 worker lease for the
    run_id is a live attempt → prior_run_found (belt-and-suspenders liveness)."""
    import os

    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs
    from hpc_agent.state.run_record import _current_homedir

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    _touch_tasks_py(tmp_path)
    detached = _current_homedir() / "_detached"
    detached.mkdir(parents=True, exist_ok=True)
    # Our own pid is definitionally alive → a live lease on run-id "ridge-abcd1234".
    (detached / "submit-s2-ridge-abcd1234.lease.json").write_text(
        f'{{"run_id": "ridge-abcd1234", "block": "submit-s2", "pid": {os.getpid()}}}',
        encoding="utf-8",
    )
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec())

    assert res.stage_reached == "prior_run_found"
    assert res.prior_run_id == "ridge-abcd1234-canary"
    assert res.prior_status == "in_flight"  # no record → live-by-lease default
    assert res.prior_cluster is None  # no record → cluster unknown
    bs.assert_not_called()
    ws.assert_not_called()


def test_absent_tasks_no_scaffold_spec_escalates(tmp_path: Path) -> None:
    """tasks.py absent + no build_tasks spec → needs_scaffold_interview."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    # No .hpc/tasks.py on disk; build_tasks omitted.
    with (
        mock.patch(f"{_SEAM}.compute_run_id") as cr,
        mock.patch(f"{_SEAM}.find_prior_run") as fp,
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
        mock.patch(f"{_SEAM}.build_tasks_py") as bt,
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec(build_tasks=None))

    assert res.stage_reached == "needs_scaffold_interview"
    assert res.needs_decision is True
    assert res.run_id is None
    assert res.submit_spec is None
    assert res.sidecar_path is None
    # Stopped at the escalation — none of the downstream atoms ran.
    cr.assert_not_called()
    fp.assert_not_called()
    bs.assert_not_called()
    ws.assert_not_called()
    bt.assert_not_called()


def test_absent_tasks_with_scaffold_spec_builds_then_resolves(tmp_path: Path) -> None:
    """tasks.py absent + build_tasks supplied → scaffold deterministically, then resolved."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    # build_tasks_py is mocked, so it won't actually write tasks.py; that is
    # fine — the seam is what we test, and compute_run_id is also mocked.
    built = {"profile": "ridge"}
    with (
        mock.patch(f"{_SEAM}.build_tasks_py", return_value={"wrote": True}) as bt,
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec", return_value=built),
        mock.patch(f"{_SEAM}.write_run_sidecar", return_value=_sidecar_ret()),
    ):
        res = resolve_submit_inputs(tmp_path, spec=_spec(build_tasks=_build_tasks_input()))

    assert res.stage_reached == "resolved"
    assert res.needs_decision is False
    assert res.submit_spec == built
    bt.assert_called_once()  # the deterministic scaffold fired


def test_undercount_task_count_refused_naming_both_counts(tmp_path: Path) -> None:
    """An UNDERCOUNT (declared < the materialized tasks.total()) must fail LOUD.

    The finding-21 silent class: an undercount sizes the job array 1-total_tasks,
    the higher task_ids never dispatch, and the run returns incomplete results
    found only at harvest. compute-run-id materialized the true count (== 2 here);
    a spec declaring 1 must be refused with SpecInvalid naming BOTH the declared
    value and the true count — never build the spec / write the sidecar."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),  # true total == 2
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
        mock.patch(f"{_SEAM}.build_tasks_py"),
        pytest.raises(errors.SpecInvalid) as excinfo,
    ):
        resolve_submit_inputs(tmp_path, spec=_spec_with_counts(submit_total=1, sidecar_count=1))

    msg = str(excinfo.value)
    assert "submit.total_tasks=1" in msg  # names the declared submit count
    assert "sidecar.task_count=1" in msg  # names the declared sidecar count
    assert "2 tasks" in msg  # names the true count compute-run-id materialized
    # Refused BEFORE the job array was sized / the sidecar written.
    bs.assert_not_called()
    ws.assert_not_called()


def test_overcount_task_count_refused_naming_both_counts(tmp_path: Path) -> None:
    """An OVERCOUNT (declared > tasks.total()) is equally a spec-authoring error —
    the array would index past the task list. Refused with the same loud
    SpecInvalid; the spec is never built and the sidecar never written."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),  # true total == 2
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
        mock.patch(f"{_SEAM}.build_tasks_py"),
        pytest.raises(errors.SpecInvalid) as excinfo,
    ):
        resolve_submit_inputs(tmp_path, spec=_spec_with_counts(submit_total=5, sidecar_count=5))

    msg = str(excinfo.value)
    assert "submit.total_tasks=5" in msg
    assert "sidecar.task_count=5" in msg
    assert "2 tasks" in msg  # the true count
    bs.assert_not_called()
    ws.assert_not_called()


def test_one_count_disagreeing_refused(tmp_path: Path) -> None:
    """The two agent-authored counts must ALSO agree with each other: a spec whose
    submit.total_tasks matches the true count but sidecar.task_count does not (or
    vice-versa) is still refused — the guard demands all three equal."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    _touch_tasks_py(tmp_path)
    with (
        mock.patch(f"{_SEAM}.compute_run_id", return_value=_cr()),  # true total == 2
        mock.patch(f"{_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_SEAM}.build_submit_spec") as bs,
        mock.patch(f"{_SEAM}.write_run_sidecar") as ws,
        mock.patch(f"{_SEAM}.build_tasks_py"),
        pytest.raises(errors.SpecInvalid) as excinfo,
    ):
        resolve_submit_inputs(tmp_path, spec=_spec_with_counts(submit_total=2, sidecar_count=1))

    msg = str(excinfo.value)
    assert "sidecar.task_count=1" in msg
    assert "2 tasks" in msg
    bs.assert_not_called()
    ws.assert_not_called()
