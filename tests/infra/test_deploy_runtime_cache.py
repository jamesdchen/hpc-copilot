"""Content-hash deploy cache for :func:`deploy_runtime` (#242).

``deploy_runtime`` records a manifest (``.hpc/.deploy_state.json``) of each
shipped file's sha256 + the producing package version, and on a re-deploy
skips any file whose sha and package version both still match. Since #252 the
surviving files ship in ONE batched transfer (rsync delta / tar fallback), so
these tests patch the ssh prelude (which carries the manifest read) and the
``_deploy_transfer`` seam to assert *which* dst_rels cross the wire.
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


def _run_deploy(*, manifest_stdout: str, use_cache: bool | None = None):
    """Run deploy_runtime with the ssh prelude returning *manifest_stdout*.

    Returns ``(dst_rels, prelude_cmd)`` — the list of dst_rels handed to the
    single batched transfer (empty when nothing shipped), and the prelude
    ssh command string.
    """
    captured: dict[str, list[str]] = {"dst_rels": []}

    def _capture(*, ssh_target, remote_path, items):
        captured["dst_rels"] = [it.dst_rel for it in items]

    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout=manifest_stdout, stderr=""),
        ) as mock_ssh,
        patch("hpc_agent.infra.transport._deploy_transfer", side_effect=_capture),
    ):
        transport.deploy_runtime(
            ssh_target="u@c", remote_path="/p", scheduler="sge", use_cache=use_cache
        )
    return captured["dst_rels"], mock_ssh.call_args[0][0]


_MANIFEST_REL = transport._DEPLOY_MANIFEST_REL


def test_full_cache_hit_ships_nothing():
    # The cluster already holds exactly what we'd deploy: the remote manifest
    # matches the locally-computed one. No file — not even the manifest — is
    # re-shipped, so no transfer fires.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dst_rels, _ = _run_deploy(manifest_stdout=json.dumps(manifest))
    assert dst_rels == [], dst_rels


def test_absent_manifest_deploys_everything_then_writes_manifest():
    # First-ever deploy: cat printed nothing → no remote manifest → every file
    # ships in the one transfer, and the manifest rides along to record it.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dst_rels, _ = _run_deploy(manifest_stdout="")
    assert _MANIFEST_REL in dst_rels
    file_rels = [d for d in dst_rels if d != _MANIFEST_REL]
    assert sorted(file_rels) == sorted(manifest["files"])


def test_touching_one_file_redeploys_only_that_file():
    # Remote manifest matches except one entry whose sha is stale → exactly
    # that file ships (plus the manifest rewrite to refresh its sha).
    manifest = transport._local_deploy_manifest(scheduler="sge")
    target_rel = ".hpc/_hpc_dispatch.py"
    assert target_rel in manifest["files"]
    stale = json.loads(json.dumps(manifest))
    stale["files"][target_rel] = "0" * 64  # a sha that can't match real bytes

    dst_rels, _ = _run_deploy(manifest_stdout=json.dumps(stale))
    file_rels = [d for d in dst_rels if d != _MANIFEST_REL]
    assert file_rels == [target_rel], file_rels
    assert _MANIFEST_REL in dst_rels


def test_pkg_version_mismatch_redeploys_everything():
    # Same file shas, but the manifest was written by a different package
    # version: a wheel upgrade invalidates the whole cache (mitigation a).
    manifest = transport._local_deploy_manifest(scheduler="sge")
    old = json.loads(json.dumps(manifest))
    old["pkg_version"] = "0.0.0-ancient"
    dst_rels, _ = _run_deploy(manifest_stdout=json.dumps(old))
    file_rels = [d for d in dst_rels if d != _MANIFEST_REL]
    assert sorted(file_rels) == sorted(manifest["files"])


def test_corrupt_manifest_falls_back_to_full_deploy():
    # Unparseable manifest → safe fallback: deploy everything (mitigation b).
    dst_rels, _ = _run_deploy(manifest_stdout="{ this is not valid json")
    manifest = transport._local_deploy_manifest(scheduler="sge")
    file_rels = [d for d in dst_rels if d != _MANIFEST_REL]
    assert sorted(file_rels) == sorted(manifest["files"])


def test_no_deploy_cache_env_skips_manifest_entirely(monkeypatch):
    # HPC_NO_DEPLOY_CACHE=1 forces a full deploy and ships no manifest, even
    # when the cluster manifest would have produced a full hit.
    monkeypatch.setenv("HPC_NO_DEPLOY_CACHE", "1")
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dst_rels, prelude_cmd = _run_deploy(manifest_stdout=json.dumps(manifest))
    file_rels = [d for d in dst_rels if d != _MANIFEST_REL]
    assert sorted(file_rels) == sorted(manifest["files"])
    assert _MANIFEST_REL not in dst_rels
    # And the prelude ssh did NOT append a manifest `cat` (cache disabled).
    assert ".hpc/.deploy_state.json" not in prelude_cmd


def test_use_cache_false_param_overrides_default():
    # The explicit param wins like the env var: a full deploy, no manifest.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dst_rels, _ = _run_deploy(manifest_stdout=json.dumps(manifest), use_cache=False)
    file_rels = [d for d in dst_rels if d != _MANIFEST_REL]
    assert sorted(file_rels) == sorted(manifest["files"])
    assert _MANIFEST_REL not in dst_rels
