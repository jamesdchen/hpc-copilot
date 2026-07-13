"""Tests for doctor's dead detached-worker scan (T3).

The §5 stalled-driver scan only walks IN-FLIGHT runs, so a detached submit
block (S2/S3/S4/speculate) that dies mid-flight on a run whose journal is
ALREADY terminal is invisible to it — most sharply the S4 harvest, which runs
AFTER the run is terminal. doctor's `scan_dead_detached_workers` closes that
blind spot: a lease with a DEAD pid and NO recorded block-terminal is surfaced
as a drafted re-invoke proposal (detection only — doctor never re-runs).

Cluster-free: leases are fabricated on disk and `_pid_alive` is monkeypatched
so liveness never depends on a real pid or wall-clock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hpc_agent.ops.recover.doctor as doctor_mod
from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent.ops.recover.doctor import doctor
from hpc_agent.state.block_terminal import record_terminal
from hpc_agent.state.run_record import _current_homedir

_NOW = "2026-07-06T01:00:00+00:00"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _write_lease(*, block: str, run_id: str, pid: int, experiment_dir: Path) -> Path:
    """Fabricate a `<block>-<run_id>.lease.json` under the journal home's
    `_detached/` dir, mirroring `_spawn_detached`'s stamped shape — including
    the `--experiment-dir` flag `launch_submit_block_detached` always puts in
    the child argv (the scan reads it back to scope the GLOBAL lease dir to
    the current experiment)."""
    detached_dir = _current_homedir() / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    lease_path = detached_dir / f"{block}-{run_id}.lease.json"
    lease_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "block": block,
                "pid": pid,
                "log_path": str(detached_dir / f"{block}-{run_id}.log"),
                "argv": [
                    "python",
                    "-m",
                    "hpc_agent",
                    block,
                    "--spec",
                    str(detached_dir / f"{block}-{run_id}.spec.json"),
                    "--experiment-dir",
                    str(experiment_dir),
                ],
            }
        ),
        encoding="utf-8",
    )
    return lease_path


def _dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pid reads DEAD — deterministic, no real process involved."""
    monkeypatch.setattr(doctor_mod, "_pid_alive", lambda _pid: False)


def _alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_mod, "_pid_alive", lambda _pid: True)


def test_dead_worker_without_terminal_is_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead S4 harvest worker with NO recorded terminal → alert + attention."""
    _dead(monkeypatch)
    _write_lease(
        block="submit-s4", run_id="pi-train-abc123", pid=999_999_999, experiment_dir=tmp_path
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is True
    assert "1 dead detached worker(s) with no harvest" in out["attention_summary"]
    # The drafted proposal rides the envelope's alerts list.
    messages = [a["message"] for a in out["alerts"]]
    assert len(messages) == 1
    proposal = messages[0]
    assert "submit-s4" in proposal
    assert "pi-train-abc123" in proposal
    assert "idempotent" in proposal.lower()
    assert "re-invoke" in proposal.lower()
    assert out["alerts"][0]["ts"] == _NOW


def test_dead_worker_with_recorded_terminal_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead pid WITH a recorded block-terminal = normal completion → not surfaced."""
    _dead(monkeypatch)
    _write_lease(
        block="submit-s4", run_id="pi-train-done", pid=999_999_999, experiment_dir=tmp_path
    )
    record_terminal(
        tmp_path,
        run_id="pi-train-done",
        block="submit-s4",
        cmd_sha="sha-done",
        result_dump={"run_id": "pi-train-done", "block": "submit-s4"},
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is False
    assert out["alerts"] == []
    assert "dead detached worker" not in out["attention_summary"]


def test_finished_submit_worker_uses_the_writer_key_and_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-07-07 key-mismatch fix, end to end: the submit recorder keys the
    terminal by ``terminal_block_key(result.block)`` (the canonical VERB), which is
    the SAME string the lease stamps and this scan reads off it. A FINISHED submit
    worker is therefore recognized (dead pid WITH terminal) and NOT mis-drafted as
    a dead-no-terminal re-invoke."""
    from hpc_agent.state.block_terminal import terminal_block_key

    _dead(monkeypatch)
    _write_lease(block="submit-s4", run_id="pi-train-fin", pid=999_999_999, experiment_dir=tmp_path)
    # Record under the EXACT key the submit writer now uses.
    record_terminal(
        tmp_path,
        run_id="pi-train-fin",
        block=terminal_block_key("s4"),  # -> "submit-s4"
        cmd_sha="sha",
        result_dump={"run_id": "pi-train-fin", "block": "s4"},
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is False
    assert out["alerts"] == []


def test_dead_worker_with_legacy_short_key_terminal_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deprecation-window fallback: a run whose terminal was recorded PRE-FIX under
    the short "s2" key (lease still verb-keyed) is still recognized as finished —
    the scan reads the verb key then falls back to the short key, so no spurious
    re-invoke fires for a mid-flight run that predates the fix."""
    _dead(monkeypatch)
    _write_lease(
        block="submit-s2", run_id="pi-mid-flight", pid=999_999_999, experiment_dir=tmp_path
    )
    record_terminal(
        tmp_path,
        run_id="pi-mid-flight",
        block="s2",  # the pre-fix SHORT key
        cmd_sha="sha",
        result_dump={"run_id": "pi-mid-flight", "block": "s2"},
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is False
    assert out["alerts"] == []


def test_live_worker_is_not_surfaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lease still naming a LIVE pid is a running worker — never flagged,
    even with no terminal yet."""
    _alive(monkeypatch)
    _write_lease(block="submit-s3", run_id="pi-train-running", pid=4242, experiment_dir=tmp_path)

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is False
    assert out["alerts"] == []


def test_mixed_leases_surface_only_the_dead_no_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two dead leases, one with a terminal (skipped) and one without (surfaced)."""
    _dead(monkeypatch)
    _write_lease(block="submit-s4", run_id="run-finished", pid=999_999_998, experiment_dir=tmp_path)
    record_terminal(
        tmp_path,
        run_id="run-finished",
        block="submit-s4",
        cmd_sha="sha",
        result_dump={"ok": True},
    )
    _write_lease(block="submit-s2", run_id="run-crashed", pid=999_999_999, experiment_dir=tmp_path)

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is True
    assert "1 dead detached worker(s) with no harvest" in out["attention_summary"]
    messages = [a["message"] for a in out["alerts"]]
    assert len(messages) == 1
    assert "run-crashed" in messages[0]
    assert "submit-s2" in messages[0]
    assert all("run-finished" not in m for m in messages)


def test_no_detached_dir_is_all_clear(tmp_path: Path) -> None:
    """No `_detached/` dir at all → fail-open, nothing surfaced."""
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))
    assert out["needs_attention"] is False
    assert out["alerts"] == []
    assert "all clear" in out["attention_summary"]


