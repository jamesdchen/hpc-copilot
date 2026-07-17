"""Submit-once fault drills — the apex ``qsub dispatch→job-id window`` (U3).

AUDIT §7 row 4 / FAULT-HARNESS §4 row 4 (moved §4→§2 by this wave): the ONE
genuinely non-idempotent actuation. A drop AFTER the scheduler accepts the array
but BEFORE its stdout job id reaches the client leaves an orphan. The submit-once
contract makes recovery a READ: the dispatching shell persisted the id in a
cluster-durable jobmap MARKER, and reconcile adopts it with **no re-qsub** — or,
when the marker is pending / absent, lands on positive evidence at every rung and
NEVER settles on absence.

These drills assert the DOCTRINE outcome (adopt-no-reqsub / stay-submitting /
never-GC'd / one-dispatch / phantom-id-refused), hermetic (no real ssh), patching
the transport seam with the harness vocabulary.
"""

from __future__ import annotations

import subprocess
import threading
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.monitor import reconcile as R
from hpc_agent.ops.submit.runner import mint_submitting_record
from hpc_agent.state import run_record
from hpc_agent.state.index import find_submitting_runs, prune_terminal_runs
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_ACK = "__HPC_JOBMAP_ACK__"
_SCHED_ACK = "__HPC_SCHED_ACK__=0"


def _load(exp: Path, run_id: str) -> RunRecord:
    rec = load_run(exp, run_id)
    assert rec is not None
    return rec


