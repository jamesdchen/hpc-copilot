"""Liveness heartbeat for detached workers (run-#12 findings 3/16/27).

Pins the one-seam heartbeat that keeps a detached worker's log from reading as a
0-byte freeze during legitimate long work:

* heartbeat lines appear in the log for a slow verb (short interval);
* the envelope stays the newest PARSEABLE JSON line (the consumer contract —
  backward scan skips non-JSON prose), and a beat mid-flight at stop is
  suppressed by the post-snapshot stop re-check;
* ``HPC_DETACH_HEARTBEAT_SEC=0`` (and a non-detached process) disables it;
* a raising log-write never crashes the worker;
* the frozen-at-birth branch spells out ``no verb output yet`` in the line.

In-process and hermetic (the ``detached_heartbeat`` CM takes an injectable
stream), matching the existing ``tests/_kernel/lifecycle`` patterns — no
subprocess spawn.
"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from hpc_agent._kernel.lifecycle import heartbeat as hb


@pytest.fixture
def _detached_env(monkeypatch):
    """Mark this process a detached worker with a fast heartbeat cadence."""
    monkeypatch.setenv("HPC_DETACHED_RUN_ID", "ml-hbtest")
    monkeypatch.setenv("HPC_DETACH_HEARTBEAT_SEC", "0.05")


def _last_nonempty(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# ─── the heartbeat fires for a slow verb ───────────────────────────────────


def test_heartbeat_lines_appear_for_slow_verb(_detached_env):
    stream = io.StringIO()
    with hb.detached_heartbeat(stream=stream):
        time.sleep(0.3)  # a slow verb: several 0.05s beats elapse
    out = stream.getvalue()
    assert "[hb] alive" in out
    assert out.count("[hb]") >= 1


# ─── the envelope stays the newest parseable JSON line ─────────────────────


def test_envelope_is_last_parseable_json_line(_detached_env):
    """The consumer contract (aggregate_blocks._harvest_ledger_tail): envelope
    readers scan BACKWARD for the newest parseable JSON line, so `[hb]` prose
    must never be JSON and the envelope must stay the newest JSON line even if
    a straggler beat lands after it."""
    stream = io.StringIO()
    envelope = '{"ok": true, "run_id": "ml-hbtest"}'
    with hb.detached_heartbeat(stream=stream):
        time.sleep(0.3)  # let a few beats land first
        stream.write(envelope + "\n")  # the verb's final envelope
    newest_json = None
    for ln in reversed([ln for ln in stream.getvalue().splitlines() if ln.strip()]):
        try:
            newest_json = json.loads(ln)
        except json.JSONDecodeError:
            continue
        break
    assert newest_json == {"ok": True, "run_id": "ml-hbtest"}


def test_mid_flight_beat_suppressed_on_stop(_detached_env, monkeypatch):
    """A beat already past its wait when stop is signalled re-checks the stop
    event after the (slow) snapshot and exits without writing. Deterministic:
    the patched snapshot blocks until the CM's finally has set the stop event,
    guaranteeing the straddling beat sees it."""
    stopped = threading.Event()

    def _slow_snapshot():
        stopped.wait(timeout=5.0)  # released by the CM's finally via join order
        return None, 0.0, True, True

    monkeypatch.setattr(hb, "_child_snapshot", _slow_snapshot)
    stream = io.StringIO()
    envelope = '{"ok": true}'
    with hb.detached_heartbeat(stream=stream):
        time.sleep(0.15)  # a beat passes its wait and blocks in the snapshot
        stream.write(envelope + "\n")
    stopped.set()  # release the blocked snapshot AFTER stop was signalled
    time.sleep(0.1)  # give the loop a beat to (wrongly) write if it were going to
    assert _last_nonempty(stream.getvalue()) == envelope
    assert "[hb]" not in stream.getvalue()


def test_no_lines_after_cm_exit(_detached_env):
    stream = io.StringIO()
    with hb.detached_heartbeat(stream=stream):
        time.sleep(0.12)
    after = stream.getvalue()
    time.sleep(0.2)  # several would-be intervals
    assert stream.getvalue() == after


# ─── escape hatches ────────────────────────────────────────────────────────


def test_zero_interval_disables(monkeypatch):
    monkeypatch.setenv("HPC_DETACHED_RUN_ID", "ml-hbtest")
    monkeypatch.setenv("HPC_DETACH_HEARTBEAT_SEC", "0")
    stream = io.StringIO()
    with hb.detached_heartbeat(stream=stream):
        time.sleep(0.2)
    assert stream.getvalue() == ""


def test_non_detached_process_disables(monkeypatch):
    monkeypatch.delenv("HPC_DETACHED_RUN_ID", raising=False)
    monkeypatch.setenv("HPC_DETACH_HEARTBEAT_SEC", "0.05")
    stream = io.StringIO()
    with hb.detached_heartbeat(stream=stream):
        time.sleep(0.2)
    assert stream.getvalue() == ""


# ─── a raising write never kills the worker ────────────────────────────────


def test_raising_log_write_does_not_crash_worker(_detached_env):
    class _Raising(io.StringIO):
        def write(self, _s):  # type: ignore[override]
            raise OSError("disk full")

    ran = {"body": False}
    with hb.detached_heartbeat(stream=_Raising()):
        time.sleep(0.2)  # beats fire and every write raises — all swallowed
        ran["body"] = True
    assert ran["body"] is True  # the worker body completed normally


# ─── frozen-at-birth branch ────────────────────────────────────────────────


def test_frozen_at_birth_line(monkeypatch):
    """No child, negligible CPU, past the beat threshold → the line says so."""
    monkeypatch.setattr(hb, "_self_cpu_seconds", lambda: 0.0)
    line = hb._build_line(
        elapsed_sec=90.0,
        count=hb._FROZEN_AFTER_HEARTBEATS,
        child_name=None,
        child_cpu=0.0,
        saw_child_ever=False,
        psutil_ok=True,
    )
    assert line.startswith("[hb] alive 90s")
    assert "no children" in line
    assert "no verb output yet (frozen-at-birth suspect)" in line


def test_busy_worker_line_names_child(monkeypatch):
    monkeypatch.setattr(hb, "_self_cpu_seconds", lambda: 42.0)
    line = hb._build_line(
        elapsed_sec=480.0,
        count=16,
        child_name="scp.exe",
        child_cpu=17.2,
        saw_child_ever=True,
        psutil_ok=True,
    )
    assert line == "[hb] alive 480s | child=scp.exe cpu=17.2s"


def test_frozen_flag_suppressed_without_psutil(monkeypatch):
    """A missing psutil must never manufacture a false frozen suspicion."""
    monkeypatch.setattr(hb, "_self_cpu_seconds", lambda: 0.0)
    line = hb._build_line(
        elapsed_sec=90.0,
        count=hb._FROZEN_AFTER_HEARTBEATS,
        child_name=None,
        child_cpu=0.0,
        saw_child_ever=False,
        psutil_ok=False,
    )
    assert "frozen-at-birth" not in line
