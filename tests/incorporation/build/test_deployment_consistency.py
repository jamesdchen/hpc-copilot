"""S5 — deployment-consistency guard (incident 6).

Three layers, all anchored on the REPO_DIR ↔ deploy-target identity:

1. **build-time REPO_DIR invariant** — a job_env REPO_DIR that diverges from the
   deploy target derived from remote_path → ``SpecInvalid`` (repo_dir_deploy_mismatch).
2. **post-deploy remote existence preflight** — ``test -f "$REPO_DIR/<executor>"``
   over a MOCKED ssh → ``executor_missing_at_repo_dir`` when the file is absent.
3. **build-time static manifest check** — an executor whose script is present
   locally but stripped by an rsync exclude → ``SpecInvalid``
   (executor_not_in_deploy_manifest).

The build-time tests reuse the directory's autouse clusters.yaml isolation
(``conftest.py``); ``_required()`` mirrors ``test_submit_spec.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent.incorporation.build.submit_spec import build_submit_spec
from hpc_agent.infra.backends._remote_base import (
    deploy_target_for,
    executor_script_path,
    preflight_executor_exists,
)


def _required() -> dict:
    return dict(
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="alice@h2.idre.ucla.edu",
        remote_path="/u/scratch/alice/exp",
        run_id="20260101-000000-deadbee",
        cmd_sha="a" * 64,
        total_tasks=42,
        backend="sge",
        conda_env="ml-py311",
        conda_source="/u/local/apps/anaconda3/2024.06/etc/profile.d/conda.sh",
    )


# --- the single deploy-target derivation -----------------------------------


def test_deploy_target_for_normalises_trailing_slash() -> None:
    assert deploy_target_for("/u/scratch/alice/exp/") == "/u/scratch/alice/exp"
    assert deploy_target_for("/u/scratch/alice/exp") == "/u/scratch/alice/exp"


def test_executor_script_path_extracts_first_py_token() -> None:
    assert executor_script_path("python3 executors/foo.py --seed $SEED") == "executors/foo.py"


def test_executor_script_path_none_for_one_liner_and_module() -> None:
    # The canonical register_run one-liner and a -m / run-module dispatch carry
    # no checkable .py token → None (the preflight no-ops on these).
    assert executor_script_path('python3 -c "import runpy as _r; _r.run_path(x)"') is None
    assert executor_script_path("python3 -m hpc_agent.executor_cli run-module pkg.mod:fn") is None
    assert executor_script_path("") is None


# --- Layer 1: build-time REPO_DIR ↔ deploy-target invariant -----------------


def test_repo_dir_defaults_to_deploy_target() -> None:
    """The happy path: REPO_DIR is the normalised deploy target of remote_path."""
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(**{**_required(), "remote_path": "/u/scratch/alice/exp/"})
    )
    assert spec["job_env"]["REPO_DIR"] == "/u/scratch/alice/exp"
    assert spec["remote_path"] == "/u/scratch/alice/exp/"


def test_divergent_repo_dir_override_refused() -> None:
    """A stale/hand-rolled REPO_DIR in extra_env that diverges from the deploy
    target (the 2026-06 live-canary drift class) → SpecInvalid at build."""
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(
            spec=BuildSubmitSpecInput(
                **_required(),
                extra_env={"REPO_DIR": "/u/scratch/alice/hpc-demo"},
            )
        )
    msg = str(excinfo.value)
    assert "repo_dir_deploy_mismatch" in msg
    assert "/u/scratch/alice/hpc-demo" in msg  # the divergent value
    assert "/u/scratch/alice/exp" in msg  # the deploy target


def test_matching_repo_dir_override_passes() -> None:
    """Back-compat: a REPO_DIR override that EQUALS remote_path is fine (it does
    not diverge), so a caller that already threads the correct value is unaffected."""
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            extra_env={"REPO_DIR": "/u/scratch/alice/exp"},
        )
    )
    assert spec["job_env"]["REPO_DIR"] == "/u/scratch/alice/exp"


def test_matching_repo_dir_override_passes_with_trailing_slash() -> None:
    """A trailing-slash difference is not drift — both normalise to the same
    target, so the override is accepted (kept verbatim; `cd` tolerates it)."""
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            extra_env={"REPO_DIR": "/u/scratch/alice/exp/"},
        )
    )
    # No SpecInvalid raised; the override normalises to the deploy target.
    assert spec["job_env"]["REPO_DIR"].rstrip("/") == "/u/scratch/alice/exp"


# --- Layer 2: post-deploy remote existence preflight (mocked SSH) -----------


class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_preflight_passes_when_executor_present() -> None:
    calls: list[tuple[str, str]] = []

    def fake_ssh(cmd: str, *, ssh_target: str) -> _FakeProc:
        calls.append((cmd, ssh_target))
        return _FakeProc(0)  # test -f succeeded

    preflight_executor_exists(
        ssh_target="alice@h2",
        remote_path="/u/scratch/alice/exp/",
        executor="python train.py --seed $SEED",
        ssh_run=fake_ssh,
    )
    # The probe targets REPO_DIR/<script>, normalised + quoted.
    assert calls == [("test -f /u/scratch/alice/exp/train.py", "alice@h2")]


def test_preflight_raises_executor_missing_when_absent() -> None:
    def fake_ssh(cmd: str, *, ssh_target: str) -> _FakeProc:
        return _FakeProc(1)  # test -f failed: file not under REPO_DIR

    with pytest.raises(errors.RemoteCommandFailed) as excinfo:
        preflight_executor_exists(
            ssh_target="alice@h2",
            remote_path="/u/scratch/alice/demo-hpc",
            executor="python train.py --seed $SEED",
            ssh_run=fake_ssh,
        )
    msg = str(excinfo.value)
    assert "executor_missing_at_repo_dir" in msg
    assert "train.py" in msg
    assert "/u/scratch/alice/demo-hpc" in msg


def test_preflight_noops_on_one_liner_executor() -> None:
    """The canonical register_run one-liner has no .py token to probe — the
    preflight must issue ZERO ssh calls (no file to test -f)."""
    called = False

    def fake_ssh(cmd: str, *, ssh_target: str) -> _FakeProc:
        nonlocal called
        called = True
        return _FakeProc(0)

    preflight_executor_exists(
        ssh_target="alice@h2",
        remote_path="/u/scratch/alice/exp",
        executor="python3 -c \"import runpy as _r; _r.run_path('x.py')\"",
        ssh_run=fake_ssh,
    )
    assert called is False


def test_preflight_absolute_script_probed_verbatim() -> None:
    calls: list[str] = []

    def fake_ssh(cmd: str, *, ssh_target: str) -> _FakeProc:
        calls.append(cmd)
        return _FakeProc(0)

    preflight_executor_exists(
        ssh_target="alice@h2",
        remote_path="/u/scratch/alice/exp",
        executor="python /opt/shared/train.py",
        ssh_run=fake_ssh,
    )
    # An absolute path is probed verbatim, NOT anchored under REPO_DIR.
    assert calls == ["test -f /opt/shared/train.py"]


# --- Layer 2 (integration): the submit-flow prelude wires it in -------------


def test_prelude_runs_executor_preflight_after_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared prelude issues the existence preflight (mocked ssh_run) after
    the deploy arm, surfacing a missing executor before any canary is scheduled."""
    from hpc_agent.ops import submit_flow as sf

    # Stub the network arms: connectivity gate + uv probe + rsync/deploy all no-op.
    monkeypatch.setattr(sf, "_preflight_probe", lambda *a, **k: None)
    monkeypatch.setattr(sf, "_validate_ssh_target", lambda t: t)
    monkeypatch.setattr(sf, "_run_uv_preflight_for_batch", lambda **k: None)
    monkeypatch.setattr(sf, "_push_and_deploy", lambda **k: None)

    # The deployed tree is MISSING the executor → test -f returns non-zero.
    import hpc_agent.infra.remote as remote_mod

    monkeypatch.setattr(remote_mod, "ssh_run", lambda cmd, *, ssh_target: _FakeProc(1))

    with pytest.raises(errors.RemoteCommandFailed) as excinfo:
        sf._run_shared_prelude(
            experiment_dir=Path("/tmp/exp"),
            ssh_target="alice@h2",
            remote_path="/u/scratch/alice/demo-hpc",
            rsync_excludes=None,
            scheduler="sge",
            job_envs=[{"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"}],
            skip_preflight=False,
            skip_prelude_io=False,
            per_task_executors=["python train.py --seed $SEED"],
        )
    assert "executor_missing_at_repo_dir" in str(excinfo.value)


