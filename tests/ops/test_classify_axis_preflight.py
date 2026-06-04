"""Tests for the ``classify-axis-preflight`` composite primitive (WS5 #6).

Pins the sequential discover-runs → cache-check → (conditional) recall
orchestration: argv composition (including recall's optional ``--root``
/ ``--task-kind``), sub-call order, the cache-check decision matrix
(hit requires both run_name and a matching run_signature_sha), the
conditional-recall branch matrix (skip when data_axis supplied OR the
cache hit), overall-derivation precedence, the null-slot shape for a
skipped recall, and the synthesised-ErrorEnvelope shape.

The subprocess plumbing is mocked at :func:`_run_subprocess` (so these
tests don't need a real ``hpc-agent`` binary on PATH) and the cache
read at :func:`hpc_agent.state.axes.read_executor` (so no axes.yaml is
needed on disk).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent.ops import classify_axis_preflight as cp


def _ok_subresult(envelope_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canned subprocess SubResult with ``ok: true``."""
    env: dict[str, Any] = {"ok": True, "idempotent": True, "data": envelope_data or {}}
    return {"envelope": env, "elapsed_sec": 0.05, "ok": True}


def _err_subresult(error_code: str = "spec_invalid") -> dict[str, Any]:
    """Canned subprocess SubResult with ``ok: false`` carrying *error_code*."""
    env = {
        "ok": False,
        "error_code": error_code,
        "message": "synthetic test failure",
        "category": "user",
        "retry_safe": False,
    }
    return {"envelope": env, "elapsed_sec": 0.05, "ok": False}


def _patch_run_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    by_name: dict[str, dict[str, Any]],
    *,
    record_order: list[str] | None = None,
) -> None:
    """Patch :func:`_run_subprocess` to return canned SubResults by name."""

    def fake(call: cp.SubCall, *, timeout_sec: float) -> dict[str, Any]:
        if record_order is not None:
            record_order.append(call.name)
        return by_name.get(call.name, _ok_subresult())

    monkeypatch.setattr(cp, "_run_subprocess", fake)


def _patch_read_executor(
    monkeypatch: pytest.MonkeyPatch,
    entry: dict[str, Any] | None,
    *,
    record: list[tuple[str | None]] | None = None,
) -> None:
    """Patch ``read_executor`` (imported inside ``_run_cache_check``)."""

    def fake(experiment_dir: Any, run_name: str | None) -> dict[str, Any] | None:
        if record is not None:
            record.append((run_name,))
        return entry

    # _run_cache_check does ``from hpc_agent.state.axes import read_executor``
    # at call time, so patch the source module's attribute.
    import hpc_agent.state.axes as axes_mod

    monkeypatch.setattr(axes_mod, "read_executor", fake)


# A stored executor entry whose sha we control in tests.
_SHA = "a" * 64
_OTHER_SHA = "b" * 64


class TestBuildSubcalls:
    """argv composition + the conditional recall flag-wiring."""

    def test_discover_only_when_recall_off(self) -> None:
        calls = cp._build_subcalls(
            experiment_dir=Path("/exp"), root=None, task_kind=None, run_recall=False
        )
        assert [c.name for c in calls] == ["discover-runs"]
        exp = str(Path("/exp"))
        assert calls[0].argv == ["hpc-agent", "discover-runs", "--experiment-dir", exp]

    def test_recall_appended_with_root_and_task_kind(self) -> None:
        calls = cp._build_subcalls(
            experiment_dir=Path("/exp"),
            root="/experiments",
            task_kind="forecast",
            run_recall=True,
        )
        assert [c.name for c in calls] == ["discover-runs", "recall"]
        recall = next(c for c in calls if c.name == "recall")
        assert recall.argv == [
            "hpc-agent",
            "recall",
            "--root",
            "/experiments",
            "--task-kind",
            "forecast",
        ]

    def test_recall_omits_optional_flags_when_none(self) -> None:
        calls = cp._build_subcalls(
            experiment_dir=Path("/exp"), root=None, task_kind=None, run_recall=True
        )
        recall = next(c for c in calls if c.name == "recall")
        # Bare recall — falls back to config.json:experiment_roots, no filter.
        assert recall.argv == ["hpc-agent", "recall"]
        assert "--root" not in recall.argv
        assert "--task-kind" not in recall.argv


