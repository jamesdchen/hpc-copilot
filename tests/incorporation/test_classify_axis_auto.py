"""Tests for the ``classify-axis-auto`` composite primitive (Surface 3).

Pins the one-call collapse of preflight → easy → record and the five
terminal branches:

* **A** caller ``data_axis`` → record ``classified_by="interview"``;
* **B** preflight cache hit → reuse the stored classification, NO re-write;
* **C** a prior campaign's confident classification for the same run →
  record ``classified_by="recall"``;
* **D** ``classify-axis-easy`` returns a ``_CONFIDENT_KIND`` (all four,
  incl. ``bounded_halo``'s ``halo_expr``) → record ``classified_by="agent"``;
* **E** ``unclassifiable`` / ``function_not_found`` → NO record, return
  ``{needs_llm_tree: true, ...}``.

Plus: ``ambiguous_run`` (multiple runs, no scope) → ``SpecInvalid``, and a
SEQUENCING-GUARD test that asserts ``easy`` receives exactly the
``source_path`` / ``run_name`` the preflight produced — the invariant the
bug (hand-sequenced + mislabelled "in parallel") violated.

The sub-calls are mocked at their source modules (the composite imports
them at call time inside the function body), so no real ``discover-runs``
binary, ``axes.yaml`` on disk, or ``recall`` walk is needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.classify_axis_auto import ClassifyAxisAutoInput
from hpc_agent.incorporation import classify_axis_auto as caa
from hpc_agent.state.pack_declarations import AxisHintsDecl, PackEcho

_SHA = "a" * 64


def _hint_decl(*hints: dict[str, str], pack: str = "toy-widgets") -> AxisHintsDecl:
    """A toy ``AxisHintsDecl`` — pack-ignorant payload the classifier consumes."""
    return AxisHintsDecl(
        hints=tuple(hints),
        echo=PackEcho(pack=pack, version="0.0.1", sha=_SHA),
    )


# ── canned preflight / sub-call builders ─────────────────────────────────


def _run_row(name: str = "forecast", path: str = "/exp/notebooks/forecast.py") -> dict[str, Any]:
    return {
        "name": name,
        "path": path,
        "gpu": False,
        "run_signature_sha": _SHA,
        "flags": [],
    }


def _discover_subresult(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "envelope": {"ok": True, "idempotent": True, "data": {"runs": rows}},
        "elapsed_sec": 0.01,
        "ok": True,
    }


def _cache_subresult(*, hit: bool, stored: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "envelope": {"ok": True, "idempotent": True, "data": {"hit": hit, "stored": stored}},
        "elapsed_sec": 0.01,
        "ok": True,
    }


def _recall_subresult(campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "envelope": {"ok": True, "idempotent": True, "data": {"campaigns": campaigns}},
        "elapsed_sec": 0.01,
        "ok": True,
    }


def _preflight(
    *,
    rows: list[dict[str, Any]] | None = None,
    recall: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "overall": "pass",
        "elapsed_total_sec": 0.03,
        "discover_runs": _discover_subresult(rows if rows is not None else [_run_row()]),
        "cache_check": _cache_subresult(hit=False),
        "recall": recall,
    }


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    preflight: dict[str, Any] | None = None,
    cache: dict[str, Any] | None = None,
    easy: dict[str, Any] | None = None,
    record_calls: list[dict[str, Any]] | None = None,
    easy_calls: list[dict[str, Any]] | None = None,
) -> None:
    """Patch every sub-call the composite makes at call time.

    Patches at the SOURCE module (the composite does ``from
    hpc_agent.ops.classify_axis_preflight import classify_axis_preflight``
    etc. inside its body, so the source-module attribute is what binds).
    """
    import hpc_agent.incorporation.classify_axis as rec_mod
    import hpc_agent.incorporation.classify_axis_easy as easy_mod
    import hpc_agent.ops.classify_axis_preflight as pre_mod

    def fake_preflight(**kwargs: Any) -> dict[str, Any]:
        return preflight if preflight is not None else _preflight()

    monkeypatch.setattr(pre_mod, "classify_axis_preflight", fake_preflight)

    def fake_cache(**kwargs: Any) -> dict[str, Any]:
        return cache if cache is not None else _cache_subresult(hit=False)

    monkeypatch.setattr(pre_mod, "_run_cache_check", fake_cache)

    def fake_easy(*, source_path: str, run_name: str) -> dict[str, Any]:
        if easy_calls is not None:
            easy_calls.append({"source_path": source_path, "run_name": run_name})
        return (
            easy
            if easy is not None
            else {
                "kind": "unclassifiable",
                "evidence": "matcher abstained",
                "halo_expr": None,
                "tried": ["independent", "bounded_halo"],
            }
        )

    monkeypatch.setattr(easy_mod, "classify_axis_easy", fake_easy)

    def fake_record(experiment_dir: Any, *, spec: Any) -> dict[str, Any]:
        axis = spec.data_axis.model_dump(exclude_none=True, mode="json")
        if record_calls is not None:
            record_calls.append(
                {
                    "run_name": spec.run_name,
                    "run_signature_sha": spec.run_signature_sha,
                    "data_axis": axis,
                    "classified_by": spec.classified_by,
                }
            )
        return {
            "axes_path": str(Path(experiment_dir) / ".hpc" / "axes.yaml"),
            "run_name": spec.run_name,
            "data_axis": axis,
            "classified_by": spec.classified_by,
            "classified_at": "2026-01-01T00:00:00+00:00",
            "wrote": True,
        }

    monkeypatch.setattr(rec_mod, "classify_axis", fake_record)


# ── Branch A: caller-supplied data_axis ──────────────────────────────────


class TestBranchAInterview:
    def test_caller_data_axis_recorded_as_interview(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        easy_calls: list[dict[str, Any]] = []
        _patch(monkeypatch, record_calls=record_calls, easy_calls=easy_calls)
        spec = ClassifyAxisAutoInput.model_validate(
            {"run_name": "forecast", "data_axis": {"kind": "independent"}}
        )
        result = caa.classify_axis_auto(tmp_path, spec=spec)
        assert result["recorded"] is True
        assert result["kind"] == "independent"
        assert result["classified_by"] == "interview"
        # The matcher is NOT run when the caller resolved the axis.
        assert easy_calls == []
        assert record_calls[0]["classified_by"] == "interview"
        assert record_calls[0]["data_axis"] == {"kind": "independent"}


# ── Branch B: cache hit ──────────────────────────────────────────────────


class TestBranchBCacheHit:
    def test_cache_hit_reuses_no_rewrite(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        easy_calls: list[dict[str, Any]] = []
        stored = {
            "run_signature_sha": _SHA,
            "data_axis": {"kind": "sequential"},
            "classified_by": "agent",
        }
        _patch(
            monkeypatch,
            cache=_cache_subresult(hit=True, stored=stored),
            record_calls=record_calls,
            easy_calls=easy_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path, spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"})
        )
        assert result["recorded"] is True
        assert result["kind"] == "sequential"
        assert result["classified_by"] == "agent"
        # The whole point of a cache hit: NO re-write, NO matcher run.
        assert record_calls == []
        assert easy_calls == []


# ── Branch C: recall structural match ────────────────────────────────────


class TestBranchCRecall:
    def test_recall_match_recorded_as_recall(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        easy_calls: list[dict[str, Any]] = []
        recall = _recall_subresult(
            [
                {"data_axes": {"other_run": {"kind": "independent"}}},
                {"data_axes": {"forecast": {"kind": "bounded_halo", "halo_expr": "w * 48"}}},
            ]
        )
        _patch(
            monkeypatch,
            preflight=_preflight(recall=recall),
            record_calls=record_calls,
            easy_calls=easy_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path, spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"})
        )
        assert result["recorded"] is True
        assert result["kind"] == "bounded_halo"
        assert result["classified_by"] == "recall"
        # Recall short-circuits the matcher.
        assert easy_calls == []
        # The flat recall halo_expr is re-nested into halo: {expr}.
        assert record_calls[0]["data_axis"] == {
            "kind": "bounded_halo",
            "halo": {"expr": "w * 48"},
        }

    def test_recall_no_match_for_other_run_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A prior campaign classified a DIFFERENTLY-named run — no
        # structural match, so the composite must NOT reuse it.
        record_calls: list[dict[str, Any]] = []
        recall = _recall_subresult([{"data_axes": {"some_other_run": {"kind": "sequential"}}}])
        _patch(
            monkeypatch,
            preflight=_preflight(recall=recall),
            easy={
                "kind": "independent",
                "evidence": "DOALL",
                "halo_expr": None,
                "tried": ["independent"],
            },
            record_calls=record_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path, spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"})
        )
        # Fell through to the matcher (branch D) — recorded as agent, not recall.
        assert result["classified_by"] == "agent"
        assert result["kind"] == "independent"


# ── Branch D: confident matcher kinds (all four) ─────────────────────────


class TestBranchDAgent:
    @pytest.mark.parametrize(
        ("easy_kind", "halo_expr", "expected_kind", "expected_axis"),
        [
            ("independent", None, "independent", {"kind": "independent"}),
            ("sequential", None, "sequential", {"kind": "sequential"}),
            ("no_loop_detected", None, "cartesian", {"kind": "cartesian"}),
            (
                "bounded_halo",
                "train_window * 48",
                "bounded_halo",
                {"kind": "bounded_halo", "halo": {"expr": "train_window * 48"}},
            ),
        ],
    )
    def test_confident_kind_recorded_as_agent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        easy_kind: str,
        halo_expr: str | None,
        expected_kind: str,
        expected_axis: dict[str, Any],
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        _patch(
            monkeypatch,
            easy={
                "kind": easy_kind,
                "evidence": f"AST matched {easy_kind}",
                "halo_expr": halo_expr,
                "tried": [easy_kind],
            },
            record_calls=record_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path, spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"})
        )
        assert result["recorded"] is True
        assert result["kind"] == expected_kind
        assert result["classified_by"] == "agent"
        assert record_calls[0]["data_axis"] == expected_axis
        assert record_calls[0]["classified_by"] == "agent"


# ── Branch E: matcher abstains → needs_llm_tree (both abstain kinds) ──────


class TestBranchENeedsLlmTree:
    @pytest.mark.parametrize("abstain_kind", ["unclassifiable", "function_not_found"])
    def test_abstain_returns_needs_llm_tree_no_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, abstain_kind: str
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        _patch(
            monkeypatch,
            easy={
                "kind": abstain_kind,
                "evidence": f"{abstain_kind}: matcher abstained",
                "halo_expr": None,
                "tried": ["independent", "bounded_halo", "sequential"],
            },
            record_calls=record_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path, spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"})
        )
        assert result.get("needs_llm_tree") is True
        assert "recorded" not in result
        assert result["run_name"] == "forecast"
        assert result["source_path"] == "/exp/notebooks/forecast.py"
        assert result["run_signature_sha"] == _SHA
        assert result["evidence"] == f"{abstain_kind}: matcher abstained"
        assert result["tried"] == ["independent", "bounded_halo", "sequential"]
        # The recorder is NEVER called on an abstain.
        assert record_calls == []


# ── ambiguous_run → SpecInvalid ──────────────────────────────────────────


class TestAmbiguousRun:
    def test_multiple_runs_no_scope_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch(
            monkeypatch,
            preflight=_preflight(rows=[_run_row("forecast"), _run_row("backcast")]),
        )
        with pytest.raises(errors.SpecInvalid, match="ambiguous_run"):
            caa.classify_axis_auto(tmp_path, spec=ClassifyAxisAutoInput())

    def test_no_runs_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _patch(monkeypatch, preflight=_preflight(rows=[]))
        with pytest.raises(errors.SpecInvalid, match="ambiguous_run"):
            caa.classify_axis_auto(tmp_path, spec=ClassifyAxisAutoInput())

    def test_scoped_run_name_not_found_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch(monkeypatch, preflight=_preflight(rows=[_run_row("forecast")]))
        with pytest.raises(errors.SpecInvalid, match="ambiguous_run"):
            caa.classify_axis_auto(
                tmp_path, spec=ClassifyAxisAutoInput.model_validate({"run_name": "missing"})
            )


# ── SEQUENCING GUARD: easy receives what preflight produced ──────────────


class TestSequencingGuard:
    """The invariant the bug violated: easy is fed the EXACT source_path /
    run_name preflight resolved — not a value the LLM hand-wired in parallel."""

    def test_easy_receives_preflight_resolved_source_and_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        easy_calls: list[dict[str, Any]] = []
        # The preflight resolves a specific path; the matcher MUST be fed it.
        rows = [_run_row(name="weird_run", path="/exp/src/deep/weird_run.py")]
        _patch(
            monkeypatch,
            preflight=_preflight(rows=rows),
            easy={
                "kind": "unclassifiable",
                "evidence": "abstained",
                "halo_expr": None,
                "tried": [],
            },
            easy_calls=easy_calls,
        )
        # No run_name supplied → resolved from the sole discover-runs row.
        caa.classify_axis_auto(tmp_path, spec=ClassifyAxisAutoInput())
        assert len(easy_calls) == 1
        assert easy_calls[0]["run_name"] == "weird_run"
        assert easy_calls[0]["source_path"] == "/exp/src/deep/weird_run.py"


# ── S3 axis hints: the pure caution-not-clearance helper (T9c) ────────────


class TestApplyAxisHintsHelper:
    """`_apply_axis_hints` is pure and pack-ignorant: it decides confirm vs.
    demote off an opaque hint list + core's own verdict. Toy names only."""

    def test_no_hints_is_none_verdict(self) -> None:
        out = caa._apply_axis_hints("independent", "forecast", [])
        assert out.verdict == "none"
        assert out.hint_kinds == ()
        assert out.confirmations == () and out.conflicts == ()

    def test_non_matching_pattern_is_none(self) -> None:
        # The hint's regex does not match run_name → it never applies.
        out = caa._apply_axis_hints(
            "independent", "forecast", [_hint_decl({"pattern": "^train$", "axis": "Sequential"})]
        )
        assert out.verdict == "none"

    def test_agreeing_hint_confirms_and_echoes(self) -> None:
        out = caa._apply_axis_hints(
            "independent", "forecast", [_hint_decl({"pattern": "fore", "axis": "Independent"})]
        )
        assert out.verdict == "agree"
        assert out.hint_kinds == ("independent",)
        assert len(out.confirmations) == 1
        # The pack {pack, version, sha} echo rides the confirmation verbatim.
        assert out.confirmations[0]["pack"] == {
            "pack": "toy-widgets",
            "version": "0.0.1",
            "sha": _SHA,
        }

    def test_disagreeing_hint_conflicts_naming_both(self) -> None:
        out = caa._apply_axis_hints(
            "independent", "forecast", [_hint_decl({"pattern": "fore", "axis": "Sequential"})]
        )
        assert out.verdict == "conflict"
        assert out.core_kind == "independent"  # core's candidate
        assert out.hint_kinds == ("sequential",)  # the hint's candidate
        assert out.conflicts[0]["pack"]["pack"] == "toy-widgets"

    def test_unresolvable_core_never_cleared_by_hint(self) -> None:
        # core_kind=None (matcher abstained): a matching hint can only CONFLICT —
        # it may never clear an axis core could not resolve structurally.
        out = caa._apply_axis_hints(
            None, "forecast", [_hint_decl({"pattern": "fore", "axis": "Independent"})]
        )
        assert out.verdict == "conflict"
        assert out.core_kind is None
        assert out.confirmations == ()

    def test_multiple_hints_any_disagreement_demotes(self) -> None:
        # One agrees, one disagrees → caution wins (the safe direction).
        out = caa._apply_axis_hints(
            "independent",
            "forecast",
            [
                _hint_decl({"pattern": "fore", "axis": "Independent"}),
                _hint_decl({"pattern": "cast", "axis": "Sequential"}, pack="other-pack"),
            ],
        )
        assert out.verdict == "conflict"
        assert set(out.hint_kinds) == {"independent", "sequential"}

    def test_multiple_hints_all_agree_confirms(self) -> None:
        out = caa._apply_axis_hints(
            "sequential",
            "forecast",
            [
                _hint_decl({"pattern": "fore", "axis": "Sequential"}),
                _hint_decl({"pattern": "cast", "axis": "Sequential"}, pack="other-pack"),
            ],
        )
        assert out.verdict == "agree"
        assert len(out.confirmations) == 2


