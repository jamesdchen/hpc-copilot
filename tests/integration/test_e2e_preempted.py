"""End-to-end test for the preempted-state plumbing.

Wires together the cluster-side dispatcher's SIGTERM trap with the
agent-surface's failure-clustering and envelope-key path:

  dispatch.py exit 130 + SIGTERM stderr line
   → ops.recover.failure_signatures.classify finds the 'preempted' pattern
   → ops.recover.runner_failures.cluster_failures_by_fingerprint groups all bumped tasks
   → ops.recover.failures_atom.fetch_failures surfaces preempted_count /
     preempted_task_ids on the envelope
   → cmd_failures (and now cmd_status) carry those keys

The intent: a campus user whose low-priority job got bumped sees a
single, cohesive 'preempted' diagnostic across every surface, instead
of one cluster-side mechanism contradicting another.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from hpc_agent.ops.recover.failure_signatures import classify
from hpc_agent.ops.recover.runner_failures import cluster_failures_by_fingerprint
from hpc_agent.state import run_record
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


def _log_entry(task_id: int, *, content: str = "", exit_code: int | None = None) -> dict:
    out: dict = {"task_id": task_id, "content": content}
    if exit_code is not None:
        out["exit_code"] = exit_code
    return out


class TestCategorizeRecognisesPreemption:
    def test_sigterm_stderr_line_categorises_as_preempted(self) -> None:
        """The dispatcher's SIGTERM-trap stderr line is the canonical
        signal — every other surface keys off the 'preempted' cluster
        bucket that this match populates."""
        stderr = (
            "starting task 0\n"
            "running executor\n"
            "[hpc-agent] SIGTERM received; cluster preemption imminent\n"
        )
        assert classify(stderr, None)["error_class"] == "preempted"

    def test_exit_code_130_is_a_fallback_when_stderr_clipped(self) -> None:
        """If the stderr tail was clipped before the SIGTERM line lands,
        cluster_failures_by_fingerprint still maps exit-130 tasks to the
        'preempted' cluster — exit 130 is a definitive signal even
        without the stderr breadcrumb."""
        logs = [
            _log_entry(0, content="some unrelated trailing line", exit_code=130),
        ]
        clusters = cluster_failures_by_fingerprint(logs)
        assert len(clusters) == 1
        # The runner classifies exit-130-without-stderr as preempted via
        # the explicit fallback in cluster_failures_by_fingerprint.
        assert clusters[0]["category"] == "preempted"


class TestClusterFailuresByFingerprintGroupsPreempted:
    def test_multiple_preempted_tasks_collapse_into_one_cluster(self) -> None:
        """Three tasks all bumped by the same SIGTERM stderr land in a
        single 'preempted' cluster, with all three task ids surfaced."""
        sigterm_line = "[hpc-agent] SIGTERM received; cluster preemption imminent"
        logs = [
            _log_entry(0, content=f"trace line\n{sigterm_line}\n"),
            _log_entry(1, content=f"trace line\n{sigterm_line}\n"),
            _log_entry(2, content=f"trace line\n{sigterm_line}\n"),
        ]
        clusters = cluster_failures_by_fingerprint(logs)
        # Find the preempted cluster (ordering by count, single bucket here).
        preempted = [c for c in clusters if c.get("category") == "preempted"]
        assert len(preempted) == 1, clusters
        assert sorted(preempted[0]["task_ids"]) == [0, 1, 2]
        assert preempted[0]["count"] == 3

    def test_mixed_real_failures_and_preempted_separate(self) -> None:
        """A real OOM and two preempted tasks must NOT collapse — the
        campus user needs the diagnostic to stay legible."""
        sigterm_line = "[hpc-agent] SIGTERM received; cluster preemption imminent"
        logs = [
            _log_entry(0, content="torch.cuda.OutOfMemoryError: CUDA out of memory."),
            _log_entry(1, content=f"work\n{sigterm_line}\n"),
            _log_entry(2, content=f"work\n{sigterm_line}\n"),
        ]
        clusters = cluster_failures_by_fingerprint(logs)
        cats = {c.get("category") for c in clusters}
        assert "preempted" in cats
        assert "gpu_oom" in cats


class TestFailuresEnvelopeSurfacesPreemptedKeys:
    """The ops/recover/failures_atom.py envelope walks the cluster set and surfaces
    preempted_count + preempted_task_ids at the top level so a harness
    can branch without parsing per-cluster ``error_class`` strings."""

    def test_fetch_failures_surfaces_preempted_keys_in_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive the real ``fetch_failures`` atom with mocked SSH
        primitives and assert that ``preempted_count`` and
        ``preempted_task_ids`` appear at the envelope top level.

        This is the inverse of the previous tautological test, which
        re-implemented the production loop in the test body and never
        called the atom under test."""
        from hpc_agent.ops.recover import failures_atom

        # Redirect HPC_HOMEDIR for the journal write (both bindings —
        # see tests/state/test_session.py for the rationale).
        home = tmp_path / "home_hpc"
        monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)

        experiment = tmp_path / "exp"
        experiment.mkdir()
        record = RunRecord(
            run_id="r1-preempted",
            profile="p",
            cluster="hoffman2",
            ssh_target="user@h",
            remote_path="/x",
            job_name="j",
            job_ids=["job_42"],
            total_tasks=3,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment.resolve()),
        )
        upsert_run(experiment, record)

        # Mock the SSH primitives: three failed tasks, all preempted.
        # ``_ssh_status_report`` is imported directly into failures_atom
        # (canonical path: hpc_agent.ops.monitor.status._ssh_status_report);
        # patch the binding on the consuming module, not the runner facade.
        monkeypatch.setattr(
            failures_atom,
            "_ssh_status_report",
            lambda **_: {
                "tasks": {
                    "0": {"status": "failed"},
                    "1": {"status": "failed"},
                    "2": {"status": "failed"},
                }
            },
        )
        sigterm_line = "[hpc-agent] SIGTERM received; cluster preemption imminent"
        monkeypatch.setattr(
            failures_atom,
            "fetch_task_logs",
            lambda **_: [
                _log_entry(0, content=f"trace\n{sigterm_line}\n"),
                _log_entry(1, content=f"trace\n{sigterm_line}\n"),
                _log_entry(2, content=f"trace\n{sigterm_line}\n"),
            ],
        )

        # Call the production atom — this is what was missing before.
        envelope = failures_atom.fetch_failures(experiment_dir=experiment, run_id="r1-preempted")

        # Top-level envelope keys are what a harness branches on.
        assert envelope["run_id"] == "r1-preempted"
        assert envelope["failed_count"] == 3
        assert envelope["preempted_count"] == 3
        assert envelope["preempted_task_ids"] == [0, 1, 2]
        # Sanity: the underlying cluster shape still carries the
        # preempted category so per-cluster consumers keep working.
        assert any(c.get("error_class") == "preempted" for c in envelope["clusters"])


