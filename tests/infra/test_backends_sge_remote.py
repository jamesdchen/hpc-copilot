"""Backend contract tests for :class:`RemoteSGEBackend`.

The remote SGE backend wraps each ``qsub`` invocation in an SSH call via
a caller-provided ``ssh_run`` callable.  These tests exercise the shape
of the string passed to ``ssh_run`` and the stdout parsing that turns an
SGE ``qsub`` response into a job ID.
"""

from __future__ import annotations

import shlex
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends.sge_remote import RemoteSGEBackend
from hpc_agent.infra.throughput import JobBatch, SubmissionPlan


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _single_batch_plan(start: int = 1, end: int = 1) -> SubmissionPlan:
    """A one-wave, one-batch plan covering tasks ``start..end`` (1-based)."""
    batch = JobBatch(
        batch_index=0,
        task_start=start,
        task_end=end,
        array_size=end - start + 1,
        est_wall_s=None,
        wave=0,
    )
    return SubmissionPlan(
        batches=[batch],
        total_tasks=end - start + 1,
        total_batches=1,
        max_concurrent=1,
        est_total_wall_s=None,
        strategy="test",
    )


class _SSHRecorder:
    """Minimal ssh_run stub that records every invocation and returns a canned
    CompletedProcess from a provided responder."""

    def __init__(self, responder):
        self.calls: list[str] = []
        self._responder = responder

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(cmd)
        return self._responder(cmd)


# ---------------------------------------------------------------------------
# constructor validation
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_missing_script_raises(self):
        with pytest.raises(errors.SpecInvalid, match="script"):
            RemoteSGEBackend(
                ssh_run=lambda cmd: _cp(),
                remote_repo="/tmp",
            )

    def test_missing_ssh_run_raises(self):
        with pytest.raises(errors.SpecInvalid, match="ssh_run"):
            RemoteSGEBackend(script="job.sh", remote_repo="/tmp")

    def test_missing_remote_repo_raises(self):
        with pytest.raises(errors.SpecInvalid, match="remote_repo"):
            RemoteSGEBackend(script="job.sh", ssh_run=lambda cmd: _cp())


# ---------------------------------------------------------------------------
# submit_plan builds an SSH-wrapped qsub command
# ---------------------------------------------------------------------------


