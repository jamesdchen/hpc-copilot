"""U3-d — the reconcile submit-once recovery ladder (SUBMIT-ONCE-DESIGN §3.4/§4).

Reconcile is the SOLE transition-out owner of a ``submitting`` record. Every rung
lands on POSITIVE evidence; absence is never a settle. These drills patch
``remote.ssh_run`` (the jobmap read + the U3-c token query + the jobmap clear) and
``read_announcements`` (the Δ6 cross-check), and assert the DOCTRINE outcome per
rung — adopt-with-no-reqsub / disambiguate / safe-resubmit / stay-submitting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent.infra.jobmap import CANARY_WAVE_KEY  # noqa: F401 (Δ5 keying sanity)
from hpc_agent.ops.monitor import reconcile as R
from hpc_agent.state import run_record
from hpc_agent.state.index import find_in_flight_runs, find_runs_by_campaign
from hpc_agent.state.journal import is_resubmittable_terminal, load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


def _sub_record(run_id: str = "run-x", attempt: int = 0) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="slurm",
        cluster="adhoc-not-in-yaml",  # forces resolve_ssh_target → record.ssh_target
        ssh_target="user@host",
        remote_path="/home/u/demo",
        job_name="job",
        job_ids=[],
        total_tasks=10,
        submitted_at="2026-01-01T00:00:00Z",
        experiment_dir="/e",
        status="submitting",
        attempt=attempt,
    )


_ACK = "__HPC_JOBMAP_ACK__"
_SCHED_ACK = "__HPC_SCHED_ACK__=0"


def _jobmap_stdout(*, token: str = "run-x#0", attempt: int = 0, wave_line: str | None) -> str:
    marker = f'{{"token":"{token}","state":"pending","attempt":{attempt},"waves":{{}}}}'
    lines = [_ACK, marker]
    if wave_line is not None:
        lines.append(wave_line)
    return "\n".join(lines) + "\n"


class _Router:
    """A patched ``ssh_run`` dispatching by command substring, recording calls."""

    def __init__(self, *, jobmap: object, token: object = None) -> None:
        self.jobmap = jobmap  # proc | Exception | None
        self.token = token  # proc for the squeue/qstat token query
        self.calls: list[str] = []

    def __call__(self, cmd: str, *, ssh_target: str | None = None, **_: object):
        self.calls.append(cmd)
        if ".hpc/submit" in cmd and "rm -f" not in cmd:
            if isinstance(self.jobmap, BaseException):
                raise self.jobmap
            return self.jobmap
        if "rm -f" in cmd:
            return _proc(0)  # jobmap clear: rc0
        if "squeue" in cmd or "qstat" in cmd:
            if isinstance(self.token, BaseException):
                raise self.token
            return self.token
        return _proc(0)


def _proc(rc: int, stdout: str = "", stderr: str = ""):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def exp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    e = tmp_path / "exp"
    e.mkdir()
    return e


def _patch_ssh(monkeypatch: pytest.MonkeyPatch, router: _Router) -> None:
    monkeypatch.setattr(R.remote, "ssh_run", router)


def _patch_announce(
    monkeypatch: pytest.MonkeyPatch, *, present: bool | Exception, announced: int = 0
) -> None:
    def fake(**_: object) -> dict:
        if isinstance(present, BaseException):
            raise present
        return {
            "present": present,
            "announced": announced,
            "complete": 0,
            "failed": 0,
            "missing": 0,
        }

    monkeypatch.setattr(R, "read_announcements", fake)


def _recover(exp: Path, run_id: str = "run-x") -> RunRecord:
    rec = load_run(exp, run_id)
    assert rec is not None
    return R._recover_submitting(exp, run_id, record=rec, scheduler="slurm")


# ── rung 1a — adopt from the marker, NO re-qsub ───────────────────────────────


def test_rung1a_adopts_marker_id_and_promotes(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(
            0,
            _jobmap_stdout(wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 12345"),
        )
    )
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=True)

    out = _recover(exp)
    assert out.status == "in_flight"
    assert out.job_ids == ["12345"]
    assert out.last_status.get("verdict_reason") == "submit_once_adopted_from_marker"
    assert out.last_status.get("announce_crosscheck") == "confirmed"
    # No submission command was ever issued (adopt, never re-qsub).
    assert not any(("sbatch" in c or "qsub " in c) for c in router.calls)


def test_adopt_gate_refuses_nonzero_rc_falls_to_1b(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Δ4: an rc≠0 marker is a confirmed failed dispatch — NOT adopted. Falls to
    # 1b; a clean-miss token query then safe-resubmits.
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_line="__HPC_JOBMAP_WAVE__ wave-0 3 ")),
        token=_proc(0, _SCHED_ACK + "\n"),  # query ran, token absent → clean miss
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "abandoned"
    assert is_resubmittable_terminal(out)


def test_adopt_gate_refuses_regex_miss_falls_to_1b(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 garbage-no-digits")),
        token=_proc(0, _SCHED_ACK + "\n"),
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "abandoned"


# ── rung 1b — disambiguate by the U3-c correlation token ──────────────────────


def test_rung1b_token_hit_adopts(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_line=None)),  # pending, no id
        token=_proc(0, "12345|run-x#0\n" + _SCHED_ACK + "\n"),
    )
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=True)
    out = _recover(exp)
    assert out.status == "in_flight"
    assert out.job_ids == ["12345"]


def test_rung1b_clean_miss_safe_resubmits(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_line=None)),
        token=_proc(0, _SCHED_ACK + "\n"),  # ran, token absent
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "abandoned"
    assert out.last_status.get("verdict_reason") == "submit_once_never_dispatched_safe_resubmit"
    # the jobmap was cleared on the transition
    assert any("rm -f" in c for c in router.calls)


def test_rung1b_query_transport_severed_stays_submitting(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_line=None)),
        token=_proc(255, "", "ssh: connect: Connection refused"),
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "submitting"  # UNKNOWN, never a settle


def test_rung1b_query_no_ack_stays_submitting(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # rc-0 but NO scheduler ack line → the query did not run to completion → UNKNOWN.
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_line=None)),
        token=_proc(0, "12345|run-x#0\n"),  # no __HPC_SCHED_ACK__ line
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "submitting"


# ── rung 2 — never dispatched (Δ6 shared-FS cross-check) ───────────────────────


def test_rung2_marker_absent_and_announce_absent_safe_resubmits(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(exp, _sub_record())
    router = _Router(jobmap=_proc(0, ""))  # rc0, NO ack ⇒ submit dir absent
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=False)
    out = _recover(exp)
    assert out.status == "abandoned"
    assert any("rm -f" in c for c in router.calls)


def test_rung2_delta6_announce_present_refuses_resubmit(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Marker absent but the announce dir EXISTS (possible non-shared-FS split /
    # marker lost the race) — a resubmit would duplicate a live array. Stay.
    upsert_run(exp, _sub_record())
    router = _Router(jobmap=_proc(0, ""))
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=True, announced=3)
    out = _recover(exp)
    assert out.status == "submitting"


def test_rung2_announce_crosscheck_severed_stays_submitting(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(exp, _sub_record())
    router = _Router(jobmap=_proc(0, ""))
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=ConnectionError("severed"))
    out = _recover(exp)
    assert out.status == "submitting"


# ── rung 3 — everything severed → UNKNOWN, never a settle ──────────────────────


def test_rung3_jobmap_transport_severed_stays_submitting(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(exp, _sub_record())
    _patch_ssh(monkeypatch, _Router(jobmap=_proc(255, "", "connection reset")))
    out = _recover(exp)
    assert out.status == "submitting"


def test_rung3_jobmap_read_raises_stays_submitting(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(exp, _sub_record())
    _patch_ssh(monkeypatch, _Router(jobmap=ConnectionError("dropped")))
    out = _recover(exp)
    assert out.status == "submitting"


# ── the intercept + sole-owner + attempt-bump invariants ──────────────────────


def test_reconcile_one_intercepts_submitting_and_never_probes(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A submitting record must route to recovery, NOT the alive/reporter probe
    # path (which would leave it stranded). A severed read keeps it submitting.
    upsert_run(exp, _sub_record())
    _patch_ssh(monkeypatch, _Router(jobmap=_proc(255)))
    rec, alive_failed = R._reconcile_one(exp, "run-x", scheduler="slurm")
    assert not isinstance(rec, R.OrphanedReconcile)
    assert rec.status == "submitting"
    assert alive_failed is False


def test_safe_resubmit_makes_next_submit_bump_attempt(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hpc_agent.ops.submit.runner import allocate_attempt

    upsert_run(exp, _sub_record(attempt=0))
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_line=None)),
        token=_proc(0, _SCHED_ACK + "\n"),
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert is_resubmittable_terminal(out)
    # The ruled allocation path bumps attempt for the reconcile-authorized redo.
    assert allocate_attempt(out) == 1


def test_canary_submitting_uses_its_own_run_id(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Δ5: a canary is a DISTINCT run_id with its own jobmap — recovery keys on it.
    upsert_run(exp, _sub_record(run_id="run-x-canary"))
    router = _Router(
        jobmap=_proc(
            0,
            "\n".join(
                [
                    _ACK,
                    '{"token":"run-x-canary#0","state":"pending","attempt":0,"waves":{}}',
                    "__HPC_JOBMAP_WAVE__ canary 0 Submitted batch job 55",
                ]
            )
            + "\n",
        )
    )
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=True)
    canary_rec = load_run(exp, "run-x-canary")
    assert canary_rec is not None
    out = R._recover_submitting(exp, "run-x-canary", record=canary_rec, scheduler="slurm")
    assert out.status == "in_flight"
    assert out.job_ids == ["55"]


def test_campaign_submitting_child_neither_live_nor_settled(exp: Path) -> None:
    """§5: a campaign child rides the same contract. A ``submitting`` child is
    surfaced by ``find_runs_by_campaign`` (status-agnostic join on campaign_id) so
    reconcile can own it, but is NOT in the monitor live set (``find_in_flight_runs``)
    — the same "not yet live, not done" treatment a parked child gets. The campaign
    loop never blind-resubmits it; reconcile is the sole transition-out owner."""
    child = _sub_record(run_id="camp-child")
    child.campaign_id = "camp-1"
    upsert_run(exp, child)
    by_campaign = {r.run_id for r in find_runs_by_campaign(exp, "camp-1")}
    assert "camp-child" in by_campaign  # reconcile can find it
    assert find_in_flight_runs(exp) == []  # but the monitor never treats it as live