class TestTaskIdSpaceSeam:
    """Phase-2 (#301) boundary test: drive the REAL scheduler-query ingest
    edge with a 1-based array row and assert the reporter's output and the
    resubmit submit edge share one 0-based ``HpcTaskId`` space — closing the
    /failures→/resubmit off-by-one at the seam rather than by mocking the
    already-converted shape."""

    def test_reporter_output_and_resubmit_input_share_hpctaskid_space(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hpc_agent.infra.backends import query as qmod
        from hpc_agent.models.mapreduce.reduce.status import report_status
        from hpc_agent.ops.recover.batching import resubmit_plan

        # The scheduler reports array index 2 (1-based ArrayIndex) as a
        # preemption (exit 130). sacct emits ``<job>_<array_idx>``.
        stdout = "777_2|FAILED|130:0\n"
        monkeypatch.setattr(
            qmod.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(stdout=stdout, stderr="", returncode=0),
        )

        # Ingest edge: query_sacct converts ArrayIndex 2 → HpcTaskId 1.
        report = report_status(
            result_dir=tmp_path, job_ids=["777"], total_tasks=3, scheduler="slurm"
        )
        # Reporter surfaces the preempted id in the 0-based domain space.
        assert report["preempted_task_ids"] == [1]
        assert report["tasks"]["1"]["status"] == "failed"

        # The very same id flows into resubmit with NO compensating shift,
        # and the submit edge maps it back to the original ArrayIndex 2 —
        # the round-trip closes, so task k is resubmitted as exactly task k.
        plan = resubmit_plan(task_count=3, failed_task_ids=report["preempted_task_ids"])
        assert plan.batches[0].task_ids == (1,)
        assert plan.batches[0].task_range == "2"
