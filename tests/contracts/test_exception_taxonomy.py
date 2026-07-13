"""Contract pin for the G7 per-site-exception-taxonomy generator.

Failure classification (transient-transport vs deterministic-env; the tolerant
durable-record read degrade set) is owned by ONE definition in
:mod:`hpc_agent.errors` and consumed by import, not re-enumerated as a
hand-copied ``except`` tuple at every consumer. These are the never-fires
route-through assertions (the repo's ``test_layers_share_one_drift_predicate``
style): they fail the moment a poll/read seam grows its own twin again.
"""

from __future__ import annotations

import inspect
import json

from hpc_agent import errors

# ── The taxonomy itself ─────────────────────────────────────────────────────


def test_transient_transport_set_members() -> None:
    """The one poll-tolerance set carries every transient-transport class — a new
    transport error joins HERE, not at each poll seam."""
    members = set(errors.TRANSIENT_TRANSPORT_ERRORS)
    assert {
        errors.RemoteCommandFailed,
        errors.SshCircuitOpen,
        errors.SshUnreachable,
        errors.SshSlotWaitTimeout,
        OSError,
    } <= members
    # TimeoutError rides the budget via its OSError base (no separate member needed).
    assert issubclass(TimeoutError, OSError)


def test_tolerant_record_read_set_members() -> None:
    """The one durable-record degrade set carries the full present-or-gap tuple —
    the drift that sealed a corrupt sidecar into a crash (#43/#73)."""
    members = set(errors.TOLERANT_RECORD_READ_ERRORS)
    assert {
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        errors.HpcError,
    } <= members


def test_is_deterministic_env_failure_fire_path() -> None:
    """rc 126/127 is the broken-env signature (escalate fast); everything else is
    transient (ride the budget)."""
    assert errors.is_deterministic_env_failure(errors.RemoteCommandFailed("x", returncode=127))
    assert errors.is_deterministic_env_failure(errors.RemoteCommandFailed("x", returncode=126))
    # Any OTHER returncode, or None, is transient.
    assert not errors.is_deterministic_env_failure(errors.RemoteCommandFailed("x", returncode=1))
    assert not errors.is_deterministic_env_failure(errors.RemoteCommandFailed("x"))
    # A transport blip (open breaker, dropped connection) is never deterministic-env.
    assert not errors.is_deterministic_env_failure(errors.SshCircuitOpen("open"))
    assert not errors.is_deterministic_env_failure(OSError("reset"))


# ── Route-through: consumers import the ONE definition, no local twin ─────────


def test_poll_seams_route_through_shared_classifier() -> None:
    """``monitor_flow`` and ``verify_canary`` consume the shared rc-126/127
    classifier instead of each carrying a hand-copied returncode-in-(126,127)
    twin (G7/G11 overlap)."""
    import hpc_agent.ops.monitor_flow as monitor_flow
    import hpc_agent.ops.verify_canary as verify_canary

    mf_src = inspect.getsource(monitor_flow)
    vc_src = inspect.getsource(verify_canary)

    assert "errors.is_deterministic_env_failure" in mf_src
    assert "errors.is_deterministic_env_failure" in vc_src
    # No re-inlined twin: neither module re-hardcodes the rc-126/127 literal set.
    assert "(126, 127)" not in mf_src
    assert "(126, 127)" not in vc_src


def test_verify_canary_catches_the_shared_transient_tuple() -> None:
    """The canary poll loop catches ``errors.TRANSIENT_TRANSPORT_ERRORS`` — one
    tuple, not a per-site enumeration that forgets ``SshCircuitOpen`` (#50)."""
    import hpc_agent.ops.verify_canary as verify_canary

    assert "errors.TRANSIENT_TRANSPORT_ERRORS" in inspect.getsource(verify_canary)


def test_export_dossier_routes_through_tolerant_reader() -> None:
    """``export_dossier`` reads sidecars through the shared tolerant reader, so a
    torn/foreign/non-UTF-8 sidecar degrades to a gap instead of crashing the
    dossier / recompute lock (#43)."""
    import hpc_agent.ops.export_dossier as export_dossier

    assert "read_run_sidecar_or_empty" in inspect.getsource(export_dossier)


# ── Behavioral: the shared tolerant reader degrades every corrupt shape ───────


def test_read_run_sidecar_or_empty_degrades_corrupt_shapes(tmp_path) -> None:
    """A torn, non-UTF-8, or absent sidecar all read as ``{}`` (present-or-gap)."""
    from hpc_agent.state.runs import read_run_sidecar_or_empty, run_sidecar_path

    # Absent → {}.
    assert read_run_sidecar_or_empty(tmp_path, "no-such-run") == {}

    # Torn JSON → {}.
    torn = run_sidecar_path(tmp_path, "torn-run")
    torn.parent.mkdir(parents=True, exist_ok=True)
    torn.write_text('{"run_id": "torn-run"', encoding="utf-8")  # truncated
    assert read_run_sidecar_or_empty(tmp_path, "torn-run") == {}

    # Non-UTF-8 (UTF-16 with BOM, the Windows-redirect shape) → {}.
    utf16 = run_sidecar_path(tmp_path, "utf16-run")
    utf16.write_bytes(b"\xff\xfe" + '{"run_id": "utf16-run"}'.encode("utf-16-le"))
    assert read_run_sidecar_or_empty(tmp_path, "utf16-run") == {}