# ── S3 axis hints: the classify_axis_auto terminal shapes (T9c) ───────────


class TestAxisHintsIntegration:
    """The hint wiring at the branch D/E boundary, exercised end to end. The
    injected ``axis_hints`` is the pack-ignorant list the ``state`` resolver
    would hand the classifier — here supplied directly."""

    def test_no_hints_branch_d_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        _patch(
            monkeypatch,
            easy={"kind": "independent", "evidence": "DOALL", "halo_expr": None, "tried": ["ind"]},
            record_calls=record_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path,
            spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"}),
            axis_hints=[],
        )
        assert result["recorded"] is True
        assert result["classified_by"] == "agent"
        assert record_calls[0]["data_axis"] == {"kind": "independent"}

    def test_agreeing_hint_records_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        _patch(
            monkeypatch,
            easy={"kind": "independent", "evidence": "DOALL", "halo_expr": None, "tried": ["ind"]},
            record_calls=record_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path,
            spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"}),
            axis_hints=[_hint_decl({"pattern": "fore", "axis": "Independent"})],
        )
        # Agreement proceeds unchanged: recorded as agent, byte-identical record.
        assert result["recorded"] is True
        assert result["classified_by"] == "agent"
        assert result["kind"] == "independent"
        assert record_calls[0]["data_axis"] == {"kind": "independent"}

    def test_disagreeing_hint_demotes_naming_both(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        _patch(
            monkeypatch,
            easy={"kind": "independent", "evidence": "DOALL", "halo_expr": None, "tried": ["ind"]},
            record_calls=record_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path,
            spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"}),
            axis_hints=[_hint_decl({"pattern": "fore", "axis": "Sequential"})],
        )
        # Disagreement demotes to the decision tree — NO record — naming BOTH.
        assert result.get("needs_llm_tree") is True
        assert "recorded" not in result
        assert record_calls == []
        assert "independent" in result["evidence"]  # core's candidate
        assert "sequential" in result["evidence"]  # the hint's candidate
        assert "toy-widgets" in result["evidence"]

    def test_unresolvable_with_hint_stays_needs_llm_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record_calls: list[dict[str, Any]] = []
        _patch(
            monkeypatch,
            easy={
                "kind": "unclassifiable",
                "evidence": "abstained",
                "halo_expr": None,
                "tried": ["ind", "halo"],
            },
            record_calls=record_calls,
        )
        result = caa.classify_axis_auto(
            tmp_path,
            spec=ClassifyAxisAutoInput.model_validate({"run_name": "forecast"}),
            axis_hints=[_hint_decl({"pattern": "fore", "axis": "Independent"})],
        )
        # A hint NEVER auto-resolves an axis core could not resolve structurally.
        assert result.get("needs_llm_tree") is True
        assert "recorded" not in result
        assert record_calls == []
        # The hint rides along as caution, but the tree still decides.
        assert "toy-widgets" in result["evidence"]
