"""``combine-wave`` idempotency on ``(run_id, wave)``.

The primitive declares ``idempotent=True``, so a no-``force`` replay of an
already-combined wave must be a success — not a ``CombinerFailed`` that
appends the wave to ``failed_waves`` while it still sits in
``combined_waves``. Two witnesses cover it: the journal's
``combined_waves`` (checked before any ssh), and the cluster combiner's
no-force "output already exists (use --force)" refusal (the on-cluster
witness when the journal lost the wave). A genuine combiner failure must
still land in ``failed_waves``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "ml_abcd1234"


@pytest.fixture(autouse=True)
def _journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _seed_run(tmp_path: Path, *, combined_waves: list[int] | None = None) -> RunRecord:
    rec = RunRecord(
        run_id=_RUN_ID,
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/exp",
        job_name="ml",
        job_ids=["12345"],
        total_tasks=2,
        submitted_at="2026-07-01T00:00:00+00:00",
        experiment_dir=str(tmp_path),
        combined_waves=combined_waves or [],
    )
    upsert_run(tmp_path, rec)
    return rec


def _combine(tmp_path: Path, *, force: bool = False) -> tuple[bool, str, str]:
    return combine_wave(
        tmp_path,
        _RUN_ID,
        wave=0,
        ssh_target="user@host",
        remote_path="/exp",
        force=force,
    )


def test_replay_of_combined_wave_is_success_without_ssh(tmp_path: Path) -> None:
    """No-force replay of a journal-combined wave: success, zero ssh."""
    _seed_run(tmp_path, combined_waves=[0])

    def _boom(**_kw: object) -> tuple[bool, str, str]:
        raise AssertionError("replay of a combined wave must not reach the cluster")

    with patch("hpc_agent.infra.transport.run_combiner_checked", side_effect=_boom):
        ok, stdout, _stderr = _combine(tmp_path)

    assert ok is True
    assert "already combined" in stdout
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == [0]
    assert record.failed_waves == []


def test_force_reruns_combiner_for_combined_wave(tmp_path: Path) -> None:
    """--force keeps its semantics: the combiner runs even when combined."""
    _seed_run(tmp_path, combined_waves=[0])

    with patch(
        "hpc_agent.infra.transport.run_combiner_checked",
        return_value=(True, "combined", ""),
    ) as combiner:
        ok, _stdout, _stderr = _combine(tmp_path, force=True)

    assert ok is True
    combiner.assert_called_once()
    assert combiner.call_args.kwargs["force"] is True


def test_combiner_already_exists_refusal_records_combined_not_failed(tmp_path: Path) -> None:
    """Journal lost the wave but the cluster output exists: the combiner's
    no-force refusal is recognized as the idempotent success, and the journal
    is repaired (wave in combined_waves, never failed_waves)."""
    _seed_run(tmp_path)

    stderr = (
        "[combiner] ERROR: output already exists: _combiner/wave_0.json (use --force to overwrite)"
    )
    with patch(
        "hpc_agent.infra.transport.run_combiner_checked",
        return_value=(False, "", stderr),
    ):
        ok, stdout, _stderr = _combine(tmp_path)

    assert ok is True
    assert "already combined" in stdout
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == [0]
    assert record.failed_waves == []


def test_genuine_failure_still_records_failed_wave(tmp_path: Path) -> None:
    """A real combiner failure keeps landing in failed_waves."""
    _seed_run(tmp_path)

    with patch(
        "hpc_agent.infra.transport.run_combiner_checked",
        return_value=(False, "", "boom: missing metrics"),
    ):
        ok, _stdout, _stderr = _combine(tmp_path)

    assert ok is False
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.failed_waves == [0]
    assert record.combined_waves == []
