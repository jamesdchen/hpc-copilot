"""Backend contract tests for :class:`SGEBackend`.

These tests lock down the *shape* of the ``qsub`` command line produced
by the SGE backend so accidental refactors of flag ordering or filtering
are caught in CI.  No real scheduler is touched — ``subprocess.run`` is
patched at the module level used by ``HPCBackend._execute_command``.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends.sge import SGEBackend

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


class TestResourceFlags:
    """SGE resource asks → qsub flags (#146). Opt-in per field."""

    def _res(self, **kw):
        from hpc_agent._wire.workflows.submit_flow import SubmitResources

        return SubmitResources(**kw)

    def test_none_and_empty_emit_no_flags(self, tmp_path):
        backend = SGEBackend(script=str(tmp_path / "j.sh"))
        assert backend.resource_flags(None) == []
        assert backend.resource_flags(self._res()) == []

    def test_walltime_formats_as_hms(self, tmp_path):
        backend = SGEBackend(script=str(tmp_path / "j.sh"))
        # 2h on the dot.
        assert backend.resource_flags(self._res(walltime_sec=7200)) == ["-l", "h_rt=02:00:00"]
        # >99h still renders (no two-digit-hour truncation).
        assert backend.resource_flags(self._res(walltime_sec=90061)) == ["-l", "h_rt=25:01:01"]

    def test_mem_and_cpus(self, tmp_path, monkeypatch):
        # run-14 SGE mem semantics: h_data is PER-SLOT and vmem-enforced, so the
        # per-task-total mem_mb is divided across the -pe slots and multiplied by
        # the disclosed vmem headroom factor. Pin the factor so the assertion is
        # deterministic regardless of the ambient HPC_SGE_VMEM_FACTOR.
        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "2")
        backend = SGEBackend(script=str(tmp_path / "j.sh"))
        flags = backend.resource_flags(self._res(mem_mb=8192, cpus=4))
        # ceil(8192 * 2.0 / 4) = 4096M per slot → 4 slots × 4096 = 16384M total
        # (2× the 8192 per-task ask, the headroom); NOT the old verbatim 8192M
        # which, per-slot × 4, would have queued for 32G.
        assert "-l" in flags and "h_data=4096M" in flags
        assert "h_data=8192M" not in flags
        assert flags[flags.index("-pe") : flags.index("-pe") + 3] == ["-pe", "shared", "4"]


class TestSgeMemTranslation:
    """Fire-path coverage for the run-14 SGE h_data per-slot + vmem-headroom
    translation (``sge_h_data_mb`` / ``sge_vmem_factor`` — the ONE definition
    every SGE mem emitter routes through)."""

    def _res(self, **kw):
        from hpc_agent._wire.workflows.submit_flow import SubmitResources

        return SubmitResources(**kw)

    def test_divides_per_task_total_across_slots(self, monkeypatch):
        # Factor 1 isolates the PER-SLOT division from the headroom: a
        # per-task-total of 16000MB across 4 slots is 4000M per slot = 16G total,
        # NOT 16000M per slot (= 64G, the queue-starvation bug).
        from hpc_agent.infra.backends import sge_h_data_mb

        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "1")
        assert sge_h_data_mb(16000, 4) == 4000
        # No PE (single slot): the whole ask lands on one slot.
        assert sge_h_data_mb(16000, None) == 16000
        assert sge_h_data_mb(16000, 1) == 16000

    def test_vmem_headroom_applied_and_ceils(self, monkeypatch):
        from hpc_agent.infra.backends import sge_h_data_mb

        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "1.5")
        # ceil(16000 * 1.5 / 4) = ceil(6000) = 6000M per slot.
        assert sge_h_data_mb(16000, 4) == 6000
        # ceil rounds a fractional per-slot value UP (never under-requests):
        # ceil(1000 * 1.5 / 3) = ceil(500.0) = 500; ceil(1001*1.5/3)=ceil(500.5)=501.
        assert sge_h_data_mb(1001, 3) == 501

    def test_default_factor_is_the_disclosed_headroom(self, monkeypatch):
        from hpc_agent.infra.backends._engine import DEFAULT_SGE_VMEM_FACTOR, sge_vmem_factor

        monkeypatch.delenv("HPC_SGE_VMEM_FACTOR", raising=False)
        assert sge_vmem_factor() == DEFAULT_SGE_VMEM_FACTOR

    def test_env_override_and_bad_value_falls_back(self, monkeypatch):
        from hpc_agent.infra.backends._engine import DEFAULT_SGE_VMEM_FACTOR, sge_vmem_factor

        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "3.0")
        assert sge_vmem_factor() == 3.0
        # A fat-fingered / non-positive factor must never zero out a mem ask.
        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "not-a-number")
        assert sge_vmem_factor() == DEFAULT_SGE_VMEM_FACTOR
        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "0")
        assert sge_vmem_factor() == DEFAULT_SGE_VMEM_FACTOR

    def test_translation_is_disclosed_via_warning(self, tmp_path, monkeypatch, caplog):
        # The emitter WARNs whenever the emitted per-slot number differs from the
        # spec's mem_mb, so the operator can audit what the scheduler was asked.
        import logging

        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "2")
        backend = SGEBackend(script=str(tmp_path / "j.sh"))
        with caplog.at_level(logging.WARNING, logger="hpc_agent.infra.backends._engine"):
            flags = backend.resource_flags(self._res(mem_mb=8192, cpus=4))
        assert "h_data=4096M" in flags
        assert any("SGE mem translation" in r.message for r in caplog.records)


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
# constructor validation
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_missing_script_raises(self):
        with pytest.raises(errors.SpecInvalid, match="script"):
            SGEBackend()
