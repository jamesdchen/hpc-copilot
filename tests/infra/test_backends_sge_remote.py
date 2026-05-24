"""Backend contract tests for :class:`RemoteSGEBackend`.

The remote SGE backend wraps each ``qsub`` invocation in an SSH call via
a caller-provided ``ssh_run`` callable.  These tests exercise the shape
of the string passed to ``ssh_run`` and the stdout parsing that turns an
SGE ``qsub`` response into a job ID.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpc_agent.infra.backends.sge_remote import RemoteSGEBackend


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


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
        with pytest.raises(ValueError, match="script"):
            RemoteSGEBackend(
                ssh_run=lambda cmd: _cp(),
                remote_repo="/tmp",
            )

    def test_missing_ssh_run_raises(self):
        with pytest.raises(ValueError, match="ssh_run"):
            RemoteSGEBackend(script="job.sh", remote_repo="/tmp")

    def test_missing_remote_repo_raises(self):
        with pytest.raises(ValueError, match="remote_repo"):
            RemoteSGEBackend(script="job.sh", ssh_run=lambda cmd: _cp())


# ---------------------------------------------------------------------------
# submit_plan / submit_array_tracked build an SSH-wrapped qsub command
# ---------------------------------------------------------------------------


class TestSSHWrappedCommand:
    def test_submit_array_tracked_ssh_command_shape(self, tmp_path):
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

        out = backend.submit_array_tracked(
            "probe",
            total_tasks=10,
            tasks_per_array=10,
            job_env={},
            cwd=tmp_path,
        )
        assert out == [("1-10", "42")]

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

    def test_wave_dependency_hold_jid_in_ssh_command(self, tmp_path):
        """When submit_plan runs a second wave, the remote qsub must carry
        ``-hold_jid`` with the prior wave's job IDs joined by commas."""
        from hpc_agent.ops.submit.throughput import JobBatch, SubmissionPlan

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
        wave0_jid = submissions[0][1]

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        assert len(qsub_calls) == 2
        # First wave: no hold_jid.
        assert "-hold_jid" not in qsub_calls[0]
        # Second wave: hold_jid carries the first wave's job ID.
        assert "-hold_jid" in qsub_calls[1]
        assert wave0_jid in qsub_calls[1]


# ---------------------------------------------------------------------------
# stdout parsing
# ---------------------------------------------------------------------------


class TestStdoutParsing:
    def test_parses_first_integer_as_job_id(self, tmp_path):
        """submit_plan picks the first integer in stdout — verify that the
        canonical ``Your job-array 42.1-10:1`` format yields ``"42"``."""
        from hpc_agent.ops.submit.throughput import JobBatch, SubmissionPlan

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
        assert submissions == [("1-10", "42")]


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
            backend.submit_array_tracked(
                "probe",
                total_tasks=5,
                tasks_per_array=5,
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
        backend.submit_array_tracked(
            "probe",
            total_tasks=5,
            tasks_per_array=5,
            job_env={},
            cwd=tmp_path,
        )

        mkdir_calls = [c for c in recorder.calls if c.startswith("mkdir -p")]
        assert mkdir_calls
        assert "mkdir -p '/remote/path with space/logs'" in mkdir_calls[0]

        qsub_calls = [c for c in recorder.calls if "qsub" in c]
        assert len(qsub_calls) == 1
        assert "cd '/remote/path with space'" in qsub_calls[0]
        assert "'/remote/path with space/job.sh'" in qsub_calls[0]

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
            backend.submit_array_tracked(
                "probe",
                total_tasks=5,
                tasks_per_array=5,
                job_env={},
                cwd=tmp_path,
            )
