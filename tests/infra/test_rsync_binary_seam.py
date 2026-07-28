"""The ``HPC_RSYNC_BINARY`` resolution seam + the batch-shim override warning.

Until 2026-07-28 rsync was the ONE transport binary with no resolution seam:
ssh/scp/ssh-add ride ``_resolve_binary`` (env override → native win32 default →
bare PATH name) while rsync was a bare ``shutil.which("rsync")`` + a literal
``"rsync"`` argv[0]. The 2026-07-27 session had a working ``C:\\msys64`` rsync
invisible to the PATH probe all night, and the attempted workaround — PATH
surgery in the calling shell — poisoned the ssh resolution instead
(``announce read failed (rc=255)``). ``rsync_binary()`` is the sanctioned
pin: env override or bare-PATH name, deliberately NO auto-probe of known
install locations (an MSYS2 rsync paired with a foreign-runtime ssh dies in
``dup() in/out/err failed`` — the msys-2.0.dll clash — so a binary the
framework picked silently would carry a pairing it cannot validate).

The shim warning is the guard the same session lacked: an ``HPC_SSH_BINARY``
override pointing at a ``.cmd``/``.bat`` routes EVERY spawn through cmd.exe
(~8,191-char command-line ceiling; rsync's ``RSYNC_RSH`` cannot exec it at
all) and previously failed with nothing but the spawn layer's opaque
"The command line is too long".
"""

from __future__ import annotations

import sys

import pytest

from hpc_agent.infra import ssh_options
from hpc_agent.infra.ssh_options import rsync_binary


@pytest.fixture(autouse=True)
def _fresh_warn_cache():
    """The shim warning is warn-once via ``functools.cache`` — isolate tests."""
    ssh_options._warn_batch_shim_override.cache_clear()
    yield
    ssh_options._warn_batch_shim_override.cache_clear()


def test_default_is_bare_path_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """No override → bare ``"rsync"`` — PATH resolution byte-identical to the
    pre-seam behaviour (and NO native-Windows default: Windows ships no rsync).

    kills: adding an auto-probed win32 default (the pairing hazard the seam
    deliberately refuses)."""
    monkeypatch.delenv("HPC_RSYNC_BINARY", raising=False)
    assert rsync_binary() == "rsync"


def test_env_override_wins_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HPC_RSYNC_BINARY`` wins unconditionally — the explicit opt-in for an
    off-PATH install (e.g. ``C:\\msys64\\usr\\bin\\rsync.exe``)."""
    monkeypatch.setenv("HPC_RSYNC_BINARY", r"C:\msys64\usr\bin\rsync.exe")
    assert rsync_binary() == r"C:\msys64\usr\bin\rsync.exe"


def test_have_rsync_probes_the_resolved_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_have_rsync`` consults the RESOLVED binary: an override pointing at a
    real executable turns the probe True (activating the rsync path) and a
    bogus override turns it False (activating the scp/tar fallback) — no PATH
    surgery either way.

    kills: leaving the availability probe on the bare ``"rsync"`` name (the
    override would steer the argv but not the routing decision)."""
    from hpc_agent.infra import transport

    monkeypatch.setenv("HPC_RSYNC_BINARY", sys.executable)  # exists, executable
    assert transport._have_rsync() is True
    monkeypatch.setenv("HPC_RSYNC_BINARY", r"C:\nonexistent\rsync-nowhere.exe")
    assert transport._have_rsync() is False


def test_batch_shim_ssh_override_warns_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``.cmd`` ``HPC_SSH_BINARY`` override is honored (the override contract
    is unconditional) but warned about ONCE — naming the cmd.exe ceiling and
    the RSYNC_RSH incompatibility instead of leaving the spawn layer to fail
    with the opaque "The command line is too long" (2026-07-27, a passthrough
    ``wssh.cmd`` cost the session its transport for 49 minutes).

    kills: dropping the warning; warning on every call (log spam a poll loop
    would multiply)."""
    monkeypatch.setenv("HPC_SSH_BINARY", r"C:\Users\u\.local\bin\wssh.cmd")
    assert ssh_options._ssh_binary() == r"C:\Users\u\.local\bin\wssh.cmd"  # honored
    first = capsys.readouterr().err
    assert "wssh.cmd" in first
    assert "8,191" in first
    ssh_options._ssh_binary()
    assert capsys.readouterr().err == ""  # warn-once


def test_exe_override_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A direct ``.exe`` override — the recommended shape — stays silent."""
    monkeypatch.setenv("HPC_SSH_BINARY", r"C:\Windows\System32\OpenSSH\ssh.exe")
    ssh_options._ssh_binary()
    assert capsys.readouterr().err == ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
