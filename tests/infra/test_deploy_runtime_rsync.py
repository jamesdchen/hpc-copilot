"""deploy_runtime ships its files in ONE batched transfer (#252).

The prior N-scp fan-out (#245) is replaced by a single ``rsync -az`` delta where
rsync is on PATH, with a single ``tar c | ssh tar x`` stream as the fallback
(native Windows). These tests assert exactly one transfer invocation and that a
failed transfer surfaces. The rsync leg is deliberately NOT ``--inplace`` (#F20):
an in-place rewrite tears the live ``.hpc/_hpc_dispatch.py`` under a concurrent
in-flight array; rsync's default temp-then-atomic-rename replaces it whole.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hpc_agent.infra import transport


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")


def _prelude_ok():
    # The mkdir/rm/cat prelude ssh: empty stdout → no remote manifest → full deploy.
    return patch(
        "hpc_agent.infra.transport.ssh_run",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    )


def test_deploy_issues_a_single_rsync_invocation():
    n_files = len(transport._build_deploy_items(scheduler="sge"))
    # 2 stubs + dispatch + combiner + 3 templates (cpu/gpu/mpi) + 2 preambles
    # + the 7-module status-reporter eager closure (#349): status, rollup,
    # task_id, vocabulary, errors, time, _guard.
    assert n_files == 16

    calls: list[list[str]] = []

    def _fake_run(argv, *_a, **_kw):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        _prelude_ok(),
        patch("hpc_agent.infra.transport._have_rsync", return_value=True),
        patch("hpc_agent.infra.transport.run_capture_bounded", side_effect=_fake_run),
    ):
        transport.deploy_runtime(
            ssh_target="u@c", remote_path="/p", scheduler="sge", use_cache=False
        )

    rsync_calls = [c for c in calls if c and c[0] == "rsync"]
    assert len(rsync_calls) == 1, f"expected ONE rsync, got {len(rsync_calls)}: {calls}"
    # Delta + archive flags, no --delete (deploy merges, never removes).
    rsync = rsync_calls[0]
    assert "-az" in rsync
    # #F20 fire-path: NO --inplace, so rsync writes a temp file then atomically
    # renames — a concurrent array task never execs a torn dispatcher/preamble.
    assert "--inplace" not in rsync
    assert "--delete" not in rsync
    assert rsync[-1] == "u@c:/p/"


def test_deploy_falls_back_to_tar_when_rsync_absent():
    captured: dict[str, object] = {}

    def _capture_tar(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        _prelude_ok(),
        patch("hpc_agent.infra.transport._have_rsync", return_value=False),
        patch("hpc_agent.infra.transport._tar_ssh_push", side_effect=_capture_tar) as tar,
    ):
        transport.deploy_runtime(
            ssh_target="u@c", remote_path="/p", scheduler="sge", use_cache=False
        )

    assert tar.call_count == 1
    # The fallback merges (never removes) the staged subset.
    assert captured["delete"] is False
    assert captured["ssh_target"] == "u@c"
    assert captured["remote_path"] == "/p"


def test_rsync_deploy_translates_win32_local_src(monkeypatch):
    # _rsync_deploy ships from a tempfile.TemporaryDirectory staging path; on
    # win32 that is a C:\...\Temp\tmpXXXX dir whose drive colon MSYS rsync
    # mis-parses as remote host "C" ("source and destination cannot both be
    # remote") — the exact break #10 fixes. The local src must reach argv in
    # the /c/... form.
    monkeypatch.setattr(transport.sys, "platform", "win32")
    with patch(
        "hpc_agent.infra.transport.run_capture_bounded",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    ) as run_mock:
        transport._rsync_deploy(
            ssh_target="u@c",
            remote_path="/p",
            staging=Path("D:\\Temp\\tmpABC"),
        )
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "rsync"
    # argv = ["rsync", "-az", src, dst] → src is second-to-last (no --inplace, #F20).
    assert cmd[-2] == "/d/Temp/tmpABC/"
    assert cmd[-1] == "u@c:/p/"


def test_rsync_nonzero_exit_raises():
    with (
        _prelude_ok(),
        patch("hpc_agent.infra.transport._have_rsync", return_value=True),
        patch(
            "hpc_agent.infra.transport.run_capture_bounded",
            return_value=SimpleNamespace(returncode=23, stdout="", stderr="rsync boom"),
        ),
        pytest.raises(RuntimeError, match="rsync deploy"),
    ):
        transport.deploy_runtime(
            ssh_target="u@c", remote_path="/p", scheduler="sge", use_cache=False
        )
