"""``wait-any-detached`` — the fleet waiter (one seat for N runs, run-queue §7).

Mirrors ``test_wait_detached.py``'s determinism: the lease dir is a tmp path
(monkeypatched homedir) and pid liveness is a scripted stub — no real
processes, no sleeps beyond tiny intervals.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from hpc_agent._kernel.lifecycle import detached as detached_mod
from hpc_agent._wire.queries.wait_any_detached import DetachedTarget, WaitAnyDetachedInput
from hpc_agent.ops.monitor.wait_any_detached import wait_any_detached

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def homedir(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    (tmp_path / "_detached").mkdir()
    return tmp_path


def _write_lease(
    homedir: Path,
    *,
    block: str,
    run_id: str,
    pid: int,
    experiment_dir: Path | None = None,
) -> None:
    lease = {
        "run_id": run_id,
        "block": block,
        "pid": pid,
        "log_path": str(homedir / "_detached" / f"{block}-{run_id}.log"),
        "argv": ["x"],
    }
    if experiment_dir is not None:
        lease["experiment_dir"] = str(experiment_dir)
    (homedir / "_detached" / f"{block}-{run_id}.lease.json").write_text(
        json.dumps(lease), encoding="utf-8"
    )


def _spec(*targets: tuple[str, str | None], **kwargs) -> WaitAnyDetachedInput:
    return WaitAnyDetachedInput(
        targets=[DetachedTarget(run_id=run_id, block=block) for run_id, block in targets],
        **kwargs,
    )


def _row(out, run_id: str):
    (row,) = [r for r in out.targets if r.run_id == run_id]
    return row


# ── spec validation: the watch set is required and must be sane ──────────────


def test_empty_targets_is_refused_at_spec_validation() -> None:
    """An empty watch set has no wait to perform — refused at the wire boundary,
    never a full-budget no-op."""
    with pytest.raises(ValidationError):
        WaitAnyDetachedInput(targets=[])


def test_targets_is_required() -> None:
    with pytest.raises(ValidationError):
        WaitAnyDetachedInput()


def test_duplicate_target_is_refused() -> None:
    """One identity twice would poll one lease twice and report one worker under
    two rows — a caller bug, refused rather than tolerated."""
    with pytest.raises(ValidationError):
        _spec(("run-abc", "submit-s2"), ("run-abc", "submit-s2"))


def test_same_run_different_blocks_is_allowed() -> None:
    spec = _spec(("run-abc", "submit-s2"), ("run-abc", "submit-s3"))
    assert len(spec.targets) == 2


# ── the wait ────────────────────────────────────────────────────────────────


def test_any_exit_short_circuits_the_whole_set(homedir, monkeypatch) -> None:
    """Three live workers; the second dies → return at once, naming only it."""
    _write_lease(homedir, block="submit-s2", run_id="run-a", pid=11)
    _write_lease(homedir, block="submit-s2", run_id="run-b", pid=22)
    _write_lease(homedir, block="submit-s2", run_id="run-c", pid=33)
    calls: list[int] = []

    def fake_alive(pid: int) -> bool:
        calls.append(pid)
        # Every pid alive on the first-poll resolution + one full tick, then
        # pid 22 dies.
        return not (pid == 22 and calls.count(22) > 1)

    monkeypatch.setattr(detached_mod, "pid_alive", fake_alive)
    out = wait_any_detached(
        spec=_spec(
            ("run-a", None),
            ("run-b", None),
            ("run-c", None),
            timeout_sec=30,
            poll_interval_sec=0.01,
        )
    )
    assert out.outcome == "worker_exited"
    assert out.triggered_count == 1
    assert _row(out, "run-b").state == "worker_exited"
    assert _row(out, "run-b").triggered is True
    assert _row(out, "run-b").pid == 22
    assert _row(out, "run-b").log_path.endswith("submit-s2-run-b.log")
    assert [r.state for r in out.targets] == ["still_running", "worker_exited", "still_running"]
    assert out.waited_sec < 5  # short-circuited, did not sit out the budget


def test_simultaneous_exits_both_trigger(homedir, monkeypatch) -> None:
    """Two pids dying between the same pair of polls are BOTH triggering rows —
    which is why `triggered` is per-row, not one top-level pointer."""
    _write_lease(homedir, block="submit-s2", run_id="run-a", pid=11)
    _write_lease(homedir, block="submit-s2", run_id="run-b", pid=22)
    seen: list[int] = []

    def fake_alive(pid: int) -> bool:
        seen.append(pid)
        return len(seen) <= 2  # alive for the first-poll resolution only

    monkeypatch.setattr(detached_mod, "pid_alive", fake_alive)
    out = wait_any_detached(
        spec=_spec(("run-a", None), ("run-b", None), timeout_sec=30, poll_interval_sec=0.01)
    )
    assert out.outcome == "worker_exited"
    assert out.triggered_count == 2
    assert all(r.state == "worker_exited" and r.triggered for r in out.targets)


def test_all_running_times_out_with_full_snapshot(homedir, monkeypatch) -> None:
    _write_lease(homedir, block="submit-s2", run_id="run-a", pid=11)
    _write_lease(homedir, block="submit-s3", run_id="run-b", pid=22)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: True)
    out = wait_any_detached(
        spec=_spec(("run-a", None), ("run-b", None), timeout_sec=0.05, poll_interval_sec=0.01)
    )
    assert out.outcome == "timeout"
    assert out.triggered_count == 0
    assert [r.state for r in out.targets] == ["still_running", "still_running"]
    assert [r.pid for r in out.targets] == [11, 22]
    assert [r.block for r in out.targets] == ["submit-s2", "submit-s3"]
    # No terminal read for a live worker — the payload stays null (wait-detached's
    # timeout behaviour, per row).
    assert all(r.brief is None and r.relay is None and r.next_verb is None for r in out.targets)
    assert out.waited_sec >= 0.05


def test_mixed_dead_and_running_returns_at_once(homedir, monkeypatch) -> None:
    """One target already gone at the first poll → return immediately with the
    full snapshot; the live sibling reports still_running, not a lost wait."""
    _write_lease(homedir, block="submit-s2", run_id="run-dead", pid=11)
    _write_lease(homedir, block="submit-s2", run_id="run-live", pid=22)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: pid == 22)
    out = wait_any_detached(
        spec=_spec(("run-dead", None), ("run-live", None), timeout_sec=30, poll_interval_sec=0.01)
    )
    assert out.outcome == "no_live_worker"
    assert out.triggered_count == 1
    assert out.waited_sec == 0.0
    assert _row(out, "run-dead").state == "no_live_worker"
    assert _row(out, "run-dead").triggered is True
    assert _row(out, "run-dead").block == "submit-s2"  # recovered from the dead lease
    assert _row(out, "run-live").state == "still_running"
    assert _row(out, "run-live").triggered is False
    assert _row(out, "run-live").pid == 22


def test_all_dead_is_the_degenerate_no_live_worker(homedir, monkeypatch) -> None:
    _write_lease(homedir, block="submit-s2", run_id="run-a", pid=11)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: False)
    out = wait_any_detached(spec=_spec(("run-a", None), ("run-never-launched", None)))
    assert out.outcome == "no_live_worker"
    assert out.triggered_count == 2
    assert [r.state for r in out.targets] == ["no_live_worker", "no_live_worker"]
    # A target that never had a lease at all still reports, with nulls.
    assert _row(out, "run-never-launched").pid is None
    assert _row(out, "run-never-launched").block is None


def test_no_leases_at_all_returns_immediately(homedir) -> None:
    out = wait_any_detached(spec=_spec(("run-abc", None)))
    assert out.outcome == "no_live_worker"
    assert out.waited_sec == 0.0


def test_block_filter_selects_the_named_worker(homedir, monkeypatch) -> None:
    """Identity handling is wait-detached's: the block filter picks the lease."""
    _write_lease(homedir, block="submit-s2", run_id="run-abc", pid=1)
    _write_lease(homedir, block="submit-s3", run_id="run-abc", pid=2)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: pid == 2)
    out = wait_any_detached(
        spec=_spec(("run-abc", "submit-s3"), timeout_sec=0.05, poll_interval_sec=0.01)
    )
    assert out.outcome == "timeout"  # pid 2 stays alive; the filter found it
    assert _row(out, "run-abc").block == "submit-s3"
    assert _row(out, "run-abc").pid == 2


