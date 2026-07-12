"""``poll-detached`` — the instant, non-blocking snapshot of a detached worker.

Deterministic: the lease dir is a tmp path (``HPC_JOURNAL_DIR`` env), journal +
terminal records are seeded on disk, and pid liveness is a scripted stub — no
real processes, no sleeps, no cluster contact. The four derived states are each
exercised over synthetic lease/journal/terminal fixtures, and a dedicated test
proves the snapshot never imports an SSH transport module at runtime.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.queries.poll_detached import PollDetachedInput
from hpc_agent.ops.monitor import poll_detached as poll_detached_mod
from hpc_agent.ops.monitor.poll_detached import poll_detached
from hpc_agent.state.block_terminal import record_terminal
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "run-abc"
_BLOCK = "campaign-run"


@pytest.fixture
def homedir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Journal home whose ``_detached/`` holds the lease files."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    (home / "_detached").mkdir()
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    """The experiment tree the journal + block-terminal records live under."""
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _write_lease(homedir: Path, *, block: str, run_id: str, pid: int) -> None:
    lease = {
        "run_id": run_id,
        "block": block,
        "pid": pid,
        "log_path": str(homedir / "_detached" / f"{block}-{run_id}.log"),
        "argv": ["x"],
    }
    (homedir / "_detached" / f"{block}-{run_id}.lease.json").write_text(
        json.dumps(lease), encoding="utf-8"
    )


def _seed_journal(experiment: Path, *, run_id: str, status: str = "in_flight") -> None:
    rec = RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="user@host",
        remote_path="/remote",
        job_name="p",
        job_ids=["9001"],
        total_tasks=1,
        submitted_at="2026-05-21T12:00:00+00:00",
        experiment_dir=str(experiment),
        status=status,
    )
    upsert_run(experiment, rec)


def _seed_terminal(experiment: Path, *, run_id: str, block: str) -> None:
    record_terminal(
        experiment,
        run_id=run_id,
        block=block,
        cmd_sha="deadbeef",
        result_dump={"block": block, "needs_decision": False},
    )


def _alive(_pid: int) -> bool:
    return True


def _dead(_pid: int) -> bool:
    return False


# ── the four derived states ──────────────────────────────────────────────────


def test_no_lease_state(homedir: Path, experiment: Path) -> None:
    """No lease file → no_lease, with the pid/liveness signals all empty."""
    out = poll_detached(
        experiment_dir=experiment, spec=PollDetachedInput(run_id=_RUN_ID, block=_BLOCK)
    )
    assert out.state == "no_lease"
    assert out.lease_present is False
    assert out.pid is None
    assert out.pid_alive is False
    assert out.terminal_recorded is False
    assert out.journal_status is None
    assert out.watch == "journal"
    assert out.run_id == _RUN_ID
    assert out.block == _BLOCK


def test_running_state(homedir: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lease present + pid alive → running (regardless of terminal/journal)."""
    _write_lease(homedir, block=_BLOCK, run_id=_RUN_ID, pid=4242)
    monkeypatch.setattr(poll_detached_mod, "pid_alive", _alive)
    out = poll_detached(
        experiment_dir=experiment, spec=PollDetachedInput(run_id=_RUN_ID, block=_BLOCK)
    )
    assert out.state == "running"
    assert out.lease_present is True
    assert out.pid == 4242
    assert out.pid_alive is True


