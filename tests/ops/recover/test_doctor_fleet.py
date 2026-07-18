"""Fleet-wide stalled-driver scan for the ``doctor`` watchdog (§5, cross-repo).

The §5 stalled scan is per-``repo_hash`` namespaced, so a driver stalled under
one experiment repo is invisible to a ``doctor`` scoped to a sibling repo — and
the OS-scheduled watchdog hard-codes its ``--experiment-dir`` at install, so the
one unattended detector covered only that dir. ``spec.fleet`` unions the scan
across every journaled experiment; the durable spec ``doctor-install`` writes
now bakes ``fleet=true`` so the unattended tick covers the whole fleet.

Detection only, still no SSH — the union just composes ``find_stalled_runs``
per discovered namespace (``discover_fleet_experiments``, the ONE non-creating
``*/repo.json`` glob).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent.ops.recover.doctor import doctor, find_stalled_runs_fleet
from hpc_agent.ops.recover.doctor_install import _write_durable_spec
from hpc_agent.state.journal import stamp_tick, upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A SHARED journal home for every experiment on this "machine" — the fleet
    # glob discovers each experiment's namespace under it.
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, experiment_dir: Path, *, status: str = "in_flight") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir=str(experiment_dir),
        status=status,
    )


def _stall(experiment_dir: Path, run_id: str) -> None:
    """Journal an overdue (stalled) run under *experiment_dir*'s namespace."""
    upsert_run(experiment_dir, _record(run_id, experiment_dir))
    stamp_tick(
        run_id,
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",  # lapsed
        experiment_dir=experiment_dir,
    )


def _healthy(experiment_dir: Path, run_id: str) -> None:
    upsert_run(experiment_dir, _record(run_id, experiment_dir))
    stamp_tick(
        run_id,
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",  # in the future
        experiment_dir=experiment_dir,
    )


def _two_experiments(tmp_path: Path) -> tuple[Path, Path]:
    """Two real, distinct experiment dirs sharing the one journal home."""
    exp_a = tmp_path / "expA"
    exp_b = tmp_path / "expB"
    exp_a.mkdir()
    exp_b.mkdir()
    return exp_a, exp_b


# ── two-namespace regression: the cross-repo blind spot ───────────────────────


def test_fleet_false_does_not_surface_a_sibling_repo_stall(tmp_path: Path) -> None:
    """The incident, pinned: a stall journaled under experiment A is INVISIBLE to
    a doctor scoped to experiment B when fleet is off — today's per-repo behavior."""
    exp_a, exp_b = _two_experiments(tmp_path)
    _stall(exp_a, "stalled-in-A")

    out = doctor(experiment_dir=exp_b, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))

    assert out["stalled_count"] == 0
    assert out["stalled"] == []
    assert out["needs_attention"] is False


def test_fleet_true_surfaces_a_sibling_repo_stall_with_experiment_dir(tmp_path: Path) -> None:
    """The fix: fleet=true unions the scan so A's stall surfaces even though the
    doctor was invoked for B, and the proposal + evidence name WHERE (exp A)."""
    exp_a, exp_b = _two_experiments(tmp_path)
    _stall(exp_a, "stalled-in-A")

    out = doctor(experiment_dir=exp_b, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00", fleet=True))

    assert out["stalled_count"] == 1
    hit = out["stalled"][0]
    assert hit["run_id"] == "stalled-in-A"
    # Stamped WHERE: the sibling experiment_dir rides both the human-facing
    # proposal line and the machine-readable evidence.
    assert str(exp_a) in hit["proposal"]
    assert hit["evidence"]["experiment_dir"] == str(exp_a)
    assert out["needs_attention"] is True
    assert out["attention_summary"].startswith("NEEDS ATTENTION")


def test_fleet_union_covers_stalls_across_multiple_namespaces(tmp_path: Path) -> None:
    """A stall in EACH of two repos both surface under one fleet scan, each
    stamped with its own experiment_dir."""
    exp_a, exp_b = _two_experiments(tmp_path)
    _stall(exp_a, "stalled-in-A")
    _stall(exp_b, "stalled-in-B")

    out = doctor(experiment_dir=exp_a, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00", fleet=True))

    assert out["stalled_count"] == 2
    by_run = {h["run_id"]: h for h in out["stalled"]}
    assert by_run["stalled-in-A"]["evidence"]["experiment_dir"] == str(exp_a)
    assert by_run["stalled-in-B"]["evidence"]["experiment_dir"] == str(exp_b)


# ── healthy fleet: no false alarm ─────────────────────────────────────────────


def test_fleet_true_all_healthy_surfaces_nothing(tmp_path: Path) -> None:
    exp_a, exp_b = _two_experiments(tmp_path)
    _healthy(exp_a, "healthy-A")
    _healthy(exp_b, "healthy-B")

    out = doctor(experiment_dir=exp_b, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00", fleet=True))

    assert out["stalled_count"] == 0
    assert out["stalled"] == []
    assert out["needs_attention"] is False
    assert "all clear" in out["attention_summary"]


# ── non-creating on an empty journal home ─────────────────────────────────────


def test_fleet_union_is_non_creating_on_empty_journal_home(tmp_path: Path) -> None:
    """The fleet union scaffolds nothing: on a journal home that does not exist
    yet it returns [] and leaves no directory behind (attention-queue
    watermark-neutrality — a fleet read persists no projection)."""
    home = tmp_path / "journal"
    assert not home.exists()

    result = find_stalled_runs_fleet("2026-07-03T01:00:00+00:00")

    assert result == []
    assert not home.exists()  # the read created no namespace


def test_fleet_union_rejects_malformed_now(tmp_path: Path) -> None:
    """Signature parity with find_stalled_runs: a non-ISO now fails loud."""
    with pytest.raises(ValueError):
        find_stalled_runs_fleet("not-a-timestamp")


# ── scheduled-spec coverage: the unattended watchdog covers the fleet ─────────


def test_durable_doctor_spec_bakes_fleet_true(tmp_path: Path) -> None:
    """What doctor-install writes for the OS scheduler carries fleet=true, so the
    one unattended tick covers every journaled repo — not just this dir."""
    spec_path = _write_durable_spec(tmp_path, notify=True)
    on_disk = json.loads(spec_path.read_text(encoding="utf-8"))

    assert on_disk["fleet"] is True
    assert on_disk["notify"] is True
    # And the durable payload is a VALID DoctorSpec (extra=forbid) — the scheduled
    # `doctor --spec` reads it back through the model.
    spec = DoctorSpec.model_validate(on_disk)
    assert spec.fleet is True
    assert spec.notify is True