def test_identity_mismatch_reads_as_no_live_worker(homedir, monkeypatch) -> None:
    """A target naming a block with no lease of its own is not a crash and does
    not silently match a SIBLING block's lease — it reads as no_live_worker."""
    _write_lease(homedir, block="submit-s2", run_id="run-abc", pid=1)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: True)
    out = wait_any_detached(spec=_spec(("run-abc", "submit-s4")))
    assert out.outcome == "no_live_worker"
    assert _row(out, "run-abc").state == "no_live_worker"
    assert _row(out, "run-abc").pid is None


def test_corrupt_lease_is_skipped_not_fatal(homedir, monkeypatch) -> None:
    (homedir / "_detached" / "submit-s2-run-abc.lease.json").write_text(
        "{not json", encoding="utf-8"
    )
    _write_lease(homedir, block="submit-s2", run_id="run-live", pid=22)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: True)
    out = wait_any_detached(spec=_spec(("run-abc", None), ("run-live", None)))
    assert out.outcome == "no_live_worker"
    assert _row(out, "run-abc").state == "no_live_worker"


# ── the wake payload: the triggering row carries the exited worker's terminal ──


def _record_worker_terminal(experiment_dir: Path, *, run_id: str, block: str) -> None:
    from hpc_agent.state.block_terminal import record_terminal

    record_terminal(
        experiment_dir,
        run_id=run_id,
        block=block,
        cmd_sha="",
        result_dump={
            "block": "s2",
            "stage_reached": "canary_verified",
            "needs_decision": True,
            "relay": "canary green, est. 4 core-hours",
            "run_id": run_id,
            "brief": {"run_id": run_id, "verified": True, "est_core_hours": 4},
            "next_block": {"verb": "submit-s3", "why": "launch main", "spec_hint": {}},
        },
    )


