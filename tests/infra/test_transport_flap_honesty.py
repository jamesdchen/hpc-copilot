"""L4: the delta must not be laundered into a full copy by a flap.

The delta engine itself already exists (``infra/transport/_delta.py``) and is the
live rsync-less staging path — nothing here rebuilds it. What is under test is
the HONESTY of its fallback, which is what cost the 2026-07-30 night:

* leg A (the remote hash-manifest round-trip) is exactly what a flapping tunnel
  severs, and its ``(None, set())``-on-trouble contract routed straight to a
  whole-tree re-ship — 266 MB per attempt — while the log blamed a cold cache;
* the full-copy WARN asserted "NO DELTA" (false on the live path) and advised
  installing WSL/MSYS rsync (a documented msys-2.0.dll trap).
"""

from __future__ import annotations

import subprocess

import pytest

from hpc_agent import errors
from hpc_agent.infra.transport import _delta, _disclose


def test_transport_severed_manifest_probe_is_reported_as_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A severed leg A fills the out-channel — "the link failed", not "no manifest"."""

    def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise TimeoutError("read severed mid-manifest")

    monkeypatch.setattr("hpc_agent.infra.transport._guarded_ssh_bounded", _boom)
    status: dict[str, object] = {}
    result = _delta._remote_push_manifest(
        ssh_target="u@h", remote_path="/r", exclude=[], timeout=5.0, probe_status=status
    )
    assert result == (None, set()), "the (None, set()) safety contract is unchanged"
    assert status["failed_on_transport"] is True
    assert "TimeoutError" in str(status["detail"])


def test_the_none_contract_is_unchanged_without_the_out_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that pass no ``probe_status`` see byte-identical behaviour."""

    def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise TimeoutError("severed")

    monkeypatch.setattr("hpc_agent.infra.transport._guarded_ssh_bounded", _boom)
    assert _delta._remote_push_manifest(
        ssh_target="u@h", remote_path="/r", exclude=[], timeout=5.0
    ) == (None, set())


def test_a_genuinely_absent_manifest_is_not_flagged_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first ship prints nothing — full copy is CORRECT and must not be blamed on the link."""
    monkeypatch.setattr(
        "hpc_agent.infra.transport._guarded_ssh_bounded",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )
    status: dict[str, object] = {}
    manifest, known = _delta._remote_push_manifest(
        ssh_target="u@h", remote_path="/r", exclude=[], timeout=5.0, probe_status=status
    )
    assert (manifest, known) == (None, set())
    assert status.get("failed_on_transport") is not True


def test_flap_refuses_to_degrade_into_a_whole_tree_reship(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The caller surfaces instead of full-copying, so the L3 retry can resume."""
    (tmp_path / "a.txt").write_text("payload")
    monkeypatch.setattr("hpc_agent.infra.transport.shutil.which", lambda _n: None)

    def _probe(*, probe_status=None, **_k):  # type: ignore[no-untyped-def]
        if probe_status is not None:
            probe_status["failed_on_transport"] = True
            probe_status["detail"] = "TimeoutError: severed"
        return None, set()

    monkeypatch.setattr("hpc_agent.infra.transport._remote_push_manifest", _probe)
    from hpc_agent.infra import transport

    with pytest.raises(errors.SshUnreachable) as excinfo:
        transport.rsync_push(ssh_target="u@h", remote_path="/r", local_path=tmp_path, delete=True)
    message = str(excinfo.value)
    assert "refusing to degrade a flap into a whole-tree re-ship" in message
    assert "resumes as soon as the link holds" in message


# ── the stale disclosure (L4.2 / L4.3) ───────────────────────────────────────


def test_full_copy_warning_no_longer_claims_no_delta(capsys: pytest.CaptureFixture) -> None:
    """ "NO DELTA" was false on the live path — the line must name the MODE instead."""
    _disclose._disclose_no_rsync(266 * 1024 * 1024, reason="first deploy")
    err = capsys.readouterr().err
    assert "NO DELTA" not in err
    assert "MODE=full-tar" in err
    assert "first deploy" in err


def test_full_copy_warning_states_the_matched_runtime_constraint(
    capsys: pytest.CaptureFixture,
) -> None:
    """Recommending WSL/MSYS rsync blind walks into the msys-2.0.dll clash."""
    _disclose._disclose_no_rsync(1024, reason="first deploy")
    err = capsys.readouterr().err
    assert "WSL" not in err
    assert "dup() in/out/err failed" in err, "the actual failure mode must be named"
    assert "ssh_options" in err, "the recorded finding must be pointed at"
    assert "HPC_RSYNC_BINARY" in err and "HPC_SSH_BINARY" in err


def test_push_mode_disclosure_carries_the_slo_numbers(capsys: pytest.CaptureFixture) -> None:
    """Bytes shipped vs bytes unchanged — the readiness ledger's efficiency substrate."""
    _disclose.disclose_push_mode(
        mode="delta-tar",
        reason="remote content-hash manifest available",
        n_ship=3,
        n_unchanged=97,
        shipped_bytes=1024 * 1024,
        total_bytes=100 * 1024 * 1024,
    )
    err = capsys.readouterr().err
    assert "MODE=delta-tar" in err
    assert "shipped 3 file(s)" in err
    assert "100 file(s)" in err
    assert "97 file(s) already identical" in err
    assert "1% of the tree" in err


def test_push_mode_disclosure_says_unknown_rather_than_zero(
    capsys: pytest.CaptureFixture,
) -> None:
    """rsync computes its delta on the wire; a printed 0 would read as "nothing shipped"."""
    _disclose.disclose_push_mode(
        mode="rsync",
        reason="rsync on PATH (rsync computes its own delta)",
        n_ship=None,
        n_unchanged=None,
        shipped_bytes=None,
        total_bytes=5 * 1024 * 1024,
    )
    err = capsys.readouterr().err
    assert "MODE=rsync" in err
    assert "not itemized" in err
    assert "shipped 0" not in err
