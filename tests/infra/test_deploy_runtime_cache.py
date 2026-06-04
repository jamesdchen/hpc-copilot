"""Content-hash deploy cache for :func:`deploy_runtime` (#242).

``deploy_runtime`` records a manifest (``.hpc/.deploy_state.json``) of each
shipped file's sha256 + the producing package version, and on a re-deploy
skips any file whose sha and package version both still match. These tests
patch the ssh prelude (which carries the manifest read) and the scp
subprocess so we can assert *which* files cross the wire on the second run.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hpc_agent.infra import transport


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")
    # The cache is the subject under test — make sure no ambient opt-out hides it.
    monkeypatch.delenv("HPC_NO_DEPLOY_CACHE", raising=False)


def _scp_dsts(mock_run) -> list[str]:
    """Remote destinations (last argv token) of every scp subprocess call."""
    return [c[0][0][-1] for c in mock_run.call_args_list]


def _run_deploy(*, manifest_stdout: str):
    """Run deploy_runtime with the ssh prelude returning *manifest_stdout*.

    Returns the list of scp destinations issued during the deploy.
    """
    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout=manifest_stdout, stderr=""),
        ),
        patch("hpc_agent.infra.transport.subprocess.run") as mock_run,
    ):
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        transport.deploy_runtime(ssh_target="u@c", remote_path="/p", scheduler="sge")
    return _scp_dsts(mock_run)


def test_full_cache_hit_issues_zero_scps():
    # The cluster already holds exactly what we'd deploy: the remote manifest
    # matches the locally-computed one. No file — not even the manifest — is
    # re-shipped.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dsts = _run_deploy(manifest_stdout=json.dumps(manifest))
    assert dsts == [], dsts


def test_absent_manifest_deploys_everything_then_writes_manifest():
    # First-ever deploy: cat printed nothing → no remote manifest → every file
    # ships, and the run records the manifest for next time.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dsts = _run_deploy(manifest_stdout="")
    # One scp per enumerated file, plus the manifest write.
    assert len(dsts) == len(manifest["files"]) + 1
    assert any(d.endswith("/.hpc/.deploy_state.json") for d in dsts)


def test_touching_one_file_redeploys_only_that_file():
    # Remote manifest matches except one entry whose sha is stale → exactly
    # that file re-ships (plus the manifest rewrite to refresh its sha).
    manifest = transport._local_deploy_manifest(scheduler="sge")
    target_rel = ".hpc/_hpc_dispatch.py"
    assert target_rel in manifest["files"]
    stale = json.loads(json.dumps(manifest))
    stale["files"][target_rel] = "0" * 64  # a sha that can't match real bytes

    dsts = _run_deploy(manifest_stdout=json.dumps(stale))
    file_dsts = [d for d in dsts if not d.endswith("/.hpc/.deploy_state.json")]
    assert len(file_dsts) == 1, file_dsts
    assert file_dsts[0].endswith("/.hpc/_hpc_dispatch.py")
    # The manifest is rewritten so the refreshed sha persists.
    assert any(d.endswith("/.hpc/.deploy_state.json") for d in dsts)


def test_pkg_version_mismatch_redeploys_everything():
    # Same file shas, but the manifest was written by a different package
    # version: a wheel upgrade invalidates the whole cache (mitigation a).
    manifest = transport._local_deploy_manifest(scheduler="sge")
    old = json.loads(json.dumps(manifest))
    old["pkg_version"] = "0.0.0-ancient"
    dsts = _run_deploy(manifest_stdout=json.dumps(old))
    file_dsts = [d for d in dsts if not d.endswith("/.hpc/.deploy_state.json")]
    assert len(file_dsts) == len(manifest["files"])


def test_corrupt_manifest_falls_back_to_full_deploy():
    # Unparseable manifest → safe fallback: deploy everything (mitigation b).
    dsts = _run_deploy(manifest_stdout="{ this is not valid json")
    manifest = transport._local_deploy_manifest(scheduler="sge")
    file_dsts = [d for d in dsts if not d.endswith("/.hpc/.deploy_state.json")]
    assert len(file_dsts) == len(manifest["files"])


def test_no_deploy_cache_env_skips_manifest_entirely(monkeypatch):
    # HPC_NO_DEPLOY_CACHE=1 forces a full deploy and writes no manifest, even
    # when the cluster manifest would have produced a full hit.
    monkeypatch.setenv("HPC_NO_DEPLOY_CACHE", "1")
    manifest = transport._local_deploy_manifest(scheduler="sge")
    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(manifest), stderr=""),
        ) as mock_ssh,
        patch("hpc_agent.infra.transport.subprocess.run") as mock_run,
    ):
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        transport.deploy_runtime(ssh_target="u@c", remote_path="/p", scheduler="sge")
    dsts = _scp_dsts(mock_run)
    # Full deploy, no manifest write.
    assert len(dsts) == len(manifest["files"])
    assert not any(d.endswith("/.hpc/.deploy_state.json") for d in dsts)
    # And the prelude ssh did NOT append a manifest `cat` (cache disabled).
    prelude_cmd = mock_ssh.call_args[0][0]
    assert ".hpc/.deploy_state.json" not in prelude_cmd


def test_use_cache_false_param_overrides_default():
    # The explicit param wins like the env var: a full deploy, no manifest.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(manifest), stderr=""),
        ),
        patch("hpc_agent.infra.transport.subprocess.run") as mock_run,
    ):
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        transport.deploy_runtime(
            ssh_target="u@c", remote_path="/p", scheduler="sge", use_cache=False
        )
    dsts = _scp_dsts(mock_run)
    assert len(dsts) == len(manifest["files"])
    assert not any(d.endswith("/.hpc/.deploy_state.json") for d in dsts)