def test_exited_recorded_state(
    homedir: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lease present, pid dead, terminal on disk → exited_recorded."""
    _write_lease(homedir, block=_BLOCK, run_id=_RUN_ID, pid=4242)
    _seed_terminal(experiment, run_id=_RUN_ID, block=_BLOCK)
    monkeypatch.setattr(poll_detached_mod, "pid_alive", _dead)
    out = poll_detached(
        experiment_dir=experiment, spec=PollDetachedInput(run_id=_RUN_ID, block=_BLOCK)
    )
    assert out.state == "exited_recorded"
    assert out.lease_present is True
    assert out.pid == 4242
    assert out.pid_alive is False
    assert out.terminal_recorded is True


def test_exited_unrecorded_state(
    homedir: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lease present, pid dead, NO terminal → exited_unrecorded (the run-#12 gap)."""
    _write_lease(homedir, block=_BLOCK, run_id=_RUN_ID, pid=4242)
    monkeypatch.setattr(poll_detached_mod, "pid_alive", _dead)
    out = poll_detached(
        experiment_dir=experiment, spec=PollDetachedInput(run_id=_RUN_ID, block=_BLOCK)
    )
    assert out.state == "exited_unrecorded"
    assert out.lease_present is True
    assert out.pid_alive is False
    assert out.terminal_recorded is False


# ── signal reporting edges ───────────────────────────────────────────────────


def test_journal_status_is_reported(homedir: Path, experiment: Path) -> None:
    """The journal status is surfaced verbatim (orthogonal to the lease state)."""
    _seed_journal(experiment, run_id=_RUN_ID, status="complete")
    out = poll_detached(
        experiment_dir=experiment, spec=PollDetachedInput(run_id=_RUN_ID, block=_BLOCK)
    )
    assert out.journal_status == "complete"
    # No lease was written, so the derived state is still no_lease — journal
    # status and lease state are independent signals.
    assert out.state == "no_lease"


def test_terminal_only_matched_for_its_block(
    homedir: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal recorded for a DIFFERENT block does not count as recorded here."""
    _write_lease(homedir, block=_BLOCK, run_id=_RUN_ID, pid=7)
    _seed_terminal(experiment, run_id=_RUN_ID, block="submit-s2")
    monkeypatch.setattr(poll_detached_mod, "pid_alive", _dead)
    out = poll_detached(
        experiment_dir=experiment, spec=PollDetachedInput(run_id=_RUN_ID, block=_BLOCK)
    )
    assert out.terminal_recorded is False
    assert out.state == "exited_unrecorded"


def test_corrupt_lease_is_present_not_no_lease(
    homedir: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt lease file is still lease_present (a worker WAS launched)."""
    (homedir / "_detached" / f"{_BLOCK}-{_RUN_ID}.lease.json").write_text(
        "{not json", encoding="utf-8"
    )
    # No monkeypatch needed: pid is None, so pid_alive is never called.
    out = poll_detached(
        experiment_dir=experiment, spec=PollDetachedInput(run_id=_RUN_ID, block=_BLOCK)
    )
    assert out.lease_present is True
    assert out.pid is None
    assert out.pid_alive is False
    assert out.state == "exited_unrecorded"


# ── the zero-SSH guarantee ───────────────────────────────────────────────────

# Modules that, if imported on the snapshot's runtime path, would mean it dialed
# (or could dial) the cluster. Poisoning them to None makes any ``import`` of
# them raise ImportError (the idiom used in tests/infra/test_ssh_engine.py), so a
# clean return proves the read never touched a transport module.
_TRANSPORT_MODULES = (
    "hpc_agent.infra.remote",
    "hpc_agent.infra.transport",
    "hpc_agent.infra.ssh_engine",
    "hpc_agent.infra.ssh_slots",
    "hpc_agent.infra.ssh_circuit",
    "hpc_agent.infra.ssh_throttle",
    "asyncssh",
    "paramiko",
)


def test_never_opens_ssh(homedir: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot completes with every SSH transport module import-poisoned."""
    _write_lease(homedir, block=_BLOCK, run_id=_RUN_ID, pid=4242)
    _seed_journal(experiment, run_id=_RUN_ID, status="in_flight")
    _seed_terminal(experiment, run_id=_RUN_ID, block=_BLOCK)
    monkeypatch.setattr(poll_detached_mod, "pid_alive", _dead)
    for name in _TRANSPORT_MODULES:
        monkeypatch.setitem(sys.modules, name, None)

    out = poll_detached(
        experiment_dir=experiment, spec=PollDetachedInput(run_id=_RUN_ID, block=_BLOCK)
    )
    # If any read had reached for a transport module the import would have raised
    # ImportError before this point; a correct terminal-state answer confirms the
    # full fusion path ran without one.
    assert out.state == "exited_recorded"
    assert out.journal_status == "in_flight"
