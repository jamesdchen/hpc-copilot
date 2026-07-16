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

import re
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from hpc_agent.ops.aggregate.combine import combine_wave, combine_waves
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


def test_foreign_run_refusal_not_journaled_combined(tmp_path: Path) -> None:
    """F05 FIRE PATH: the combiner's refusal names a DIFFERENT run_id (a foreign
    partial persisting under the delete-protected _combiner/). It must NOT be
    journaled as this run's combined wave — ok stays False so the force retry
    recombines this run over its own data."""
    _seed_run(tmp_path)

    stderr = (
        "[combiner] ERROR: output already exists: _combiner/wave_0.json "
        "(use --force to overwrite) [run_id=some_other_run]"
    )
    with patch(
        "hpc_agent.infra.transport.run_combiner_checked",
        return_value=(False, "", stderr),
    ):
        ok, _stdout, _stderr = _combine(tmp_path)

    assert ok is False
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == []  # NOT adopted
    assert record.failed_waves == [0]


def test_same_run_refusal_with_run_id_is_recognized(tmp_path: Path) -> None:
    """The refusal that names OUR run_id is still the idempotent success."""
    _seed_run(tmp_path)

    stderr = (
        f"[combiner] ERROR: output already exists: _combiner/wave_0.json "
        f"(use --force to overwrite) [run_id={_RUN_ID}]"
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


def test_wave_flagged_for_recombine_not_re_adopted(tmp_path: Path) -> None:
    """F06 FIRE PATH: a wave a resubmit invalidated (present in failed_waves, its
    stale partial still on the cluster) must NOT be re-adopted from the
    'already exists' refusal on a no-force pass — ok stays False so the force
    retry recombines it over the recovered tasks."""
    # Wave 0 was invalidated: dropped from combined_waves, recorded failed.
    rec = _seed_run(tmp_path)
    rec.failed_waves = [0]
    upsert_run(tmp_path, rec)

    stderr = (
        f"[combiner] ERROR: output already exists: _combiner/wave_0.json "
        f"(use --force to overwrite) [run_id={_RUN_ID}]"
    )
    with patch(
        "hpc_agent.infra.transport.run_combiner_checked",
        return_value=(False, "", stderr),
    ):
        ok, _stdout, _stderr = _combine(tmp_path)

    assert ok is False  # not re-adopted despite the same-run refusal
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == []
    assert record.failed_waves == [0]


def test_force_recombine_clears_failed_and_marks_combined(tmp_path: Path) -> None:
    """After the invalidation, a FORCE recombine (the retry ``_combine_missing``
    fires) succeeds and moves the wave out of failed_waves into combined_waves."""
    rec = _seed_run(tmp_path)
    rec.failed_waves = [0]
    upsert_run(tmp_path, rec)

    with patch(
        "hpc_agent.infra.transport.run_combiner_checked",
        return_value=(True, "combined", ""),
    ):
        ok, _stdout, _stderr = _combine(tmp_path, force=True)

    assert ok is True
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == [0]
    assert record.failed_waves == []


# ── P4 tier-1: fused multi-wave combine (one ssh exec per burst) ──────────────


class _FakeBatchSSH:
    """A counting ``ssh_run`` stand-in that synthesizes the sentinel frame.

    Reads the ``--wave N`` occurrences out of the outgoing fused command and
    emits a ``__HPC_WAVE_BEGIN__/END__`` frame per wave (rc from *rc_by_wave*,
    default 0) plus the trailing ``__HPC_BATCH_END__`` — unless *drop_batch_end*
    is set, which models a truncated stream (the E3 fallback trigger).
    """

    def __init__(
        self, rc_by_wave: dict[int, int] | None = None, *, drop_batch_end: bool = False
    ) -> None:
        self.rc_by_wave = rc_by_wave or {}
        self.drop_batch_end = drop_batch_end
        self.calls: list[str] = []

    def __call__(self, cmd: str, *, ssh_target: str, **_kw: object) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        waves = [int(m) for m in re.findall(r"--wave (\d+)", cmd)]
        lines: list[str] = []
        for w in waves:
            lines.append(f"__HPC_WAVE_BEGIN__ {w}")
            rc = self.rc_by_wave.get(w, 0)
            lines.append(
                f"[combiner] wrote _combiner/wave_{w}.json"
                if rc == 0
                else f"[combiner] ERROR: boom in wave {w}"
            )
            lines.append(f"__HPC_WAVE_END__ {w} rc={rc}")
        if not self.drop_batch_end:
            lines.append("__HPC_BATCH_END__")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="\n".join(lines) + "\n", stderr=""
        )

    @property
    def exec_count(self) -> int:
        return len(self.calls)


