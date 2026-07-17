"""Transfer-plane pipe + deadline + probe-garble drills.

The tar-stream pipes are the justified streaming exemption (AUDIT §2 / §8): the
contract they get instead of one-round-trip-returning-JSON is positive-evidence
of completion — a pump-side break forces rc != 0 so a truncated stream can never
read as success. These drills pin that, plus the fused-combine truncation
fallback, the bounded-runner deadline (tree-kill), and the version-probe
no-demotion contract.

Audit rows drilled (§7):
  * "Kill ssh mid-`tar|ssh` push/pull" → pump error → rc forced != 0
  * "Truncate the fused combine stream (no BATCH_END_SENTINEL)" → per-wave fallback
  * "Remote self-destruct fires / hung child" → ``run_capture_bounded`` deadline + reap
  * garbled ``ssh -V`` probe → ``_local_openssh_major`` None → NO demotion
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from hpc_agent.infra import ssh_options
from hpc_agent.infra.bounded_subprocess import run_capture_bounded
from hpc_agent.infra.transport import _combiner, _pull

from .conftest import proc, sleeper_argv

_PUMP = "hpc_agent.infra.transport._pull._pump_with_progress"


def _fake_ssh_proc(returncode: int = 0) -> MagicMock:
    """A stand-in for the ssh SOURCE ``Popen`` — no real ssh spawned."""
    m = MagicMock(name="ssh_proc")
    m.returncode = returncode
    m.stdout = MagicMock(name="ssh_stdout")  # truthy; the code closes it
    m.wait.return_value = returncode
    return m


def _install_fake_pull_subprocess(monkeypatch, *, ssh_rc: int, tar_rc: int) -> MagicMock:
    """Replace the ssh ``Popen`` + local ``tar x`` bounded runner in ``_pull`` so
    ``_pull_transfer`` exercises its rc-folding logic with NO real subprocesses."""
    fake_ssh = _fake_ssh_proc(returncode=ssh_rc)
    monkeypatch.setattr(_pull.subprocess, "Popen", lambda *a, **k: fake_ssh)
    monkeypatch.setattr(
        _pull, "run_capture_bounded", lambda *a, **k: proc(tar_rc, stdout="", stderr="")
    )
    return fake_ssh


def test_pull_pump_sever_forces_nonzero_rc(monkeypatch, sever_at, tmp_path) -> None:
    """AUDIT §7 'Kill ssh mid-pull' — a pump-thread break must NEVER read as success.

    Both pipe halves report rc 0 (the contrived mock), but the pump raised
    mid-stream: the ``pump_error`` fold forces rc = 1 and discloses 'pump error',
    so a truncated pull is refused, not trusted. (Pure-local: no ssh/tar spawned.)
    """
    _install_fake_pull_subprocess(monkeypatch, ssh_rc=0, tar_rc=0)
    sever_at(_PUMP, exc=ConnectionError, message="peer reset mid-stream")

    result = _pull._pull_transfer(
        ssh_target="user@h",
        remote_cmd="tar c .",
        local_path=tmp_path,
        codec_flag=None,
        total_bytes=1_000,
        timeout=5.0,
    )
    assert result.returncode != 0  # DOCTRINE: truncated stream is not success
    assert "pump error" in result.stderr


def test_pull_pump_hang_is_bounded_and_reaps_ssh(monkeypatch, hang_at, tmp_path) -> None:
    """AUDIT §7 pump-kill: a HUNG pump cannot wedge the pull — the pump-thread join
    deadline fires and the ssh SOURCE is reaped (no leak). Short test-tuned waits.
    """
    fake_ssh = _install_fake_pull_subprocess(monkeypatch, ssh_rc=0, tar_rc=0)
    hang_at(_PUMP, seconds=1.0)  # pump blocks past the caller's tiny join deadline

    _pull._pull_transfer(
        ssh_target="user@h",
        remote_cmd="tar c .",
        local_path=tmp_path,
        codec_flag=None,
        total_bytes=1_000,
        timeout=0.05,  # join_timeout derives from this — fires well before 1.0 s
    )
    assert fake_ssh.kill.called  # DOCTRINE: hung pump ⇒ ssh reaped, never wedged/leaked


def test_combine_batch_truncation_falls_back(garble_at) -> None:
    """AUDIT §7 'Truncate the fused combine stream (no BATCH_END_SENTINEL)' → the
    checked wrapper returns None (positive-evidence of a complete stream absent),
    so the caller degrades to per-wave combines (E3) — never parse-and-trust a
    truncated batch.
    """
    truncated = (
        f"{_combiner._WAVE_BEGIN_SENTINEL} 0\n"
        "combiner output for wave 0\n"
        f"{_combiner._WAVE_END_SENTINEL} 0 rc=0\n"
        f"{_combiner._WAVE_BEGIN_SENTINEL} 1\n"
        "...stream severed here, no WAVE_END, no BATCH_END..."
    )
    garble_at("hpc_agent.infra.transport._combiner.ssh_run", return_value=proc(0, stdout=truncated))
    result = _combiner.run_combiner_batch_checked(
        ssh_target="h", remote_path="/p", wave_forces=[(0, False), (1, False)], run_id="r"
    )
    assert result is None  # DOCTRINE: missing BATCH_END ⇒ refuse, fall back per-wave


def test_bounded_runner_deadline_fires_and_reaps() -> None:
    """AUDIT §7 bounded one-shot: a hung child hits the ``run_capture_bounded``
    deadline and raises ``TimeoutExpired`` promptly (tree-kill), never runs to the
    child's full lifetime. Real short-lived sleeper — the OS-pipe deadline is
    kernel-enforced, so this is the honest hang (not a mock).
    """
    with pytest.raises(subprocess.TimeoutExpired):
        run_capture_bounded(sleeper_argv(30.0), timeout_sec=0.5)


def test_garbled_version_probe_does_not_demote(garble_at) -> None:
    """AUDIT §7 'garbled probe output': an unparseable ``ssh -V`` yields
    ``_local_openssh_major() is None``, and the named-pipe capability MUST NOT
    retreat from its default on a probe hiccup — the ``_local_openssh_major``
    'don't demote on None' contract.
    """
    # Uncached: prove the parse itself yields None on garbage.
    garble_at(
        "hpc_agent.infra.ssh_options.subprocess.run",
        return_value=proc(0, stderr="garbled non-version bytes \x00\x01"),
    )
    assert ssh_options._local_openssh_major() is None

    # Consumer contract: a None probe does NOT demote the named-pipe default.
    ssh_options._windows_openssh_named_pipe_supported.cache_clear()
    try:
        assert ssh_options._windows_openssh_named_pipe_supported() is True
    finally:
        ssh_options._windows_openssh_named_pipe_supported.cache_clear()
