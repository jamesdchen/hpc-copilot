"""Backend contract tests for :class:`SGEBackend`.

These tests lock down the *shape* of the ``qsub`` command line produced
by the SGE backend so accidental refactors of flag ordering or filtering
are caught in CI.  No real scheduler is touched — ``subprocess.run`` is
patched at the module level used by ``HPCBackend._execute_command``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends.sge import SGEBackend


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    """Fake ``subprocess.CompletedProcess`` for monkeypatching."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# _build_command
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_basic_command_shape(self, tmp_path):
        script = str(tmp_path / "job.sh")
        log_dir = str(tmp_path / "logs")
        backend = SGEBackend(script=script, log_dir=log_dir)

        cmd = backend._build_command("1-10", "myjob", {})

        assert cmd[0] == "qsub"
        # -t <range>
        assert "-t" in cmd
        assert cmd[cmd.index("-t") + 1] == "1-10"
        # -N <name>
        assert "-N" in cmd
        assert cmd[cmd.index("-N") + 1] == "myjob"
        # -o <log_dir>
        assert "-o" in cmd
        assert cmd[cmd.index("-o") + 1] == log_dir
        # -j y (join stderr into stdout)
        assert "-j" in cmd
        assert cmd[cmd.index("-j") + 1] == "y"
        # script path is the final positional arg
        assert cmd[-1] == script

    def test_pass_env_keys_filters_job_env(self, tmp_path):
        backend = SGEBackend(
            script=str(tmp_path / "job.sh"),
            log_dir=str(tmp_path / "logs"),
            pass_env_keys=("FOO", "QUUX"),
        )
        env = {"FOO": "1", "BAR": "nope", "QUUX": "2"}
        cmd = backend._build_command("1-5", "j", env)

        assert "-v" in cmd
        pass_vars = cmd[cmd.index("-v") + 1]
        # Only FOO and QUUX forwarded; BAR filtered out.
        parts = pass_vars.split(",")
        assert "FOO=1" in parts
        assert "QUUX=2" in parts
        assert all(not p.startswith("BAR=") for p in parts)

    def test_empty_pass_env_keys_omits_v_flag(self, tmp_path):
        backend = SGEBackend(
            script=str(tmp_path / "job.sh"),
            log_dir=str(tmp_path / "logs"),
            pass_env_keys=(),
        )
        cmd = backend._build_command("1-5", "j", {"FOO": "1", "BAR": "2"})
        assert "-v" not in cmd

    def test_extra_flags_appear_before_script(self, tmp_path):
        script = str(tmp_path / "job.sh")
        backend = SGEBackend(script=script, log_dir=str(tmp_path / "logs"))

        cmd = backend._build_command("1-5", "j", {}, extra_flags=["-hold_jid", "123,456"])

        # Extra flags are between the scheduler args and the script path.
        hold_idx = cmd.index("-hold_jid")
        script_idx = cmd.index(script)
        assert hold_idx < script_idx
        assert cmd[hold_idx + 1] == "123,456"
        # Script is still the last positional arg.
        assert cmd[-1] == script


# ---------------------------------------------------------------------------
# _build_dependency_flag
# ---------------------------------------------------------------------------


class TestDependencyFlag:
    def test_multiple_ids_joined_with_comma(self, tmp_path):
        backend = SGEBackend(script=str(tmp_path / "j.sh"))
        assert backend._build_dependency_flag(["123", "456"]) == [
            "-hold_jid",
            "123,456",
        ]

    def test_empty_list_returns_empty(self, tmp_path):
        backend = SGEBackend(script=str(tmp_path / "j.sh"))
        assert backend._build_dependency_flag([]) == []


# ---------------------------------------------------------------------------
# submit_array_tracked (subprocess mocked)
# ---------------------------------------------------------------------------


class TestSubmitArrayTracked:
    def test_happy_path_returns_range_and_jobid(self, monkeypatch, tmp_path):
        def fake_run(cmd, *args, **kwargs):
            return _cp(
                stdout='Your job-array 12345.1-10:1 ("probe") has been submitted\n',
                returncode=0,
            )

        monkeypatch.setattr("hpc_agent.infra.backends.subprocess.run", fake_run)

        backend = SGEBackend(
            script=str(tmp_path / "job.sh"),
            log_dir=str(tmp_path / "logs"),
        )
        out = backend.submit_array_tracked(
            "probe",
            total_tasks=10,
            tasks_per_array=10,
            job_env={},
            cwd=tmp_path,
        )
        assert out == [("1-10", "12345")]

    def test_nonzero_returncode_raises_with_stderr(self, monkeypatch, tmp_path):
        def fake_run(cmd, *args, **kwargs):
            return _cp(stdout="", stderr="qsub: bad thing", returncode=2)

        monkeypatch.setattr("hpc_agent.infra.backends.subprocess.run", fake_run)

        backend = SGEBackend(
            script=str(tmp_path / "job.sh"),
            log_dir=str(tmp_path / "logs"),
        )
        with pytest.raises(RuntimeError, match="qsub: bad thing"):
            backend.submit_array_tracked(
                "probe",
                total_tasks=10,
                tasks_per_array=10,
                job_env={},
                cwd=tmp_path,
            )


# ---------------------------------------------------------------------------
# constructor validation
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_missing_script_raises(self):
        with pytest.raises(errors.SpecInvalid, match="script"):
            SGEBackend()
