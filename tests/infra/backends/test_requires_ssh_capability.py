"""Tests for the ``requires_ssh`` capability + ``fetch_results`` / ``fetch_logs`` hooks.

Pure-API ("crowd-compute") backend support
(``docs/proposals/crowd-compute-backend.md``) branches the submit / preflight /
monitor / aggregate flows on a backend's ``requires_ssh`` class attribute rather
than re-parsing the scheduler name. These tests pin the base-class default, the
built-in ladder's value, the ``backend_requires_ssh`` accessor (which reads the
capability off the class WITHOUT constructing it), and the loud defaults of the
two artifact-pull hooks a pure-API backend overrides.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import backends
from hpc_agent.infra.backends import HPCBackend, backend_requires_ssh, register


def test_base_class_defaults_to_requires_ssh() -> None:
    # The SSH ladder is the safe default; only a pure-API backend opts out.
    assert HPCBackend.requires_ssh is True


def test_builtin_backends_require_ssh() -> None:
    for name in ("slurm", "sge", "pbspro", "torque"):
        assert backend_requires_ssh(name) is True, name


def test_pure_api_backend_opts_out() -> None:
    @register("fake-pure-api")
    class FakePureApiBackend(HPCBackend):
        scheduler_name = "fake-pure-api"
        requires_ssh = False

        def _build_command(self, *a: object, **k: object) -> list[str]:
            raise NotImplementedError

    try:
        assert FakePureApiBackend.requires_ssh is False
        # Read off the class without constructing it — the prelude's path.
        assert backend_requires_ssh("fake-pure-api") is False
    finally:
        backends._REGISTRY.pop("fake-pure-api", None)


def test_unknown_backend_conservatively_requires_ssh() -> None:
    # A genuinely unknown name fails later at construction; the gate must not
    # flip the capability to False (which would skip a needed SSH preflight).
    assert backend_requires_ssh("no-such-backend-xyz") is True


def test_fetch_hooks_default_to_not_implemented() -> None:
    # SSH backends never reach these hooks (results come back over rsync); a
    # pure-API backend overrides them. The base must be loud, like the other
    # capability hooks (alive_job_ids, query_jobs, ...).
    class BareBackend(HPCBackend):
        log_dir = "."

        def _build_command(self, *a: object, **k: object) -> list[str]:
            raise NotImplementedError

    backend = BareBackend()
    with pytest.raises(NotImplementedError, match="fetch_results"):
        backend.fetch_results("run-1", "/tmp/dest")
    with pytest.raises(NotImplementedError, match="fetch_logs"):
        backend.fetch_logs("run-1")
