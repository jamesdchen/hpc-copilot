"""Tests for the plugin-backend construction seam in
``remote_factory.build_remote_backend``.

Core edit #2 of ``docs/proposals/crowd-compute-backend.md``: a
*backend_name* the factory's inline ladder doesn't know, but the
registry does, constructs itself via
:meth:`HPCBackend.from_build_context` — receiving the whole
:class:`BackendBuildContext` so the backend (not the factory) decides
which fields it needs. A registered backend that hasn't overridden the
hook fails loud (NotImplementedError, the capability-hook convention),
and a name nothing registered still raises the typed
``unknown backend`` error.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends import (
    _REGISTRY,
    BackendBuildContext,
    HPCBackend,
    register,
)
from hpc_agent.infra.backends.remote_factory import build_remote_backend

_FACTORY_KWARGS = dict(
    script="/remote/job.sh",
    ssh_target="user@login.example.edu",
    remote_path="/remote/repo",
    pass_env_keys=None,
    job_env_keys=("EXECUTOR",),
    slurm_account="acct",
    slurm_cluster="clu",
)


@pytest.fixture
def crowd_backend():
    """Register a from_build_context-capable backend; unregister after."""

    @register("fakecrowdfactory")
    class _FakeCrowdBackend(HPCBackend):
        scheduler_name = "fakecrowdfactory"

        def __init__(self, ctx: BackendBuildContext):
            self.ctx = ctx
            self.log_dir = ""

        @classmethod
        def from_build_context(cls, ctx: BackendBuildContext) -> HPCBackend:
            return cls(ctx)

        def _build_command(self, *a, **k):  # pragma: no cover - never called
            raise NotImplementedError

    yield _FakeCrowdBackend
    _REGISTRY.pop("fakecrowdfactory", None)


class TestPluginConstructionSeam:
    def test_registered_backend_constructs_from_build_context(self, crowd_backend):
        backend = build_remote_backend(backend_name="fakecrowdfactory", **_FACTORY_KWARGS)
        assert isinstance(backend, crowd_backend)
        # The context carries every factory input, so the backend owns
        # the decision of which fields matter.
        ctx = backend.ctx
        assert ctx.backend_name == "fakecrowdfactory"
        assert ctx.script == "/remote/job.sh"
        assert ctx.ssh_target == "user@login.example.edu"
        assert ctx.remote_path == "/remote/repo"
        assert ctx.pass_env_keys is None
        assert ctx.job_env_keys == ("EXECUTOR",)
        assert ctx.slurm_account == "acct"
        assert ctx.slurm_cluster == "clu"
        # Bound transport is present for SSH-shaped plugin backends
        # (not invoked here — that would SSH for real).
        assert callable(ctx.ssh_run)

    def test_registered_backend_without_hook_is_loud(self):
        @register("fakecrowdnohook")
        class _NoHookBackend(HPCBackend):
            scheduler_name = "fakecrowdnohook"

            def _build_command(self, *a, **k):  # pragma: no cover - never called
                raise NotImplementedError

        try:
            with pytest.raises(NotImplementedError, match="from_build_context"):
                build_remote_backend(backend_name="fakecrowdnohook", **_FACTORY_KWARGS)
        finally:
            _REGISTRY.pop("fakecrowdnohook", None)

    def test_unregistered_name_still_raises_unknown_backend(self):
        with pytest.raises(errors.SpecInvalid, match="unknown backend"):
            build_remote_backend(backend_name="no-such-backend", **_FACTORY_KWARGS)

    def test_builtin_ladder_unaffected(self, crowd_backend):
        # A built-in family must keep taking the inline ladder, never the
        # plugin seam, even while a plugin backend is registered.
        backend = build_remote_backend(backend_name="sge", **_FACTORY_KWARGS)
        assert type(backend).__name__ == "RemoteSGEBackend"
