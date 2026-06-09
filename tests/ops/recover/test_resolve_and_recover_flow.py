"""Composite tests for the #240 resolve-and-recover auto-fire.

The resolver (:func:`resolve`) and the features glue are unit-tested in
``test_resolve.py`` / ``test_features_glue.py``. This file pins the *composite*
that routes a per-cluster :class:`Resolution` into a resubmit (an applicable
code verdict) or a parked escalation (a judgement verdict, or a code verdict
``resubmit_flow`` cannot enact): the failure fetch and ``resubmit_flow`` are
injected, so these tests assert the wiring without touching a cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent.ops.resolve_and_recover_flow import (
    _coerce_task_ids,
    _concrete_overrides,
    maybe_resolve_and_recover,
)
from hpc_agent.state import run_record
from hpc_agent.state.journal import is_held, load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

_RUN_ID = "20260606-120000-aaa"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_record(experiment_dir: Path, **overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "myjob",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-06-06T12:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "script": ".hpc/templates/cpu_array.sh",
        "backend": "slurm",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
        "auto_recover_on_failure": True,
        "max_auto_recovers": 2,
        "auto_recover_count": 0,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _write_sidecar(experiment_dir: Path, *, resources: dict[str, Any] | None = None) -> None:
    """Write a minimal run sidecar so the resolver/glue have a resource_spec.

    The translatable fixes (increase-mem* / increase-walltime) scale the current
    ``resources`` knob, so a code verdict only resubmits when the sidecar
    supplies the relevant ``mem_mb`` / ``walltime_sec``.
    """
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.26",
        submitted_at="2026-06-06T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
        resources=resources or None,
    )


def _fetcher(clusters: list[dict[str, Any]]):
    """Build a failures_fetcher stub returning *clusters* as the report."""

    def _fetch(*, experiment_dir: Path, run_id: str, **kw: Any) -> dict[str, Any]:
        return {"run_id": run_id, "failed_count": 0, "clusters": list(clusters)}

    return _fetch


def _cluster(error_class: str, *, task_ids: list[Any], fingerprint: str = "fp") -> dict[str, Any]:
    return {
        "error_class": error_class,
        "category": error_class,
        "fingerprint": fingerprint,
        "task_ids": list(task_ids),
    }


class _Recorder:
    """Records resubmit() calls and returns a stub result."""

    def __init__(self, *, deduped: bool = False, new_job_ids: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._deduped = deduped
        self._new_job_ids = new_job_ids or ["9100"]

    def __call__(self, experiment_dir: Path, run_id: str, **kwargs: Any) -> Any:
        self.calls.append({"experiment_dir": experiment_dir, "run_id": run_id, **kwargs})

        class _Result:
            deduped = self._deduped
            cluster_submitted = True
            new_job_ids = list(self._new_job_ids)

        return _Result()


# ── _concrete_overrides: suggested-fix → concrete resubmit overrides ──────────


def test_concrete_overrides_scales_mem() -> None:
    assert _concrete_overrides(
        {"action": "increase-mem-per-gpu", "factor": 1.5}, {"mem_mb": 4000}
    ) == {"mem_mb": 6000}
    assert _concrete_overrides({"action": "increase-mem", "factor": 2.0}, {"mem_mb": 1000}) == {
        "mem_mb": 2000
    }


def test_concrete_overrides_scales_walltime() -> None:
    assert _concrete_overrides(
        {"action": "increase-walltime", "factor": 1.5}, {"walltime_sec": 3600}
    ) == {"walltime_sec": 5400}


def test_concrete_overrides_retry_different_node_needs_no_change() -> None:
    assert _concrete_overrides({"action": "retry-on-different-node"}, None) == {}


def test_concrete_overrides_none_for_task_kwarg_fixes() -> None:
    # increase-parallelism / reduce-width change a task kwarg, not a scheduler
    # flag — resubmit_flow cannot enact them.
    assert (
        _concrete_overrides({"action": "increase-parallelism", "knob": "tp_size", "factor": 2}, {})
        is None
    )
    assert _concrete_overrides({"action": "reduce-width", "factor": 0.5}, {}) is None


def test_concrete_overrides_none_without_a_resource_to_scale() -> None:
    # A factor fix with no current mem_mb/walltime_sec cannot be made concrete.
    assert _concrete_overrides({"action": "increase-mem", "factor": 1.5}, {}) is None
    assert _concrete_overrides({"action": "increase-walltime", "factor": 1.5}, None) is None


def test_coerce_task_ids_rejects_non_int() -> None:
    assert _coerce_task_ids([0, 1, 2]) == [0, 1, 2]
    assert _coerce_task_ids(["3", 4]) == [3, 4]
    assert _coerce_task_ids([0, "not-an-int"]) is None
    assert _coerce_task_ids([True]) is None  # bool is not a task id


# ── code verdict (applicable) → auto-resubmit with translated overrides ───────


def test_code_verdict_resubmits_with_translated_mem_override(
    journal_home: Path, experiment: Path
) -> None:
    """gpu_oom with no parallelism/width context → catalog fix
    increase-mem-per-gpu; translated against the sidecar's current mem_mb and
    auto-resubmitted with a concrete mem_mb override."""
    _seed_record(experiment)
    _write_sidecar(experiment, resources={"mem_mb": 4000})
    rec = _Recorder(new_job_ids=["9100", "9101"])
    fetch = _fetcher([_cluster("gpu_oom", task_ids=[0, 1])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert len(outcome.resubmitted) == 1
    c = outcome.resubmitted[0]
    assert c.decided_by == "code"
    assert c.overrides == {"mem_mb": 6000}  # 4000 * 1.5
    assert c.new_job_ids == ["9100", "9101"]

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["failed_task_ids"] == [0, 1]
    assert call["category"] == "gpu_oom"
    assert call["overrides"] == {"mem_mb": 6000}
    assert call["from_checkpoint"] is True
    assert call["submit_to_cluster"] is True
    assert call["script"] == ".hpc/templates/cpu_array.sh"
    assert call["backend"] == "slurm"
    assert call["job_name"] == "myjob"

    assert load_run(experiment, _RUN_ID).auto_recover_count == 1
    assert outcome.auto_recover_count == 1


def test_walltime_verdict_translates_against_current_walltime(
    journal_home: Path, experiment: Path
) -> None:
    _seed_record(experiment)
    _write_sidecar(experiment, resources={"walltime_sec": 3600})
    rec = _Recorder()
    fetch = _fetcher([_cluster("walltime", task_ids=[0])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert len(outcome.resubmitted) == 1
    assert rec.calls[0]["overrides"] == {"walltime_sec": 5400}  # 3600 * 1.5


def test_node_failure_resubmits_with_empty_overrides(journal_home: Path, experiment: Path) -> None:
    """retry-on-different-node needs no resource change — a fresh resubmit IS the
    fix, so it auto-acts even without a sidecar."""
    _seed_record(experiment)
    rec = _Recorder()
    fetch = _fetcher([_cluster("node_failure", task_ids=[0])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert len(outcome.resubmitted) == 1
    assert rec.calls[0]["overrides"] == {}


# ── code verdict resubmit_flow can't enact → surfaced, not resubmitted ────────


def test_reshard_verdict_is_surfaced_not_resubmitted(journal_home: Path, experiment: Path) -> None:
    """gpu_oom at tp_size=2 → increase-parallelism. That changes a task kwarg,
    which resubmit_flow cannot apply, so the deterministic fix is surfaced as a
    decided_by="code" escalation (parked) rather than resubmitted-identical."""
    _seed_record(experiment)
    _write_sidecar(experiment, resources={"mem_mb": 4000})
    from hpc_agent.state.runs import write_run_sidecar

    # Stamp tp_size into the spec_kwargs pocket the resolver discriminates on.
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.10.26",
        submitted_at="2026-06-06T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
        resources={"mem_mb": 4000},
        extra={"spec_kwargs": {"tp_size": 2}},
    )
    rec = _Recorder()
    fetch = _fetcher([_cluster("gpu_oom", task_ids=[0])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert rec.calls == []  # the reshard cannot be auto-applied
    assert len(outcome.held) == 1
    held = outcome.held[0]
    assert held.decided_by == "code"
    assert held.escalation is not None
    assert held.escalation.candidate_actions[0].action == "increase-parallelism"
    assert "not auto-applicable" in held.reason
    # The run is parked on the surfaced verdict.
    assert is_held(load_run(experiment, _RUN_ID))


def test_non_integer_task_id_escalates_without_crashing(
    journal_home: Path, experiment: Path
) -> None:
    """A non-int task id must not crash the unattended loop with a ValueError —
    the whole cluster is surfaced as a decided_by="code" escalation instead."""
    _seed_record(experiment)
    _write_sidecar(experiment, resources={"mem_mb": 4000})
    rec = _Recorder()
    fetch = _fetcher([_cluster("gpu_oom", task_ids=["not-an-int"])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert rec.calls == []  # never resubmitted
    assert len(outcome.held) == 1
    assert outcome.held[0].decided_by == "code"
    assert "non-integer task id" in outcome.held[0].reason
    assert is_held(load_run(experiment, _RUN_ID))


def test_applicable_fix_without_sidecar_resource_escalates(
    journal_home: Path, experiment: Path
) -> None:
    """gpu_oom → increase-mem-per-gpu, but with no sidecar mem_mb to scale the
    factor against, the fix can't be made concrete → surfaced, not resubmitted."""
    _seed_record(experiment)  # no sidecar written
    rec = _Recorder()
    fetch = _fetcher([_cluster("gpu_oom", task_ids=[0])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert rec.calls == []
    assert len(outcome.held) == 1
    assert outcome.held[0].decided_by == "code"


# ── judgement verdict → park + surface, run shows held ────────────────────────


def test_judgement_verdict_marks_pending_and_run_is_held(
    journal_home: Path, experiment: Path
) -> None:
    _seed_record(experiment)
    rec = _Recorder()
    fetch = _fetcher([_cluster("code_bug", task_ids=[0, 1], fingerprint="fp-bug")])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert rec.calls == []  # judgement never auto-resubmits
    assert len(outcome.held) == 1
    held = outcome.held[0]
    assert held.decided_by == "judgement"
    assert held.escalation is not None
    assert held.escalation.cluster.fingerprint == "fp-bug"

    # The run is now parked on a pending verdict.
    record = load_run(experiment, _RUN_ID)
    assert is_held(record)
    assert record.pending_verdict.get("decided_by") == "judgement"


# ── exhausted strategy → escalates (not a re-loop) ────────────────────────────


def test_exhausted_strategy_escalates(journal_home: Path, experiment: Path) -> None:
    """gpu_oom whose increase-mem-per-gpu was already tried this episode (recorded
    in record.retries) → the resolver escalates rather than looping the fix, and
    the composite parks it."""
    _seed_record(
        experiment,
        retries={
            "0": {
                "attempts": 1,
                "category": "gpu_oom",
                "overrides": {"action": "increase-mem-per-gpu", "factor": 1.5},
            }
        },
    )
    rec = _Recorder()
    fetch = _fetcher([_cluster("gpu_oom", task_ids=[0])])

    outcome = maybe_resolve_and_recover(
        experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch, max_code_attempts=1
    )

    assert rec.calls == []
    assert len(outcome.held) == 1
    assert outcome.held[0].decided_by == "judgement"
    assert is_held(load_run(experiment, _RUN_ID))


# ── preempted cluster NOT handled by this composite ───────────────────────────


def test_preempted_cluster_is_skipped(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    rec = _Recorder()
    fetch = _fetcher([_cluster("preempted", task_ids=[0, 1])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert rec.calls == []
    assert len(outcome.skipped) == 1
    assert outcome.skipped[0].error_class == "preempted"
    assert not is_held(load_run(experiment, _RUN_ID))  # not parked either


# ── opt-in OFF → no side effect, but verdict surfaced ─────────────────────────


def test_opt_in_off_surfaces_verdict_without_side_effect(
    journal_home: Path, experiment: Path
) -> None:
    _seed_record(experiment, auto_recover_on_failure=False)
    _write_sidecar(experiment, resources={"mem_mb": 4000})
    rec = _Recorder()
    fetch = _fetcher(
        [
            _cluster("gpu_oom", task_ids=[0], fingerprint="fp-oom"),
            _cluster("code_bug", task_ids=[1], fingerprint="fp-bug"),
        ]
    )

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    # No resubmit, no park.
    assert rec.calls == []
    record = load_run(experiment, _RUN_ID)
    assert not is_held(record)
    assert record.auto_recover_count == 0

    # But both verdicts are still surfaced as data.
    dispositions = {c.disposition for c in outcome.clusters}
    assert dispositions == {"verdict_only"}
    by_class = {c.error_class: c for c in outcome.clusters}
    assert by_class["gpu_oom"].decided_by == "code"
    assert by_class["gpu_oom"].overrides == {"mem_mb": 6000}  # translated, surfaced
    assert by_class["code_bug"].decided_by == "judgement"
    assert by_class["code_bug"].escalation is not None


# ── multiple clusters → one escalation does not block another's resubmit ──────


def test_one_escalation_does_not_block_other_resubmit(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment, max_auto_recovers=5)
    _write_sidecar(experiment, resources={"mem_mb": 4000, "walltime_sec": 3600})
    rec = _Recorder()
    fetch = _fetcher(
        [
            _cluster("code_bug", task_ids=[0], fingerprint="fp-bug"),  # judgement → park
            _cluster("gpu_oom", task_ids=[1], fingerprint="fp-oom"),  # code → resubmit (mem)
            _cluster("walltime", task_ids=[2], fingerprint="fp-wall"),  # code → resubmit (walltime)
        ]
    )

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    # The judgement cluster parked, the two code clusters BOTH resubmitted.
    assert len(outcome.held) == 1
    assert outcome.held[0].error_class == "code_bug"
    assert {c.error_class for c in outcome.resubmitted} == {"gpu_oom", "walltime"}
    assert len(rec.calls) == 2
    assert is_held(load_run(experiment, _RUN_ID))  # the run is parked from the code_bug
    assert load_run(experiment, _RUN_ID).auto_recover_count == 2


# ── cap reached → park instead of looping a fix ───────────────────────────────


def test_cap_reached_parks_code_verdict(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment, max_auto_recovers=1, auto_recover_count=1)
    _write_sidecar(experiment, resources={"mem_mb": 4000})
    rec = _Recorder()
    fetch = _fetcher([_cluster("gpu_oom", task_ids=[0])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert rec.calls == []
    assert len(outcome.held) == 1
    assert "cap reached" in outcome.held[0].reason
    assert load_run(experiment, _RUN_ID).auto_recover_count == 1


# ── deduped replay does not consume a cap slot ────────────────────────────────


def test_deduped_replay_does_not_increment_count(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    _write_sidecar(experiment, resources={"mem_mb": 4000})
    rec = _Recorder(deduped=True)
    fetch = _fetcher([_cluster("gpu_oom", task_ids=[0])])

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert len(outcome.resubmitted) == 1
    assert len(rec.calls) == 1
    assert load_run(experiment, _RUN_ID).auto_recover_count == 0


# ── distinct request_id per fired recover ─────────────────────────────────────


def test_request_id_distinct_per_attempt(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment, max_auto_recovers=5)
    _write_sidecar(experiment, resources={"mem_mb": 4000})
    rec = _Recorder()
    fetch = _fetcher([_cluster("gpu_oom", task_ids=[0])])

    maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)
    maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=fetch)

    assert rec.calls[0]["request_id"] != rec.calls[1]["request_id"]
    assert load_run(experiment, _RUN_ID).auto_recover_count == 2


# ── graceful escalate paths (no crash) ────────────────────────────────────────


def test_fetch_error_surfaces_reason(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    rec = _Recorder()

    def _boom(**kw: Any) -> dict[str, Any]:
        raise errors.SshUnreachable("ssh down")

    outcome = maybe_resolve_and_recover(experiment, _RUN_ID, resubmit=rec, failures_fetcher=_boom)

    assert outcome.clusters == ()
    assert "could not fetch cluster failures" in outcome.reason
    assert rec.calls == []


def test_no_record_surfaces_reason(journal_home: Path, experiment: Path) -> None:
    rec = _Recorder()
    outcome = maybe_resolve_and_recover(
        experiment, "nonexistent-run", resubmit=rec, failures_fetcher=_fetcher([])
    )
    assert "no journal record" in outcome.reason
    assert rec.calls == []


def test_no_failed_clusters_is_clean_noop(journal_home: Path, experiment: Path) -> None:
    _seed_record(experiment)
    rec = _Recorder()
    outcome = maybe_resolve_and_recover(
        experiment, _RUN_ID, resubmit=rec, failures_fetcher=_fetcher([])
    )
    assert outcome.clusters == ()
    assert rec.calls == []
