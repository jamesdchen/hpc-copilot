"""The determinism-fingerprint DOUBLE CANARY (docs/design/determinism-fingerprint.md).

``submit_and_verify`` fires a SECOND canary (``<main>-canary2``) after the first
verifies, mints the n=2 fingerprint prior from the two executions' task-0
metrics, and blocks the main array if the second canary FAILS (a same-code
passed-then-failed nondeterminism finding). These tests mock the transport /
scheduler seams (``submit_flow`` / ``verify_canary`` / ``fire_second_canary``)
exactly as ``test_submit_and_verify.py`` does; the sample-minting and pull legs
are exercised with mocked rsync so nothing touches SSH.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest import mock

from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

if TYPE_CHECKING:
    from pathlib import Path

_SAV = "hpc_agent.ops.submit_and_verify"
_MAIN = "ml_run_abcd1234"


def _submit_spec(*, canary: bool = True, auto_resume: bool = False) -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=_MAIN,
        total_tasks=4,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=canary,
        auto_resume_on_kill=auto_resume,
    )


def _spec(**kw: object) -> SubmitAndVerifySpec:
    return SubmitAndVerifySpec(
        submit=_submit_spec(**kw),  # type: ignore[arg-type]
        poll_interval_sec=1,
        wait_budget_sec=5,
    )


def _submit_env(*, canary: bool = True, deduped: bool = False) -> object:
    from hpc_agent.ops.submit_flow import SubmitFlowResult

    return SubmitFlowResult(
        run_id=_MAIN,
        job_ids=["12345"],
        total_tasks=4,
        deduped=deduped,
        canary_done=canary,
        canary_run_id=f"{_MAIN}-canary" if canary else None,
        canary_job_ids=["12344"] if canary else None,
    )


def _deduped_env() -> object:
    from hpc_agent.ops.submit_flow import SubmitFlowResult

    return SubmitFlowResult(
        run_id=_MAIN,
        job_ids=["12345"],
        total_tasks=4,
        deduped=True,
        canary_done=False,
        canary_run_id=None,
        canary_job_ids=None,
    )


def _verify_env(*, ok: bool, failure_kind: str | None = None) -> dict:
    return {
        "ok": ok,
        "failure_kind": failure_kind,
        "details": "happy" if ok else "boom",
        "stderr_tail": "" if ok else "RuntimeError\n",
        "metrics_fingerprint": None,
    }


# --- the orchestration (seam mocked) -----------------------------------------


def test_second_canary_fires_verifies_and_mints_then_proceeds(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()) as m_submit,
        mock.patch(f"{_SAV}.verify_canary", return_value=_verify_env(ok=True)) as m_verify,
        mock.patch(f"{_SAV}.fire_second_canary", return_value=["9999"]) as m_fire,
        mock.patch(f"{_SAV}._mint_double_canary_sample") as m_mint,
    ):
        result = submit_and_verify(tmp_path, spec=_spec())

    # The SECOND canary fired with the -canary2 run_id (never the completed first).
    m_fire.assert_called_once()
    assert m_fire.call_args.kwargs["canary_run_id"] == f"{_MAIN}-canary2"
    # verify_canary ran twice — first canary, then the second.
    assert m_verify.call_count == 2
    second_kw = m_verify.call_args_list[1].kwargs
    assert second_kw["canary_run_id"] == f"{_MAIN}-canary2"
    # The -canary2 substitution: expect_output/fingerprint OMITTED so the
    # verify_canary run-id refusal never fires on a path built for -canary.
    assert second_kw["expect_output"] is None
    assert second_kw["fingerprint"] is None
    # The n=2 prior was minted, and the fused Phase-2 main array still launched.
    m_mint.assert_called_once()
    assert m_submit.call_count == 2
    assert result.verified is True
    assert result.job_ids == ["12345"]


def test_double_canary_runs_on_stop_after_canary_s2(tmp_path: Path) -> None:
    """At S2 (stop_after_canary) the fingerprint is minted BEFORE the return —
    the block-split flow mints at S2, not at S3's launch_main_array."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()) as m_submit,
        mock.patch(f"{_SAV}.verify_canary", return_value=_verify_env(ok=True)),
        mock.patch(f"{_SAV}.fire_second_canary", return_value=["9999"]) as m_fire,
        mock.patch(f"{_SAV}._mint_double_canary_sample") as m_mint,
    ):
        result = submit_and_verify(tmp_path, spec=_spec(), stop_after_canary=True)

    m_fire.assert_called_once()
    m_mint.assert_called_once()
    assert m_submit.call_count == 1  # S2 stop: the main array did NOT launch
    assert result.verified is True
    assert result.job_ids == []


