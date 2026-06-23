"""Pure-API campaign-loop support: the submit prelude and the batch driver.

A pure-API backend (``requires_ssh=False``) has no login node and no shared
filesystem, so :func:`hpc_agent.ops.submit_flow._run_shared_prelude` must skip
its SSH probe / ``command -v uv`` / rsync+deploy wholesale and let the backend's
own ``_execute_command`` dispatch over its API. The first two tests pin the
prelude's early-return both ways.

The end-to-end batch-driver path — a pure-API ``SubmitFlowSpec`` threading
``requires_ssh=False`` all the way through ``submit_flow_batch`` — was deferred
until the backend-name widening (#337, Class A): ``_wire/_shared.BackendName``
is no longer a ``Literal`` over the four built-ins but a registry-validated
``str``, so a registered plugin backend is now expressible as a spec. The final
test exercises that path: a spec naming a registered pure-API backend reaches
the batch's prelude with ``requires_ssh=False`` and touches zero SSH.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import backends
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops import submit_flow as sf


def _boom(*a: object, **k: object) -> object:
    raise AssertionError("pure-API prelude must not touch SSH")


@pytest.fixture
def pure_api_backend():
    """Register a pure-API backend (``requires_ssh=False``) for the batch test."""

    @backends.register("fakebatchbackend")
    class _FakeBatchBackend(HPCBackend):  # pragma: no cover - never executed
        scheduler_name = "fakebatchbackend"
        requires_ssh = False

        def _build_command(self, *a: object, **k: object) -> object:
            raise NotImplementedError

    try:
        yield "fakebatchbackend"
    finally:
        backends._REGISTRY.pop("fakebatchbackend", None)


def test_prelude_skips_all_ssh_for_pure_api(monkeypatch, tmp_path) -> None:
    # Every SSH/shared-FS touchpoint blows up; requires_ssh=False must still
    # return cleanly — proof the whole prelude is the gate, not just a probe.
    monkeypatch.setattr(sf, "_validate_ssh_target", _boom)
    monkeypatch.setattr(sf, "_preflight_probe", _boom)
    monkeypatch.setattr(sf, "_push_and_deploy", _boom)
    monkeypatch.setattr(sf, "_run_uv_preflight_for_batch", _boom)

    sf._run_shared_prelude(
        experiment_dir=tmp_path,
        ssh_target="ignored",
        remote_path="/ignored",
        rsync_excludes=None,
        scheduler="github-actions",
        job_envs=[{}],
        requires_ssh=False,
        skip_preflight=False,
        skip_prelude_io=False,
    )  # no raise → no SSH touched


def test_prelude_runs_ssh_path_when_required(monkeypatch, tmp_path) -> None:
    # Sanity: requires_ssh=True still reaches the ssh-target validator (stopped
    # there to avoid a real probe/rsync).
    seen: list[str] = []
    monkeypatch.setattr(sf, "_validate_ssh_target", lambda t: seen.append(t) or t)
    monkeypatch.setattr(sf, "_preflight_probe", lambda *a, **k: None)
    monkeypatch.setattr(sf, "_run_uv_preflight_for_batch", lambda **k: None)
    monkeypatch.setattr(sf, "_push_and_deploy", lambda **k: None)

    sf._run_shared_prelude(
        experiment_dir=tmp_path,
        ssh_target="u@h",
        remote_path="/r",
        rsync_excludes=None,
        scheduler="sge",
        job_envs=[{}],
        requires_ssh=True,
        skip_preflight=True,
        skip_prelude_io=True,
    )
    assert seen == ["u@h"]


def test_batch_threads_pure_api_backend_with_zero_ssh(
    monkeypatch, tmp_path, pure_api_backend
) -> None:
    # The headline of #337 Class A: a SubmitFlowSpec naming a registered
    # pure-API backend is now *expressible* (the backend name validates), and
    # submit_flow_batch derives requires_ssh=False from it — so the real shared
    # prelude early-returns and touches no SSH. Every SSH touchpoint is
    # booby-trapped; only the per-spec submission (a later increment's concern)
    # is stubbed.
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
    from hpc_agent._wire.workflows.submit_flow_batch import SubmitFlowBatchSpec

    monkeypatch.setenv("HPC_SUBMIT_NO_LOCK", "1")
    monkeypatch.setattr(sf, "_validate_ssh_target", _boom)
    monkeypatch.setattr(sf, "_preflight_probe", _boom)
    monkeypatch.setattr(sf, "_push_and_deploy", _boom)
    monkeypatch.setattr(sf, "_run_uv_preflight_for_batch", _boom)
    monkeypatch.setattr(sf, "_ensure_run_sidecar", lambda *a, **k: None)

    # Capture the requires_ssh the batch computed, then run the real prelude
    # (which must early-return for a pure-API backend — proof, not assumption).
    seen: dict[str, object] = {}
    real_prelude = sf._run_shared_prelude

    def _spy_prelude(**kwargs: object) -> object:
        seen["requires_ssh"] = kwargs["requires_ssh"]
        return real_prelude(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sf, "_run_shared_prelude", _spy_prelude)

    sentinel = object()
    monkeypatch.setattr(sf, "_submit_one_spec", lambda **k: sentinel)

    spec = SubmitFlowSpec(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote/exp",
        job_name="j",
        run_id="r-1",
        total_tasks=2,
        backend=pure_api_backend,
        script=".hpc/templates/cpu_array.sh",
        job_env={},
        canary=False,
    )
    batch = SubmitFlowBatchSpec(specs=[spec])

    results = sf.submit_flow_batch(tmp_path, spec=batch)

    assert seen["requires_ssh"] is False  # derived from the pure-API backend
    assert results == [sentinel]  # per-spec submission reached, no SSH raised
