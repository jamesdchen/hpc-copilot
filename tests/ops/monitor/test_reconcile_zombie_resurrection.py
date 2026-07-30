"""Rung 0 — the acceptance-evidence class (2026-07-30 zombie resurrection).

**The live defect.** A canary attempt (``har_base_sweep-<sha>-canary2``) was
minted ``submitting`` and its worker was then killed by a network failure BEFORE
it ever dialled the cluster — the job provably never queued. Every later
reconcile pass hit the ladder's severed rung (the operator's VPN was still down,
``ssh`` rc 255) and re-asserted ``submitting`` via ``_stay_submitting``; the
submit front door (``_resolve_layer1``) kept reading that as "a live prior
attempt is in flight for this run_id" and refused four consecutive submits.
The operator had to hand-abandon a record that provably never queued a job.

**The class.** A jobless ``submitting`` record covers two categorically
different situations that the pre-fix ladder could only separate by ASKING THE
CLUSTER — and the fault that produces the orphan is normally the same fault that
makes the cluster unreadable, so the ladder had no terminating rung for its own
most common orphan:

* the dispatch exec was never issued → nothing can be queued → TERMINAL;
* the dispatch exec went out and its answer was lost → something may be live →
  AMBIGUOUS, and must never be auto-abandoned however long the scheduler stays
  silent.

The discriminator is therefore "could acceptance evidence ever have existed",
recorded locally at submit time (``dispatch_evidence``: ``pending`` at mint,
``actuated`` immediately before the exec) — never elapsed time, never the
cluster's silence.

Each drill below is the shape of the real record, with paths anonymized. The
zombie drills go RED on the pre-fix rule (which returns ``submitting``); the
conservative twins go GREEN on both and are the regression guards that keep the
fix from becoming a killer of live jobs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent._kernel.contract.vocabulary import (
    NEVER_DISPATCHED_VERDICT_REASON,
    DispatchState,
)
from hpc_agent.ops.monitor import reconcile as R
from hpc_agent.state import run_record
from hpc_agent.state.journal import (
    dispatch_never_actuated,
    is_resubmittable_terminal,
    load_run,
    upsert_run,
)
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


# ── the real record, anonymized ───────────────────────────────────────────────


def _canary2_record(
    *,
    dispatch_evidence: dict | None,
    run_id: str = "sweep-53c27e42-canary2",
) -> RunRecord:
    """The live canary2 journal record, field-for-field, with paths anonymized.

    ``job_ids=[]`` / ``total_tasks=1`` / ``attempt=1`` / ``status="submitting"``
    are copied from the real record; only the ssh target and remote path are
    generic. ``cluster`` is deliberately not a clusters.yaml name so
    ``resolve_ssh_target`` would fall back to ``record.ssh_target`` — rung 0 must
    not even reach that call.
    """
    rec = RunRecord(
        run_id=run_id,
        profile="sweep",
        cluster="adhoc-not-in-yaml",
        ssh_target="user@login1.example.edu",
        remote_path="/scratch/user/experiment",
        job_name="sweep_canary",
        job_ids=[],
        total_tasks=1,
        submitted_at="2026-07-30T10:09:19+00:00",
        experiment_dir="/e",
        status="submitting",
        attempt=1,
    )
    if dispatch_evidence is not None:
        rec.dispatch_evidence = dict(dispatch_evidence)
    return rec


_PENDING = {"state": str(DispatchState.PENDING), "at": "2026-07-30T10:09:19+00:00"}
_ACTUATED = {"state": str(DispatchState.ACTUATED), "at": "2026-07-30T10:09:20+00:00"}

_ACK = "__HPC_JOBMAP_ACK__"
_SCHED_ACK = "__HPC_SCHED_ACK__=0"
# The cluster-side pending marker for this run+attempt (carries no adoptable id).
_MARKER = '{"token":"sweep-53c27e42-canary2#1","state":"pending","attempt":1,"waves":{}}'


def _proc(rc: int, stdout: str = "", stderr: str = ""):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class _CountingSsh:
    """``ssh_run`` stand-in that records every call and can simulate the outage."""

    def __init__(self, *, result: object) -> None:
        self.result = result
        self.calls: list[str] = []

    def __call__(self, cmd: str, *, ssh_target: str | None = None, **_: object):
        self.calls.append(cmd)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def exp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    e = tmp_path / "exp"
    e.mkdir()
    return e


def _recover(exp: Path, run_id: str = "sweep-53c27e42-canary2") -> RunRecord:
    rec = load_run(exp, run_id)
    assert rec is not None
    return R._recover_submitting(exp, run_id, record=rec, scheduler="slurm")


def _fail_all_ssh(monkeypatch: pytest.MonkeyPatch) -> _CountingSsh:
    """Patch ssh AND the announce census to the live outage (VPN down, rc 255)."""
    ssh = _CountingSsh(result=_proc(255, "", "ssh: connect to host: Network is unreachable"))
    monkeypatch.setattr(R.remote, "ssh_run", ssh)

    def _severed(**_: object) -> dict:
        raise ConnectionError("network is unreachable")

    monkeypatch.setattr(R, "read_announcements", _severed)
    return ssh


# ── the zombie: no acceptance evidence ever existed ───────────────────────────


def test_never_actuated_settles_abandoned_under_total_outage(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE REGRESSION. Pre-fix this returned ``submitting`` — forever."""
    upsert_run(exp, _canary2_record(dispatch_evidence=_PENDING))
    _fail_all_ssh(monkeypatch)

    out = _recover(exp)

    assert out.status == "abandoned"
    assert is_resubmittable_terminal(out)
    assert out.last_status.get("verdict_reason") == NEVER_DISPATCHED_VERDICT_REASON
    assert out.last_status.get("acceptance_evidence") == "never_actuated"


