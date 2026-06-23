"""``backend_for_record`` constructs a backend from a run record via the registry.

The monitor/aggregate pure-API transports (#337 Increments 4–5) drive a run's
liveness / logs / results through the backend *instance* hooks, which need a
constructed backend. ``backend_for_record`` builds it through
``build_remote_backend`` → ``from_build_context`` so a registered pure-API
plugin backend is constructed the same way a built-in SSH family is — proof the
orchestrator routes through the registry, not a concrete backend module.
"""

from __future__ import annotations

from types import SimpleNamespace

from hpc_agent.infra import backends
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.infra.backends.remote_factory import backend_for_record


def _record(backend_name: str) -> SimpleNamespace:
    # Only the fields ``backend_for_record`` reads — duck-typed for the test.
    return SimpleNamespace(
        backend=backend_name,
        script=".hpc/templates/cpu_array.sh",
        ssh_target="u@h",
        remote_path="/remote/exp",
        job_env={"EXECUTOR": "x"},
    )


def test_builtin_slurm_builds_remote_backend() -> None:
    from hpc_agent.infra.backends.slurm_remote import RemoteSlurmBackend

    backend = backend_for_record(_record("slurm"))
    assert isinstance(backend, RemoteSlurmBackend)
    assert backend.requires_ssh is True


def test_scheduler_arg_overrides_record_backend() -> None:
    from hpc_agent.infra.backends.sge_remote import RemoteSGEBackend

    # The record names slurm, but the caller-held scheduler wins.
    backend = backend_for_record(_record("slurm"), scheduler="sge")
    assert isinstance(backend, RemoteSGEBackend)


def test_pure_api_plugin_built_via_from_build_context() -> None:
    built: dict[str, object] = {}

    @backends.register("fakeapibackend")
    class _FakeApiBackend(HPCBackend):
        scheduler_name = "fakeapibackend"
        requires_ssh = False

        def __init__(self, *, run_remote_path: str) -> None:
            self.run_remote_path = run_remote_path

        @classmethod
        def from_build_context(cls, ctx: object) -> _FakeApiBackend:
            # A pure-API backend ignores the SSH pair; record that it was
            # routed here with the record's fields.
            built["ctx_backend_name"] = ctx.backend_name  # type: ignore[attr-defined]
            return cls(run_remote_path=ctx.remote_path)  # type: ignore[attr-defined]

        def _build_command(self, *a: object, **k: object) -> object:
            raise NotImplementedError

    try:
        backend = backend_for_record(_record("fakeapibackend"))
        assert isinstance(backend, _FakeApiBackend)
        assert backend.requires_ssh is False
        assert built["ctx_backend_name"] == "fakeapibackend"
        assert backend.run_remote_path == "/remote/exp"
    finally:
        backends._REGISTRY.pop("fakeapibackend", None)
