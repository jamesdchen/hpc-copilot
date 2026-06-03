"""Resolver stubs — scheduler label -> profile, no LLM in the loop.

Agent A (TEST safety net). The resolver itself is being added by the
spine / Agent C; these tests pin the CONTRACT around it:

1. A KNOWN scheduler label ("slurm" / "sge") resolves to its golden
   profile WITHOUT any LLM call.
2. ``register_profile(profile)`` makes ``get_backend_class(label)``
   return a backend bound to that custom profile, and
   ``build_backend_class(profile)`` produces such a class directly.

Anything that depends on infra not yet built is marked with a clear
``NEEDS_SPINE`` comment and an ``importorskip`` / try-import guard so
the file imports cleanly today and the not-yet-built parts are easy to
collect and run after integration.

The resolver entrypoint the contract references is:
    from hpc_agent.models.mapreduce.reduce.status import <resolver>
Its concrete name is owned by the spine/Agent C; we probe the likely
names and skip (not fail) until one exists, so this file is a forward
contract rather than a hard dependency.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpc_agent.infra.backends import get_backend, get_backend_class


def _noop_ssh(_cmd: str) -> SimpleNamespace:
    return SimpleNamespace(stdout="", stderr="", returncode=0)


# ---------------------------------------------------------------------------
# (1) Known labels resolve through the registry today (no LLM, no infra gap).
#     These run NOW and lock the pre-populated registry behaviour the
#     resolver builds on.
# ---------------------------------------------------------------------------


class TestKnownLabelResolution:
    def test_slurm_label_resolves(self):
        cls = get_backend_class("slurm")
        assert cls.scheduler_name == "slurm"
        assert cls.template_ext == ".slurm"

    def test_sge_label_resolves(self):
        cls = get_backend_class("sge")
        assert cls.scheduler_name == "sge"
        assert cls.template_ext == ".sh"

    def test_unknown_label_raises_spec_invalid(self):
        from hpc_agent import errors

        with pytest.raises(errors.SpecInvalid, match="Unknown backend"):
            get_backend_class("torque-not-registered")

    def test_get_backend_instantiates_known_label(self):
        b = get_backend(
            "slurm",
            script="x.slurm",
            ssh_run=_noop_ssh,
            remote_repo="/repo",
            log_dir="logs",
        )
        assert b._build_command("1-1", "j", {})[0] == "sbatch"


# ---------------------------------------------------------------------------
# (2) FROZEN-CONTRACT: register_profile / build_backend_class.
#     NEEDS_SPINE — these import the new registry helpers the spine adds
#     to infra/backends/__init__.py. They will ERROR on import until the
#     spine lands; that is expected and intentional.
# ---------------------------------------------------------------------------


def _make_profile(**overrides):
    """Build a SchedulerProfile from the frozen contract module.

    NEEDS_SPINE: hpc_agent.infra.backends.profile.SchedulerProfile.
    """
    from hpc_agent.infra.backends.profile import SchedulerProfile

    kw = dict(
        name="myslurm",
        family="slurm",
        submit_bin="sbatch",
        job_id_regex=r"Submitted batch job\s+(\d+)",
        template_ext=".slurm",
        supports_test_only_eta=True,
        error_states=frozenset({"FAILED", "CANCELLED", "TIMEOUT"}),
    )
    kw.update(overrides)
    return SchedulerProfile(**kw)


class TestRegisterProfile:
    """NEEDS_SPINE: register_profile + build_backend_class in
    infra/backends/__init__.py."""

    def test_register_profile_makes_label_resolvable(self):
        from hpc_agent.infra.backends import get_backend_class, register_profile

        prof = _make_profile(name="customsched")
        register_profile(prof, remote=True)
        cls = get_backend_class("customsched")
        assert cls.profile is prof
        assert cls.template_ext == ".slurm"

    def test_build_backend_class_binds_profile(self):
        from hpc_agent.infra.backends import HPCBackend, build_backend_class

        prof = _make_profile(name="buildme")
        cls = build_backend_class(prof, remote=True)
        assert issubclass(cls, HPCBackend)
        assert cls.profile is prof
        assert cls.scheduler_name == prof.name
        assert cls.supports_test_only_eta is True

    def test_registered_profile_command_shape_matches_family(self):
        """A custom slurm-family profile must build sbatch-shaped commands."""
        from hpc_agent.infra.backends import get_backend, register_profile

        register_profile(_make_profile(name="cmdshape"), remote=True)
        b = get_backend(
            "cmdshape",
            script="x.slurm",
            ssh_run=_noop_ssh,
            remote_repo="/repo",
            log_dir="logs",
        )
        cmd = b._build_command("1-1", "j", {})
        assert cmd[0] == "sbatch"
        assert cmd[-1] == "x.slurm"


# ---------------------------------------------------------------------------
# (3) FROZEN-CONTRACT: the no-LLM label resolver entrypoint.
#     The resolver entrypoint already exists on reduce.status:
#       resolve_scheduler_profile(scheduler, *, cfg=, result_dir=, probe=, llm=)
#     returning a SchedulerProfile. It still depends on the spine's
#     hpc_agent.infra.backends.profile module, so these ERROR on import of
#     that module until integration. NEEDS_SPINE for the profile module.
# ---------------------------------------------------------------------------


class _ExplodingLLM:
    """A sentinel LLM handle: any attribute access / call fails the test.

    Lets us prove a known label resolves WITHOUT touching the LLM seam.
    """

    def __getattr__(self, name):  # noqa: ANN001
        raise AssertionError(f"LLM must not be used for a known scheduler label (touched .{name})")

    def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("LLM must not be called for a known scheduler label")


class TestLabelResolverNoLLM:
    """The resolver maps a KNOWN label to its golden profile WITHOUT an LLM
    call. NEEDS_SPINE: hpc_agent.infra.backends.profile."""

    @pytest.mark.parametrize(
        ("label", "family"),
        [("slurm", "slurm"), ("sge", "sge"), ("pbspro", "pbspro"), ("torque", "torque")],
    )
    def test_known_label_resolves_to_golden_without_llm(self, label, family):
        from hpc_agent.infra.backends.profile import (
            PBSPRO_PROFILE,
            SGE_PROFILE,
            SLURM_PROFILE,
            TORQUE_PROFILE,
        )
        from hpc_agent.models.mapreduce.reduce.status import resolve_scheduler_profile

        golden = {
            "slurm": SLURM_PROFILE,
            "sge": SGE_PROFILE,
            "pbspro": PBSPRO_PROFILE,
            "torque": TORQUE_PROFILE,
        }[family]
        # An exploding LLM proves path (3) is taken (NO LLM for known family) —
        # and that pbspro/torque are treated as KNOWN (not auto-authored).
        resolved = resolve_scheduler_profile(label, cfg={}, llm=_ExplodingLLM())
        assert resolved == golden

    def test_known_label_ignores_case(self):
        from hpc_agent.infra.backends.profile import SLURM_PROFILE
        from hpc_agent.models.mapreduce.reduce.status import resolve_scheduler_profile

        assert resolve_scheduler_profile("SLURM", cfg={}, llm=_ExplodingLLM()) == SLURM_PROFILE

    def test_resolved_known_label_is_registered(self):
        """After resolving "slurm", get_backend_class("slurm") still works
        (resolution is idempotent w.r.t. the pre-populated registry)."""
        from hpc_agent.models.mapreduce.reduce.status import resolve_scheduler_profile

        resolve_scheduler_profile("slurm", cfg={}, llm=_ExplodingLLM())
        assert get_backend_class("slurm").scheduler_name == "slurm"
