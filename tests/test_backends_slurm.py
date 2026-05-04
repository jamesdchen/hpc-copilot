"""Backend contract tests for :class:`SlurmBackend`.

Locks down the shape of the ``sbatch`` command produced by the Slurm
backend so accidental refactors of flag ordering, ``--export`` formatting,
or dependency syntax are caught in CI.  ``subprocess.run`` is patched
at the module level used by ``HPCBackend._execute_command``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from claude_hpc.infra.backends.slurm import SlurmBackend


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# _build_command
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_basic_shape(self, tmp_path):
        script = str(tmp_path / "job.slurm")
        log_dir = str(tmp_path / "logs")
        backend = SlurmBackend(script=script, log_dir=log_dir)

        cmd = backend._build_command("1-10", "myjob", {})

        assert cmd[0] == "sbatch"
        # --array <range>
        assert "--array" in cmd
        assert cmd[cmd.index("--array") + 1] == "1-10"
        # --job-name <name>
        assert "--job-name" in cmd
        assert cmd[cmd.index("--job-name") + 1] == "myjob"
        # --output / --error with SLURM substitution tokens
        assert "--output" in cmd
        assert cmd[cmd.index("--output") + 1] == f"{log_dir}/%x_%A_%a.out"
        assert "--error" in cmd
        assert cmd[cmd.index("--error") + 1] == f"{log_dir}/%x_%A_%a.err"
        # script path is the final positional arg
        assert cmd[-1] == script

    def test_account_only_when_set(self, tmp_path):
        backend_no_acct = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            account="",
            log_dir=str(tmp_path / "logs"),
        )
        cmd = backend_no_acct._build_command("1-5", "j", {})
        assert "--account" not in cmd

        backend_with_acct = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            account="my-acct",
            log_dir=str(tmp_path / "logs"),
        )
        cmd = backend_with_acct._build_command("1-5", "j", {})
        assert "--account" in cmd
        assert cmd[cmd.index("--account") + 1] == "my-acct"

    def test_clusters_only_when_set(self, tmp_path):
        backend_no_cluster = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            cluster="",
            log_dir=str(tmp_path / "logs"),
        )
        cmd = backend_no_cluster._build_command("1-5", "j", {})
        # No single-token --clusters=... should appear.
        assert not any(a.startswith("--clusters=") for a in cmd)

        backend_with_cluster = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            cluster="hoffman2",
            log_dir=str(tmp_path / "logs"),
        )
        cmd = backend_with_cluster._build_command("1-5", "j", {})
        # Per current code, --clusters is a single token --clusters=<name>.
        assert "--clusters=hoffman2" in cmd

    def test_export_formed_when_job_env_nonempty(self, tmp_path):
        backend = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            log_dir=str(tmp_path / "logs"),
        )
        cmd = backend._build_command("1-5", "j", {"FOO": "1", "BAR": "2"})

        assert "--export" in cmd
        export_val = cmd[cmd.index("--export") + 1]
        assert export_val.startswith("ALL,")
        parts = export_val.split(",")
        assert parts[0] == "ALL"
        assert "FOO=1" in parts
        assert "BAR=2" in parts

    def test_export_absent_when_job_env_empty(self, tmp_path):
        backend = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            log_dir=str(tmp_path / "logs"),
        )
        cmd = backend._build_command("1-5", "j", {})
        assert "--export" not in cmd


# ---------------------------------------------------------------------------
# _build_dependency_flag
# ---------------------------------------------------------------------------


class TestDependencyFlag:
    def test_multiple_ids(self, tmp_path):
        backend = SlurmBackend(script=str(tmp_path / "j.sh"))
        assert backend._build_dependency_flag(["100", "200"]) == [
            "--dependency",
            "afterany:100:200",
        ]

    def test_empty_list_returns_empty(self, tmp_path):
        backend = SlurmBackend(script=str(tmp_path / "j.sh"))
        assert backend._build_dependency_flag([]) == []


# ---------------------------------------------------------------------------
# submit_array_tracked with mocked subprocess
# ---------------------------------------------------------------------------


class TestSubmitArrayTracked:
    def test_nonzero_returncode_raises(self, monkeypatch, tmp_path):
        def fake_run(cmd, *args, **kwargs):
            return _cp(stdout="", stderr="sbatch: bad", returncode=1)

        monkeypatch.setattr("claude_hpc.infra.backends.subprocess.run", fake_run)

        backend = SlurmBackend(
            script=str(tmp_path / "job.slurm"),
            log_dir=str(tmp_path / "logs"),
        )
        with pytest.raises(RuntimeError, match="sbatch: bad"):
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
        with pytest.raises(ValueError, match="script"):
            SlurmBackend()


# ─── Bug 12: anchored job-id regex ignores warning prefixes ──────────────


class TestJobIdParsingAnchored:
    """Previously the backend used ``re.search(r"(\\d+)", stdout)`` and would
    parse the FIRST run of digits — so an sbatch warning like ``sbatch:
    warning: 30% pre-empt; Submitted batch job 12345`` produced ``"30"``
    as the job id.  The anchored regex now requires the ``Submitted batch
    job`` phrase.
    """

    def test_warning_prefix_does_not_poison_job_id(self, monkeypatch, tmp_path):
        warning_stdout = (
            "sbatch: warning: 30% of nodes pre-empt; "
            "Submitted batch job 12345\n"
        )

        def fake_run(cmd, *args, **kwargs):
            return _cp(stdout=warning_stdout, returncode=0)

        monkeypatch.setattr("claude_hpc.infra.backends.subprocess.run", fake_run)
        backend = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            log_dir=str(tmp_path / "logs"),
        )
        out = backend.submit_array_tracked(
            "j", total_tasks=1, tasks_per_array=1, job_env={}, cwd=tmp_path,
        )
        assert out == [("1-1", "12345")]

    def test_clean_output_still_parses(self, monkeypatch, tmp_path):
        def fake_run(cmd, *args, **kwargs):
            return _cp(stdout="Submitted batch job 99999\n", returncode=0)

        monkeypatch.setattr("claude_hpc.infra.backends.subprocess.run", fake_run)
        backend = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            log_dir=str(tmp_path / "logs"),
        )
        out = backend.submit_array_tracked(
            "j", total_tasks=1, tasks_per_array=1, job_env={}, cwd=tmp_path,
        )
        assert out == [("1-1", "99999")]


# ─── Bug 3: hung scheduler subprocess surfaces TimeoutExpired ────────────


class TestSubmitTimeout:
    """A hung qsub/sbatch (NFS stall, scheduler outage) used to block the
    agent indefinitely.  ``_execute_command`` now passes a 120 s timeout
    so the underlying subprocess raises ``TimeoutExpired`` for callers
    to map onto a cluster-category error.
    """

    def test_timeout_propagates_to_caller(self, monkeypatch, tmp_path):
        import subprocess as sp

        def fake_run(cmd, *args, **kwargs):
            assert "timeout" in kwargs, (
                "backend submit subprocess must enforce a timeout"
            )
            raise sp.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

        monkeypatch.setattr("claude_hpc.infra.backends.subprocess.run", fake_run)
        backend = SlurmBackend(
            script=str(tmp_path / "j.sh"),
            log_dir=str(tmp_path / "logs"),
        )
        with pytest.raises(sp.TimeoutExpired):
            backend.submit_array_tracked(
                "j", total_tasks=1, tasks_per_array=1, job_env={}, cwd=tmp_path,
            )