def test_failed_second_canary_blocks_loudly_and_mints_nothing(tmp_path: Path) -> None:
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    verifies = [_verify_env(ok=True), _verify_env(ok=False, failure_kind="nonzero_exit")]
    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()) as m_submit,
        mock.patch(f"{_SAV}.verify_canary", side_effect=verifies),
        mock.patch(f"{_SAV}.fire_second_canary", return_value=["9999"]),
        mock.patch(f"{_SAV}._mint_double_canary_sample") as m_mint,
    ):
        result = submit_and_verify(tmp_path, spec=_spec())

    # The main array NEVER launches — blocked exactly like a failed first canary.
    assert m_submit.call_count == 1
    assert result.verified is False
    assert result.job_ids == []
    assert result.failure_kind == "nonzero_exit"
    # A failed second canary appends NO sample.
    m_mint.assert_not_called()


def test_no_double_canary_env_reverts_to_single_canary(tmp_path: Path, monkeypatch) -> None:
    """HPC_NO_DOUBLE_CANARY=1 (operator env) skips BOTH the second execution and
    the sample — the single-canary path is byte-compatible (main still launches)."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    monkeypatch.setenv("HPC_NO_DOUBLE_CANARY", "1")
    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()) as m_submit,
        mock.patch(f"{_SAV}.verify_canary", return_value=_verify_env(ok=True)) as m_verify,
        mock.patch(f"{_SAV}.fire_second_canary") as m_fire,
        mock.patch(f"{_SAV}._mint_double_canary_sample") as m_mint,
    ):
        result = submit_and_verify(tmp_path, spec=_spec())

    m_fire.assert_not_called()
    m_verify.assert_called_once()  # single canary only
    m_mint.assert_not_called()
    assert m_submit.call_count == 2  # the main array still launches
    assert result.verified is True


def test_deduped_submit_skips_the_double_canary(tmp_path: Path) -> None:
    """A cache/dedup skip returns before the verify seam, so the second canary
    never fires and no sample is minted (the fingerprint doesn't grow)."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_deduped_env()),
        mock.patch(f"{_SAV}.verify_canary") as m_verify,
        mock.patch(f"{_SAV}.fire_second_canary") as m_fire,
        mock.patch(f"{_SAV}._mint_double_canary_sample") as m_mint,
    ):
        submit_and_verify(tmp_path, spec=_spec())

    m_verify.assert_not_called()
    m_fire.assert_not_called()
    m_mint.assert_not_called()


def test_second_canary_fires_BEFORE_first_is_verified(tmp_path: Path) -> None:
    """RANK 8: both canaries queue CONCURRENTLY — the second is fired BEFORE the
    first is verified (not after), so the scheduler parallelizes their queue+run
    instead of serializing a second full cycle. Proven by the call order: the
    ``fire_second_canary`` lands ahead of the FIRST ``verify_canary``."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    order: list[str] = []

    def _fire(*_a, **kw):
        order.append(f"fire:{kw['canary_run_id']}")
        return ["9999"]

    def _verify(*_a, **kw):
        order.append(f"verify:{kw['canary_run_id']}")
        return _verify_env(ok=True)

    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()),
        mock.patch(f"{_SAV}.verify_canary", side_effect=_verify),
        mock.patch(f"{_SAV}.fire_second_canary", side_effect=_fire),
        mock.patch(f"{_SAV}._mint_double_canary_sample"),
    ):
        submit_and_verify(tmp_path, spec=_spec())

    # The fire of -canary2 precedes the verify of the FIRST canary (concurrency).
    assert order[0] == f"fire:{_MAIN}-canary2"
    assert order[1] == f"verify:{_MAIN}-canary"
    assert order[2] == f"verify:{_MAIN}-canary2"


def test_failed_first_canary_does_not_verify_the_concurrent_second(tmp_path: Path) -> None:
    """RANK 8: on a broken FIRST canary the concurrently-fired second is an orphan
    — the main array is already blocked, so the second is NOT verified (one
    verify_canary call), just closed. The first canary already proved the code
    broken; a second verdict would be redundant SSH."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()) as m_submit,
        mock.patch(
            f"{_SAV}.verify_canary",
            return_value=_verify_env(ok=False, failure_kind="nonzero_exit"),
        ) as m_verify,
        mock.patch(f"{_SAV}.fire_second_canary", return_value=["9999"]) as m_fire,
        mock.patch(f"{_SAV}._mint_double_canary_sample") as m_mint,
    ):
        result = submit_and_verify(tmp_path, spec=_spec())

    m_fire.assert_called_once()  # the second WAS fired concurrently...
    m_verify.assert_called_once()  # ...but never verified — the first already failed
    m_mint.assert_not_called()
    assert m_submit.call_count == 1  # main array never launched
    assert result.verified is False
    assert result.failure_kind == "nonzero_exit"


