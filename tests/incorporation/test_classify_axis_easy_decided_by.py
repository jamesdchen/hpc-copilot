"""The classify-axis-easy primitive emits a kernel decided_by alongside kind.

Pins the kernel symmetry with classify-campaign-path: a confident matcher
kind resolves decided_by="code"; the function_not_found / unclassifiable
tail escalates decided_by="judgement".
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.incorporation.classify_axis_easy import classify_axis_easy

_CONFIDENT = {"independent", "bounded_halo", "sequential", "no_loop_detected"}


def test_no_loop_function_resolves_code(tmp_path: Path) -> None:
    p = tmp_path / "e.py"
    p.write_text("def run(i):\n    return {'x': i}\n", encoding="utf-8")
    out = classify_axis_easy(source_path=str(p), run_name="run")
    assert out["kind"] == "no_loop_detected"
    assert out["decided_by"] == "code"


def test_function_not_found_escalates_judgement(tmp_path: Path) -> None:
    p = tmp_path / "e.py"
    p.write_text("def other():\n    pass\n", encoding="utf-8")
    out = classify_axis_easy(source_path=str(p), run_name="run")
    assert out["kind"] == "function_not_found"
    assert out["decided_by"] == "judgement"


def test_decided_by_tracks_kind(tmp_path: Path) -> None:
    p = tmp_path / "e.py"
    p.write_text("def run(i):\n    return {'x': i}\n", encoding="utf-8")
    out = classify_axis_easy(source_path=str(p), run_name="run")
    expected = "code" if out["kind"] in _CONFIDENT else "judgement"
    assert out["decided_by"] == expected