def test_malformed_lease_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A torn / pid-less lease never crashes the scan (fail-open)."""
    _dead(monkeypatch)
    detached_dir = _current_homedir() / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    (detached_dir / "submit-s4-garbage.lease.json").write_text("{not valid json", encoding="utf-8")
    (detached_dir / "submit-s4-nopid.lease.json").write_text(
        json.dumps({"run_id": "r", "block": "submit-s4"}), encoding="utf-8"
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))

    # A pid-less lease can't be probed → skipped; a live pid default would also
    # skip. Neither raises.
    assert out["needs_attention"] is False
    assert out["alerts"] == []


# ── experiment scoping: the lease dir is GLOBAL, the terminal store is not ─────


def test_foreign_experiment_finished_worker_is_not_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NORMALLY-FINISHED worker from ANOTHER experiment must draft nothing.

    The `_detached/` lease dir is global (one journal home serves every
    experiment) while the block-terminal store is per-experiment, so the other
    project's terminal is invisible from HERE: unscoped, this dead-pid lease
    read as dead-with-no-terminal and permanently flipped needs_attention in
    every other project's doctor run. The scan scopes each lease by the
    `--experiment-dir` its argv carries, so the foreign worker is skipped."""
    _dead(monkeypatch)
    other_exp = tmp_path / "other-project"
    other_exp.mkdir()
    _write_lease(block="submit-s4", run_id="foreign-run", pid=999_999_999, experiment_dir=other_exp)
    # Its terminal IS recorded — under ITS OWN experiment dir, not ours.
    record_terminal(
        other_exp,
        run_id="foreign-run",
        block="submit-s4",
        cmd_sha="sha",
        result_dump={"run_id": "foreign-run", "block": "submit-s4"},
    )

    our_exp = tmp_path / "this-project"
    our_exp.mkdir()
    out = doctor(experiment_dir=our_exp, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is False
    assert out["alerts"] == []


def test_foreign_experiment_dead_worker_is_not_surfaced_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a genuinely-dead foreign worker (no terminal ANYWHERE) belongs to
    the other experiment's doctor run, not this one's — and it IS surfaced
    there, so scoping loses no coverage."""
    _dead(monkeypatch)
    other_exp = tmp_path / "other-project"
    other_exp.mkdir()
    _write_lease(
        block="submit-s2", run_id="foreign-crashed", pid=999_999_999, experiment_dir=other_exp
    )

    our_exp = tmp_path / "this-project"
    our_exp.mkdir()
    out = doctor(experiment_dir=our_exp, spec=DoctorSpec(now=_NOW))
    assert out["needs_attention"] is False
    assert out["alerts"] == []

    # The owning experiment's own doctor run still drafts the proposal.
    out_theirs = doctor(experiment_dir=other_exp, spec=DoctorSpec(now=_NOW))
    assert out_theirs["needs_attention"] is True
    assert any("foreign-crashed" in a["message"] for a in out_theirs["alerts"])


def test_own_dead_worker_still_surfaced_alongside_foreign_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoping filters foreign leases only: our own dead-no-terminal worker
    still drafts its proposal in the same scan."""
    _dead(monkeypatch)
    our_exp = tmp_path / "this-project"
    our_exp.mkdir()
    other_exp = tmp_path / "other-project"
    other_exp.mkdir()
    _write_lease(block="submit-s4", run_id="our-crashed", pid=999_999_999, experiment_dir=our_exp)
    _write_lease(block="submit-s4", run_id="foreign-run", pid=999_999_998, experiment_dir=other_exp)

    out = doctor(experiment_dir=our_exp, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is True
    messages = [a["message"] for a in out["alerts"]]
    assert len(messages) == 1
    assert "our-crashed" in messages[0]


def test_lease_without_experiment_dir_flag_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease whose argv names no `--experiment-dir` (torn / hand-written —
    the one writer always stamps it) cannot be scoped to ANY experiment, so it
    is skipped rather than risking a false NEEDS-ATTENTION."""
    _dead(monkeypatch)
    detached_dir = _current_homedir() / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    (detached_dir / "submit-s4-unscoped.lease.json").write_text(
        json.dumps(
            {
                "run_id": "unscoped-run",
                "block": "submit-s4",
                "pid": 999_999_999,
                "log_path": str(detached_dir / "submit-s4-unscoped.log"),
                "argv": ["python", "-m", "hpc_agent", "submit-s4"],
            }
        ),
        encoding="utf-8",
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=_NOW))

    assert out["needs_attention"] is False
    assert out["alerts"] == []