class TestSSHWrappedCommand:
    def test_submit_plan_ssh_command_shape(self, tmp_path):
        def responder(cmd):
            return _cp(
                stdout='Your job-array 42.1-10:1 ("probe") has been submitted\n',
                returncode=0,
            )

        recorder = _SSHRecorder(responder)
        backend = RemoteSGEBackend(
            script="/remote/path/job.sh",
            ssh_run=recorder,
            remote_repo="/remote/path",
            log_dir="/remote/path/logs",
        )

        out = backend.submit_plan(
            _single_batch_plan(1, 10),
            "probe",
            job_env={},
            cwd=tmp_path,
        )
        assert out == [(0, "1-10", "42")]

        # ssh_run was called at least once for `mkdir -p <log_dir>` and then
        # once for qsub itself.
        mkdir_calls = [c for c in recorder.calls if c.startswith("mkdir -p")]
        assert mkdir_calls, f"Expected a mkdir call, got {recorder.calls!r}"

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        assert len(qsub_calls) == 1
        remote_cmd = qsub_calls[0]

        # The command string must cd into the remote repo before running qsub.
        assert "cd /remote/path" in remote_cmd
        # Must contain the qsub flags in the expected form.
        assert "qsub" in remote_cmd
        assert "-t 1-10" in remote_cmd
        assert "-N probe" in remote_cmd
        assert "-o /remote/path/logs" in remote_cmd
        assert "-j y" in remote_cmd
        # Script path is present.
        assert "/remote/path/job.sh" in remote_cmd

    def test_submit_leg_runs_under_the_non_idempotent_scope(self, tmp_path):
        """F54/F55: the scheduler-submit leg (_execute_command) must mark its
        ssh_run NON-idempotent so a client timeout / post-dispatch engine failure
        on the qsub is not re-executed (duplicate array). The idempotent-by-default
        mkdir (_setup_log_dir) stays idempotent. Assert the ambient flag the real
        ssh_run reads is False during the qsub call and True during the mkdir."""
        from hpc_agent.infra import remote as _remote

        seen: dict[str, bool] = {}

        def responder(cmd):
            # Record the ambient idempotence flag the real ssh_run would read.
            key = "qsub" if "qsub" in cmd else ("mkdir" if cmd.startswith("mkdir") else "other")
            seen[key] = _remote._CURRENT_IDEMPOTENT.get()
            return _cp(
                stdout='Your job-array 42.1-10:1 ("probe") has been submitted\n',
                returncode=0,
            )

        backend = RemoteSGEBackend(
            script="/remote/path/job.sh",
            ssh_run=_SSHRecorder(responder),
            remote_repo="/remote/path",
            log_dir="/remote/path/logs",
        )
        backend.submit_plan(_single_batch_plan(1, 10), "probe", job_env={}, cwd=tmp_path)
        assert seen.get("qsub") is False, "the qsub submit leg must run non-idempotent"
        assert seen.get("mkdir") is True, "the idempotent mkdir must not be marked non-idempotent"

    def test_wave_dependency_hold_jid_in_ssh_command(self, tmp_path):
        """When submit_plan runs a second wave, the remote qsub must carry
        ``-hold_jid`` with the prior wave's job IDs joined by commas."""
        from hpc_agent.infra.throughput import JobBatch, SubmissionPlan

        plan = SubmissionPlan(
            batches=[
                JobBatch(
                    batch_index=0,
                    task_start=1,
                    task_end=5,
                    array_size=5,
                    est_wall_s=None,
                    wave=0,
                ),
                JobBatch(
                    batch_index=1,
                    task_start=6,
                    task_end=10,
                    array_size=5,
                    est_wall_s=None,
                    wave=1,
                ),
            ],
            total_tasks=10,
            total_batches=2,
            max_concurrent=1,
            est_total_wall_s=None,
            strategy="test",
        )

        counter = {"n": 99}

        def responder(cmd):
            if "qsub" not in cmd:
                return _cp()
            counter["n"] += 1
            return _cp(
                stdout=(f'Your job-array {counter["n"]}.1-5:1 ("probe") has been submitted\n'),
                returncode=0,
            )

        recorder = _SSHRecorder(responder)
        backend = RemoteSGEBackend(
            script="/remote/path/job.sh",
            ssh_run=recorder,
            remote_repo="/remote/path",
            log_dir="/remote/path/logs",
        )

        submissions = backend.submit_plan(
            plan,
            job_name="probe",
            job_env={},
            cwd=tmp_path,
        )
        assert len(submissions) == 2
        # submit_plan returns (wave, task_range, job_id) tuples (#339).
        assert submissions[0][0] == 0
        assert submissions[1][0] == 1
        wave0_jid = submissions[0][2]

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        assert len(qsub_calls) == 2
        # First wave: no hold_jid.
        assert "-hold_jid" not in qsub_calls[0]
        # Second wave: hold_jid carries the first wave's job ID.
        assert "-hold_jid" in qsub_calls[1]
        assert wave0_jid in qsub_calls[1]

    def test_index_bounded_wave_emits_local_range_and_task_offset(self, tmp_path):
        """#339: an index-bounded backend submits each wave as a LOCAL ``-t``
        range (within the scheduler's array cap) and ships the global start as
        ``TASK_OFFSET`` via ``-v`` — even though TASK_OFFSET is NOT in the
        caller's pass_env_keys (it's a framework-internal var). Wave 0 (offset 0)
        omits it, staying byte-identical to a ≤cap submission."""
        from hpc_agent.infra.throughput import JobBatch, SubmissionPlan

        plan = SubmissionPlan(
            batches=[
                JobBatch(
                    batch_index=0, task_start=1, task_end=5, array_size=5, est_wall_s=None, wave=0
                ),
                JobBatch(
                    batch_index=1, task_start=6, task_end=10, array_size=5, est_wall_s=None, wave=1
                ),
            ],
            total_tasks=10,
            total_batches=2,
            max_concurrent=1,
            est_total_wall_s=None,
            strategy="test",
        )

        counter = {"n": 99}

        def responder(cmd):
            if "qsub" not in cmd:
                return _cp()
            counter["n"] += 1
            return _cp(
                stdout=f'Your job-array {counter["n"]}.1-5:1 ("probe") has been submitted\n',
                returncode=0,
            )

        recorder = _SSHRecorder(responder)
        backend = RemoteSGEBackend(
            script="/remote/path/job.sh",
            ssh_run=recorder,
            remote_repo="/remote/path",
            log_dir="/remote/path/logs",
        )
        # FOO is allow-listed; TASK_OFFSET is deliberately NOT — it must still
        # transport as a framework var.
        backend.pass_env_keys = ("FOO",)

        backend.submit_plan(plan, job_name="probe", job_env={"FOO": "bar"}, cwd=tmp_path)

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        assert len(qsub_calls) == 2
        # Both waves submit the LOCAL 1-5 range (never the global 6-10 that would
        # exceed the array-index cap).
        assert "-t 1-5" in qsub_calls[0]
        assert "-t 1-5" in qsub_calls[1]
        assert "-t 6-10" not in qsub_calls[1]
        # Wave 0 (offset 0) omits TASK_OFFSET; wave 1 (offset 5) carries it.
        assert "TASK_OFFSET" not in qsub_calls[0]
        assert "TASK_OFFSET=5" in qsub_calls[1]


