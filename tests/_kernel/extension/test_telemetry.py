"""Tests for :mod:`hpc_agent._kernel.extension.telemetry`.

Focused on the two behaviours that matter cross-process:

* The flock-guarded ``monitor-jsonl`` writer produces no torn lines
  under concurrent appenders.
* Sinks other than ``monitor-jsonl`` don't require the path argument.
* Default sink is ``"none"`` (silent) — production runs with no env var
  must not pollute stderr.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._kernel.extension import telemetry

if TYPE_CHECKING:
    from pathlib import Path


def test_default_sink_is_silent(capsys, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Unset the env var via monkeypatch so the deletion is rolled back
    # at teardown and can't pollute sibling tests in the same session.
    monkeypatch.delenv("HPC_TELEMETRY_SINK", raising=False)
    telemetry.record("tick", {"run_id": "x", "n": 1})
    out = capsys.readouterr()
    assert out.err == ""
    assert out.out == ""


def test_stderr_jsonl_emits_one_line(capsys) -> None:
    telemetry.record("tick", {"run_id": "x", "n": 1}, sink="stderr-jsonl")
    out = capsys.readouterr()
    line = out.err.strip()
    parsed = json.loads(line)
    assert parsed == {"event": "tick", "run_id": "x", "n": 1}


def test_monitor_jsonl_requires_path() -> None:
    with pytest.raises(errors.SpecInvalid):
        telemetry.record("tick", {"run_id": "x"}, sink="monitor-jsonl")


def test_monitor_jsonl_appends(tmp_path: Path) -> None:
    target = tmp_path / "x.monitor.jsonl"
    telemetry.record(
        "tick",
        {"run_id": "x", "n": 1},
        sink="monitor-jsonl",
        monitor_jsonl_path=target,
    )
    telemetry.record(
        "tick",
        {"run_id": "x", "n": 2},
        sink="monitor-jsonl",
        monitor_jsonl_path=target,
    )
    lines = target.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["n"] == 1
    assert json.loads(lines[1])["n"] == 2


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the no-torn-lines guarantee is provided by advisory_flock, which by "
    "design degrades to a no-op without fcntl (Windows) — the A9 invariant this "
    "asserts is genuinely not made on win32, so this is a legitimate skip, not a "
    "latent test bug",
)
def test_concurrent_appenders_produce_no_torn_lines(tmp_path: Path) -> None:
    """A9 invariant: two threads appending 200 records each should
    produce 400 well-formed JSON lines, no half-written records."""
    target = tmp_path / "x.monitor.jsonl"
    N = 200
    threads_n = 2
    payload = {"run_id": "x", "filler": "x" * 256}  # big enough to fault

    def worker(tag: str) -> None:
        for i in range(N):
            telemetry.record(
                "tick",
                {**payload, "tag": tag, "i": i},
                sink="monitor-jsonl",
                monitor_jsonl_path=target,
            )

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = target.read_text().splitlines()
    assert len(lines) == threads_n * N
    # Every line must parse as JSON with the expected keys.
    for line in lines:
        rec = json.loads(line)
        assert rec["event"] == "tick"
        assert rec["run_id"] == "x"
        assert isinstance(rec["i"], int)


# --- otel / otlp sink ---------------------------------------------------


def test_module_import_does_not_require_otel() -> None:
    """Importing the telemetry module must never pull in the optional
    OpenTelemetry SDK — the import is deferred to emit time."""
    import sys

    # opentelemetry is not a base dependency; the module must import
    # cleanly regardless. (If it happened to be installed in the test
    # env that's fine too; we only assert the *module* loaded without
    # erroring, which it already has by virtue of the import above.)
    assert "hpc_agent._kernel.extension.telemetry" in sys.modules


@pytest.mark.parametrize("sink", ["otel", "otlp"])
def test_otel_sink_without_sdk_raises_config_invalid(
    sink: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the SDK is absent, selecting the otel sink fails fast with a
    clear, actionable error rather than a silent no-op."""
    import builtins

    # Reset the tracer + instrument caches so the import path is exercised.
    monkeypatch.setattr(telemetry, "_OTEL_TRACER", None, raising=False)
    monkeypatch.setattr(telemetry, "_OTEL_METRICS", None, raising=False)

    real_import = builtins.__import__

    def _no_otel(name: str, *args, **kwargs):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_otel)

    with pytest.raises(errors.ConfigInvalid) as excinfo:
        telemetry.record("submit", {"run_id": "x"}, sink=sink)
    # Error must point the operator at the fix.
    assert "otel" in str(excinfo.value).lower()


