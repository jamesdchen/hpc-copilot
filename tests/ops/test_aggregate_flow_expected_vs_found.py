"""Foreign-layout disclosure — the per-task fallback's mismatch error says
EXPECTED vs FOUND.

Post-exploration checker: an adopted/foreign run's result tree frequently does
not match the declared ``result_dir_template`` / ``summary_artifact``. When
``_per_task_metrics_reduce`` (the no-combiner fallback) finds no readable
sidecars, the failure previously named only what was ABSENT; it now states
what was EXPECTED (the declared template + summary artifact) AND what was
actually FOUND (a bounded, deterministic sample of the pulled mirror), so a
layout mismatch is diagnosable from the message alone.

Pinned here:

* (c) MISMATCH — the raise carries the declared result_dir_template, the
  declared summary_artifact name, and a bounded sample of the real tree; the
  empty-mirror case states the pull filter that matched nothing.
* (d) NUMERICS REGRESSION — the happy foreign-layout path (declared
  summary_artifact honored end-to-end) reduces to byte-identical numbers:
  the disclosure work never touches the weighted-mean reducer.
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

_RUN_ID = "20260814-000000-expfound"


def _fake_record(*, template: str | None = "results/{run_id}/task_{task_id}") -> SimpleNamespace:
    return SimpleNamespace(
        ssh_target="u@h",
        remote_path="/remote",
        total_tasks=3,
        result_dir_template=template,
    )


# ── (c) the mismatch error: expected vs found ────────────────────────────────


def test_mismatch_error_names_expected_template_and_found_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign tree (wrong filenames for the declared artifact) raises with
    BOTH sides: the declared template + summary_artifact, and a sample of the
    files the pull actually landed."""

    def _fake_pull(*, local_dir: str, include: list[str] | None, **_kw: Any) -> SimpleNamespace:
        from pathlib import Path

        # A foreign layout: per-task dirs exist but carry a differently-named
        # summary (the freestyle run wrote summary.json, not metrics.json).
        for t in range(2):
            d = Path(local_dir) / f"job_output_{t}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "summary.json").write_text(json.dumps({"m": 1.0}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(agg, "rsync_pull", _fake_pull)
    with pytest.raises(errors.RemoteCommandFailed) as exc_info:
        agg._per_task_metrics_reduce(
            tmp_path,
            _RUN_ID,
            record=_fake_record(),
            out=tmp_path,
            results_subdir="results",
            summary_name="metrics.json",
        )
    msg = str(exc_info.value)
    # EXPECTED side: the declared template AND the summary artifact, verbatim.
    assert "results/{run_id}/task_{task_id}" in msg
    assert "'metrics.json'" in msg
    assert "expected" in msg
    # FOUND side: a sample of the real tree — the foreign filenames are named.
    assert "found" in msg
    assert "job_output_0/summary.json" in msg


def test_mismatch_error_empty_mirror_names_pull_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the pull lands NOTHING, the found side says so explicitly and names
    the include filter that matched nothing — distinguishing 'wrong names in
    the tree' from 'no files at all'."""

    def _fake_pull(**_kw: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(agg, "rsync_pull", _fake_pull)
    with pytest.raises(errors.RemoteCommandFailed) as exc_info:
        agg._per_task_metrics_reduce(
            tmp_path,
            _RUN_ID,
            record=_fake_record(),
            out=tmp_path,
            results_subdir="results",
            summary_name="metrics.json",
        )
    msg = str(exc_info.value)
    assert "results/{run_id}/task_{task_id}" in msg
    assert "NO files" in msg
    assert "metrics.json" in msg  # the pull filter names the artifact


def test_mismatch_error_without_template_still_names_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record with no result_dir_template (and no sidecar) still gets the
    expected/found frame — expected degrades to 'no template declared' plus
    the artifact name; the frame never raises on missing metadata."""

    def _fake_pull(**_kw: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(agg, "rsync_pull", _fake_pull)
    with pytest.raises(errors.RemoteCommandFailed) as exc_info:
        agg._per_task_metrics_reduce(
            tmp_path,
            _RUN_ID,
            record=_fake_record(template=None),
            out=tmp_path,
            results_subdir="results",
            summary_name="metrics.json",
        )
    msg = str(exc_info.value)
    assert "no result_dir_template declared" in msg
    assert "'metrics.json'" in msg


def test_unparseable_branch_keeps_its_diagnosis_and_gains_expected_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matched-but-unparseable branch keeps its 'NONE parsed as JSON'
    diagnosis (different remediation) AND now carries the expected/found frame."""

    def _fake_pull(*, local_dir: str, include: list[str] | None, **_kw: Any) -> SimpleNamespace:
        from pathlib import Path

        sidecar = Path(local_dir) / "task_0" / "metrics.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("estimator,rmse\nridge,0.42\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(agg, "rsync_pull", _fake_pull)
    with pytest.raises(errors.RemoteCommandFailed) as exc_info:
        agg._per_task_metrics_reduce(
            tmp_path,
            _RUN_ID,
            record=_fake_record(),
            out=tmp_path,
            results_subdir="results",
            summary_name="metrics.json",
        )
    msg = str(exc_info.value)
    assert "NONE parsed as JSON" in msg
    assert "expected" in msg
    assert "task_0/metrics.json" in msg  # the found sample includes the real file


def test_found_sample_is_bounded_and_deterministic(tmp_path: Path) -> None:
    """The tree sample never exceeds its bound, spreads across top-level dirs,
    and is deterministic (sorted) — bounded disclosure, not a tree dump."""
    root = tmp_path / "mirror"
    for t in range(20):
        d = root / f"task_{t:02d}"
        d.mkdir(parents=True)
        (d / "a.json").write_text("{}", encoding="utf-8")
        (d / "b.json").write_text("{}", encoding="utf-8")

    sample = agg._mirror_tree_sample(root, limit=8)

    assert len(sample) == 8
    assert sample == sorted(sample)
    # Spread: 8 distinct top-level dirs, not 8 files from task_00.
    assert len({p.split("/", 1)[0] for p in sample}) == 8
    # Deterministic across calls.
    assert sample == agg._mirror_tree_sample(root, limit=8)
    # Missing dir → empty, never a raise.
    assert agg._mirror_tree_sample(tmp_path / "absent") == []


# ── (d) numerics regression: happy foreign-layout path unchanged ─────────────


def test_happy_foreign_layout_numerics_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign-layout run whose declared summary_artifact IS honored reduces
    to the exact weighted-mean numbers — the disclosure changes touch only the
    error path, never the reducer math or the returned shape."""

    payloads = [
        {"metric": 4.0, "n_samples": 1},
        {"metric": 8.0, "n_samples": 3},
    ]

    def _fake_pull(*, local_dir: str, include: list[str] | None, **_kw: Any) -> SimpleNamespace:
        from pathlib import Path

        assert include is not None and "results_reduce.json" in include
        for t, payload in enumerate(payloads):
            d = Path(local_dir) / f"task_{t}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "results_reduce.json").write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(agg, "rsync_pull", _fake_pull)
    out = agg._per_task_metrics_reduce(
        tmp_path,
        _RUN_ID,
        record=_fake_record(),
        out=tmp_path,
        results_subdir="results",
        summary_name="results_reduce.json",
    )
    # Weighted mean: (4*1 + 8*3) / (1+3) = 7.0; n_samples summed = 4.
    assert out == {_RUN_ID: {"metric": pytest.approx(7.0), "n_samples": 4}}