def test_never_actuated_verdict_needs_no_cluster_at_all(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero SSH is a correctness requirement, not an optimisation.

    The orphan is produced by an outage that is still in force when reconcile
    runs, so any rung that has to reach the cluster falls through to
    ``_stay_submitting`` and re-asserts the zombie.
    """
    upsert_run(exp, _canary2_record(dispatch_evidence=_PENDING))
    ssh = _fail_all_ssh(monkeypatch)

    out = _recover(exp)

    assert out.status == "abandoned"
    assert ssh.calls == []


def test_never_actuated_settles_even_with_a_healthy_cluster(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict is the record's, not the network's — a reachable cluster
    cannot flip it, because a dispatch that never ran can have left nothing
    there to find."""
    upsert_run(exp, _canary2_record(dispatch_evidence=_PENDING))
    ssh = _CountingSsh(result=_proc(0, ""))
    monkeypatch.setattr(R.remote, "ssh_run", ssh)

    out = _recover(exp)

    assert out.status == "abandoned"
    assert ssh.calls == []


def test_never_actuated_unblocks_the_next_submit(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's actual pain: the stuck record refused every later submit.

    ``_resolve_layer1`` routes a ``submitting`` record to ``_RECONCILE`` ("do NOT
    re-submit until reconcile resolves it"). After rung 0 settles it, the same
    front door PROCEEDs and the attempt ordinal advances, so a fresh submit can
    never adopt the dead attempt's identity.
    """
    from hpc_agent.ops.submit.runner import _PROCEED, _resolve_layer1, allocate_attempt

    upsert_run(exp, _canary2_record(dispatch_evidence=_PENDING))
    _fail_all_ssh(monkeypatch)

    out = _recover(exp)

    decision = _resolve_layer1(
        out,
        invalidate_on_code_change=False,
        current_executor=None,
        current_tasks_py_sha=None,
        current_cluster="adhoc-not-in-yaml",
    )
    assert decision.action == _PROCEED
    assert allocate_attempt(out) == 2


def test_never_actuated_with_kill_stamped_over_zero_jobs(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live record also carried a kill stamped over an EMPTY job set.

    ``is_kill_confirmed`` correctly refuses to settle on that (killing zero jobs
    proves nothing), which is why no code path could close the record. Rung 0
    settles it on the dispatch evidence instead — the kill stamps are irrelevant
    either way.
    """
    rec = _canary2_record(dispatch_evidence=_PENDING)
    rec.kill_requested_at = "2026-07-30T10:30:00+00:00"
    rec.kill_confirmed_at = "2026-07-30T10:30:05+00:00"
    upsert_run(exp, rec)
    _fail_all_ssh(monkeypatch)

    assert _recover(exp).status == "abandoned"


def test_reconcile_one_settles_the_zombie_end_to_end(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_run(exp, _canary2_record(dispatch_evidence=_PENDING))
    _fail_all_ssh(monkeypatch)

    rec, alive_check_failed = R._reconcile_one(exp, "sweep-53c27e42-canary2", scheduler="slurm")

    assert not isinstance(rec, R.OrphanedReconcile)
    assert rec.status == "abandoned"
    assert alive_check_failed is False


def test_never_dispatched_verdict_reason_is_the_shared_one(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One class, one verdict reason — so the campaign circuit breaker keeps
    counting this as a control-plane fault rather than an experiment failure
    (``meta.campaign.atoms.circuit_breaker._is_never_dispatched``)."""
    from hpc_agent.meta.campaign.atoms.circuit_breaker import _is_never_dispatched

    upsert_run(exp, _canary2_record(dispatch_evidence=_PENDING))
    _fail_all_ssh(monkeypatch)

    assert _is_never_dispatched(_recover(exp)) is True


# ── the conservative twins: acceptance evidence MAY exist → never terminal ────


def test_actuated_and_scheduler_silent_stays_submitting(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE TWIN. A dispatch DID go out and the cluster is unreachable: a live
    array may be sitting behind that silence, so the run stays non-terminal
    forever rather than be killed on ambiguity."""
    upsert_run(exp, _canary2_record(dispatch_evidence=_ACTUATED))
    _fail_all_ssh(monkeypatch)

    out = _recover(exp)

    assert out.status == "submitting"
    assert out.last_status.get("verdict_reason") == "recovery_unknown_recensus"


def test_actuated_and_marker_pending_with_severed_token_query_stays_submitting(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker exists but names no id, and the scheduler query cannot run —
    the deepest ambiguity in the ladder. Unchanged by rung 0."""

    def router(cmd: str, *, ssh_target: str | None = None, **_: object):
        if ".hpc/submit" in cmd and "rm -f" not in cmd:
            return _proc(0, "\n".join([_ACK, _MARKER]) + "\n")
        return _proc(255, "", "connection reset")

    upsert_run(exp, _canary2_record(dispatch_evidence=_ACTUATED))
    monkeypatch.setattr(R.remote, "ssh_run", router)

    assert _recover(exp).status == "submitting"


def test_actuated_and_announce_present_refuses_resubmit(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker absent but the announce census says the dispatcher ran — a
    resubmit would duplicate a possibly-live array. Unchanged by rung 0."""
    upsert_run(exp, _canary2_record(dispatch_evidence=_ACTUATED))
    monkeypatch.setattr(R.remote, "ssh_run", _CountingSsh(result=_proc(0, "")))
    monkeypatch.setattr(
        R,
        "read_announcements",
        lambda **_: {"present": True, "announced": 1, "complete": 0, "failed": 0, "missing": 0},
    )

    assert _recover(exp).status == "submitting"


def test_actuated_with_adoptable_marker_still_adopts(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rung 0 must not shadow adoption: a recoverable id still promotes to
    in_flight with no re-qsub."""
    stdout = (
        "\n".join([_ACK, _MARKER, "__HPC_JOBMAP_WAVE__ canary 0 Submitted batch job 10721456"])
        + "\n"
    )
    upsert_run(exp, _canary2_record(dispatch_evidence=_ACTUATED))
    monkeypatch.setattr(R.remote, "ssh_run", _CountingSsh(result=_proc(0, stdout)))
    monkeypatch.setattr(
        R,
        "read_announcements",
        lambda **_: {"present": True, "announced": 1, "complete": 0, "failed": 0, "missing": 0},
    )

    out = _recover(exp)

    assert out.status == "in_flight"
    assert out.job_ids == ["10721456"]


def test_legacy_record_without_the_stamp_is_unchanged(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-fix record carries NO dispatch evidence. Absence proves nothing, so
    the ladder behaves EXACTLY as it did before the field existed."""
    upsert_run(exp, _canary2_record(dispatch_evidence=None))
    _fail_all_ssh(monkeypatch)

    out = _recover(exp)

    assert out.dispatch_evidence == {}
    assert out.status == "submitting"


def test_clean_miss_still_needs_a_cluster_answer_when_actuated(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actuated record's terminal path is unchanged: it takes a clean,
    ACKED cluster miss — positive evidence the array never entered the queue —
    not a severed read."""

    def router(cmd: str, *, ssh_target: str | None = None, **_: object):
        if ".hpc/submit" in cmd and "rm -f" not in cmd:
            return _proc(0, "\n".join([_ACK, _MARKER]) + "\n")
        if "squeue" in cmd or "qstat" in cmd:
            return _proc(0, _SCHED_ACK + "\n")
        return _proc(0)

    upsert_run(exp, _canary2_record(dispatch_evidence=_ACTUATED))
    monkeypatch.setattr(R.remote, "ssh_run", router)

    out = _recover(exp)

    assert out.status == "abandoned"
    assert out.last_status.get("acceptance_evidence") is None


# ── the predicate's own contract ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ({"state": "pending"}, True),
        ({"state": "actuated"}, False),
        ({}, False),
        ({"state": ""}, False),
        ({"at": "2026-07-30T10:09:19+00:00"}, False),
    ],
)
def test_dispatch_never_actuated_only_fires_on_positive_pending(
    evidence: dict, expected: bool
) -> None:
    assert dispatch_never_actuated(_canary2_record(dispatch_evidence=evidence)) is expected
