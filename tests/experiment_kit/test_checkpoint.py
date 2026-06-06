"""Tests for the #294 PR1 checkpoint-aware recovery helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.experiment_kit import checkpoint as ck


@pytest.fixture(autouse=True)
def _reset_interval_timer() -> None:
    ck._reset_should_checkpoint_state()


def test_write_read_round_trip(tmp_path: Path) -> None:
    state = {"weights": [1, 2, 3], "step": 7}
    path = ck.write_checkpoint(state, iteration=7, result_dir=tmp_path)
    assert path == tmp_path / "_checkpoints" / "checkpoint-7.pkl"
    assert path.is_file()
    assert ck.read_checkpoint(path) == state


def test_write_is_atomic_no_tmp_left(tmp_path: Path) -> None:
    ck.write_checkpoint({"x": 1}, iteration=0, result_dir=tmp_path)
    leftovers = list((tmp_path / "_checkpoints").glob("*.tmp"))
    assert leftovers == []


def test_read_latest_fresh_run_returns_none_zero(tmp_path: Path) -> None:
    state, next_iter = ck.read_latest_checkpoint(result_dir=tmp_path)
    assert state is None
    assert next_iter == 0


def test_read_latest_picks_highest_iteration_and_next_index(tmp_path: Path) -> None:
    ck.write_checkpoint({"i": 2}, iteration=2, result_dir=tmp_path)
    ck.write_checkpoint({"i": 10}, iteration=10, result_dir=tmp_path)
    ck.write_checkpoint({"i": 5}, iteration=5, result_dir=tmp_path)
    state, next_iter = ck.read_latest_checkpoint(result_dir=tmp_path)
    assert state == {"i": 10}
    assert next_iter == 11  # resume AFTER the latest checkpointed iteration
    assert ck.latest_checkpoint(result_dir=tmp_path).name == "checkpoint-10.pkl"


def test_result_dir_resolves_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_RESULT_DIR", str(tmp_path))
    ck.write_checkpoint({"a": 1}, iteration=3)  # no explicit result_dir
    assert (tmp_path / "_checkpoints" / "checkpoint-3.pkl").is_file()
    state, next_iter = ck.read_latest_checkpoint()
    assert state == {"a": 1} and next_iter == 4


def test_empty_checkpoint_file_is_ignored(tmp_path: Path) -> None:
    ckdir = tmp_path / "_checkpoints"
    ckdir.mkdir()
    (ckdir / "checkpoint-0.pkl").write_bytes(b"")  # 0-byte (crashed mid-write, pre-atomic)
    assert ck.latest_checkpoint(result_dir=tmp_path) is None
    assert ck.read_latest_checkpoint(result_dir=tmp_path) == (None, 0)


def test_read_latest_skips_corrupt_newest(tmp_path: Path) -> None:
    ck.write_checkpoint({"good": 1}, iteration=1, result_dir=tmp_path)
    # A newer but unreadable checkpoint must not force a from-scratch restart.
    (tmp_path / "_checkpoints" / "checkpoint-2.pkl").write_bytes(b"\x80\x05 not a pickle")
    state, next_iter = ck.read_latest_checkpoint(result_dir=tmp_path)
    assert state == {"good": 1}
    assert next_iter == 2


def test_checkpoint_iteration_parsing() -> None:
    assert ck.checkpoint_iteration("checkpoint-42.pkl") == 42
    assert ck.checkpoint_iteration("/a/b/_checkpoints/checkpoint-0.pkl") == 0
    assert ck.checkpoint_iteration("metrics.json") is None


def test_should_checkpoint_walltime_margin_no_deadline_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HPC_WALLTIME_END_EPOCH", raising=False)
    assert ck.should_checkpoint(strategy="walltime_margin", margin_min=10) is False


def test_should_checkpoint_walltime_margin_fires_within_margin() -> None:
    # deadline 5 min out, margin 10 min → within margin → True.
    assert (
        ck.should_checkpoint(
            strategy="walltime_margin", margin_min=10, deadline_epoch=1000.0, _now_epoch=700.0
        )
        is True
    )
    # deadline 30 min out, margin 10 min → not yet → False.
    assert (
        ck.should_checkpoint(
            strategy="walltime_margin", margin_min=10, deadline_epoch=2500.0, _now_epoch=700.0
        )
        is False
    )


def test_should_checkpoint_walltime_margin_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_WALLTIME_END_EPOCH", "1000")
    assert ck.should_checkpoint(strategy="walltime_margin", margin_min=10, _now_epoch=500.0) is True


def test_should_checkpoint_interval_arms_then_fires() -> None:
    # First call arms the timer (returns False so a loop skips iteration 0).
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=0.0) is False
    # Before the interval elapses → still False.
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=300.0) is False
    # After interval_min (10 min = 600s) → True, and re-arms.
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=600.0) is True
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=900.0) is False
    assert ck.should_checkpoint(strategy="interval", interval_min=10, _now_mono=1200.0) is True


def test_should_checkpoint_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError, match="unknown checkpoint strategy"):
        ck.should_checkpoint(strategy="every-full-moon")


def test_checkpoint_dir_prefers_stable_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # #294 PR3: the dispatcher exports HPC_CHECKPOINT_DIR (the STABLE final dir),
    # which must win over HPC_RESULT_DIR (the WIP dir) so checkpoints survive a
    # kill/resubmit. An explicit result_dir still overrides both.
    stable = tmp_path / "final" / "_checkpoints"
    monkeypatch.setenv("HPC_CHECKPOINT_DIR", str(stable))
    monkeypatch.setenv("HPC_RESULT_DIR", str(tmp_path / "wip"))  # must be ignored
    assert ck.checkpoint_dir() == stable
    ck.write_checkpoint({"x": 1}, iteration=0)  # no explicit result_dir
    assert (stable / "checkpoint-0.pkl").is_file()
    assert not (tmp_path / "wip" / "_checkpoints").exists()
    # explicit result_dir wins over the env.
    assert ck.checkpoint_dir(tmp_path / "z") == tmp_path / "z" / "_checkpoints"


def test_run_iterations_fresh_run_completes_and_checkpoints(tmp_path: Path) -> None:
    final = ck.run_iterations(
        lambda s, i: s + 1, init=0, n=5, result_dir=tmp_path, checkpoint_every=1
    )
    assert final == 5
    assert ck.latest_checkpoint(result_dir=tmp_path).name == "checkpoint-4.pkl"
    assert ck.read_checkpoint(ck.latest_checkpoint(result_dir=tmp_path)) == 5


def test_run_iterations_resumes_and_skips_done_work(tmp_path: Path) -> None:
    # Pre-seed a checkpoint at iteration 4 (state=5): a resume must continue from
    # iteration 5 and never re-run 0..4 — the executor side of --from-checkpoint.
    ck.write_checkpoint(5, iteration=4, result_dir=tmp_path)
    calls: list[int] = []

    def step(state: int, i: int) -> int:
        calls.append(i)
        return state + 1

    final = ck.run_iterations(step, init=0, n=10, result_dir=tmp_path, checkpoint_every=1)
    assert final == 10
    assert calls == [5, 6, 7, 8, 9]  # 0..4 skipped


def test_run_iterations_init_callable_lazy_on_resume(tmp_path: Path) -> None:
    ck.write_checkpoint(3, iteration=0, result_dir=tmp_path)  # resume → state 3, iter 1
    called: list[int] = []

    def init() -> int:
        called.append(1)
        return 0

    final = ck.run_iterations(
        lambda s, i: s + 1, init=init, n=2, result_dir=tmp_path, checkpoint_every=1
    )
    assert called == []  # init NOT called on resume
    assert final == 4


def test_run_iterations_checkpoints_final_state_without_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default strategy=interval (30 min) never fires in a fast loop, but the
    # final state must still be checkpointed so a resume redoes nothing.
    monkeypatch.delenv("HPC_WALLTIME_END_EPOCH", raising=False)
    final = ck.run_iterations(lambda s, i: s + 1, init=0, n=5, result_dir=tmp_path)
    assert final == 5
    assert ck.latest_checkpoint(result_dir=tmp_path).name == "checkpoint-4.pkl"
    assert ck.read_checkpoint(ck.latest_checkpoint(result_dir=tmp_path)) == 5


def test_run_iterations_zero_iterations_is_noop(tmp_path: Path) -> None:
    assert ck.run_iterations(lambda s, i: s + 1, init=42, n=0, result_dir=tmp_path) == 42
    assert ck.latest_checkpoint(result_dir=tmp_path) is None


def test_public_reexport_from_experiment_kit() -> None:
    from hpc_agent import experiment_kit as ek

    for name in (
        "write_checkpoint",
        "read_checkpoint",
        "read_latest_checkpoint",
        "latest_checkpoint",
        "checkpoint_dir",
        "should_checkpoint",
        "run_iterations",
    ):
        assert hasattr(ek, name), f"{name} not re-exported from experiment_kit"