def test_fire_failure_degrades_to_single_canary(tmp_path: Path) -> None:
    """RANK 8: if the second canary can't be FIRED (e.g. its sidecar ship raised),
    the submit degrades to a single canary — the first is still verified on its own
    merits and the main array launches; the n=2 sample simply doesn't mint."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()) as m_submit,
        mock.patch(f"{_SAV}.verify_canary", return_value=_verify_env(ok=True)) as m_verify,
        mock.patch(f"{_SAV}.fire_second_canary", side_effect=RuntimeError("ship failed")),
        mock.patch(f"{_SAV}._mint_double_canary_sample") as m_mint,
    ):
        result = submit_and_verify(tmp_path, spec=_spec())

    m_verify.assert_called_once()  # only the first canary was verified
    m_mint.assert_not_called()  # no second execution → no n=2 sample
    assert m_submit.call_count == 2  # the main array still launched
    assert result.verified is True


# --- the pull leg (rsync mocked) ---------------------------------------------


def _seed_canary(tmp_path: Path, run_id: str) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    upsert_run(
        tmp_path,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/remote",
            job_name="j",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-07-08T00:00:00Z",
            experiment_dir=str(tmp_path),
            status="complete",
        ),
    )
    write_run_sidecar(
        tmp_path,
        run_id=run_id,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-08T00:00:00Z",
        executor="python run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=1,
        tasks_py_sha="b" * 64,
        remote_path="/remote",
        cluster="hoffman2",
    )


def test_pull_lands_metrics_under_pulls_dir(tmp_path: Path, monkeypatch) -> None:
    from hpc_agent.ops import submit_and_verify as sav
    from hpc_agent.state.fingerprint_store import pulls_dir

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    _seed_canary(tmp_path, f"{_MAIN}-canary")

    seen: dict = {}

    def _fake_rsync(*, ssh_target, remote_path, remote_subdir, local_dir, include=None, **_kw):
        seen["remote_subdir"] = remote_subdir
        seen["include"] = include
        dest = pulls_dir(tmp_path, f"{_MAIN}-canary")
        assert str(dest) == str(local_dir)  # the pull targets the T3 pulls dir
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "metrics.json").write_text(json.dumps({"loss": 1.0}), encoding="utf-8")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("hpc_agent.infra.transport.rsync_pull", _fake_rsync)

    path = sav._pull_canary_task0_metrics(tmp_path, f"{_MAIN}-canary")

    assert seen["include"] == ["metrics.json"]
    # Task-0 result dir rendered from result_dir_template.
    assert seen["remote_subdir"] == f"results/{_MAIN}-canary/task_0"
    assert path.name == "metrics.json"
    assert pulls_dir(tmp_path, f"{_MAIN}-canary") in path.parents


def test_both_canary_metrics_pull_folds_into_one_cycle(tmp_path: Path, monkeypatch) -> None:
    """F6 (latency-elimination): BOTH canaries' task-0 metrics are fetched in ONE
    pull cycle — a single ``rsync_pull`` covering their common ancestor with an
    include per canary — not two round-trips (four under the tar engine). The two
    payloads still land DISTINCTLY on disk so the sample compares two identities.
    """
    from pathlib import Path as _P

    from hpc_agent.infra import transport
    from hpc_agent.ops import submit_and_verify as sav
    from hpc_agent.state.fingerprint_store import pulls_dir

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    _seed_canary(tmp_path, f"{_MAIN}-canary")
    _seed_canary(tmp_path, f"{_MAIN}-canary2")

    calls: list[dict] = []

    def _fake_rsync(*, ssh_target, remote_path, remote_subdir, local_dir, include=None, **_kw):
        calls.append({"remote_subdir": remote_subdir, "include": list(include or [])})
        for rel in include or []:
            dest = _P(local_dir).joinpath(*rel.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({"loss": 1.0}), encoding="utf-8")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transport, "rsync_pull", _fake_rsync)

    path_a, path_b = sav._pull_both_canary_task0_metrics(
        tmp_path,
        first_canary_run_id=f"{_MAIN}-canary",
        second_canary_run_id=f"{_MAIN}-canary2",
        dest_run_id=_MAIN,
    )

    # ONE cycle covered BOTH canaries — the fold (2 round-trips, not 4).
    assert len(calls) == 1
    # Scoped to the two result dirs' common ancestor, anchored includes per canary.
    assert calls[0]["remote_subdir"] == "results"
    # U-HW1 widened the folded pull by the runtime sidecar (hardware facts ride
    # home in the same rsync the fingerprint mint already runs — zero new dials).
    assert sorted(calls[0]["include"]) == [
        f"{_MAIN}-canary/task_0/_runtime.json",
        f"{_MAIN}-canary/task_0/metrics.json",
        f"{_MAIN}-canary2/task_0/_runtime.json",
        f"{_MAIN}-canary2/task_0/metrics.json",
    ]
    # Both payloads landed distinctly under the dest run's pulls dir.
    assert path_a.is_file() and path_b.is_file()
    assert path_a != path_b
    root = pulls_dir(tmp_path, _MAIN)
    assert root in path_a.parents and root in path_b.parents


# --- the mint leg (append_sample real, pull stubbed) -------------------------


def test_mint_appends_sample_with_double_canary_labels(tmp_path: Path, monkeypatch) -> None:
    from hpc_agent.ops import submit_and_verify as sav
    from hpc_agent.state import fingerprint_store
    from hpc_agent.state.runs import write_run_sidecar

    # The main run's sidecar supplies the identity fields.
    write_run_sidecar(
        tmp_path,
        run_id="run-x",
        cmd_sha="c" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-08T00:00:00Z",
        executor="python run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="d" * 64,
        cluster="hoffman2",
        remote_path="/remote",
    )

    def _fake_pull_both(experiment_dir, *, first_canary_run_id, second_canary_run_id, dest_run_id):
        # F6: ONE folded pull returns BOTH canaries' task-0 payloads, landed
        # distinctly under the dest run's pulls dir. A tiny float jitter between
        # the two executions is the observed spread.
        d = fingerprint_store.pulls_dir(experiment_dir, dest_run_id)
        d.mkdir(parents=True, exist_ok=True)
        fa = d / f"{first_canary_run_id}.json"
        fb = d / f"{second_canary_run_id}.json"
        fa.write_text(json.dumps({"loss": 1.0, "steps": 10}), encoding="utf-8")
        fb.write_text(json.dumps({"loss": 1.0002, "steps": 10}), encoding="utf-8")
        return fa, fb

    monkeypatch.setattr(sav, "_pull_both_canary_task0_metrics", _fake_pull_both)

    base = _submit_spec().model_copy(update={"run_id": "run-x"})
    sav._mint_double_canary_sample(
        tmp_path,
        base=base,
        first_canary_run_id="run-x-canary",
        second_canary_run_id="run-x-canary2",
    )

    samples, skipped = fingerprint_store.read_samples(tmp_path, "c" * 64)
    assert skipped == 0
    assert len(samples) == 1
    s = samples[0]
    assert s["source"] == "double-canary"
    assert s["scale"] == "canary"
    assert s["verdict"] == "auto_cleared"
    assert s["same_submission"] is True
    assert s["run_ids"] == ["run-x-canary", "run-x-canary2"]
    assert s["cluster"] == "hoffman2"
    assert s["identity"] == {
        "cmd_sha": "c" * 64,
        "tasks_py_sha": "d" * 64,
        "executor": "python run.py --seed $SEED",
    }
    # per_key carries the observed loss spread (float, nonzero abs_diff).
    loss = next(d for d in s["per_key"] if d["key"] == "loss")
    assert loss["static_class"] == "float"
    assert loss["abs_diff"] > 0


def test_mint_is_best_effort_a_pull_miss_never_raises(tmp_path: Path, monkeypatch) -> None:
    """Evidence minting must never fail a submit whose two canaries both passed —
    a pull miss is warned and swallowed, and no sample lands."""
    from hpc_agent.ops import submit_and_verify as sav
    from hpc_agent.state import fingerprint_store
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id="run-y",
        cmd_sha="e" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-08T00:00:00Z",
        executor="python run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="f" * 64,
        cluster="hoffman2",
        remote_path="/remote",
    )

    def _boom(experiment_dir, *, first_canary_run_id, second_canary_run_id, dest_run_id):
        raise RuntimeError("pull failed")

    monkeypatch.setattr(sav, "_pull_both_canary_task0_metrics", _boom)

    base = _submit_spec().model_copy(update={"run_id": "run-y"})
    # Must NOT raise.
    sav._mint_double_canary_sample(
        tmp_path,
        base=base,
        first_canary_run_id="run-y-canary",
        second_canary_run_id="run-y-canary2",
    )
    samples, _ = fingerprint_store.read_samples(tmp_path, "e" * 64)
    assert samples == []
