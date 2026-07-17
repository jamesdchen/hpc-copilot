"""Jobmap weave into the submit atom's TWO command shapes (U3-b, premortem Δ7/Δ3).

``RemoteHPCBackend._execute_command`` has a cached-bin DIRECT form and a
LOGIN-SHELL form. The submit-once jobmap marker must fold into BOTH, and — the
hard flag-off regression pin — with ``HPC_SUBMIT_ONCE`` unset the emitted command
string must be BYTE-IDENTICAL to the pre-U3 form.
"""

from __future__ import annotations

import shlex
from types import SimpleNamespace

import pytest

from hpc_agent.infra.backends._remote_base import RemoteHPCBackend


class _Backend(RemoteHPCBackend):
    """Minimal RemoteHPCBackend with a captured ``ssh_run`` (no real SSH)."""

    def __init__(self, remote_repo: str = "/home/u/demo") -> None:
        self.remote_repo = remote_repo
        self.log_dir = f"{remote_repo}/logs"
        self._resolved_bins: dict[str, str] = {}
        self.calls: list[str] = []

    def ssh_run(self, cmd: str):  # type: ignore[override]
        self.calls.append(cmd)
        # Emit a parseable Slurm id so submit_one-style parsing would succeed.
        return SimpleNamespace(returncode=0, stdout="Submitted batch job 12345\n", stderr="")


_QSUB = ["sbatch", "--array=1-4", "job.sh"]
_JOB_ENV = {"HPC_RUN_ID": "run-x", "HPC_SUBMIT_ATTEMPT": "0", "HPC_SUBMIT_WAVE_KEY": "wave-0"}


def _plain_login_inner(b: _Backend) -> str:
    from hpc_agent.infra.backends._remote_base import _BIN_MARKER

    cmd_str = " ".join(shlex.quote(a) for a in _QSUB)
    return (
        f'echo "{_BIN_MARKER}=$(command -v {shlex.quote(_QSUB[0])})" 1>&2; '
        f"cd {shlex.quote(b.remote_repo)} && {cmd_str}"
    )


def _plain_direct(b: _Backend, cached: str) -> str:
    abs_cmd = " ".join([shlex.quote(cached), *(shlex.quote(a) for a in _QSUB[1:])])
    return f"cd {shlex.quote(b.remote_repo)} && {abs_cmd}"


# ── flag OFF: byte-identity (the regression pin) ──────────────────────────────


def test_dispatch_core_off_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_SUBMIT_ONCE", raising=False)
    b = _Backend()
    cmd_str = " ".join(shlex.quote(a) for a in _QSUB)
    core = b._dispatch_core(cmd_str, _JOB_ENV)
    assert core == f"cd {shlex.quote(b.remote_repo)} && {cmd_str}"


def test_login_shape_off_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_SUBMIT_ONCE", raising=False)
    b = _Backend()
    b._execute_command(list(_QSUB), _JOB_ENV, cwd=None)  # type: ignore[arg-type]
    # Login form (no cached bin): the exact historical inner, byte-for-byte.
    assert b.calls == [f"bash -lc {shlex.quote(_plain_login_inner(b))}"]


def test_direct_shape_off_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_SUBMIT_ONCE", raising=False)
    b = _Backend()
    b._resolved_bins["sbatch"] = "/usr/bin/sbatch"
    b._execute_command(list(_QSUB), _JOB_ENV, cwd=None)  # type: ignore[arg-type]
    assert b.calls == [_plain_direct(b, "/usr/bin/sbatch")]


def test_off_ignores_run_id_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with HPC_RUN_ID present, the flag OFF ⇒ no weave.
    monkeypatch.delenv("HPC_SUBMIT_ONCE", raising=False)
    b = _Backend()
    core = b._dispatch_core("sbatch x", {"HPC_RUN_ID": "run-x"})
    assert "jobmap" not in core and "__hpc_jid" not in core


# ── flag ON: both shapes woven ────────────────────────────────────────────────


def _assert_woven(core_or_cmd: str) -> None:
    assert ".hpc/submit/run-x.jobmap" in core_or_cmd  # pending marker
    assert '"token":"%s"' in core_or_cmd  # token carried
    assert "__hpc_jid=$(" in core_or_cmd  # id captured server-side
    assert "__hpc_rc=$?" in core_or_cmd  # rc captured (Δ4)
    assert "run-x.jobmap.wave-0.id" in core_or_cmd  # per-wave id file (Δ5 key)
    assert 'exit "$__hpc_rc"' in core_or_cmd  # returncode propagated


def test_dispatch_core_on_weaves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    b = _Backend()
    core = b._dispatch_core("sbatch --array=1-4 job.sh", _JOB_ENV)
    _assert_woven(core)
    # The original dispatch command is still present, wrapped in the capture.
    assert "sbatch --array=1-4 job.sh" in core


def test_login_shape_on_weaves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    b = _Backend()
    b._execute_command(list(_QSUB), _JOB_ENV, cwd=None)  # type: ignore[arg-type]
    assert len(b.calls) == 1
    call = b.calls[0]
    assert call.startswith("bash -lc ")  # still the login-shell form
    _assert_woven(call)


def test_direct_shape_on_weaves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    b = _Backend()
    b._resolved_bins["sbatch"] = "/usr/bin/sbatch"
    b._execute_command(list(_QSUB), _JOB_ENV, cwd=None)  # type: ignore[arg-type]
    assert len(b.calls) == 1
    call = b.calls[0]
    assert not call.startswith("bash -lc ")  # direct form (no login-shell wrapper)
    assert "/usr/bin/sbatch" in call  # cached absolute bin
    _assert_woven(call)


def test_on_without_run_id_does_not_weave(monkeypatch: pytest.MonkeyPatch) -> None:
    # The double gate: flag ON but no HPC_RUN_ID ⇒ cannot key a marker ⇒ plain.
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    b = _Backend()
    core = b._dispatch_core("sbatch x", {})
    assert core == f"cd {shlex.quote(b.remote_repo)} && sbatch x"


def test_direct_stale_cache_falls_through_under_weave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale-cache 127 self-heal must survive the weave: a cached bin that
    now returns 127 drops the cache and re-tries the login form. The weave's
    ``exit "$__hpc_rc"`` is what preserves the 127 the direct form checks."""
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")

    class _Stale(_Backend):
        def ssh_run(self, cmd: str):  # type: ignore[override]
            self.calls.append(cmd)
            # First (direct, cached) call returns 127 (stale path); the login
            # retry (bash -lc …) returns a good id.
            rc = 0 if cmd.startswith("bash -lc ") else 127
            return SimpleNamespace(returncode=rc, stdout="Submitted batch job 9\n", stderr="")

    b = _Stale()
    b._resolved_bins["sbatch"] = "/usr/bin/sbatch"
    b._execute_command(list(_QSUB), _JOB_ENV, cwd=None)  # type: ignore[arg-type]
    # Two calls: the stale direct form, then the login-shell re-resolve.
    assert len(b.calls) == 2
    assert not b.calls[0].startswith("bash -lc ")  # direct (woven, starts mkdir -p)
    assert b.calls[1].startswith("bash -lc ")
    # Cache dropped on the 127.
    assert "sbatch" not in b._resolved_bins