def _proc(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _sub_record(
    run_id: str = "run-x", *, status: str = "submitting", attempt: int = 0
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="slurm",
        cluster="adhoc-not-in-yaml",  # resolve_ssh_target → record.ssh_target
        ssh_target="user@host",
        remote_path="/home/u/demo",
        job_name="job",
        job_ids=[],
        total_tasks=10,
        submitted_at="2026-01-01T00:00:00Z",
        experiment_dir="/e",
        status=status,
        attempt=attempt,
    )


def _marker_stdout(*, wave_line: str | None) -> str:
    lines = [_ACK, '{"token":"run-x#0","state":"pending","attempt":0,"waves":{}}']
    if wave_line is not None:
        lines.append(wave_line)
    return "\n".join(lines) + "\n"


class _Transport:
    """ssh_run stub: records every command, returns per-substring, spies re-qsub."""

    def __init__(self, *, jobmap, token=None) -> None:  # type: ignore[no-untyped-def]
        self.jobmap = jobmap
        self.token = token
        self.calls: list[str] = []

    def __call__(self, cmd: str, *, ssh_target: str | None = None, **_: object):  # type: ignore[no-untyped-def]
        self.calls.append(cmd)
        if ".hpc/submit" in cmd and "rm -f" not in cmd:
            if isinstance(self.jobmap, BaseException):
                raise self.jobmap
            return self.jobmap
        if "rm -f" in cmd:
            return _proc(0)
        if "squeue" in cmd or "qstat" in cmd:
            if isinstance(self.token, BaseException):
                raise self.token
            return self.token
        return _proc(0)

    @property
    def reqsub_count(self) -> int:
        return sum(("sbatch" in c or "qsub " in c) for c in self.calls)


@pytest.fixture
def exp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    e = tmp_path / "exp"
    e.mkdir()
    return e


def _patch_announce(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    monkeypatch.setattr(
        R,
        "read_announcements",
        lambda **_: {"present": present, "announced": 0, "complete": 0, "failed": 0, "missing": 0},
    )


# ── the apex drill: dispatch→id-window sever, reconcile adopts, ZERO re-qsub ────


def test_dispatch_id_window_sever_then_reconcile_adopts_no_reqsub(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUDIT §7 row 4. The mint-before-dispatch record lands ``submitting``; the
    dispatch is severed after the scheduler accepted (the promote never runs), so
    the client holds no id. The server-side marker DID persist it — reconcile
    reads the marker and adopts with NO re-``qsub`` (the duplicate-array the
    contract exists to prevent)."""
    # 1. mint-before-dispatch: the durable submitting record with empty job_ids.
    rec, minted = mint_submitting_record(
        exp,
        run_id="run-x",
        profile="slurm",
        cluster="adhoc-not-in-yaml",
        ssh_target="user@host",
        remote_path="/home/u/demo",
        job_name="job",
        total_tasks=10,
    )
    assert minted and rec.status == "submitting" and rec.job_ids == []
    # 2. the drop happened (promote never ran). Recovery reads the cluster marker.
    tr = _Transport(
        jobmap=_proc(
            0, _marker_stdout(wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 12345")
        )
    )
    monkeypatch.setattr(R.remote, "ssh_run", tr)
    _patch_announce(monkeypatch, present=True)
    out = R._recover_submitting(exp, "run-x", record=_load(exp, "run-x"), scheduler="slurm")
    assert out.status == "in_flight"
    assert out.job_ids == ["12345"]
    assert tr.reqsub_count == 0  # the load-bearing assertion: NO re-dispatch


def test_planted_severed_jobmap_read_leaves_submitting_never_settles(
    exp: Path, monkeypatch: pytest.MonkeyPatch, sever_at
) -> None:
    """A planted SEVERED jobmap read (the channel raises) must leave the run
    ``submitting`` — UNKNOWN, never a settle onto absence."""
    upsert_run(exp, _sub_record())
    sever_at("hpc_agent.ops.monitor.reconcile.remote.ssh_run")
    out = R._recover_submitting(exp, "run-x", record=_load(exp, "run-x"), scheduler="slurm")
    assert out.status == "submitting"


def test_planted_submitting_record_pruned_by_nothing(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skew-prune drill (premortem Δ3 / harness §2.2): a planted ``submitting``
    orphan is pruned by NOTHING — the new-guard ``prune_terminal_runs`` keeps it
    (``status not in TERMINAL_STATUSES``), so the only durable evidence reconcile
    -recovery needs survives. The old ``== "in_flight"`` guard would have GC'd it
    (pinned red-then-green in tests/state/test_submitting_state)."""
    upsert_run(exp, _sub_record(run_id="orphan-sub"))
    upsert_run(exp, _sub_record(run_id="done-1", status="complete"))
    upsert_run(exp, _sub_record(run_id="done-2", status="failed"))
    prune_terminal_runs(exp, keep=0)  # prune everything prunable
    survivors = {r.run_id for r in find_submitting_runs(exp)}
    assert "orphan-sub" in survivors
    assert load_run(exp, "orphan-sub") is not None


def test_double_submit_race_yields_one_dispatch(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Δ1 compare-and-mint race at the drill level (harness §1.2): two
    genuinely concurrent same-run_id mints serialize to EXACTLY one submitting
    record + one dispatch; the loser is routed to reconcile and refuses."""
    from hpc_agent.state.run_record import journal_dir

    journal_dir(exp)  # prime the namespace so threads race only on the run lock
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            _r, minted = mint_submitting_record(
                exp,
                run_id="race",
                profile="slurm",
                cluster="adhoc-not-in-yaml",
                ssh_target="user@host",
                remote_path="/home/u/demo",
                job_name="job",
                total_tasks=10,
            )
            with lock:
                outcomes.append("minted" if minted else "dedup")
        except errors.SpecInvalid:
            with lock:
                outcomes.append("refused")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["minted", "refused"]
    assert len(find_submitting_runs(exp)) == 1  # one durable record, no duplicate


def test_adopt_gate_refuses_phantom_id_rc_nonzero(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Δ4 phantom-id guard: a marker whose recorded ``rc≠0`` is a CONFIRMED failed
    dispatch — a ``qsub`` that printed garbage to stdout must NOT be adopted as a
    live array. It falls to rung-1b; a clean-miss token query then safe-resubmits
    (submitting→abandoned), never a permanently-``in_flight`` ghost."""
    upsert_run(exp, _sub_record())
    tr = _Transport(
        jobmap=_proc(0, _marker_stdout(wave_line="__HPC_JOBMAP_WAVE__ wave-0 7 12345")),
        token=_proc(0, _SCHED_ACK + "\n"),  # query ran, token absent → clean miss
    )
    monkeypatch.setattr(R.remote, "ssh_run", tr)
    out = R._recover_submitting(exp, "run-x", record=_load(exp, "run-x"), scheduler="slurm")
    assert out.status != "in_flight"
    assert out.status == "abandoned"  # safe-resubmit, not a phantom-id adopt


def test_marker_append_survives_timeout_k_grace_is_a_single_rename(
    exp: Path,
) -> None:
    """O5 substrate (design §OPEN-5): the marker append is a SINGLE ``mv`` rename
    — no ``fork``, no subshell — so it completes inside the 10 s SIGKILL grace even
    under login-node fork exhaustion; only a wedged FS could reap it mid-``mv``.
    That is WHY the append-killed window is near-unreachable and rung-1b (the
    token query, drilled above) — not the append — is the load-bearing recovery.
    We assert the reachable proxy: the post-dispatch fragment is a bare
    ``> tmp && mv -f`` with no fork-introducing construct."""
    from hpc_agent.infra import jobmap

    frag = jobmap.build_post_dispatch_shell(remote_path="exp", run_id="run-x", wkey="wave-0")
    # The append is a redirect-to-temp then an atomic rename, guarded by ``&&``.
    assert "mv -f" in frag
    # Fork-free: no command substitution ``$(`` and no pipe ``|`` in the append
    # (the ``&&`` guarding the rename is a logical sequencer, NOT a fork). Only a
    # wedged FS could reap a bare ``mv`` mid-syscall — the O5 near-unreachability
    # argument that makes the append-killed window off the table and rung-1b the
    # load-bearing recovery.
    assert "$(" not in frag
    assert "|" not in frag
    assert frag.rstrip().endswith("2>/dev/null")


@pytest.mark.xfail(
    reason="remote-half timeout -k reap of the marker append is not locally "
    "injectable (FAULT-HARNESS §4 row 15, 'remote half') — needs a real/faked "
    "remote; the reachability argument (single fork-free rename, O5) is asserted "
    "by the sibling drill instead",
    strict=False,
)
def test_marker_append_reaped_mid_mv_remote_half() -> None:
    raise AssertionError("remote-half timeout -k reap not injectable in-process")
