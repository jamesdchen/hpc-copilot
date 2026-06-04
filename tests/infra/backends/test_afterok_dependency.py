"""afterok (success-only) scheduler dependency flags (#250)."""

from __future__ import annotations

import pytest

from hpc_agent.infra.backends import get_backend


def _noop_ssh(_cmd):
    from types import SimpleNamespace

    return SimpleNamespace(stdout="", stderr="", returncode=0)


def _backend(family: str):
    kw = dict(script="cpu.sh", ssh_run=_noop_ssh, remote_repo="/r")
    if family != "slurm":  # SGE/PBS take pass_env_keys; SLURM does not
        kw["pass_env_keys"] = ("K",)
    return get_backend(family, **kw)


def test_slurm_afterok_flag():
    b = _backend("slurm")
    assert b.supports_afterok is True
    assert b._build_afterok_dependency_flag(["123"]) == [
        "--dependency",
        "afterok:123",
        "--kill-on-invalid-dep=yes",
    ]
    assert b._build_afterok_dependency_flag([]) == []


@pytest.mark.parametrize("family", ["pbspro", "torque"])
def test_pbs_afterok_flag(family):
    b = _backend(family)
    assert b.supports_afterok is True
    assert b._build_afterok_dependency_flag(["12.s", "13.s"]) == [
        "-W",
        "depend=afterok:12.s:13.s",
    ]


def test_sge_has_no_afterok():
    b = _backend("sge")
    # SGE's -hold_jid only waits for completion (any exit), not success.
    assert b.supports_afterok is False
    assert b._build_afterok_dependency_flag(["999"]) == []


def test_afterok_distinct_from_afterany():
    b = _backend("slurm")
    afterany = b._build_dependency_flag(["5"])
    afterok = b._build_afterok_dependency_flag(["5"])
    assert "afterany:5" in afterany[1]
    assert "afterok:5" in afterok[1]
    assert afterany != afterok


def test_submit_flow_appends_afterok_to_main_submission():
    # The submit-flow wiring point: resource flags + the afterok dependency both
    # reach the built scheduler command for the main array (#250).
    import re
    from pathlib import Path
    from types import SimpleNamespace

    from hpc_agent.ops import submit_flow as sf

    captured: dict[str, list[str]] = {}

    class _FakeBackend:
        JOB_ID_REGEX = re.compile(r"job (\d+)")

        def _setup_log_dir(self):
            pass

        def resource_flags(self, resources):
            return ["--res"]

        def _build_command(self, task_range, job_name, job_env, *, extra_flags):
            captured["flags"] = list(extra_flags)
            return ["sbatch"]

        def _execute_command(self, cmd, job_env, cwd):
            return SimpleNamespace(returncode=0, stdout="job 77", stderr="")

    ids = sf._make_single_array_submission(
        _FakeBackend(),
        job_name="j",
        total_tasks=3,
        job_env={},
        cwd=Path("."),
        resources=None,
        extra_flags=["--dependency", "afterok:5", "--kill-on-invalid-dep=yes"],
    )
    assert ids == ["77"]
    assert captured["flags"] == [
        "--res",
        "--dependency",
        "afterok:5",
        "--kill-on-invalid-dep=yes",
    ]