# ---------------------------------------------------------------------------
# stdout parsing
# ---------------------------------------------------------------------------


class TestStdoutParsing:
    def test_parses_first_integer_as_job_id(self, tmp_path):
        """submit_plan picks the first integer in stdout — verify that the
        canonical ``Your job-array 42.1-10:1`` format yields ``"42"``."""
        from hpc_agent.infra.throughput import JobBatch, SubmissionPlan

        plan = SubmissionPlan(
            batches=[
                JobBatch(
                    batch_index=0,
                    task_start=1,
                    task_end=10,
                    array_size=10,
                    est_wall_s=None,
                    wave=0,
                ),
            ],
            total_tasks=10,
            total_batches=1,
            max_concurrent=1,
            est_total_wall_s=None,
            strategy="test",
        )

        def responder(cmd):
            if "qsub" not in cmd:
                return _cp()
            return _cp(
                stdout='Your job-array 42.1-10:1 ("probe") has been submitted\n',
                returncode=0,
            )

        recorder = _SSHRecorder(responder)
        backend = RemoteSGEBackend(
            script="/remote/path/job.sh",
            ssh_run=recorder,
            remote_repo="/remote/path",
        )
        submissions = backend.submit_plan(
            plan,
            job_name="probe",
            job_env={},
            cwd=tmp_path,
        )
        assert submissions == [(0, "1-10", "42")]


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


class TestErrors:
    def test_nonzero_returncode_raises(self, tmp_path):
        def responder(cmd):
            if "qsub" not in cmd:
                return _cp()
            return _cp(stdout="", stderr="qsub exploded", returncode=1)

        recorder = _SSHRecorder(responder)
        backend = RemoteSGEBackend(
            script="/remote/path/job.sh",
            ssh_run=recorder,
            remote_repo="/remote/path",
        )
        with pytest.raises(RuntimeError, match="qsub exploded"):
            backend.submit_plan(
                _single_batch_plan(1, 5),
                "probe",
                job_env={},
                cwd=tmp_path,
            )

    def test_remote_repo_with_space_is_quoted(self, tmp_path):
        """Regression: remote_repo containing a space must be shell-quoted in
        both the ``cd`` prefix and the ``mkdir -p`` log-dir setup so the
        remote shell does not word-split the command."""

        def responder(cmd):
            if "qsub" not in cmd:
                return _cp()
            return _cp(
                stdout='Your job-array 42.1-5:1 ("probe") has been submitted\n',
                returncode=0,
            )

        recorder = _SSHRecorder(responder)
        backend = RemoteSGEBackend(
            script="/remote/path with space/job.sh",
            ssh_run=recorder,
            remote_repo="/remote/path with space",
            log_dir="/remote/path with space/logs",
        )
        backend.submit_plan(
            _single_batch_plan(1, 5),
            "probe",
            job_env={},
            cwd=tmp_path,
        )

        mkdir_calls = [c for c in recorder.calls if c.startswith("mkdir -p")]
        assert mkdir_calls
        assert "mkdir -p '/remote/path with space/logs'" in mkdir_calls[0]

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        assert len(qsub_calls) == 1
        # The submit command is wrapped in `bash -lc '<inner>'` (LOGIN, NOT
        # interactive) so the cluster's profile sequence sources qsub onto PATH
        # (Hoffman2 regression: bare ssh is non-login). It must NOT be `-lic`:
        # an interactive bash on a no-PTY ssh exec channel hangs until the
        # 120 s timeout (proving-run #2). Decode the wrap before asserting on
        # the inner's quoted path.
        wrap_parts = shlex.split(qsub_calls[0])
        assert wrap_parts[:2] == ["bash", "-lc"]
        assert "i" not in wrap_parts[1], "interactive bash (-i) hangs no-PTY ssh"
        inner = wrap_parts[2]
        assert "cd '/remote/path with space'" in inner
        assert "'/remote/path with space/job.sh'" in inner

    def test_unparseable_stdout_raises(self, tmp_path):
        def responder(cmd):
            if "qsub" not in cmd:
                return _cp()
            return _cp(stdout="no digits here\n", stderr="", returncode=0)

        recorder = _SSHRecorder(responder)
        backend = RemoteSGEBackend(
            script="/remote/path/job.sh",
            ssh_run=recorder,
            remote_repo="/remote/path",
        )
        with pytest.raises(RuntimeError, match="Could not parse job ID"):
            backend.submit_plan(
                _single_batch_plan(1, 5),
                "probe",
                job_env={},
                cwd=tmp_path,
            )