class TestCacheCheckDecision:
    """``_run_cache_check`` — hit requires run_name AND a matching sha."""

    def test_hit_when_sha_matches(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_read_executor(
            monkeypatch, {"run_signature_sha": _SHA, "data_axis": {"kind": "independent"}}
        )
        result = cp._run_cache_check(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert result["ok"] is True
        assert result["envelope"]["data"]["hit"] is True
        assert result["envelope"]["data"]["stored_run_signature_sha"] == _SHA

    def test_miss_on_sha_mismatch(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Signature drift — stored entry exists but sha changed.
        _patch_read_executor(monkeypatch, {"run_signature_sha": _OTHER_SHA})
        result = cp._run_cache_check(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert result["ok"] is True
        assert result["envelope"]["data"]["hit"] is False

    def test_miss_on_absent_entry(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch_read_executor(monkeypatch, None)
        result = cp._run_cache_check(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert result["ok"] is True
        assert result["envelope"]["data"]["hit"] is False
        assert result["envelope"]["data"]["stored"] is None

    def test_miss_when_run_name_none_skips_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record: list[tuple[str | None]] = []
        _patch_read_executor(monkeypatch, {"run_signature_sha": _SHA}, record=record)
        result = cp._run_cache_check(experiment_dir=tmp_path, run_name=None, run_signature_sha=_SHA)
        # No run_name → no read attempted, reported as a miss.
        assert record == []
        assert result["envelope"]["data"]["hit"] is False

    def test_miss_when_sha_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # An entry exists but we have no current sha to compare → miss.
        _patch_read_executor(monkeypatch, {"run_signature_sha": _SHA})
        result = cp._run_cache_check(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=None
        )
        assert result["envelope"]["data"]["hit"] is False

    def test_corrupt_axes_yaml_surfaces_as_failed_subresult(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import hpc_agent.state.axes as axes_mod

        def boom(experiment_dir: Any, run_name: str | None) -> dict[str, Any] | None:
            raise ValueError("axes.yaml: top-level YAML must be a mapping")

        monkeypatch.setattr(axes_mod, "read_executor", boom)
        result = cp._run_cache_check(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert result["ok"] is False
        assert result["envelope"]["ok"] is False
        assert result["envelope"]["error_code"] == "config_invalid"


class TestConditionalRecall:
    """The recall sub-call runs only when NOT (data_axis supplied OR cache hit)."""

    def test_recall_runs_on_cold_miss(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ordered: list[str] = []
        _patch_read_executor(monkeypatch, None)  # cache miss
        _patch_run_subprocess(
            monkeypatch,
            {"discover-runs": _ok_subresult({"runs": []}), "recall": _ok_subresult()},
            record_order=ordered,
        )
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path,
            run_name="forecast",
            run_signature_sha=_SHA,
            root="/experiments",
            task_kind="forecast",
        )
        # discover ran via subprocess, then recall via subprocess.
        assert ordered == ["discover-runs", "recall"]
        assert result["recall"] is not None
        assert result["recall"]["ok"] is True

    def test_recall_skipped_when_cache_hits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ordered: list[str] = []
        _patch_read_executor(monkeypatch, {"run_signature_sha": _SHA})  # cache HIT
        _patch_run_subprocess(
            monkeypatch,
            {"discover-runs": _ok_subresult()},
            record_order=ordered,
        )
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        # recall never spawned; only discover-runs went through subprocess.
        assert ordered == ["discover-runs"]
        assert result["recall"] is None
        assert result["cache_check"]["envelope"]["data"]["hit"] is True
        assert result["overall"] == "pass"

    def test_recall_skipped_when_data_axis_supplied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ordered: list[str] = []
        _patch_read_executor(monkeypatch, None)  # miss — but data_axis short-circuits
        _patch_run_subprocess(
            monkeypatch,
            {"discover-runs": _ok_subresult()},
            record_order=ordered,
        )
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path,
            run_name="forecast",
            run_signature_sha=_SHA,
            data_axis_supplied=True,
        )
        assert ordered == ["discover-runs"]
        assert result["recall"] is None
        assert result["overall"] == "pass"

    def test_data_axis_supplied_takes_precedence_over_cache_miss(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Even on a cache miss, an interview-resolved axis skips recall.
        ordered: list[str] = []
        _patch_read_executor(monkeypatch, {"run_signature_sha": _OTHER_SHA})  # miss
        _patch_run_subprocess(monkeypatch, {"discover-runs": _ok_subresult()}, record_order=ordered)
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path,
            run_name="forecast",
            run_signature_sha=_SHA,
            data_axis_supplied=True,
        )
        assert ordered == ["discover-runs"]
        assert result["recall"] is None


class TestOverallDerivation:
    """``overall`` is ``pass`` iff every sub-call that ran returned ``ok``."""

    def test_all_succeed_overall_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_read_executor(monkeypatch, None)
        _patch_run_subprocess(
            monkeypatch,
            {
                "discover-runs": _ok_subresult({"runs": [{"name": "forecast"}]}),
                "recall": _ok_subresult({"summaries": []}),
            },
        )
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert result["overall"] == "pass"
        assert result["discover_runs"]["ok"] is True
        assert result["cache_check"]["ok"] is True
        assert result["recall"]["ok"] is True

    def test_discover_fails_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_read_executor(monkeypatch, None)
        _patch_run_subprocess(
            monkeypatch,
            {
                "discover-runs": _err_subresult("spec_invalid"),
                "recall": _ok_subresult(),
            },
        )
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert result["overall"] == "fail"
        assert result["discover_runs"]["envelope"]["error_code"] == "spec_invalid"
        # Sibling work preserved — recall still ran and is recorded.
        assert result["recall"]["ok"] is True

    def test_recall_fails_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_read_executor(monkeypatch, None)
        _patch_run_subprocess(
            monkeypatch,
            {
                "discover-runs": _ok_subresult(),
                "recall": _err_subresult("config_invalid"),
            },
        )
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert result["overall"] == "fail"
        assert result["recall"]["envelope"]["error_code"] == "config_invalid"
        # Sibling work preserved.
        assert result["discover_runs"]["ok"] is True
        assert result["cache_check"]["ok"] is True

    def test_skipped_recall_does_not_fail_overall(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Cache hit → recall null; overall pass off discover + cache only.
        _patch_read_executor(monkeypatch, {"run_signature_sha": _SHA})
        _patch_run_subprocess(monkeypatch, {"discover-runs": _ok_subresult()})
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert result["recall"] is None
        assert result["overall"] == "pass"


class TestExecutionOrder:
    """Sequential — discover-runs MUST run before the (conditional) recall."""

    def test_discover_runs_first(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        ordered: list[str] = []
        _patch_read_executor(monkeypatch, None)
        _patch_run_subprocess(
            monkeypatch,
            {"discover-runs": _ok_subresult(), "recall": _ok_subresult()},
            record_order=ordered,
        )
        cp.classify_axis_preflight(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        assert ordered == ["discover-runs", "recall"]


class TestSynthErrorSubresult:
    """:func:`_synth_error_subresult` shape for spawn / timeout / parse failures."""

    def test_shape_matches_subresult_contract(self) -> None:
        result = cp._synth_error_subresult(
            error_code="cluster_timeout",
            message="recall timed out",
            category="cluster",
            elapsed_sec=42.0,
        )
        assert result["ok"] is False
        assert result["elapsed_sec"] == 42.0
        env = result["envelope"]
        assert env["ok"] is False
        assert env["error_code"] == "cluster_timeout"
        assert env["category"] == "cluster"
        assert env["retry_safe"] is False


class TestOutputSchema:
    """The returned ``data`` block validates against the output schema."""

    def test_result_validates(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import json

        from hpc_agent._kernel.contract.schema import validate

        _patch_read_executor(monkeypatch, None)
        _patch_run_subprocess(
            monkeypatch,
            {"discover-runs": _ok_subresult(), "recall": _ok_subresult()},
        )
        result = cp.classify_axis_preflight(
            experiment_dir=tmp_path, run_name="forecast", run_signature_sha=_SHA
        )
        schema_path = (
            Path(__import__("hpc_agent").__file__).parent
            / "schemas"
            / "classify_axis_preflight.output.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validate(result, schema)  # raises on violation