def test_prelude_skips_preflight_when_deploy_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the rsync+deploy arm is skipped (skip_prelude_io — a prior phase
    deployed), the existence preflight is skipped too: there is no freshly
    deployed tree to verify and no ssh should fire."""
    from hpc_agent.ops import submit_flow as sf

    monkeypatch.setattr(sf, "_preflight_probe", lambda *a, **k: None)
    monkeypatch.setattr(sf, "_validate_ssh_target", lambda t: t)
    monkeypatch.setattr(sf, "_run_uv_preflight_for_batch", lambda **k: None)

    import hpc_agent.infra.remote as remote_mod

    fired = False

    def fake_ssh(cmd, *, ssh_target):
        nonlocal fired
        fired = True
        return _FakeProc(1)

    monkeypatch.setattr(remote_mod, "ssh_run", fake_ssh)

    # Should NOT raise and should NOT fire ssh — the preflight is skipped.
    sf._run_shared_prelude(
        experiment_dir=Path("/tmp/exp"),
        ssh_target="alice@h2",
        remote_path="/u/scratch/alice/demo-hpc",
        rsync_excludes=None,
        scheduler="sge",
        job_envs=[{"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"}],
        skip_preflight=False,
        skip_prelude_io=True,
        per_task_executors=["python train.py --seed $SEED"],
    )
    assert fired is False


# --- Layer 3: build-time static deploy-manifest check -----------------------


def _write_executor_file(tmp_path: Path, rel: str) -> None:
    # Plain script content — NOT register_run-decorated, so the bare-script
    # guard (_check_register_run_executor) lets it through and this test
    # isolates the manifest layer.
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("import argparse\nprint('train')\n", encoding="utf-8")


def test_static_manifest_refuses_excluded_executor(tmp_path: Path) -> None:
    """An executor whose script is present locally but stripped by an rsync
    exclude lands no file at REPO_DIR — refused statically at build time."""
    _write_executor_file(tmp_path, "scratchwork/train.py")
    with pytest.raises(errors.SpecInvalid) as excinfo:
        build_submit_spec(
            experiment_dir=tmp_path,
            spec=BuildSubmitSpecInput(
                **_required(),
                extra_env={"EXECUTOR": "python scratchwork/train.py --seed $SEED"},
                rsync_excludes=["scratchwork/"],
            ),
        )
    msg = str(excinfo.value)
    assert "executor_not_in_deploy_manifest" in msg
    assert "scratchwork/train.py" in msg


def test_static_manifest_passes_shipped_executor(tmp_path: Path) -> None:
    """An executor in the deployed bundle (not excluded) passes the manifest
    layer cleanly — it ships under remote_path, so no refusal."""
    _write_executor_file(tmp_path, "executors/train.py")
    spec = build_submit_spec(
        experiment_dir=tmp_path,
        spec=BuildSubmitSpecInput(
            **_required(),
            extra_env={"EXECUTOR": "python executors/train.py --seed $SEED"},
            rsync_excludes=["scratchwork/"],
        ),
    )
    assert spec["job_env"]["EXECUTOR"] == "python executors/train.py --seed $SEED"


def test_static_manifest_noops_without_experiment_dir() -> None:
    """No experiment_dir → the deploy set's local root is unknown → skip
    (conservative: only refuse on a provable miss)."""
    spec = build_submit_spec(
        spec=BuildSubmitSpecInput(
            **_required(),
            extra_env={"EXECUTOR": "python anything/train.py"},
        )
    )
    assert spec["job_env"]["EXECUTOR"] == "python anything/train.py"


def test_static_manifest_noops_on_one_liner(tmp_path: Path) -> None:
    """The canonical one-liner has no script token → manifest check no-ops even
    when an exclude is present."""
    spec = build_submit_spec(
        experiment_dir=tmp_path,
        spec=BuildSubmitSpecInput(
            **_required(),
            rsync_excludes=["scratchwork/"],
        ),
    )
    # Default EXECUTOR is the dispatcher command (no user .py) → no refusal.
    assert spec["job_env"]["EXECUTOR"] == "python3 .hpc/_hpc_dispatch.py"
