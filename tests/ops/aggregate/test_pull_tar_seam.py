"""Aggregate pulls route through O2's ``tar_ssh_pull`` seam (latency audit rank 2 WIRING).

Rank 2 (pull-engine parity) is O2's build; THIS module pins the aggregate-side
WIRING — the ``_pull`` adapter that prefers ``tar_ssh_pull`` when the seam exists
and falls back to the legacy ``rsync_pull`` otherwise, normalizing both to the
``(returncode, stderr)`` shape every call site consumes.

* The routing/normalization tests run STANDALONE by injecting a fake
  ``tar_ssh_pull`` onto the module (no dependency on O2).
* One test is skipif-gated on the REAL symbol existing — it activates
  automatically once O2 merges the seam (the WS6 pattern).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hpc_agent.ops import aggregate_flow as af_module


def _fake_pull_result(*, ok: bool, stderr_tail: str = "") -> Any:
    """A stand-in for O2's ``PullResult`` (only the fields the adapter reads)."""
    return SimpleNamespace(
        ok=ok,
        files_pulled=3,
        bytes_pulled=4096,
        skipped_unchanged=0,
        stderr_tail=stderr_tail,
    )


def test_legacy_path_used_when_seam_absent(monkeypatch):
    """With no ``tar_ssh_pull``, ``_pull`` is byte-for-byte the legacy rsync call."""
    monkeypatch.setattr(af_module, "_tar_ssh_pull", None)
    seen: dict[str, Any] = {}

    def _rsync(*, ssh_target, remote_path, remote_subdir, local_dir, include=None):
        seen.update(
            ssh_target=ssh_target,
            remote_path=remote_path,
            remote_subdir=remote_subdir,
            local_dir=local_dir,
            include=include,
        )
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(af_module, "rsync_pull", _rsync)

    out = af_module._pull(
        ssh_target="u@h",
        remote_path="/remote",
        remote_subdir="results/exp",
        local_dir="/local/dst",
        include=["metrics.json"],
    )

    assert out.returncode == 0
    # The legacy call received the split (remote_path, remote_subdir) unchanged.
    assert seen == {
        "ssh_target": "u@h",
        "remote_path": "/remote",
        "remote_subdir": "results/exp",
        "local_dir": "/local/dst",
        "include": ["metrics.json"],
    }


def test_routes_through_tar_seam_when_present(monkeypatch):
    """With the seam present, ``_pull`` calls ``tar_ssh_pull`` with the JOINED
    remote path + include globs, and normalizes ``PullResult.ok`` to returncode 0."""
    calls: dict[str, Any] = {}

    def _fake_tar(*, ssh_target, remote_path, local_path, include_globs=None, **_kw):
        calls.update(
            ssh_target=ssh_target,
            remote_path=remote_path,
            local_path=local_path,
            include_globs=include_globs,
        )
        return _fake_pull_result(ok=True)

    monkeypatch.setattr(af_module, "_tar_ssh_pull", _fake_tar)
    monkeypatch.delenv("HPC_AGGREGATE_TAR_PULL", raising=False)

    out = af_module._pull(
        ssh_target="u@h",
        remote_path="/remote/",
        remote_subdir="results/exp/",
        local_dir="/local/dst",
        include=["metrics.json"],
    )

    assert out.returncode == 0
    # remote_path + remote_subdir joined into ONE path; include -> find globs.
    assert calls == {
        "ssh_target": "u@h",
        "remote_path": "/remote/results/exp",
        "local_path": Path("/local/dst"),
        "include_globs": ["metrics.json"],
    }


def test_tar_failure_normalizes_to_nonzero_returncode(monkeypatch):
    """A ``PullResult(ok=False)`` maps to returncode!=0 + its stderr_tail, so the
    call sites' existing failure handling fires unchanged."""

    def _fake_tar(*, ssh_target, remote_path, local_path, include_globs=None, **_kw):
        return _fake_pull_result(ok=False, stderr_tail="tar: cannot stat")

    monkeypatch.setattr(af_module, "_tar_ssh_pull", _fake_tar)
    monkeypatch.delenv("HPC_AGGREGATE_TAR_PULL", raising=False)

    out = af_module._pull(
        ssh_target="u@h", remote_path="/remote", remote_subdir="results", local_dir="/d"
    )

    assert out.returncode != 0
    assert out.stderr == "tar: cannot stat"


def test_env_opt_out_forces_legacy_even_when_seam_present(monkeypatch):
    """``HPC_AGGREGATE_TAR_PULL=0`` forces the legacy rsync path as a safety knob."""

    def _fake_tar(**_kw):
        raise AssertionError("tar_ssh_pull must NOT run under the opt-out")

    monkeypatch.setattr(af_module, "_tar_ssh_pull", _fake_tar)
    monkeypatch.setenv("HPC_AGGREGATE_TAR_PULL", "0")
    monkeypatch.setattr(
        af_module, "rsync_pull", lambda **_kw: SimpleNamespace(returncode=0, stderr="")
    )

    out = af_module._pull(
        ssh_target="u@h", remote_path="/remote", remote_subdir="results", local_dir="/d"
    )
    assert out.returncode == 0


@pytest.mark.skipif(
    af_module._tar_ssh_pull is None,
    reason="O2's tar_ssh_pull seam not merged yet (rank 2 pull-engine parity)",
)
def test_real_tar_seam_is_callable_from_the_adapter():
    """Activates automatically once O2 merges: the real symbol exists and the
    adapter's contract (keyword args it passes) matches the frozen signature."""
    import inspect

    sig = inspect.signature(af_module._tar_ssh_pull)
    params = set(sig.parameters)
    # The exact keyword surface the adapter drives.
    assert {"ssh_target", "remote_path", "local_path", "include_globs"} <= params