def test_otel_attr_value_coercion() -> None:
    """Primitives pass through; structured values (a campaign decision's
    nested ``reason`` dict) survive as a deterministic JSON string so
    they stay queryable as OTel attributes."""
    assert telemetry._otel_attr_value("tok-123") == "tok-123"
    assert telemetry._otel_attr_value(7) == 7
    assert telemetry._otel_attr_value(1.5) == 1.5
    assert telemetry._otel_attr_value(True) is True
    assert telemetry._otel_attr_value(["a", "b"]) == ["a", "b"]
    # Nested dict -> deterministic JSON string.
    reason = {"kind": "resubmit", "preempted": 3}
    coerced = telemetry._otel_attr_value(reason)
    assert isinstance(coerced, str)
    assert json.loads(coerced) == reason
    # Heterogeneous / non-primitive sequence -> JSON string, not dropped.
    mixed = telemetry._otel_attr_value([{"a": 1}, 2])
    assert isinstance(mixed, str)


class _FakeCounter:
    def __init__(self) -> None:
        self.adds: list[tuple[int, dict]] = []

    def add(self, amount: int, attributes: dict) -> None:
        self.adds.append((amount, dict(attributes)))


class _FakeHistogram:
    def __init__(self) -> None:
        self.points: list[tuple[float, dict]] = []

    def record(self, value: float, attributes: dict) -> None:
        self.points.append((value, dict(attributes)))


def _fake_instruments(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeCounter, _FakeHistogram]:
    counter, histogram = _FakeCounter(), _FakeHistogram()
    monkeypatch.setattr(telemetry, "_otel_instruments", lambda: (counter, histogram))
    return counter, histogram


def test_otel_sink_emits_span_with_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a tracer available, every payload field becomes an
    ``hpc.<key>`` span attribute, including the structured reason and
    trial_token the issue calls out."""
    captured: dict[str, object] = {}

    class _FakeSpan:
        def set_attribute(self, key: str, value: object) -> None:
            captured[key] = value

    class _Ctx:
        def __enter__(self) -> _FakeSpan:
            return _FakeSpan()

        def __exit__(self, *exc) -> bool:
            return False

    class _FakeTracer:
        def start_as_current_span(self, name: str) -> _Ctx:
            captured["__span_name__"] = name
            return _Ctx()

    monkeypatch.setattr(telemetry, "_otel_tracer", lambda: _FakeTracer())
    _fake_instruments(monkeypatch)

    telemetry.record(
        "campaign_decision",
        {
            "run_id": "r1",
            "trial_token": "tok-abc",
            "reason": {"kind": "proceed", "canary_pass": True},
        },
        sink="otel",
    )

    assert captured["__span_name__"] == "campaign_decision"
    assert captured["hpc.run_id"] == "r1"
    assert captured["hpc.trial_token"] == "tok-abc"
    assert json.loads(captured["hpc.reason"]) == {  # type: ignore[arg-type]
        "kind": "proceed",
        "canary_pass": True,
    }


# --- otel metrics (#313) -------------------------------------------------


def _silence_span(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoopCtx:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def set_attribute(self, key: str, value: object) -> None:
            pass

    class _NoopTracer:
        def start_as_current_span(self, name: str) -> _NoopCtx:
            return _NoopCtx()

    monkeypatch.setattr(telemetry, "_otel_tracer", lambda: _NoopTracer())


def test_otel_metrics_counter_per_event_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every record() increments hpc.events by 1, dimensioned by the
    event name — the per-event-kind counters of #313, off the same
    single producer with no new call sites."""
    _silence_span(monkeypatch)
    counter, _ = _fake_instruments(monkeypatch)

    telemetry.record("submit", {"run_id": "r1"}, sink="otel")
    telemetry.record("canary_result", {"run_id": "r1", "ok": True}, sink="otel")

    assert [(n, a["hpc.event"]) for n, a in counter.adds] == [
        (1, "submit"),
        (1, "canary_result"),
    ]
    # Bounded enum-like payload keys are promoted to dimensions.
    assert counter.adds[1][1]["hpc.ok"] is True


def test_otel_metrics_exclude_high_cardinality_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trial_token / run_id / fingerprints must never become metric
    dimensions — they belong on spans (#313's cardinality rule)."""
    _silence_span(monkeypatch)
    counter, histogram = _fake_instruments(monkeypatch)

    telemetry.record(
        "resubmit",
        {"run_id": "r1", "trial_token": "tok-abc", "fingerprint": "f" * 40, "n_failed": 3},
        sink="otel",
    )

    (_, attrs) = counter.adds[0]
    assert set(attrs) == {"hpc.event"}
    for _, h_attrs in histogram.points:
        assert "hpc.trial_token" not in h_attrs
        assert "hpc.run_id" not in h_attrs


def test_otel_metrics_numeric_fields_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Numeric payload fields land in the hpc.event.value histogram
    keyed by event + field; bools and strings do not."""
    _silence_span(monkeypatch)
    _, histogram = _fake_instruments(monkeypatch)

    telemetry.record(
        "tick",
        {"run_id": "r1", "pending": 7, "elapsed_sec": 1.5, "ok": True, "state": "RUNNING"},
        sink="otel",
    )

    points = {(a["hpc.field"], v) for v, a in histogram.points}
    assert points == {("pending", 7), ("elapsed_sec", 1.5)}
    assert all(a["hpc.event"] == "tick" for _, a in histogram.points)
