"""Tests for the ``discover-runs`` primitive (``@register_run`` discovery).

Self-contained: writes a minimal ``@register_run`` function to a tmp dir and
asserts the primitive's projection, so it does not depend on shared fixture
paths. Mirrors what ``/submit-hpc`` Step 1 invokes instead of shelling
``python .hpc/scaffold.py discover``.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.state.discover import _discover_runs_result_post, discover_runs

_SRC = """
from __future__ import annotations
from hpc_agent.experiment_kit import register_run


@register_run
def run(seed: int, lr: float) -> dict:
    return {"seed": seed, "lr": lr}
"""


def _write(d: Path) -> None:
    (d / "train.py").write_text(_SRC, encoding="utf-8")


def test_discover_runs_finds_register_run(tmp_path: Path) -> None:
    _write(tmp_path)
    infos = discover_runs(tmp_path)
    names = [i.name for i in infos]
    assert "run" in names, f"expected the @register_run function, got {names}"


def test_discover_runs_skips_hpc_dir(tmp_path: Path) -> None:
    _write(tmp_path)
    hpc = tmp_path / ".hpc"
    hpc.mkdir()
    # A decorated function under .hpc/ must NOT be discovered (framework dir).
    (hpc / "wrapper.py").write_text(_SRC, encoding="utf-8")
    names = [i.name for i in discover_runs(tmp_path)]
    assert names.count("run") == 1, f"expected exactly one run (skip .hpc/), got {names}"


def test_discover_runs_result_post_projects_envelope_shape(tmp_path: Path) -> None:
    _write(tmp_path)
    out = _discover_runs_result_post(discover_runs(tmp_path))
    assert set(out) == {"runs"}
    row = next(r for r in out["runs"] if r["name"] == "run")
    assert set(row) == {"name", "path", "gpu", "run_signature_sha", "flags"}
    assert row["gpu"] is False
    assert isinstance(row["run_signature_sha"], str) and row["run_signature_sha"]
    assert sorted(row["flags"]) == ["lr", "seed"]
