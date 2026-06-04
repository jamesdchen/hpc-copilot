"""Reachability tests for the resolved-profile path.

These cover the wiring that makes a pinned ``SchedulerProfile`` actually
usable end-to-end:

* ``register_profile`` is idempotent for an equal profile and *raises* on
  a conflicting label (rather than silently overwriting).
* ``build_remote_backend(scheduler_profile=...)`` builds a backend bound
  to the pinned profile, with the family-correct command grammar.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends import (
    get_backend_class,
    register_profile,
)
from hpc_agent.infra.backends.profile import SGE_PROFILE, SLURM_PROFILE, SchedulerProfile
from hpc_agent.infra.backends.remote_factory import build_remote_backend


def _custom_slurm_profile(name: str = "discovery2") -> SchedulerProfile:
    # A slurm-family profile with a distinct label and a tweaked job-id
    # anchor, to prove the engine uses the profile's data, not golden.
    return SchedulerProfile(
        name=name,
        family="slurm",
        submit_bin="sbatch",
        job_id_regex=r"Submitted batch job\s+(\d+)",
        template_ext=".slurm",
        supports_test_only_eta=True,
        error_states=frozenset({"FAILED", "TIMEOUT"}),
        scripts={"cpu": "#!/bin/bash\necho cpu\n", "gpu": "#!/bin/bash\necho gpu\n"},
    )


class TestRegisterProfileConflict:
    def test_idempotent_for_equal_profile(self):
        # Re-registering the golden slurm profile is a no-op (the dedicated
        # RemoteSlurmBackend stays registered).
        before = get_backend_class("slurm")
        assert register_profile(SLURM_PROFILE) is before
        assert get_backend_class("slurm") is before

    def test_idempotent_for_custom_then_same(self):
        prof = _custom_slurm_profile("eqv")
        cls1 = register_profile(prof)
        cls2 = register_profile(SchedulerProfile.from_dict(prof.to_dict()))
        assert cls1 is cls2  # equal profile -> same registered class

    def test_conflicting_label_raises(self):
        register_profile(_custom_slurm_profile("clash"))
        # Same label, different profile (sge family) -> loud refusal.
        conflicting = SchedulerProfile(
            name="clash",
            family="sge",
            submit_bin="qsub",
            job_id_regex=r"Your job(?:-array)?\s+(\d+)",
            template_ext=".sh",
            supports_test_only_eta=False,
            scripts={"cpu": "x", "gpu": "y"},
        )
        with pytest.raises(errors.SpecInvalid, match="already registered"):
            register_profile(conflicting)

    def test_cannot_clobber_golden_slurm(self):
        with pytest.raises(errors.SpecInvalid, match="already registered"):
            register_profile(_custom_slurm_profile("slurm"))


def _noop_ssh(_cmd):
    from types import SimpleNamespace

    return SimpleNamespace(stdout="", stderr="", returncode=0)


class TestBuildRemoteBackendWithProfile:
    def test_builds_profile_bound_backend(self):
        prof = _custom_slurm_profile("disc-build")
        backend = build_remote_backend(
            backend_name="slurm",
            script=".hpc/templates/cpu_array.slurm",
            ssh_target="u@h",
            remote_path="/r",
            pass_env_keys=None,
            job_env_keys=("EXECUTOR",),
            scheduler_profile=prof.to_dict(),
        )
        # The backend is bound to the pinned profile, not the golden one.
        assert backend.profile.name == "disc-build"
        assert backend.profile.error_states == frozenset({"FAILED", "TIMEOUT"})
        # slurm-family command grammar (sbatch flags), driven by the profile.
        cmd = backend._build_command("1-4", "job", {"K": "V"})
        assert cmd[0] == "sbatch"
        assert cmd[:3] == ["sbatch", "--array", "1-4"]
        assert cmd[-1] == ".hpc/templates/cpu_array.slurm"

    def test_sge_family_profile_uses_qsub_grammar(self):
        backend = build_remote_backend(
            backend_name="sge",
            script="run.sh",
            ssh_target="u@h",
            remote_path="/r",
            pass_env_keys=("K",),
            job_env_keys=("K",),
            scheduler_profile=SchedulerProfile.from_dict(
                {**SGE_PROFILE.to_dict(), "name": "sge-custom"}
            ).to_dict(),
        )
        cmd = backend._build_command("1-4", "job", {"K": "V"})
        assert cmd[0] == "qsub"
        assert "-t" in cmd and "-j" in cmd

    def test_backend_name_family_mismatch_raises(self):
        # Spec says sge, pin says slurm -> refuse (script ext would mismatch).
        with pytest.raises(errors.SpecInvalid, match="disagrees with the pinned"):
            build_remote_backend(
                backend_name="sge",
                script="run.sh",
                ssh_target="u@h",
                remote_path="/r",
                pass_env_keys=None,
                job_env_keys=("EXECUTOR",),
                scheduler_profile=_custom_slurm_profile("mismatch").to_dict(),
            )

    def test_without_profile_uses_golden(self):
        backend = build_remote_backend(
            backend_name="slurm",
            script="cpu.slurm",
            ssh_target="u@h",
            remote_path="/r",
            pass_env_keys=None,
            job_env_keys=("EXECUTOR",),
        )
        assert backend.profile is SLURM_PROFILE

    def test_pbspro_golden_uses_J_array_flag(self):
        backend = build_remote_backend(
            backend_name="pbspro",
            script=".hpc/templates/cpu_array.pbs",
            ssh_target="u@h",
            remote_path="/r",
            pass_env_keys=None,
            job_env_keys=("EXECUTOR",),
        )
        assert backend.profile.family == "pbspro"
        cmd = backend._build_command("1-4", "job", {"EXECUTOR": "x"})
        assert cmd[0] == "qsub"
        assert "-J" in cmd and "-t" not in cmd  # pbspro array flag is -J
        assert cmd[-1] == ".hpc/templates/cpu_array.pbs"

    def test_torque_golden_uses_t_array_flag(self):
        backend = build_remote_backend(
            backend_name="torque",
            script=".hpc/templates/cpu_array.pbs",
            ssh_target="u@h",
            remote_path="/r",
            pass_env_keys=None,
            job_env_keys=("EXECUTOR",),
        )
        assert backend.profile.family == "torque"
        cmd = backend._build_command("1-4", "job", {"EXECUTOR": "x"})
        assert cmd[0] == "qsub"
        assert "-t" in cmd and "-J" not in cmd  # torque array flag is -t


def test_deploy_runtime_scheduler_deploys_only_that_family():
    """deploy_runtime(scheduler='pbspro') renders ONLY that family's scripts
    (cpu_array.pbs / gpu_array.pbs) — never the sge/slurm ones — so the shared
    .pbs name can't collide with torque on a given cluster."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from hpc_agent.infra import transport

    dests: list[str] = []

    def _capture(*, ssh_target, remote_path, items):
        dests.extend(it.dst_rel for it in items)

    # deploy_runtime does the mkdir/clean via ``ssh_run`` (no-op'd here) then
    # ships the cache-filtered files in ONE batched transfer (#252) — capture
    # the staged dst_rels through the ``_deploy_transfer`` seam.
    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ),
        patch("hpc_agent.infra.transport._deploy_transfer", side_effect=_capture),
    ):
        transport.deploy_runtime(ssh_target="u@h", remote_path="/p", scheduler="pbspro")

    array_scripts = [d for d in dests if "cpu_array" in d or "gpu_array" in d]
    assert any(d.endswith("cpu_array.pbs") for d in array_scripts)
    assert any(d.endswith("gpu_array.pbs") for d in array_scripts)
    # The other families' scripts must NOT be deployed to a pbspro cluster.
    assert not any(d.endswith((".sh", ".slurm")) for d in array_scripts)