def test_ten_wave_burst_is_one_ssh_exec(tmp_path: Path) -> None:
    """10-wave burst = ONE fused ssh exec; every wave journaled combined."""
    _seed_run(tmp_path)
    fake = _FakeBatchSSH()
    with patch("hpc_agent.infra.transport._combiner.ssh_run", fake):
        results = combine_waves(
            tmp_path,
            _RUN_ID,
            waves=list(range(10)),
            ssh_target="user@host",
            remote_path="/exp",
        )

    assert fake.exec_count == 1  # the whole burst rode one exec
    assert all(results[w][0] for w in range(10))
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == list(range(10))
    assert record.failed_waves == []


def test_batch_partial_failure_journals_per_wave(tmp_path: Path) -> None:
    """A partial batch failure journals per-wave outcomes individually — no silent
    whole-batch verdict. Wave 2 fails (rc 1); the rest combine."""
    _seed_run(tmp_path)
    fake = _FakeBatchSSH(rc_by_wave={2: 1})
    with patch("hpc_agent.infra.transport._combiner.ssh_run", fake):
        results = combine_waves(
            tmp_path,
            _RUN_ID,
            waves=[0, 1, 2, 3],
            ssh_target="user@host",
            remote_path="/exp",
        )

    assert fake.exec_count == 1
    assert results[2][0] is False
    assert results[0][0] is True
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == [0, 1, 3]
    assert record.failed_waves == [2]  # honest per-wave accounting


def test_batch_missing_end_sentinel_falls_back_per_wave(tmp_path: Path) -> None:
    """E3 FIRE PATH: a truncated fused stream (no ``__HPC_BATCH_END__``) must NOT
    be parse-and-trusted — combine_waves degrades to a per-wave ``combine_wave``
    call for every affected wave rather than adopting a partial batch."""
    _seed_run(tmp_path)
    per_wave: list[int] = []

    def _fake_single(*, wave: int, **_kw: object) -> tuple[bool, str, str]:
        per_wave.append(wave)
        return (True, "combined", "")

    with (
        patch("hpc_agent.infra.transport._combiner.ssh_run", _FakeBatchSSH(drop_batch_end=True)),
        patch("hpc_agent.infra.transport.run_combiner_checked", side_effect=_fake_single),
    ):
        results = combine_waves(
            tmp_path,
            _RUN_ID,
            waves=[0, 1, 2],
            ssh_target="user@host",
            remote_path="/exp",
        )

    # Every wave fell back to its own combine_wave (never adopted the truncation).
    assert sorted(per_wave) == [0, 1, 2]
    assert all(results[w][0] for w in (0, 1, 2))
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == [0, 1, 2]


def test_batch_skips_journal_combined_wave_without_cluster(tmp_path: Path) -> None:
    """A wave already recorded combined (no force) is an idempotent journal hit —
    it is resolved with NO cluster contact and excluded from the fused exec."""
    _seed_run(tmp_path, combined_waves=[0])
    fake = _FakeBatchSSH()
    with patch("hpc_agent.infra.transport._combiner.ssh_run", fake):
        results = combine_waves(
            tmp_path,
            _RUN_ID,
            waves=[0, 1],
            ssh_target="user@host",
            remote_path="/exp",
        )

    assert results[0][0] is True
    assert "already combined (journal)" in results[0][1]
    # The fused command combined ONLY the un-combined wave 1.
    assert fake.exec_count == 1
    assert "--wave 1" in fake.calls[0]
    assert "--wave 0" not in fake.calls[0]
    record = load_run(tmp_path, _RUN_ID)
    assert record is not None
    assert record.combined_waves == [0, 1]
