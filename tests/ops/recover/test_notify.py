"""Tests for the watchdog alert notifier + dedup log (§5, incident 2026-07-17).

``doctor.alerts.log`` accumulated 55 near-identical lines for ONE stall — one per
15-min watchdog tick — because the writer appended a fresh line every tick with no
write-time dedup. The fix routes the append through the canonical JSONL seam with a
``dedup_key`` over the stall's stable identity ``(run_id, kind, since)``: one durable
line per live stall, every repeat tick a REPLAY NO-OP. The reader is dual-format so a
pre-flip legacy plaintext log still reads back, and ``alerts-ack`` advances the
acknowledgment watermark standalone.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from hpc_agent._wire.actions.alerts_ack import AlertsAckSpec
from hpc_agent.ops.recover import notify
from hpc_agent.ops.recover.alerts_ack import alerts_ack


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    # Never spawn the OS notifier (msg.exe / notify-send) from a test.
    monkeypatch.setattr(notify, "_try_run", lambda argv: False)
    return tmp_path


def _raw_lines(exp: Path) -> list[str]:
    from hpc_agent.state.run_record import journal_dir

    log = journal_dir(exp) / "doctor.alerts.log"
    if not log.is_file():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _proposal(run_id: str, since: str) -> dict[str, str]:
    return {"run_id": run_id, "last_tick_at": since}


# ── (i) repeated ticks for one stall dedup to a single durable line ──────────


def test_repeated_stall_ticks_dedup_to_one_line(tmp_path: Path) -> None:
    """The incident regression: N watchdog ticks for the SAME (run_id, kind, since)
    write exactly ONE durable line — the replay no-op."""
    props = [_proposal("stalled-run", "2026-07-17T00:00:00+00:00")]
    for _ in range(55):
        notify.raise_stall_notification(props, experiment_dir=tmp_path)

    lines = _raw_lines(tmp_path)
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["run_id"] == "stalled-run"
    assert rec["kind"] == "stall"
    assert rec["since"] == "2026-07-17T00:00:00+00:00"


# ── (ii) a genuinely different stall appends ─────────────────────────────────


def test_distinct_stalls_each_append(tmp_path: Path) -> None:
    """A different ``since`` (a fresh stall window) or a different ``run_id`` is a
    new identity — it appends rather than dedups."""
    # Same run, moved tick window → new identity.
    notify.raise_stall_notification(
        [_proposal("run-a", "2026-07-17T00:00:00+00:00")], experiment_dir=tmp_path
    )
    notify.raise_stall_notification(
        [_proposal("run-a", "2026-07-17T02:00:00+00:00")], experiment_dir=tmp_path
    )
    # Different run entirely.
    notify.raise_stall_notification(
        [_proposal("run-b", "2026-07-17T00:00:00+00:00")], experiment_dir=tmp_path
    )
    assert len(_raw_lines(tmp_path)) == 3


def test_multi_proposal_tick_writes_one_line_per_proposal(tmp_path: Path) -> None:
    """One notification carrying several stalls writes one deduped record each —
    the per-proposal granularity the old single summary line lacked."""
    props = [
        _proposal("run-a", "2026-07-17T00:00:00+00:00"),
        _proposal("run-b", "2026-07-17T00:00:00+00:00"),
    ]
    notify.raise_stall_notification(props, experiment_dir=tmp_path)
    notify.raise_stall_notification(props, experiment_dir=tmp_path)  # replay
    lines = _raw_lines(tmp_path)
    assert len(lines) == 2
    assert {json.loads(ln)["run_id"] for ln in lines} == {"run-a", "run-b"}


def test_free_form_alert_dedups_on_message(tmp_path: Path) -> None:
    """A ``raise_alert_notification`` free-form alert has no run_id/since, so it
    dedups on its own message hash — a repeated self-heal FAIL-LOUD is one line."""
    notify.raise_alert_notification("overnight self-heal FAILED", experiment_dir=tmp_path)
    notify.raise_alert_notification("overnight self-heal FAILED", experiment_dir=tmp_path)
    notify.raise_alert_notification("a different alert", experiment_dir=tmp_path)
    assert len(_raw_lines(tmp_path)) == 2


# ── (iii) legacy plaintext lines still read back ─────────────────────────────


def test_legacy_plaintext_lines_read_back(tmp_path: Path) -> None:
    """Tolerant-reader regression: a pre-flip ``<ts> <message>`` line is still
    surfaced by the dual-format reader as ``{ts, message}``."""
    from hpc_agent.state.run_record import journal_dir

    ts = "2026-07-16T23:00:00+00:00"
    msg = "hpc-agent doctor: driver stalled since 22:45, run pi-canary — re-arm?"
    log = journal_dir(tmp_path) / "doctor.alerts.log"
    log.write_text(f"{ts} {msg}\n", encoding="utf-8")

    alerts = notify.read_unacknowledged_alerts(tmp_path)
    assert alerts == [{"ts": ts, "message": msg}]


def test_reader_mixes_json_and_legacy_lines(tmp_path: Path) -> None:
    """A log holding BOTH a legacy plaintext line and a new JSON record reads both;
    a torn/garbage line is skipped, never raising."""
    from hpc_agent.state.run_record import journal_dir

    log = journal_dir(tmp_path) / "doctor.alerts.log"
    log.write_text(
        "2026-07-16T23:00:00+00:00 legacy stall line\n"
        + json.dumps({"ts": "2026-07-16T23:30:00+00:00", "kind": "stall", "message": "json stall"})
        + "\n"
        + "{ this is not valid json\n",
        encoding="utf-8",
    )
    alerts = notify.read_unacknowledged_alerts(tmp_path)
    assert [a["message"] for a in alerts] == ["legacy stall line", "json stall"]


# ── (iv) route-through pin: the append uses the canonical seam ───────────────


def test_append_routes_through_canonical_append_seam() -> None:
    """The dedup + durability contract lives in ONE place: the append goes through
    ``infra/io.append_jsonl_line`` with a ``dedup_key`` and the never-raise
    ``fsync_required=False`` floor — never a bare ``open(...).write``."""
    src = inspect.getsource(notify._append_alert_log)
    assert "append_jsonl_line(" in src
    assert "dedup_key=" in src
    assert "fsync_required=False" in src
    assert ".open(" not in src  # no bare append bypassing the seam


# ── (v) alerts-ack advances the watermark ────────────────────────────────────


def test_alerts_ack_advances_watermark_and_clears_queue(tmp_path: Path) -> None:
    """``alerts-ack`` (no spec) acknowledges up to the newest alert; afterward
    ``read_unacknowledged_alerts`` is empty and the result names the count."""
    notify.raise_stall_notification(
        [_proposal("run-a", "2026-07-17T00:00:00+00:00")], experiment_dir=tmp_path
    )
    notify.raise_stall_notification(
        [_proposal("run-b", "2026-07-17T02:00:00+00:00")], experiment_dir=tmp_path
    )
    assert len(notify.read_unacknowledged_alerts(tmp_path)) == 2

    result = alerts_ack(experiment_dir=tmp_path, spec=AlertsAckSpec())
    assert result.acknowledged_count == 2
    assert result.remaining == 0
    assert notify.read_unacknowledged_alerts(tmp_path) == []
    # Idempotent replay: nothing left to acknowledge.
    again = alerts_ack(experiment_dir=tmp_path, spec=None)
    assert again.acknowledged_count == 0
    assert again.remaining == 0


def test_alerts_ack_respects_explicit_up_to_ts(tmp_path: Path) -> None:
    """An explicit ``up_to_ts`` only clears alerts at or before it; a newer alert
    stays unacknowledged."""
    from hpc_agent.state.run_record import journal_dir

    log = journal_dir(tmp_path) / "doctor.alerts.log"
    log.write_text(
        json.dumps({"ts": "2026-07-17T00:00:00+00:00", "message": "old stall"})
        + "\n"
        + json.dumps({"ts": "2026-07-17T05:00:00+00:00", "message": "new stall"})
        + "\n",
        encoding="utf-8",
    )
    result = alerts_ack(
        experiment_dir=tmp_path, spec=AlertsAckSpec(up_to_ts="2026-07-17T00:00:00+00:00")
    )
    assert result.acknowledged_count == 1
    assert result.remaining == 1
    remaining = notify.read_unacknowledged_alerts(tmp_path)
    assert [a["message"] for a in remaining] == ["new stall"]


def test_alerts_ack_clamps_future_up_to_ts_to_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied FUTURE ``up_to_ts`` (e.g. 2099-01-01) must not
    pre-acknowledge alerts that do not exist yet. The verb clamps the watermark
    to NOW, so an alert written AFTER the ack surfaces normally instead of being
    swallowed by a watermark parked in the far future.

    ``now`` is frozen so the assertion does not race the second-precision
    watermark (a real alert's ts must land strictly after the clamp target).
    """
    from hpc_agent.ops.recover import alerts_ack as alerts_ack_mod
    from hpc_agent.state.run_record import journal_dir

    frozen_now = "2026-07-18T00:00:00+00:00"
    monkeypatch.setattr(alerts_ack_mod, "utcnow_iso", lambda: frozen_now)

    # Ack far into the future against an empty log — nothing to clear yet.
    result = alerts_ack(
        experiment_dir=tmp_path, spec=AlertsAckSpec(up_to_ts="2099-01-01T00:00:00+00:00")
    )
    assert result.acknowledged_count == 0
    # The clamp holds the watermark at now, NOT the caller's future instant —
    # without it the watermark would sit at 2099 and swallow every later alert.
    assert result.acknowledged_up_to == frozen_now

    # An alert lands AFTER the ack (ts strictly after the frozen clamp target).
    log = journal_dir(tmp_path) / "doctor.alerts.log"
    log.write_text(
        json.dumps({"ts": "2026-07-18T00:00:01+00:00", "message": "post-ack stall"}) + "\n",
        encoding="utf-8",
    )
    surfaced = notify.read_unacknowledged_alerts(tmp_path)
    assert [a["message"] for a in surfaced] == ["post-ack stall"]
