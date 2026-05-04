"""Tests for batched scheduler polling in claude_hpc.infra.backends.query.

Verify that polling N job IDs issues exactly ONE subprocess call per
scheduler tool (sacct / qstat), that the argv carries the full job list,
and that states map back to the right job IDs.

Asserts the uniform ``{"tasks": ..., "errors": ...}`` return shape.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from claude_hpc.infra.backends import query as qmod

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Recorder:
    """Callable stand-in for subprocess.run that records invocations."""

    def __init__(self, responder):
        self.calls: list[list[str]] = []
        self._responder = responder

    def __call__(self, cmd, *args, **kwargs):
        # Capture a copy so later mutation can't affect assertions.
        self.calls.append(list(cmd))
        return self._responder(cmd)


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Mimic subprocess.CompletedProcess enough for the query module."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# query_sacct
# ---------------------------------------------------------------------------


class TestQuerySacctBatched:
    def test_empty_input_no_subprocess(self, monkeypatch):
        recorder = _Recorder(lambda cmd: _cp(stdout=""))
        monkeypatch.setattr(subprocess, "run", recorder)

        out = qmod.query_sacct([])
        assert out == {"tasks": {}, "errors": []}
        assert recorder.calls == []

    def test_six_job_ids_single_subprocess_call(self, monkeypatch):
        job_ids = [f"100{i}" for i in range(6)]  # ['1000','1001',...,'1005']

        # Fabricate sacct parsable2 output: one row per job/task.
        lines = []
        for idx, jid in enumerate(job_ids):
            # Use a unique task id per job so they don't clash on the map.
            tid = idx + 1
            lines.append(f"{jid}_{tid}|COMPLETED|0:0")
            # Extra step rows - these should be ignored/merged.
            lines.append(f"{jid}_{tid}.batch|COMPLETED|0:0")
        stdout = "\n".join(lines) + "\n"

        recorder = _Recorder(lambda cmd: _cp(stdout=stdout))
        monkeypatch.setattr(subprocess, "run", recorder)

        out = qmod.query_sacct(job_ids)

        # Exactly one subprocess invocation.
        assert len(recorder.calls) == 1, recorder.calls
        argv = recorder.calls[0]
        assert argv[0] == "sacct"

        # The joined job-ID argument must contain every input ID, comma-joined.
        j_idx = argv.index("-j")
        joined = argv[j_idx + 1]
        for jid in job_ids:
            assert jid in joined
        assert "," in joined
        assert joined.count(",") == len(job_ids) - 1

        # Uniform return shape.
        assert set(out.keys()) == {"tasks", "errors"}
        assert out["errors"] == []

        # States mapped back correctly: tid -> job_id.
        result = out["tasks"]
        for idx, jid in enumerate(job_ids):
            tid = idx + 1
            assert tid in result
            assert result[tid]["state"] == "COMPLETED"
            assert result[tid]["job_id"] == jid
            assert result[tid]["exit_code"] == "0:0"

    def test_sacct_failure_returns_error_dict(self, monkeypatch):
        recorder = _Recorder(lambda cmd: _cp(stdout="", returncode=1, stderr="boom"))
        monkeypatch.setattr(subprocess, "run", recorder)

        out = qmod.query_sacct(["111", "222"])
        assert out["tasks"] == {}
        assert out["errors"] and out["errors"][0]["code"] == "sacct_unavailable"
        # Still exactly one batched call (no per-job fallback fan-out).
        assert len(recorder.calls) == 1

    def test_sacct_timeout_graceful(self, monkeypatch):
        def raiser(cmd, *a, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        monkeypatch.setattr(subprocess, "run", raiser)
        out = qmod.query_sacct(["111", "222"])
        assert out["tasks"] == {}
        assert out["errors"] and out["errors"][0]["code"] == "sacct_unavailable"

    def test_sacct_cluster_flag_prepended(self, monkeypatch):
        recorder = _Recorder(lambda cmd: _cp(stdout="999_1|COMPLETED|0:0\n"))
        monkeypatch.setattr(subprocess, "run", recorder)

        qmod.query_sacct(["999"], cluster="foo")
        assert any(a.startswith("--clusters=") for a in recorder.calls[0])


# ---------------------------------------------------------------------------
# query_sge
# ---------------------------------------------------------------------------


class TestQuerySgeBatched:
    def test_empty_input_no_subprocess(self, monkeypatch):
        recorder = _Recorder(lambda cmd: _cp(stdout=""))
        monkeypatch.setattr(subprocess, "run", recorder)

        out = qmod.query_sge([])
        assert out == {"tasks": {}, "errors": []}
        assert recorder.calls == []

    def test_single_qstat_call_for_multiple_jobs(self, monkeypatch):
        job_ids = ["500", "501", "502", "503", "504", "505"]

        # qstat output format: 9+ columns; col 0 = jid, col 4 = state, last = task spec.
        # We give the first 3 jobs running tasks; the others yield qacct hits.
        qstat_lines = [
            "job-ID prior name       user    state submit/start at     queue slots ja-task-ID",
            "------------------------------------------------------------------------",
            # Only rows matching our jid set should be consumed; others ignored.
            "500 0.5 myjob user r 04/16/2026 12:00:00 all.q 1 1-3:1",
            "501 0.5 myjob user r 04/16/2026 12:00:01 all.q 1 4-6:1",
            "502 0.5 myjob user qw 04/16/2026 12:00:02 all.q 1 7-9:1",
        ]
        qstat_stdout = "\n".join(qstat_lines) + "\n"

        def responder(cmd):
            if cmd[0] == "qstat":
                return _cp(stdout=qstat_stdout)
            if cmd[0] == "qacct":
                # Produce a terminated-task block for each queried job.
                jid = cmd[cmd.index("-j") + 1]
                # Map jid -> unique taskid so results are distinguishable.
                tid = 100 + int(jid)
                block = (
                    "==============================================================\n"
                    f"jobnumber    {jid}\n"
                    f"taskid       {tid}\n"
                    "exit_status  0\n"
                    "failed       0\n"
                )
                return _cp(stdout=block)
            return _cp(stdout="", returncode=1)

        recorder = _Recorder(responder)
        monkeypatch.setattr(subprocess, "run", recorder)

        out = qmod.query_sge(job_ids, user="user")
        assert set(out.keys()) == {"tasks", "errors"}
        result = out["tasks"]

        # Exactly ONE qstat call, no matter how many jobs were requested.
        qstat_calls = [c for c in recorder.calls if c[0] == "qstat"]
        assert len(qstat_calls) == 1, qstat_calls
        # And the -u flag reaches it.
        assert "-u" in qstat_calls[0]

        # One qacct call per unique job ID (qacct can't multi-query).
        qacct_calls = [c for c in recorder.calls if c[0] == "qacct"]
        assert len(qacct_calls) == len(set(job_ids))
        # Collect every -j argument - each input job ID must appear exactly once.
        qacct_jids = [c[c.index("-j") + 1] for c in qacct_calls]
        assert sorted(qacct_jids) == sorted(job_ids)

        # States mapped: qstat-driven tasks for running/pending jobs.
        assert result[1]["state"] == "RUNNING"
        assert result[1]["job_id"] == "500"
        assert result[4]["state"] == "RUNNING"
        assert result[4]["job_id"] == "501"
        assert result[7]["state"] == "PENDING"
        assert result[7]["job_id"] == "502"

        # qacct-driven tasks for every job (taskid = 100 + int(jid)).
        for jid in job_ids:
            tid = 100 + int(jid)
            assert tid in result
            assert result[tid]["state"] == "COMPLETED"
            assert result[tid]["job_id"] == jid

    def test_qacct_dedupes_repeated_job_ids(self, monkeypatch):
        """Repeat IDs within a tick should not trigger repeat qacct calls."""

        def responder(cmd):
            if cmd[0] == "qstat":
                return _cp(stdout="")
            # qacct: echo back a task-block for the requested jid.
            jid = cmd[cmd.index("-j") + 1]
            block = (
                "==============================================================\n"
                f"jobnumber    {jid}\n"
                "taskid       1\n"
                "exit_status  0\n"
                "failed       0\n"
            )
            return _cp(stdout=block)

        recorder = _Recorder(responder)
        monkeypatch.setattr(subprocess, "run", recorder)

        # Same job ID five times.
        qmod.query_sge(["42", "42", "42", "42", "42"], user="user")

        qacct_calls = [c for c in recorder.calls if c[0] == "qacct"]
        assert len(qacct_calls) == 1

    def test_sge_unavailable_when_all_tools_fail(self, monkeypatch):
        def responder(cmd):
            return _cp(stdout="", returncode=1)

        recorder = _Recorder(responder)
        monkeypatch.setattr(subprocess, "run", recorder)

        out = qmod.query_sge(["111"], user="user")
        assert out["tasks"] == {}
        # At least one error with code 'sge_unavailable' should be present.
        codes = [e["code"] for e in out["errors"]]
        assert "sge_unavailable" in codes
