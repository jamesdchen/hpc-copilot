"""Content-hash deploy cache for :func:`deploy_runtime` (#242).

``deploy_runtime`` records a manifest (``.hpc/.deploy_state.json``) of each
shipped file's sha256 + the producing package version, and on a re-deploy
skips any file whose sha and package version both still match. Since #252 the
surviving files ship in ONE batched transfer (rsync delta / tar fallback), so
these tests patch the ssh prelude (which carries the manifest read) and the
``_deploy_transfer`` seam to assert *which* dst_rels cross the wire.

Since #F53 the manifest itself NO LONGER rides that transfer — it is written in
a separate ssh leg (``_write_deploy_manifest``) only AFTER the files land, so
an interrupted transfer can never leave a manifest attesting un-shipped code.
The helper therefore also captures that separate write.
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

    Returns ``(dst_rels, prelude_cmd, manifest_written)`` — the list of dst_rels
    handed to the single batched transfer (empty when nothing shipped), the
    prelude ssh command string, and the JSON content the SEPARATE manifest-write
    leg (#F53) recorded (``None`` when no manifest was written).
    """
    captured: dict[str, object] = {"dst_rels": [], "manifest_written": None}

    def _capture(*, ssh_target, remote_path, items):
        captured["dst_rels"] = [it.dst_rel for it in items]

    def _capture_manifest(*, ssh_target, remote_path, content):
        captured["manifest_written"] = content

    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout=manifest_stdout, stderr=""),
        ) as mock_ssh,
        patch("hpc_agent.infra.transport._deploy_transfer", side_effect=_capture),
        patch("hpc_agent.infra.transport._write_deploy_manifest", side_effect=_capture_manifest),
    ):
        transport.deploy_runtime(
            ssh_target="u@c", remote_path="/p", scheduler="sge", use_cache=use_cache
        )
    return captured["dst_rels"], mock_ssh.call_args[0][0], captured["manifest_written"]


_MANIFEST_REL = transport._DEPLOY_MANIFEST_REL


def test_full_cache_hit_ships_nothing():
    # The cluster already holds exactly what we'd deploy: the remote manifest
    # matches the locally-computed one. No file — not even the manifest — is
    # re-shipped, so no transfer fires.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dst_rels, _, manifest_written = _run_deploy(manifest_stdout=json.dumps(manifest))
    assert dst_rels == [], dst_rels
    assert manifest_written is None  # unchanged → no manifest rewrite either


def test_absent_manifest_deploys_everything_then_writes_manifest():
    # First-ever deploy: cat printed nothing → no remote manifest → every file
    # ships in the one transfer. The manifest does NOT ride that transfer (#F53);
    # it is written in the separate leg AFTER, recording exactly what shipped.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dst_rels, _, manifest_written = _run_deploy(manifest_stdout="")
    assert _MANIFEST_REL not in dst_rels  # never rides the file transfer
    assert sorted(dst_rels) == sorted(manifest["files"])
    # The separate manifest-write leg fired, recording the full local manifest.
    assert manifest_written is not None
    assert json.loads(manifest_written) == manifest


def test_touching_one_file_redeploys_only_that_file():
    # Remote manifest matches except one entry whose sha is stale → exactly
    # that file ships. The manifest rewrite (refreshing its sha) is the SEPARATE
    # post-transfer leg (#F53), never part of the transfer item set.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    target_rel = ".hpc/_hpc_dispatch.py"
    assert target_rel in manifest["files"]
    stale = json.loads(json.dumps(manifest))
    stale["files"][target_rel] = "0" * 64  # a sha that can't match real bytes

    dst_rels, _, manifest_written = _run_deploy(manifest_stdout=json.dumps(stale))
    assert dst_rels == [target_rel], dst_rels
    assert _MANIFEST_REL not in dst_rels
    assert manifest_written is not None
    assert json.loads(manifest_written) == manifest


def test_pkg_version_mismatch_redeploys_everything():
    # Same file shas, but the manifest was written by a different package
    # version: a wheel upgrade invalidates the whole cache (mitigation a).
    manifest = transport._local_deploy_manifest(scheduler="sge")
    old = json.loads(json.dumps(manifest))
    old["pkg_version"] = "0.0.0-ancient"
    dst_rels, _, _ = _run_deploy(manifest_stdout=json.dumps(old))
    assert _MANIFEST_REL not in dst_rels
    assert sorted(dst_rels) == sorted(manifest["files"])


