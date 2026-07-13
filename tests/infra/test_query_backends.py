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


class TestSacctNonArrayJob:
    """#293 single multi-rank MPI jobs submit with ``array=False``: sacct
    reports a PLAIN JobID (no ``_``) plus its step rows. Those rows must
    ingest as the run's single task 0 — dropping them left a failed MPI
    run with no accounting state at all (``tasks={}`` and no error)."""

    def test_plain_jobid_rows_map_to_task_zero(self, monkeypatch):
        stdout = (
            "12345|FAILED|1:0|3600|8|cpu=8,gres/gpu=2\n"
            "12345.batch|FAILED|1:0|3600|8|cpu=8\n"
            "12345.extern|COMPLETED|0:0|3600|8|\n"
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["12345"])
        assert set(out["tasks"]) == {0}
        task = out["tasks"][0]
        # The main record wins; .batch/.extern steps dedup into it exactly
        # like array step rows do.
        assert task["state"] == "FAILED"
        assert task["exit_code"] == "1:0"
        assert task["job_id"] == "12345"
        assert task["elapsed_s"] == 3600
        assert task["cpu_s"] == 8 * 3600
        assert task["gpu_s"] == 2 * 3600
        assert out["errors"] == []

    def test_unrelated_plain_jobid_rows_filtered(self, monkeypatch):
        # The job_ids scoping applies to plain rows too — an unrelated
        # non-array job must not be ingested as this run's task 0.
        stdout = "888|COMPLETED|0:0\n999_1|COMPLETED|0:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        assert out["tasks"][0]["job_id"] == "999"
        assert all(t["job_id"] != "888" for t in out["tasks"].values())

    def test_non_array_resubmit_newest_wins(self, monkeypatch):
        # Same newest-wins dedup as array rows: a resubmitted MPI job's
        # newer attempt (later in job_ids) overrides the prior failure.
        stdout = "111|FAILED|1:0\n222|COMPLETED|0:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["111", "222"])
        assert out["tasks"][0]["state"] == "COMPLETED"
        assert out["tasks"][0]["job_id"] == "222"


class TestSacctPendingAggregate:
    """sacct collapses not-yet-started array elements into a single
    ``<jobid>_[<spec>]`` PENDING row (#7). These must expand to per-index
    pending tasks, never a dropped ``malformed_row``.
    """

    def test_pending_aggregate_expands_to_all_indices(self, monkeypatch):
        # 1-based ArrayIndex [1-10] -> 0-based HpcTaskId 0..9, all PENDING.
        stdout = "999_[1-10]|PENDING|0:0|0|4|\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        assert out["errors"] == []
        assert set(out["tasks"]) == set(range(10))  # 0-based keys 0..9
        t0 = out["tasks"][0]
        assert t0["state"] == "PENDING"
        assert t0["job_id"] == "999"
        assert t0["elapsed_s"] == 0
        assert t0["cpu_s"] == 0
        assert t0["gpu_s"] == 0

    def test_throttled_pending_aggregate_strips_percent_limit(self, monkeypatch):
        # The ``%2`` concurrency throttle must be stripped, not parsed as an id.
        stdout = "999_[1-10%2]|PENDING|0:0|0|4|\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        assert out["errors"] == []
        assert set(out["tasks"]) == set(range(10))

    def test_comma_and_step_bracket_spec_expands(self, monkeypatch):
        # Mixed comma list + stepped range inside the bracket.
        stdout = "999_[1,5,7-9:2]|PENDING|0:0|0|4|\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        assert out["errors"] == []
        # 1,5,7,9 (1-based) -> 0,4,6,8 (0-based)
        assert set(out["tasks"]) == {0, 4, 6, 8}

    def test_mixed_running_and_pending_aggregate(self, monkeypatch):
        # Some tasks started (999_1 RUNNING) while the remainder is aggregated
        # pending (999_[2-4]) — disjoint sets, no malformed_row, correct keys.
        stdout = "999_1|RUNNING|0:0|10|4|\n999_[2-4]|PENDING|0:0|0|4|\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        assert out["errors"] == []
        assert out["tasks"][0]["state"] == "RUNNING"  # ArrayIndex 1 -> tid 0
        assert out["tasks"][1]["state"] == "PENDING"  # ArrayIndex 2 -> tid 1
        assert out["tasks"][3]["state"] == "PENDING"  # ArrayIndex 4 -> tid 3
        assert set(out["tasks"]) == {0, 1, 2, 3}

    def test_genuinely_malformed_bracket_still_reports_error(self, monkeypatch):
        # A bracket with a non-numeric token is neither an int nor a valid
        # spec — it must still fall through to malformed_row.
        stdout = "999_[abc]|PENDING|0:0|0|4|\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))

        out = qmod.query_sacct(["999"])
        assert out["tasks"] == {}
        assert [e["code"] for e in out["errors"]] == ["malformed_row"]


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


def _qacct_block(taskid: int, exit_status: int) -> str:
    return f"=====\ntaskid       {taskid}\nexit_status  {exit_status}\nfailed       0\n"


class TestSgeResubmitDedup:
    """The run record's ``job_ids`` are ordered oldest→newest (resubmits
    append), and consumers that prefer the most-recent attempt rely on
    that order — mirroring query_sacct's newest-wins dedup. A task that
    failed in job A and completed in resubmitted job B must report the
    NEWER attempt, while live qstat data still beats qacct accounting."""

    def test_resubmitted_task_reports_newest_attempt(self, monkeypatch):
        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(stdout="")
            # qacct: the old array's attempt failed, the resubmit completed.
            if cmd[-1] == "111":
                return _cp(stdout=_qacct_block(3, 1))
            return _cp(stdout=_qacct_block(3, 0))

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["111", "222"], user="u")
        # taskid 3 (ArrayIndex) -> HpcTaskId 2. Newest (222) wins.
        assert out["tasks"][2]["state"] == "COMPLETED"
        assert out["tasks"][2]["job_id"] == "222"

    def test_live_qstat_still_beats_qacct_accounting(self, monkeypatch):
        # Task 3 is RUNNING under the resubmit (live qstat) while the old
        # array's qacct row says FAILED — the live state must survive
        # phase 2 regardless of job order.
        qstat_out = "222 0.55555 ml u r 07/09/2026 10:00:00 all.q@n1 1 3\n"

        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(stdout=qstat_out)
            if cmd[-1] == "111":
                return _cp(stdout=_qacct_block(3, 1))
            return _cp(returncode=1)  # resubmit not yet in accounting

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["111", "222"], user="u")
        assert out["tasks"][2]["state"] == "RUNNING"
        assert out["tasks"][2]["job_id"] == "222"

    def test_first_block_still_wins_within_one_job(self, monkeypatch):
        # Within a single qacct buffer (one job) the pre-existing
        # first-block-wins rule is preserved.
        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(stdout="")
            return _cp(stdout=_qacct_block(3, 0) + _qacct_block(3, 1))

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["111"], user="u")
        assert out["tasks"][2]["state"] == "COMPLETED"
