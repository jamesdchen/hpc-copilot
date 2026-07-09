"""F-J residual — aggregate-flow's per-task SSH fallback honors summary_artifact.

``_per_task_metrics_reduce`` (the no-combiner default) historically hardcoded
``metrics.json`` in its rsync ``include`` filter, its local ``rglob``, and its
``reduce_metrics`` call. A run whose executor emits a differently-named per-task
summary (e.g. ``results_reduce.json`` — proving run #10) had NOTHING pulled and
read as a harvest gap. The seam now resolves the run's declared
``summary_artifact`` once and threads it down as ``summary_name``.

FIRES: a run emitting ``results_reduce.json`` reduces when the declared name is
threaded, and RAISES under the old ``metrics.json`` assumption (the run #10 gap).
PASSES: an undeclared run resolves to ``metrics.json`` and reduces byte-identical.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent.ops import aggregate_flow as agg

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260101-000000-summaryart"
_PAYLOAD = {"metric": 7.0, "n_samples": 1}


def _fake_record() -> SimpleNamespace:
    return SimpleNamespace(ssh_target="u@h", remote_path="/remote", total_tasks=1)


def _install_fake_pull(monkeypatch: pytest.MonkeyPatch, *, remote_emits: str) -> None:
    """Booby-trap ``rsync_pull`` to model an executor that wrote ONLY *remote_emits*.

    The pull honors its ``include`` filter exactly like real rsync: a task's file
    lands locally only when the requested name matches what the executor wrote.
    So a mismatched (hardcoded) name pulls nothing — the run #10 failure shape.
    """

    def _fake_pull(*, local_dir: str, include: list[str] | None, **_kw: Any) -> SimpleNamespace:
        from pathlib import Path

        if include and remote_emits in include:
            task_dir = Path(local_dir) / "task_0"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / remote_emits).write_text(json.dumps(_PAYLOAD), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(agg, "rsync_pull", _fake_pull)


def test_fires_declared_summary_artifact_reduces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIRES: executor emits results_reduce.json.

    Threading the declared name reduces it; the old metrics.json hardcode would
    have pulled nothing and raised — exactly the run #10 harvest gap.
    """
    _install_fake_pull(monkeypatch, remote_emits="results_reduce.json")

    # Declared name honored → reduced.
    out = agg._per_task_metrics_reduce(
        tmp_path,
        _RUN_ID,
        record=_fake_record(),
        out=tmp_path,
        results_subdir="results",
        summary_name="results_reduce.json",
    )
    assert out == {_RUN_ID: {"metric": pytest.approx(7.0), "n_samples": 1}}

    # Old hardcode (metrics.json) against the SAME on-disk state → nothing
    # pulled → refuses to fabricate an aggregate (the run #10 read-as-gap).
    with pytest.raises(errors.RemoteCommandFailed):
        agg._per_task_metrics_reduce(
            tmp_path,
            _RUN_ID,
            record=_fake_record(),
            out=tmp_path / "hardcode",
            results_subdir="results",
            summary_name="metrics.json",
        )


def test_default_metrics_json_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PASSES: an undeclared run (resolved default metrics.json) reduces as before."""
    _install_fake_pull(monkeypatch, remote_emits="metrics.json")
    out = agg._per_task_metrics_reduce(
        tmp_path,
        _RUN_ID,
        record=_fake_record(),
        out=tmp_path,
        results_subdir="results",
        summary_name="metrics.json",  # what resolved_summary_artifact returns when absent
    )
    assert out == {_RUN_ID: {"metric": pytest.approx(7.0), "n_samples": 1}}