def test_corrupt_manifest_falls_back_to_full_deploy():
    # Unparseable manifest → safe fallback: deploy everything (mitigation b).
    dst_rels, _, _ = _run_deploy(manifest_stdout="{ this is not valid json")
    manifest = transport._local_deploy_manifest(scheduler="sge")
    assert _MANIFEST_REL not in dst_rels
    assert sorted(dst_rels) == sorted(manifest["files"])


def test_no_deploy_cache_env_skips_manifest_entirely(monkeypatch):
    # HPC_NO_DEPLOY_CACHE=1 forces a full deploy and ships no manifest, even
    # when the cluster manifest would have produced a full hit.
    monkeypatch.setenv("HPC_NO_DEPLOY_CACHE", "1")
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dst_rels, prelude_cmd, manifest_written = _run_deploy(manifest_stdout=json.dumps(manifest))
    assert sorted(dst_rels) == sorted(manifest["files"])
    assert _MANIFEST_REL not in dst_rels
    assert manifest_written is None  # cache disabled → no manifest write
    # And the prelude ssh did NOT append a manifest `cat` (cache disabled).
    assert ".hpc/.deploy_state.json" not in prelude_cmd


def test_use_cache_false_param_overrides_default():
    # The explicit param wins like the env var: a full deploy, no manifest.
    manifest = transport._local_deploy_manifest(scheduler="sge")
    dst_rels, _, manifest_written = _run_deploy(
        manifest_stdout=json.dumps(manifest), use_cache=False
    )
    assert sorted(dst_rels) == sorted(manifest["files"])
    assert _MANIFEST_REL not in dst_rels
    assert manifest_written is None


def test_interrupted_transfer_leaves_manifest_unwritten_so_retry_reships():
    """#F53 fire-path: a deploy whose FILE transfer is interrupted must NOT
    write the deploy-cache manifest — so the natural retry re-ships the code
    instead of reading a manifest that attests un-shipped files and reporting a
    false success over stale/torn framework code.

    Attempt 1: the transfer raises (network flap / SSH timeout). The manifest
    write must never fire, and deploy_runtime must surface the failure.
    Attempt 2 (retry): the cluster manifest is still absent (nothing was
    recorded), so every changed file re-ships and only THEN is the manifest
    written.
    """
    pkg_manifest = transport._local_deploy_manifest(scheduler="sge")

    manifest_writes: list[str] = []

    def _record_manifest(*, ssh_target, remote_path, content):
        manifest_writes.append(content)

    # --- Attempt 1: transfer dies mid-flight (after the manifest would have
    #     ridden it, under the OLD bug). ---
    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ),
        patch(
            "hpc_agent.infra.transport._deploy_transfer",
            side_effect=TimeoutError("transfer interrupted"),
        ),
        patch("hpc_agent.infra.transport._write_deploy_manifest", side_effect=_record_manifest),
        pytest.raises(TimeoutError),
    ):
        transport.deploy_runtime(ssh_target="u@c", remote_path="/p", scheduler="sge")

    # The manifest was NEVER written — the cluster still has no attestation, so
    # a retry cannot be fooled into shipping nothing.
    assert manifest_writes == []

    # --- Attempt 2: retry sees the still-absent manifest and re-ships. ---
    shipped: list[list[str]] = []

    def _capture_transfer(*, ssh_target, remote_path, items):
        shipped.append([it.dst_rel for it in items])

    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ),
        patch("hpc_agent.infra.transport._deploy_transfer", side_effect=_capture_transfer),
        patch(
            "hpc_agent.infra.transport._write_deploy_manifest", side_effect=_record_manifest
        ),
    ):
        transport.deploy_runtime(ssh_target="u@c", remote_path="/p", scheduler="sge")

    # Every file re-shipped (the interrupted attempt recorded nothing) ...
    assert len(shipped) == 1
    assert sorted(shipped[0]) == sorted(pkg_manifest["files"])
    assert _MANIFEST_REL not in shipped[0]
    # ... and only NOW, after a clean transfer, is the manifest recorded.
    assert len(manifest_writes) == 1
    assert json.loads(manifest_writes[0]) == pkg_manifest
