"""Backend-level tests for hpc_agent.infra.backends.query.

Complements tests/test_query_batch.py by focusing on the uniform return
shape - especially the error-reporting contract after A4.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from hpc_agent.infra.backends import query as qmod


def _cp(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# query_sacct
# ---------------------------------------------------------------------------


class TestSacctErrorShape:
    def test_timeout_yields_sacct_unavailable(self, monkeypatch):
        def raise_timeout(cmd, *a, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        out = qmod.query_sacct(["111"])
        assert out["tasks"] == {}
        assert len(out["errors"]) == 1
        assert out["errors"][0]["code"] == "sacct_unavailable"
        assert isinstance(out["errors"][0]["detail"], str)

    def test_file_not_found_yields_sacct_unavailable(self, monkeypatch):
        def raise_fnf(cmd, *a, **kw):
            raise FileNotFoundError("no sacct on PATH")

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        out = qmod.query_sacct(["111"])
        assert out["tasks"] == {}
        assert len(out["errors"]) == 1
        assert out["errors"][0]["code"] == "sacct_unavailable"
        assert "not found" in out["errors"][0]["detail"].lower()

    def test_malformed_row_is_skipped_not_raised(self, monkeypatch):
        # Two rows: one malformed (only 2 pipe-separated fields), one valid.
        stdout = "BADROW|PENDING\n999_1|COMPLETED|0:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        # The valid row made it in. JobId_1 (ArrayIndex 1) ingests to
        # HpcTaskId 0 via to_task_id.
        assert 0 in out["tasks"]
        assert out["tasks"][0]["state"] == "COMPLETED"
        # The malformed row was recorded as an error, not raised.
        codes = [e["code"] for e in out["errors"]]
        assert "malformed_row" in codes

    def test_cross_job_contamination_filtered(self, monkeypatch):
        # sacct returns rows for 999 AND an unrelated 888 job -- 888 must not leak.
        stdout = "888_7|COMPLETED|0:0\n999_1|COMPLETED|0:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        # ArrayIndex 1 -> HpcTaskId 0.
        assert 0 in out["tasks"]
        assert out["tasks"][0]["job_id"] == "999"
        # ArrayIndex 7 (HpcTaskId 6) from the unrelated job 888 must NOT appear.
        assert 6 not in out["tasks"]

    def test_happy_path_has_empty_errors(self, monkeypatch):
        stdout = "123_1|COMPLETED|0:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))
        out = qmod.query_sacct(["123"])
        assert out["errors"] == []

    def test_resubmit_newest_array_wins_per_task(self, monkeypatch):
        # Resubmit-extends-job_ids contract: when ``query_sacct`` is
        # called with both the original array and a later resubmit
        # array, and the same task_id appears in both, the newer array's
        # row (later position in the input list) must win — otherwise
        # monitor would surface the prior FAILED state for a task that's
        # currently RUNNING under the resubmit.
        # Failure-mode this guards against:
        #   * sacct emits oldest-first by default, so "first occurrence
        #     wins" would lock in the FAILED row.
        stdout = "111_3|FAILED|1:0\n222_3|RUNNING|0:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["111", "222"])
        # Newest (222) wins, not oldest (111). ArrayIndex 3 -> HpcTaskId 2.
        assert out["tasks"][2]["state"] == "RUNNING"
        assert out["tasks"][2]["job_id"] == "222"

    def test_resubmit_newest_wins_when_sacct_emits_newest_first(self, monkeypatch):
        # Mirror of the above but with sacct emitting newest-first —
        # the newer row arrives BEFORE the older one, so old-style
        # "first occurrence wins" would have accidentally been correct
        # here. The point of this test is to lock in that the new dedup
        # ALSO yields the right answer when row order flips.
        stdout = "222_3|RUNNING|0:0\n111_3|FAILED|1:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["111", "222"])
        # ArrayIndex 3 -> HpcTaskId 2.
        assert out["tasks"][2]["state"] == "RUNNING"
        assert out["tasks"][2]["job_id"] == "222"

    def test_main_record_still_beats_batch_step_within_same_array(self, monkeypatch):
        # The pre-existing dedup rule ("main record comes before
        # .batch/.extern steps") must survive the resubmit-aware
        # rewrite. The main row's State is authoritative; .batch /
        # .extern rows are subordinate per-step accounting.
        stdout = "999_5|COMPLETED|0:0\n999_5.batch|CANCELLED|0:15\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        # ArrayIndex 5 -> HpcTaskId 4.
        assert out["tasks"][4]["state"] == "COMPLETED"


# ---------------------------------------------------------------------------
# query_sge
# ---------------------------------------------------------------------------


class TestSgeErrorShape:
    def test_qstat_and_qacct_both_missing(self, monkeypatch):
        def raise_fnf(cmd, *a, **kw):
            raise FileNotFoundError("no SGE tools")

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        out = qmod.query_sge(["111"], user="u")
        assert out["tasks"] == {}
        codes = {e["code"] for e in out["errors"]}
        # qstat_unavailable should be recorded; sge_unavailable summary also present.
        assert "qstat_unavailable" in codes

    def test_qstat_failure_recorded_but_qacct_can_still_populate(self, monkeypatch):
        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(stdout="", returncode=1, stderr="qstat down")
            if cmd[0] == "qacct":
                block = (
                    "=====\n"
                    f"jobnumber    {cmd[-1]}\n"
                    "taskid       5\n"
                    "exit_status  0\n"
                    "failed       0\n"
                )
                return _cp(stdout=block)
            return _cp(returncode=1)

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["42"], user="u")
        # qacct populated the task map. taskid 5 (ArrayIndex) -> HpcTaskId 4.
        assert 4 in out["tasks"]
        assert out["tasks"][4]["state"] == "COMPLETED"
        # qstat_failed recorded in errors.
        codes = [e["code"] for e in out["errors"]]
        assert "qstat_failed" in codes

    def test_qacct_malformed_row_skipped_not_raised(self, monkeypatch):
        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(stdout="")
            # Malformed block: taskid is not an integer.
            block = (
                "=====\n"
                f"jobnumber    {cmd[-1]}\n"
                "taskid       NOT_AN_INT\n"
                "exit_status  0\n"
                "failed       0\n"
            )
            return _cp(stdout=block)

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["42"], user="u")
        # No valid tasks parsed from that block.
        assert out["tasks"] == {}
        codes = [e["code"] for e in out["errors"]]
        assert "malformed_row" in codes

    def test_all_tools_fail_records_sge_unavailable(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(returncode=1))
        out = qmod.query_sge(["111"], user="u")
        codes = [e["code"] for e in out["errors"]]
        assert "sge_unavailable" in codes