# ---------------------------------------------------------------------------
# login-shell amortisation (proving-run-2 Phase-0: bash -lc costs ~1.2s
# server-side per call on Hoffman2 — resolve once, then submit by abs path)
# ---------------------------------------------------------------------------
class TestLoginShellAmortisation:
    def _backend(self, responder):
        recorder = _SSHRecorder(responder)
        backend = RemoteSGEBackend(
            script="/remote/path/job.sh",
            ssh_run=recorder,
            remote_repo="/remote/path",
        )
        return backend, recorder

    @staticmethod
    def _qsub_responder(cmd):
        if "qsub" not in cmd:
            return _cp()
        stderr = (
            "__HPC_SUBMIT_BIN__=/u/systems/UGE8.6.4/bin/lx-amd64/qsub\n"
            if "__HPC_SUBMIT_BIN__" in cmd
            else ""
        )
        return _cp(stdout="Your job 111 (probe) has been submitted\n", stderr=stderr)

    def test_first_submit_uses_login_shell_and_harvests_marker(self, tmp_path):
        backend, recorder = self._backend(self._qsub_responder)
        backend.submit_plan(_single_batch_plan(1, 5), "probe", job_env={}, cwd=tmp_path)

        (qsub_call,) = [c for c in recorder.calls if "qsub" in c]
        wrap = shlex.split(qsub_call)
        assert wrap[:2] == ["bash", "-lc"]
        # Resolution rides the SAME round-trip, on stderr (stdout carries the
        # job id the JOB_ID_REGEX parses — never polluted).
        assert "__HPC_SUBMIT_BIN__" in wrap[2]
        assert backend._resolved_bins == {"qsub": "/u/systems/UGE8.6.4/bin/lx-amd64/qsub"}

    def test_second_submit_skips_login_shell_via_cached_abs_path(self, tmp_path):
        backend, recorder = self._backend(self._qsub_responder)
        backend.submit_plan(_single_batch_plan(1, 5), "probe", job_env={}, cwd=tmp_path)
        backend.submit_plan(_single_batch_plan(1, 5), "probe", job_env={}, cwd=tmp_path)

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        assert len(qsub_calls) == 2
        second = qsub_calls[1]
        assert "bash -lc" not in second  # no login shell on the warm path
        assert second.startswith("cd /remote/path && ")
        assert "/u/systems/UGE8.6.4/bin/lx-amd64/qsub" in second

    def test_stale_abs_path_falls_back_and_reheals(self, tmp_path):
        """A cached path that 127s (cluster upgrade) is dropped; the SAME call
        retries via the login shell and re-harvests a fresh marker."""
        state = {"moved": False}

        def responder(cmd):
            if "qsub" not in cmd:
                return _cp()
            if "UGE8.6.4" in cmd and state["moved"]:
                return _cp(stderr="bash: no such file\n", returncode=127)
            stderr = (
                "__HPC_SUBMIT_BIN__=/u/systems/UGE9.0.0/bin/lx-amd64/qsub\n"
                if state["moved"] and "__HPC_SUBMIT_BIN__" in cmd
                else (
                    "__HPC_SUBMIT_BIN__=/u/systems/UGE8.6.4/bin/lx-amd64/qsub\n"
                    if "__HPC_SUBMIT_BIN__" in cmd
                    else ""
                )
            )
            return _cp(stdout="Your job 222 (probe) has been submitted\n", stderr=stderr)

        backend, recorder = self._backend(responder)
        backend.submit_plan(_single_batch_plan(1, 5), "probe", job_env={}, cwd=tmp_path)
        state["moved"] = True  # scheduler tree moved out from under the cache
        backend.submit_plan(_single_batch_plan(1, 5), "probe", job_env={}, cwd=tmp_path)

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        # 1st: login shell. 2nd: stale abs path (127). 3rd: login-shell retry
        # of the SAME submit, which re-harvests the new location.
        assert len(qsub_calls) == 3
        assert "bash -lc" in qsub_calls[0]
        assert "UGE8.6.4" in qsub_calls[1] and "bash -lc" not in qsub_calls[1]
        assert "bash -lc" in qsub_calls[2]
        assert backend._resolved_bins == {"qsub": "/u/systems/UGE9.0.0/bin/lx-amd64/qsub"}

    def test_absent_marker_caches_nothing(self, tmp_path):
        def responder(cmd):
            if "qsub" not in cmd:
                return _cp()
            return _cp(stdout="Your job 333 (probe) has been submitted\n", stderr="")

        backend, recorder = self._backend(responder)
        backend.submit_plan(_single_batch_plan(1, 5), "probe", job_env={}, cwd=tmp_path)
        backend.submit_plan(_single_batch_plan(1, 5), "probe", job_env={}, cwd=tmp_path)

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        # No marker → no cache → both submits stay on the (correct) login shell.
        assert all("bash -lc" in c for c in qsub_calls)
        assert getattr(backend, "_resolved_bins", {}) == {}
