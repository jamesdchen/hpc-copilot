"""Stage 1 of pure-API campaign-loop support: the submit prelude.

A pure-API backend (``requires_ssh=False``) has no login node and no shared
filesystem, so :func:`hpc_agent.ops.submit_flow._run_shared_prelude` must skip
its SSH probe / ``command -v uv`` / rsync+deploy wholesale and let the backend's
own ``_execute_command`` dispatch over its API. These tests pin the prelude's
early-return both ways.

The end-to-end batch-driver path — a pure-API ``SubmitFlowSpec`` threading
``requires_ssh=False`` all the way through ``submit_flow_batch`` — lands with the
backend-name widening: ``_wire/_shared.BackendName`` is still a ``Literal`` over
the four built-in schedulers, so a plugin backend cannot yet be expressed as a
spec. That widening is the open orchestrator/backend-boundary decision.
"""

from __future__ import annotations

from hpc_agent.ops import submit_flow as sf


def _boom(*a: object, **k: object) -> object:
    raise AssertionError("pure-API prelude must not touch SSH")


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