def test_triggering_row_hands_back_brief_relay_next_verb(homedir, tmp_path, monkeypatch) -> None:
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    _record_worker_terminal(experiment_dir, run_id="run-a", block="submit-s2")
    _write_lease(homedir, block="submit-s2", run_id="run-a", pid=11, experiment_dir=experiment_dir)
    _write_lease(homedir, block="submit-s2", run_id="run-b", pid=22, experiment_dir=experiment_dir)
    seen: list[int] = []

    def fake_alive(pid: int) -> bool:
        seen.append(pid)
        return not (pid == 11 and seen.count(11) > 1)

    monkeypatch.setattr(detached_mod, "pid_alive", fake_alive)
    out = wait_any_detached(
        spec=_spec(("run-a", None), ("run-b", None), timeout_sec=30, poll_interval_sec=0.01)
    )
    assert out.outcome == "worker_exited"
    triggered = _row(out, "run-a")
    assert triggered.relay == "canary green, est. 4 core-hours"
    assert triggered.next_verb == "submit-s3"
    assert triggered.brief is not None and triggered.brief["verified"] is True
    # The still-running sibling carries no payload.
    assert _row(out, "run-b").brief is None


def test_dead_at_first_poll_still_reads_the_exited_workers_terminal(
    homedir, tmp_path, monkeypatch
) -> None:
    """An agent that arrives late is not stranded: the already-exited target's
    recorded terminal is still the wake payload."""
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    _record_worker_terminal(experiment_dir, run_id="run-a", block="submit-s2")
    _write_lease(homedir, block="submit-s2", run_id="run-a", pid=11, experiment_dir=experiment_dir)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: False)
    out = wait_any_detached(spec=_spec(("run-a", None)))
    assert out.outcome == "no_live_worker"
    row = _row(out, "run-a")
    assert row.next_verb == "submit-s3"
    assert row.brief is not None and row.brief["est_core_hours"] == 4


# ── the invariant that makes concurrent waiters safe: it is a PURE READ ───────


def _inventory(root: Path) -> dict[str, tuple[int, float]]:
    """Every file under *root* → (size, mtime_ns) — the read-only fingerprint."""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize("outcome", ["worker_exited", "no_live_worker", "timeout"])
def test_call_writes_nothing_anywhere(homedir, tmp_path, monkeypatch, outcome) -> None:
    """PURE READ pin: no file in the experiment dir OR the lease home is created,
    removed, or modified by a call — on ANY outcome. This read-only-ness is what
    makes N concurrent waiters safe, so it is a contract, not an artifact."""
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    _record_worker_terminal(experiment_dir, run_id="run-a", block="submit-s2")
    _write_lease(homedir, block="submit-s2", run_id="run-a", pid=11, experiment_dir=experiment_dir)
    if outcome == "worker_exited":
        seen: list[int] = []

        def probe(pid: int) -> bool:
            seen.append(pid)
            return len(seen) <= 1

        monkeypatch.setattr(detached_mod, "pid_alive", probe)
    else:
        monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: outcome == "timeout")

    before_exp = _inventory(experiment_dir)
    before_home = _inventory(homedir)
    out = wait_any_detached(spec=_spec(("run-a", None), timeout_sec=0.05, poll_interval_sec=0.01))
    assert out.outcome == outcome
    assert _inventory(experiment_dir) == before_exp
    assert _inventory(homedir) == before_home
