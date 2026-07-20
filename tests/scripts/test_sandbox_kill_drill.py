"""Hermetic tests for ``scripts/sandbox_kill_drill.py`` — the U4 kill drill.

Every test here is rung-1: no cluster, no docker, no subprocess, no real journal
— the pure contract surface is exercised against synthetic scheduler snapshots
(slurm ``squeue … -o '%i|%k'`` rows + sge ``qstat -j`` blocks, ack lines
included), canned reconcile briefs / journal records, and ``tmp_path`` journal
homes. The chain that wires this surface to a live container (``run_kill_drill``
→ ``_drive_one_attempt`` → ``_recovery_legs``) is NOT hermetically testable and
is covered only through these pure helpers, exactly as the U3 driver's chain is
covered by ``test_run_sandbox_proving.py``.

Covered pins (plan §4-U4):

1. The §3 journal-home guard is REUSED from the driver — never a fourth copy.
2. Window detection against synthetic squeue/qstat snapshots (the token read,
   including the severed-query → UNKNOWN discipline and the subjob-collapse).
3. The submit-window state machine (``not_yet`` / ``open`` / ``missed``).
4. The bounded (3) parameter-bump retry loop — hit short-circuits, miss/error
   exhaust with per-attempt evidence, the n_samples bump mints fresh run_ids.
5. The one-array / zero-re-qsub counters against synthetic scheduler state.
6. Lease-PID extraction (positive-int pid, bool rejected, host match, create_time)
   and a full read-through against a ``tmp_path`` journal home.
7. Every recovery-contract leg against canned briefs / journal records.
8. The ``main()`` guard-first refusal order (guard → knobs → cluster source).
9. The live ssh-failure catches: ``TimeoutError``/``OSError`` out of the
   ``ssh_run``-backed sites (the window poll + recovery legs 2/5) land as
   evidence rows + bounded aborts — never a traceback escaping the
   attempt/driver function — and leg 3's ``SandboxRefusal`` path
   (``run_cli_argv`` genuinely raises it) stays live.
10. The sub-second JITTERED poll (the 2026-07-19 phase-lock fix): ≤0.5s mean,
    every sleep routed through the jitter, the 180s budget unchanged.
11. The dispatched-unseen witnesses (run 29709733724): a lapsed poll arbitrates
    via the journal's promoted job_ids (local) then the ONE-ssh cluster probe
    (jobmap marker + results) — a fast-completing array is SEEN or reported
    dispatched-unseen, NEVER misread as "never entered the scheduler". Only an
    ack-fired probe that ALSO completed (exactly one results line, rc==0) and
    found nothing settles to "never dispatched"; post-ack truncation and a
    nonzero probe rc stay UNKNOWN.
12. The RUNNER_TEMP evidence-dir contract: on Actions the default workdir is
    ``$RUNNER_TEMP/sandbox-evidence/kill-drill/`` (the upload step's path);
    off Actions the mkdtemp fallback; an explicit ``--workdir`` wins.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "sandbox_kill_drill", REPO_ROOT / "scripts" / "sandbox_kill_drill.py"
)
assert _SPEC is not None and _SPEC.loader is not None
kd = importlib.util.module_from_spec(_SPEC)
sys.modules["sandbox_kill_drill"] = kd
_SPEC.loader.exec_module(kd)

SandboxRefusal = kd.SandboxRefusal

# A run token + the ack-wrapped snapshot scaffolding the framework's own
# ``scheduler_query_ran`` strips before ``parse_token_query`` reads the rows.
RUN_ID = "sandbox-pi-deadbeef"
TOKEN = f"{RUN_ID}#0"
ACK = "__HPC_SCHED_ACK__=0"


def _slurm_snap(*rows: str, ack: str | None = ACK) -> str:
    """A synthetic slurm token-query snapshot: ``<jid>|<comment>`` rows + ack."""
    lines = [*rows]
    if ack is not None:
        lines.append(ack)
    return "\n".join(lines) + "\n"


# ── §3 guard is reused from the driver (never a fourth copy) ─────────────────


def test_guard_is_the_drivers_not_a_fourth_copy() -> None:
    # The kill drill re-exports the driver's guard object — the ONE §3 guard
    # (itself a delegate to the shared sandbox_guard), not a re-implementation.
    assert kd.require_ephemeral_journal_home is kd._driver.require_ephemeral_journal_home
    assert kd.SandboxRefusal is kd._driver.SandboxRefusal


def test_guard_refuses_unset() -> None:
    with pytest.raises(SandboxRefusal, match="HPC_JOURNAL_DIR is unset"):
        kd.require_ephemeral_journal_home({})


def test_guard_refuses_production_home() -> None:
    production = Path.home() / ".claude" / "hpc"
    with pytest.raises(SandboxRefusal, match="production journal home"):
        kd.require_ephemeral_journal_home({"HPC_JOURNAL_DIR": str(production)})


def test_guard_refuses_production_subdir() -> None:
    production = Path.home() / ".claude" / "hpc"
    with pytest.raises(SandboxRefusal, match="production journal home"):
        kd.require_ephemeral_journal_home({"HPC_JOURNAL_DIR": str(production / "nested")})


def test_guard_accepts_ephemeral_tmp(tmp_path: Path) -> None:
    home = kd.require_ephemeral_journal_home({"HPC_JOURNAL_DIR": str(tmp_path / "j")})
    assert home == (tmp_path / "j").resolve()


def test_guard_accepts_sibling_namespace() -> None:
    sibling = Path.home() / ".claude" / "hpc-killdrill-test"
    home = kd.require_ephemeral_journal_home({"HPC_JOURNAL_DIR": str(sibling)})
    assert home == sibling.resolve()


# ── window detection against synthetic squeue/qstat snapshots ────────────────


def test_slurm_token_read_collects_ids_for_the_token_only() -> None:
    snap = _slurm_snap(f"12345|{TOKEN}", "99999|some-other-run#0")
    assert kd.token_job_ids("slurm", snap, TOKEN) == ["12345"]
    assert kd.array_present("slurm", snap, TOKEN) is True
    assert kd.array_present("slurm", snap, "absent-run#0") is False


def test_slurm_token_read_normalizes_subjob_rows_to_one_base_id() -> None:
    # One array's many subjob rows (12345_0..2) all carry the token; the base id
    # is the array parent. count_arrays_under_token collapses them to ONE array.
    snap = _slurm_snap(f"12345_0|{TOKEN}", f"12345_1|{TOKEN}", f"12345_2|{TOKEN}")
    assert kd.token_job_ids("slurm", snap, TOKEN) == ["12345", "12345", "12345"]
    assert kd.count_arrays_under_token("slurm", snap, TOKEN) == 1


def test_slurm_token_read_sees_a_duplicate_array_under_one_token() -> None:
    # Two DISTINCT arrays sharing a token — the corruption submit-once forbids —
    # stays visible (the pre-collapse read), unlike parse_token_query's fold.
    snap = _slurm_snap(f"12345_0|{TOKEN}", f"12346_0|{TOKEN}")
    assert kd.count_arrays_under_token("slurm", snap, TOKEN) == 2


def test_severed_query_is_unknown_never_settled_zero() -> None:
    # No ``__HPC_SCHED_ACK__`` line → the channel was severed before the trailing
    # ack echo; the read is UNKNOWN, never a settled "zero arrays".
    snap_no_ack = f"12345|{TOKEN}\n"  # an id row but NO ack
    assert kd.token_job_ids("slurm", snap_no_ack, TOKEN) == []
    assert kd.array_present("slurm", snap_no_ack, TOKEN) is False
    assert kd.count_arrays_under_token("slurm", snap_no_ack, TOKEN) == 0


def test_sge_token_read_matches_context_line() -> None:
    snap = (
        "job_number:                 123\n"
        "exec_file:                  /var/spool/x\n"
        f"context:                    HPC_TOKEN={TOKEN}\n"
        "job_number:                 456\n"
        "context:                    HPC_TOKEN=other-run#0\n"
        f"{ACK}\n"
    )
    assert kd.token_job_ids("sge", snap, TOKEN) == ["123"]
    assert kd.count_arrays_under_token("sge", snap, TOKEN) == 1
    assert kd.array_present("sge", snap, "other-run#0") is True


def test_sge_token_read_severed_is_unknown() -> None:
    snap_no_ack = (
        f"job_number:                 123\ncontext:                    HPC_TOKEN={TOKEN}\n"
    )
    assert kd.token_job_ids("sge", snap_no_ack, TOKEN) == []


# ── the submit-window state machine ───────────────────────────────────────────


def test_classify_window_not_yet_when_no_array() -> None:
    snap = _slurm_snap("77777|unrelated#0")  # token absent
    assert (
        kd.classify_window(
            scheduler="slurm",
            stdout=snap,
            token=TOKEN,
            record_status="submitting",
            record_job_ids=[],
        )
        == kd.WINDOW_NOT_YET
    )


def test_classify_window_open_when_array_live_and_record_still_submitting() -> None:
    snap = _slurm_snap(f"12345|{TOKEN}")
    assert (
        kd.classify_window(
            scheduler="slurm",
            stdout=snap,
            token=TOKEN,
            record_status="submitting",
            record_job_ids=[],
        )
        == kd.WINDOW_OPEN
    )


def test_classify_window_missed_when_record_already_promoted() -> None:
    snap = _slurm_snap(f"12345|{TOKEN}")
    # The promote beat the kill: the record carries the id (no longer submitting).
    missed = kd.classify_window(
        scheduler="slurm",
        stdout=snap,
        token=TOKEN,
        record_status="in_flight",
        record_job_ids=["12345"],
    )
    assert missed == kd.WINDOW_MISSED
    # A still-submitting record that somehow already has ids is also a miss.
    missed2 = kd.classify_window(
        scheduler="slurm",
        stdout=snap,
        token=TOKEN,
        record_status="submitting",
        record_job_ids=["12345"],
    )
    assert missed2 == kd.WINDOW_MISSED


# ── the one-array / zero-re-qsub counters ─────────────────────────────────────


def test_exactly_one_array_passes_on_a_single_array() -> None:
    snap = _slurm_snap(f"12345_0|{TOKEN}", f"12345_1|{TOKEN}")
    assert kd.exactly_one_array_problems("slurm", snap, TOKEN) == []


def test_exactly_one_array_flags_zero_and_duplicates() -> None:
    zero = _slurm_snap("77777|unrelated#0")
    problems = kd.exactly_one_array_problems("slurm", zero, TOKEN)
    assert problems and "0 arrays" in problems[0]
    dup = _slurm_snap(f"12345|{TOKEN}", f"12346|{TOKEN}")
    problems = kd.exactly_one_array_problems("slurm", dup, TOKEN)
    assert problems and "2 arrays" in problems[0]


def test_count_dispatch_commands_counts_only_sbatch_qsub() -> None:
    commands = [
        "cd /repo && sbatch --comment run#0 .hpc/templates/cpu_array.slurm",
        "squeue -u user -h -o '%i|%k'",
        "cat /repo/.hpc/submit/run.jobmap",
        "rm -f /repo/.hpc/submit/run.jobmap",
    ]
    assert kd.count_dispatch_commands(commands) == 1  # only the sbatch
    assert kd.count_dispatch_commands(["qsub -ac HPC_TOKEN=run#0 script.sh"]) == 1
    assert kd.count_dispatch_commands([]) == 0
    # A successful drill's recovery (marker read + token query, no dispatch):
    recovery_cmds = ["cat .hpc/submit/run.jobmap", "squeue -u user -h -o '%i|%k'"]
    assert kd.count_dispatch_commands(recovery_cmds) == 0  # zero re-qsub


# ── bounded (3) parameter-bump retry loop ─────────────────────────────────────


def test_retry_loop_short_circuits_on_first_hit() -> None:
    calls: list[int] = []

    def drive_one(index: int, n_samples: int):
        calls.append(index)
        return kd.WindowOutcome(kind="hit", run_id=f"run-{n_samples}")

    ok, attempts = kd.run_window_attempts(drive_one, base_n_samples=5, max_attempts=3)
    assert ok is True
    assert calls == [0]  # no further attempts after a hit
    assert len(attempts) == 1 and attempts[0].kind == "hit"


def test_retry_loop_hits_on_third_with_parameter_bump() -> None:
    kinds = iter(["missed", "missed", "hit"])

    def drive_one(index: int, n_samples: int):
        return kd.WindowOutcome(kind=next(kinds), run_id=f"run-{n_samples}")

    ok, attempts = kd.run_window_attempts(drive_one, base_n_samples=100, max_attempts=3, bump=1)
    assert ok is True
    assert [a.kind for a in attempts] == ["missed", "missed", "hit"]
    # Each attempt bumps n_samples → a fresh run_id (the determinism lesson).
    assert [a.n_samples for a in attempts] == [100, 101, 102]


def test_retry_loop_exhausts_bounded_at_three_misses() -> None:
    calls: list[tuple[int, int]] = []

    def drive_one(index: int, n_samples: int):
        calls.append((index, n_samples))
        return kd.WindowOutcome(
            kind="missed", detail="promote beat the kill", run_id=f"r{n_samples}"
        )

    ok, attempts = kd.run_window_attempts(drive_one, base_n_samples=10, max_attempts=3, bump=1)
    assert ok is False
    assert len(calls) == 3  # bounded — never a fourth attempt
    assert len(attempts) == 3
    assert all(a.kind == "missed" for a in attempts)
    assert all(a.run_id for a in attempts)  # per-attempt evidence captured


def test_retry_loop_errors_exhaust_the_budget_too() -> None:
    def drive_one(index: int, n_samples: int):
        return kd.WindowOutcome(kind="error", detail="no lease pid")

    ok, attempts = kd.run_window_attempts(drive_one, base_n_samples=1, max_attempts=3)
    assert ok is False
    assert len(attempts) == 3
    assert all(a.kind == "error" for a in attempts)


# ── lease-PID extraction (read the detached worker we must kill) ─────────────


def _lease(**overrides: object) -> dict:
    base = {
        "run_id": "run-x",
        "block": "submit-s3",
        "pid": 4321,
        "host": "myhost",
        "log_path": "/tmp/x.log",
        "experiment_dir": "/exp",
        "argv": ["python", "-m", "hpc_agent", "submit-s3"],
        "create_time": 1700000000.0,
    }
    base.update(overrides)
    return base


def test_lease_pid_accepts_positive_int_and_rejects_garbage() -> None:
    assert kd.lease_pid(_lease()) == 4321
    assert kd.lease_pid(_lease(pid=True)) is None  # a bool is NOT a pid
    assert kd.lease_pid(_lease(pid=0)) is None
    assert kd.lease_pid(_lease(pid=-5)) is None
    assert kd.lease_pid(_lease(pid="4321")) is None  # a string is not an int
    assert kd.lease_pid({}) is None
    assert kd.lease_pid(None) is None


def test_lease_host_matches_guards_locality() -> None:
    assert kd.lease_host_matches(_lease(), "myhost") is True
    assert kd.lease_host_matches(_lease(), "otherhost") is False
    assert kd.lease_host_matches(_lease(host=123), "myhost") is False
    assert kd.lease_host_matches(None, "myhost") is False


def test_lease_create_time_optional_and_typed() -> None:
    assert kd.lease_create_time(_lease()) == 1700000000.0
    lease = _lease()
    del lease["create_time"]
    assert kd.lease_create_time(lease) is None  # the field is conditional in the writer
    assert kd.lease_create_time(_lease(create_time="nope")) is None


def test_lease_kill_target_validates_both_legs() -> None:
    target = kd.lease_kill_target(_lease(), hostname="myhost")
    assert target == {"pid": 4321, "host": "myhost", "create_time": 1700000000.0}
    # A remote worker's pid is not ours to signal → refused.
    assert kd.lease_kill_target(_lease(), hostname="otherhost") is None
    assert kd.lease_kill_target(None, hostname="myhost") is None
    assert kd.lease_kill_target(_lease(pid=True), hostname="myhost") is None


def test_lease_pid_read_through_a_tmp_journal_home(tmp_path: Path) -> None:
    # End-to-end lease read: write a lease into a tmp journal home's _detached/
    # (the driver's locator), read it back, and validate the kill target — never
    # the real journal home.
    journal_home = tmp_path / "journal"
    lease_path = kd._driver.detached_lease_path(journal_home, "run-x", "submit-s3")
    lease_path.parent.mkdir(parents=True)
    hostname = socket.gethostname()
    lease_path.write_text(
        json.dumps({"pid": 4321, "host": hostname, "create_time": 1.5}), encoding="utf-8"
    )
    lease = kd._driver.read_detached_lease(journal_home, "run-x", "submit-s3")
    assert kd.lease_kill_target(lease, hostname=hostname) == {
        "pid": 4321,
        "host": hostname,
        "create_time": 1.5,
    }
    # Absent lease → no target.
    assert kd._driver.read_detached_lease(journal_home, "other-run", "submit-s3") is None


# ── recovery-contract legs against canned briefs / journal records ───────────


def test_leg_submitting_state() -> None:
    assert kd.submitting_state_problems({"status": "submitting", "job_ids": []}) == []
    problems = kd.submitting_state_problems({"status": "in_flight", "job_ids": ["12345"]})
    assert any("status" in p for p in problems)
    problems = kd.submitting_state_problems({"status": "submitting", "job_ids": ["12345"]})
    assert any("job_ids" in p for p in problems)


def _marker_stdout(*, wave_line: str | None, token: str = TOKEN, state: str = "pending") -> str:
    lines = [
        "__HPC_JOBMAP_ACK__",
        json.dumps({"token": token, "state": state, "attempt": 0, "waves": {}}),
    ]
    if wave_line is not None:
        lines.append(wave_line)
    return "\n".join(lines) + "\n"


def test_leg_marker_pending_with_wave0_id() -> None:
    marker = _marker_stdout(wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 12345")
    assert kd.marker_state_problems("slurm", marker, RUN_ID, 0) == []
    assert kd.marker_wave0_job_id("slurm", marker) == "12345"


def test_leg_marker_refuses_phantom_id_rc_nonzero() -> None:
    # The Δ4 gate: rc != 0 is a confirmed failed dispatch — never adopted.
    marker = _marker_stdout(wave_line="__HPC_JOBMAP_WAVE__ wave-0 7 garbage-no-id")
    problems = kd.marker_state_problems("slurm", marker, RUN_ID, 0)
    assert any("rc=7" in p for p in problems)
    assert kd.marker_wave0_job_id("slurm", marker) is None


def test_leg_marker_severed_read_is_never_no_marker() -> None:
    # No ack line → present=False → UNKNOWN; the leg fails loud, never settles.
    problems = kd.marker_state_problems("slurm", "", RUN_ID, 0)
    assert problems and "absent/severed" in problems[0]
    assert kd.marker_wave0_job_id("slurm", "") is None


def test_leg_marker_missing_wave_id_file() -> None:
    marker = _marker_stdout(wave_line=None)  # pending marker, no wave-0 id-file
    problems = kd.marker_state_problems("slurm", marker, RUN_ID, 0)
    assert any("wave-0" in p and "id-file" in p for p in problems)


def test_leg_marker_wrong_token_or_state() -> None:
    problems = kd.marker_state_problems(
        "slurm", _marker_stdout(wave_line=None, token="other#0"), RUN_ID, 0
    )
    assert any("token" in p for p in problems)
    problems = kd.marker_state_problems(
        "slurm", _marker_stdout(wave_line=None, state="done"), RUN_ID, 0
    )
    assert any("state" in p for p in problems)


def _adopt_brief(**last_status: object) -> dict:
    base = {
        "verdict_reason": kd.ADOPT_VERDICT_REASON,
        "adopted_job_ids": ["12345"],
        "announce_crosscheck": "confirmed",
    }
    base.update(last_status)
    return {
        "run_id": RUN_ID,
        "lifecycle_state": "in_flight",
        "combined_waves": [],
        "failed_waves": [],
        "last_status": base,
    }


def test_leg_adopt_brief_passes_on_adoption() -> None:
    assert kd.adopt_brief_problems(_adopt_brief(), "12345") == []
    # Without an expected id, any non-empty adopted_job_ids passes.
    assert kd.adopt_brief_problems(_adopt_brief()) == []


def test_leg_adopt_brief_flags_never_dispatched_and_mismatch() -> None:
    # A safe-resubmit verdict is a drill FAILURE (a re-qsub was authorized).
    never = _adopt_brief(verdict_reason=kd.NEVER_DISPATCHED_VERDICT_REASON, adopted_job_ids=[])
    never["lifecycle_state"] = "abandoned"
    problems = kd.adopt_brief_problems(never, "12345")
    assert any("lifecycle_state" in p for p in problems)
    assert any("verdict_reason" in p for p in problems)
    assert any("adopted_job_ids" in p for p in problems)
    # Adopted the WRONG id (not the marker's).
    problems = kd.adopt_brief_problems(_adopt_brief(adopted_job_ids=["99999"]), "12345")
    assert any("99999" in p for p in problems)


def test_leg_in_flight_state() -> None:
    assert kd.in_flight_state_problems({"status": "in_flight", "job_ids": ["12345"]}, "12345") == []
    problems = kd.in_flight_state_problems({"status": "submitting", "job_ids": []}, "12345")
    assert any("status" in p for p in problems)
    problems = kd.in_flight_state_problems({"status": "in_flight", "job_ids": ["99999"]}, "12345")
    assert any("job_ids" in p for p in problems)


def test_leg_harvest_results_table() -> None:
    assert kd.harvest_problems({"results_table": [{"seed": 0, "pi_estimate": 3.14}]}) == []
    assert kd.harvest_problems({"results_table": []})
    assert kd.harvest_problems({})


def test_journal_leg_reads_a_canned_record_from_a_tmp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The journal RunRecord (submitting → in_flight) lives in the ephemeral
    # journal home; read it through the REAL state.journal seam over a tmp home
    # (never the production namespace) and feed it to the leg assertions.
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    exp = tmp_path / "exp"
    exp.mkdir()
    record = RunRecord(
        run_id="run-x",
        profile="slurm",
        cluster="adhoc-not-in-yaml",
        ssh_target="user@host",
        remote_path="/home/u/demo",
        job_name="job",
        job_ids=[],
        total_tasks=8,
        submitted_at="2026-01-01T00:00:00Z",
        experiment_dir=str(exp),
        status="submitting",
        attempt=0,
    )
    upsert_run(exp, record)
    got = kd.read_journal_record(exp, "run-x")
    assert got["status"] == "submitting"
    assert got["job_ids"] == []
    assert got["attempt"] == 0
    assert kd.submitting_state_problems(got) == []
    # An absent record reads as {} → the leg fails loud (never a silent pass).
    assert kd.read_journal_record(exp, "no-such-run") == {}
    assert kd.submitting_state_problems({})


# ── live ssh-failure catches (record-and-abort, never a traceback) ───────────
#
# ssh_run raises TimeoutError on a slow/severed channel (and SshCircuitOpen /
# OSError on the breaker/transport paths) — NEVER the driver's SandboxRefusal.
# The drill's three ssh_run-backed sites (the window poll + recovery legs 2/5)
# must fold those into the SAME record-and-abort evidence path: a failing row +
# a bounded abort, with no traceback escaping the attempt/driver function.


def _raise(error: BaseException) -> Callable[..., Any]:
    """A monkeypatch-ready stub raising *error* (the canned channel failure)."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise error

    return _boom


def test_channel_failure_set_covers_the_real_ssh_run_raise_surface() -> None:
    # The old except clauses caught ONLY SandboxRefusal — dead around ssh_run.
    from hpc_agent.errors import SshCircuitOpen

    errors = kd._channel_failure_errors()
    assert TimeoutError in errors  # a slow/severed channel (remote.py:825,866)
    assert SshCircuitOpen in errors  # the per-host breaker failing fast
    assert OSError in errors  # transport-layer failures (missing ssh binary, …)
    assert kd.SandboxRefusal in errors  # kept — run_cli_argv genuinely raises it


def test_wait_for_token_severed_channel_is_a_bounded_miss_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Site 1 (the window poll): a TimeoutError out of ssh_run folds into an
    # empty snapshot, the loop keeps polling inside the SAME budget, and
    # exhaustion returns None (the caller records the error row) — no raise.
    monkeypatch.setattr(
        kd,
        "query_token_snapshot",
        _raise(TimeoutError("ssh to slurmci timed out after 30s")),
    )
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd.wait_for_token(ctx, run_id=RUN_ID, token=TOKEN, budget_sec=1, interval_sec=1)
    assert outcome is None


def test_drive_one_attempt_severed_channel_is_an_error_outcome_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Site 1 end-to-end through the attempt driver: the severed channel must
    # surface as a bounded ``error`` WindowOutcome (the evidence the retry loop
    # records), never a traceback out of _drive_one_attempt. With BOTH the poll
    # and the cluster probe severed, the verdict is UNKNOWN — the drill must
    # NOT resurrect the old "never entered the scheduler" misread.
    monkeypatch.setattr(
        kd,
        "_drive_chain_to_s3_launch",
        lambda state, ctx, *, n_samples: (RUN_ID, str(tmp_path), "/remote/exp"),
    )
    monkeypatch.setattr(
        kd._driver,
        "read_detached_lease",
        lambda home, run_id, block: {
            "pid": 4321,
            "host": socket.gethostname(),
            "create_time": 1.5,
        },
    )
    monkeypatch.setattr(
        kd,
        "read_journal_record",
        lambda *a, **k: {"status": "submitting", "job_ids": [], "attempt": 0},
    )
    monkeypatch.setattr(
        kd,
        "query_token_snapshot",
        _raise(TimeoutError("ssh to slurmci timed out after 30s")),
    )
    # The cluster witness is severed too — the probe raises the same way.
    monkeypatch.setattr(
        kd,
        "probe_dispatch_evidence",
        _raise(TimeoutError("ssh to slurmci timed out after 30s")),
    )
    # Shrink the poll budget so the test does not sit out the live 180s window.
    real_wait = kd.wait_for_token
    monkeypatch.setattr(
        kd,
        "wait_for_token",
        lambda ctx, *, run_id, token: real_wait(
            ctx, run_id=run_id, token=token, budget_sec=1, interval_sec=1
        ),
    )
    state = kd._driver.ChainState()
    ctx = SimpleNamespace(journal_home=tmp_path, ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd._drive_one_attempt(state, ctx, n_samples=1, attempt_index=0, hit={})
    assert outcome.kind == "error"
    assert outcome.run_id == RUN_ID
    assert "UNKNOWN" in outcome.detail
    assert "never entered the scheduler" not in outcome.detail


@pytest.mark.parametrize(
    "channel_error",
    [
        TimeoutError("ssh to slurmci timed out after 30s"),
        OSError("ssh: connect to host slurmci port 22: Connection refused"),
    ],
    ids=["timeout", "oserror"],
)
def test_recovery_legs_channel_failure_records_rows_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, channel_error: BaseException
) -> None:
    # Sites 2 (leg 2, jobmap marker read) and 3 (leg 5, token snapshot): the
    # ssh_run raise surface must land as failing EVIDENCE ROWS (record-and-abort),
    # never a traceback out of _recovery_legs. Leg 3's SandboxRefusal (raised by
    # run_cli_argv for real) stays live and rides the same path — pinned here.
    monkeypatch.setattr(kd, "read_journal_record", lambda *a, **k: {})
    monkeypatch.setattr(kd, "read_jobmap_marker", _raise(channel_error))
    monkeypatch.setattr(
        kd, "run_cli_argv", _raise(SandboxRefusal("reconcile: CLI invocation failed (rc=2)"))
    )
    monkeypatch.setattr(kd, "query_token_snapshot", _raise(channel_error))
    # Leg 6's CLI/detached seams are stubbed so the hermetic test stops before
    # the watch/harvest leg (its own assertions are covered elsewhere).
    monkeypatch.setattr(kd._driver, "_step_cli", lambda *a, **k: None)
    monkeypatch.setattr(kd._driver, "_launch_block_detached", lambda *a, **k: None)

    state = kd._driver.ChainState()
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm", env={}, wait_timeout=1)
    hit = {
        "run_id": RUN_ID,
        "experiment_dir": tmp_path,
        "remote_path": "/remote/exp",
        "token": TOKEN,
        "attempt": 0,
    }
    kd._recovery_legs(state, ctx, hit)  # must NOT raise

    marker_rows = [r for r in state.rows if r["step"] == "recover.marker"]
    assert marker_rows[0]["pass"] is False
    assert str(channel_error) in marker_rows[0]["detail"]
    # The LIVE leg-3 SandboxRefusal path: run_cli_argv's refusal is recorded
    # as a failing row, never raised.
    adopt_rows = [r for r in state.rows if r["step"] == "recover.adopt"]
    assert adopt_rows[0]["pass"] is False
    assert "CLI invocation failed" in adopt_rows[0]["detail"]
    one_array_rows = [r for r in state.rows if r["step"] == "recover.one-array"]
    assert one_array_rows[0]["pass"] is False
    assert str(channel_error) in one_array_rows[0]["detail"]


# ── the sub-second jittered poll (the 2026-07-19 phase-lock fix) ─────────────


def test_poll_constants_are_sub_second_jittered_with_the_180s_budget() -> None:
    # The two halves of the run-29709733724 fix: a ≤0.5s MEAN poll with a
    # non-zero jitter (a fixed 2s cadence phase-locked against the ~10.6s
    # spawn→submit beat), and the budget unchanged.
    assert kd.WINDOW_POLL_INTERVAL_SEC <= 0.5
    assert 0 < kd.WINDOW_POLL_JITTER_FRAC < 0.5
    assert kd.WINDOW_POLL_BUDGET_SEC == 180


def test_jittered_poll_interval_mean_and_bounds() -> None:
    # Symmetric uniform jitter around the 0.5s base: every draw lands in
    # [0.4, 0.6] and the mean stays at ≤0.5s (±sampling noise, ~15σ slack).
    draws = [kd.jittered_poll_interval() for _ in range(2000)]
    assert all(0.4 <= d <= 0.6 for d in draws)
    mean = sum(draws) / len(draws)
    assert abs(mean - 0.5) < 0.02
    assert mean <= 0.52
    # Pinned draws: rand=0.0 → the floor, rand=1.0 → the ceiling.
    assert kd.jittered_poll_interval(rand=lambda: 0.0) == pytest.approx(0.4)
    assert kd.jittered_poll_interval(rand=lambda: 1.0) == pytest.approx(0.6)
    # Successive draws differ — the phase random-walks off any fixed cadence.
    assert len(set(draws)) > 100


def test_wait_for_token_sleeps_via_the_jittered_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The loop must route EVERY sleep through the jitter (a bare
    # ``time.sleep(interval)`` would re-open the phase-lock).
    bases: list[float] = []

    def fake_jitter(base: float = 0.5) -> float:
        bases.append(base)
        return 0.01

    monkeypatch.setattr(kd, "jittered_poll_interval", fake_jitter)
    monkeypatch.setattr(
        kd, "query_token_snapshot", lambda *a, **k: _slurm_snap("77777|unrelated#0")
    )
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd.wait_for_token(ctx, run_id=RUN_ID, token=TOKEN, budget_sec=1, interval_sec=0.5)
    assert outcome is None
    assert len(bases) >= 3
    assert all(b == 0.5 for b in bases)


def test_wait_for_token_returns_the_winning_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fast array IS reported when the poll lands inside its squeue lifetime.
    snaps = iter(["", "", _slurm_snap(f"12345_0|{TOKEN}", f"12345_1|{TOKEN}")])
    monkeypatch.setattr(
        kd,
        "query_token_snapshot",
        lambda *a, **k: next(snaps, _slurm_snap(f"12345_0|{TOKEN}")),
    )
    monkeypatch.setattr(kd, "jittered_poll_interval", lambda base=0.5: 0.0)
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd.wait_for_token(ctx, run_id=RUN_ID, token=TOKEN, budget_sec=5, interval_sec=0.5)
    assert outcome is not None and "12345" in outcome


# ── the dispatched-unseen witnesses (poll silence is NOT dispatch absence) ────


def _probe_stdout(
    *, wave_line: str | None, results: str, token: str = TOKEN, state: str = "pending"
) -> str:
    """Synthetic probe stdout: the jobmap read half + the results-count line."""
    return _marker_stdout(wave_line=wave_line, token=token, state=state) + (
        f"{kd._PROBE_RESULTS_LINE} {results}\n"
    )


def test_build_unseen_probe_shell_reads_marker_and_results_in_one_command() -> None:
    shell = kd.build_unseen_probe_shell(remote_path="/remote/exp", run_id=RUN_ID)
    # The jobmap half is the SAME ack-gated read reconcile performs…
    assert "__HPC_JOBMAP_ACK__" in shell
    assert f"{RUN_ID}.jobmap.*.id" in shell
    # …plus the results-dir witness, quoted, with the settled-absent branch.
    assert f"results/{RUN_ID}" in shell
    assert kd._PROBE_RESULTS_LINE in shell
    assert "absent" in shell


def test_classify_unseen_probe_dispatched_and_completed() -> None:
    # The run-29709733724 case: marker pending, wave-0 id at rc==0, results on
    # disk — the array entered, ran, and completed inside the poll gap.
    probe = kd.classify_unseen_probe(
        "slurm",
        _probe_stdout(
            wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 12345", results="8"
        ),
    )
    assert probe.kind == kd.PROBE_DISPATCHED
    assert probe.job_id == "12345"
    assert probe.results_tasks == 8


def test_classify_unseen_probe_dispatched_completion_unproven() -> None:
    # A wave-0 id with no results yet: dispatched is proven, completion is not.
    probe = kd.classify_unseen_probe(
        "slurm",
        _probe_stdout(
            wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 12345",
            results="absent",
        ),
    )
    assert probe.kind == kd.PROBE_DISPATCHED
    assert probe.job_id == "12345"
    assert probe.results_tasks is None


def test_classify_unseen_probe_never_dispatched_requires_the_ack() -> None:
    # Ack FIRED (the read is good) + no marker id-file + no results → the ONE
    # genuinely settle-able "never dispatched".
    probe = kd.classify_unseen_probe("slurm", _probe_stdout(wave_line=None, results="absent"))
    assert probe.kind == kd.PROBE_NEVER_DISPATCHED
    assert probe.job_id is None
    # An explicit zero count is absence too (results dir exists but is empty).
    probe_zero = kd.classify_unseen_probe("slurm", _probe_stdout(wave_line=None, results="0"))
    assert probe_zero.kind == kd.PROBE_NEVER_DISPATCHED


def test_classify_unseen_probe_failed_dispatch_never_entered() -> None:
    # rc!=0 on the wave-0 id-file is a confirmed FAILED dispatch (the Δ4 gate)
    # — with no results, the array genuinely never entered.
    probe = kd.classify_unseen_probe(
        "slurm",
        _probe_stdout(wave_line="__HPC_JOBMAP_WAVE__ wave-0 7 garbage-no-id", results="absent"),
    )
    assert probe.kind == kd.PROBE_NEVER_DISPATCHED


def test_classify_unseen_probe_severed_read_is_unknown_never_absence() -> None:
    # No ack → UNKNOWN: the sentinel-ack doctrine forbids settling a severed
    # read as "no marker" (the old error path's lie).
    assert kd.classify_unseen_probe("slurm", "").kind == kd.PROBE_UNKNOWN
    garbage = f"12345|{TOKEN}\n{kd._PROBE_RESULTS_LINE} 8\n"  # rows but NO ack
    assert kd.classify_unseen_probe("slurm", garbage).kind == kd.PROBE_UNKNOWN


def test_classify_unseen_probe_post_ack_truncation_is_unknown_never_settled() -> None:
    # The settle gate BEYOND the ack: the probe shell unconditionally emits
    # exactly ONE results line as its last act, so an ack-fired read with NO
    # results line was truncated mid-stream (after the ack, before the results
    # leg) — UNKNOWN, never a NEVER_DISPATCHED settled from a severed read.
    truncated = _marker_stdout(wave_line=None)  # ack + marker JSON, NO results line
    assert kd.classify_unseen_probe("slurm", truncated).kind == kd.PROBE_UNKNOWN
    # Same hole with a wave line present: still truncated, still UNKNOWN.
    truncated_wave = _marker_stdout(
        wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 12345"
    )
    assert kd.classify_unseen_probe("slurm", truncated_wave).kind == kd.PROBE_UNKNOWN


def test_probe_dispatch_evidence_refuses_a_nonzero_rc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The probe shell ends ``; true`` — rc!=0 means the probe never ran to
    # completion (a dead transport, e.g. ssh's 255); even stdout that WOULD
    # settle must not arbitrate the witnesses.
    import hpc_agent.infra.remote as remote

    settling = _probe_stdout(wave_line=None, results="absent")  # would settle NEVER_DISPATCHED
    monkeypatch.setattr(
        remote, "ssh_run", lambda *a, **k: SimpleNamespace(stdout=settling, returncode=255)
    )
    with pytest.raises(OSError, match="rc=255"):
        kd.probe_dispatch_evidence("hpcuser@slurmci", "/remote/exp", RUN_ID)


def test_classify_unseen_nonzero_probe_rc_is_unknown_not_a_settle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End-to-end: a nonzero-rc probe folds into the UNKNOWN error outcome even
    # when its partial stdout carries a settling shape.
    import hpc_agent.infra.remote as remote

    monkeypatch.setattr(
        kd,
        "read_journal_record",
        lambda *a, **k: {"status": "submitting", "job_ids": [], "attempt": 0},
    )
    monkeypatch.setattr(
        remote,
        "ssh_run",
        lambda *a, **k: SimpleNamespace(
            stdout=_probe_stdout(wave_line=None, results="absent"), returncode=255
        ),
    )
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd.classify_unseen(
        ctx, experiment_dir=tmp_path, remote_path="/remote/exp", run_id=RUN_ID, token=TOKEN
    )
    assert outcome.kind == "error"
    assert "UNKNOWN" in outcome.detail
    assert "never dispatched" not in outcome.detail


def test_classify_unseen_local_journal_witness_never_touches_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Witness 1: a promoted journal record proves dispatch LOCALLY — the
    # cluster probe must not even run.
    monkeypatch.setattr(
        kd,
        "read_journal_record",
        lambda *a, **k: {"status": "in_flight", "job_ids": ["12345"], "attempt": 0},
    )
    monkeypatch.setattr(kd, "probe_dispatch_evidence", _raise(AssertionError("ssh must not run")))
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd.classify_unseen(
        ctx, experiment_dir=tmp_path, remote_path="/remote/exp", run_id=RUN_ID, token=TOKEN
    )
    assert outcome.kind == kd.DISPATCHED_UNSEEN
    assert "12345" in outcome.detail
    assert "never entered the scheduler" not in outcome.detail


def test_classify_unseen_cluster_witness_dispatched_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Witness 2: empty journal (worker died pre-promote), but the cluster-side
    # marker + results prove the dispatch — the actual 3/3 CI case.
    monkeypatch.setattr(
        kd,
        "read_journal_record",
        lambda *a, **k: {"status": "submitting", "job_ids": [], "attempt": 0},
    )
    monkeypatch.setattr(
        kd,
        "probe_dispatch_evidence",
        lambda *a, **k: _probe_stdout(
            wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 12345", results="8"
        ),
    )
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd.classify_unseen(
        ctx, experiment_dir=tmp_path, remote_path="/remote/exp", run_id=RUN_ID, token=TOKEN
    )
    assert outcome.kind == kd.DISPATCHED_UNSEEN
    assert "dispatched + completed" in outcome.detail
    assert "12345" in outcome.detail
    assert "never entered the scheduler" not in outcome.detail


def test_classify_unseen_genuinely_absent_is_never_dispatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        kd,
        "read_journal_record",
        lambda *a, **k: {"status": "submitting", "job_ids": [], "attempt": 0},
    )
    monkeypatch.setattr(
        kd,
        "probe_dispatch_evidence",
        lambda *a, **k: _probe_stdout(wave_line=None, results="absent"),
    )
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd.classify_unseen(
        ctx, experiment_dir=tmp_path, remote_path="/remote/exp", run_id=RUN_ID, token=TOKEN
    )
    assert outcome.kind == "error"
    assert "never dispatched" in outcome.detail
    assert "never entered the scheduler" not in outcome.detail


def test_classify_unseen_severed_probe_is_unknown_not_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        kd,
        "read_journal_record",
        lambda *a, **k: {"status": "submitting", "job_ids": [], "attempt": 0},
    )
    monkeypatch.setattr(kd, "probe_dispatch_evidence", _raise(TimeoutError("ssh timed out")))
    ctx = SimpleNamespace(ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd.classify_unseen(
        ctx, experiment_dir=tmp_path, remote_path="/remote/exp", run_id=RUN_ID, token=TOKEN
    )
    assert outcome.kind == "error"
    assert "UNKNOWN" in outcome.detail
    assert "never entered the scheduler" not in outcome.detail
    assert "never dispatched" not in outcome.detail


def test_drive_one_attempt_fast_array_is_dispatched_unseen_never_misread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The full attempt path for the 3/3 CI case: the poll budget lapses (the
    # fake array's squeue lifetime sat inside the poll gap) and the cluster
    # witness proves dispatch + completion — the outcome MUST be the distinct
    # dispatched-unseen verdict, never "never entered".
    monkeypatch.setattr(
        kd,
        "_drive_chain_to_s3_launch",
        lambda state, ctx, *, n_samples: (RUN_ID, str(tmp_path), "/remote/exp"),
    )
    monkeypatch.setattr(
        kd._driver,
        "read_detached_lease",
        lambda home, run_id, block: {
            "pid": 4321,
            "host": socket.gethostname(),
            "create_time": 1.5,
        },
    )
    monkeypatch.setattr(
        kd,
        "read_journal_record",
        lambda *a, **k: {"status": "submitting", "job_ids": [], "attempt": 0},
    )
    monkeypatch.setattr(kd, "wait_for_token", lambda ctx, *, run_id, token: None)
    monkeypatch.setattr(
        kd,
        "probe_dispatch_evidence",
        lambda *a, **k: _probe_stdout(
            wave_line="__HPC_JOBMAP_WAVE__ wave-0 0 Submitted batch job 12345", results="8"
        ),
    )
    state = kd._driver.ChainState()
    ctx = SimpleNamespace(journal_home=tmp_path, ssh_target="hpcuser@slurmci", backend="slurm")
    outcome = kd._drive_one_attempt(state, ctx, n_samples=1, attempt_index=0, hit={})
    assert outcome.kind == kd.DISPATCHED_UNSEEN
    assert outcome.run_id == RUN_ID
    assert "never entered the scheduler" not in outcome.detail


# ── the RUNNER_TEMP evidence-dir contract (the CI upload path) ────────────────


def test_default_drill_workdir_honors_runner_temp(tmp_path: Path) -> None:
    got = kd.default_drill_workdir({"RUNNER_TEMP": str(tmp_path / "rt")})
    assert got == tmp_path / "rt" / "sandbox-evidence" / "kill-drill"


def test_default_drill_workdir_ignores_blank_runner_temp(tmp_path: Path) -> None:
    got = kd.default_drill_workdir({"RUNNER_TEMP": "  "})
    try:
        assert got.name.startswith("hpc-killdrill-")
        assert got.is_dir()
    finally:
        shutil.rmtree(got, ignore_errors=True)


def test_main_runner_temp_lands_evidence_under_sandbox_evidence_kill_drill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The workflow's upload step points at exactly
    # $RUNNER_TEMP/sandbox-evidence/kill-drill/ — a drill run with no explicit
    # --workdir must put its evidence there on Actions.
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "rt"))
    rc = kd.main(["--clusters-config", str(tmp_path / "absent.yaml")])
    assert rc == 1  # the setup refusal is recorded as evidence
    # main() resolves the workdir; mirror that before comparing (Windows temp
    # dirs can carry alias spellings).
    evidence_dir = (tmp_path / "rt" / "sandbox-evidence" / "kill-drill").resolve()
    evidence = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["verdict"] == "fail"
    assert (evidence_dir / "evidence.md").is_file()


def test_main_explicit_workdir_beats_runner_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "rt"))
    work = tmp_path / "explicit-work"
    rc = kd.main(["--clusters-config", str(tmp_path / "absent.yaml"), "--workdir", str(work)])
    assert rc == 1
    assert (work / "evidence.json").is_file()
    assert not (tmp_path / "rt" / "sandbox-evidence" / "kill-drill").exists()


# ── main() guard-first refusal order ─────────────────────────────────────────


def test_main_refuses_without_journal_env(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("HPC_JOURNAL_DIR", raising=False)
    assert kd.main([]) == 2
    assert "HPC_JOURNAL_DIR is unset" in capsys.readouterr().err


def test_main_refuses_bad_base_n_samples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    assert kd.main(["--base-n-samples", "0"]) == 2
    assert "--base-n-samples must be >= 1" in capsys.readouterr().err


def test_main_refuses_bad_max_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    assert kd.main(["--max-attempts", "0"]) == 2
    assert "--max-attempts must be >= 1" in capsys.readouterr().err


def test_main_refuses_without_cluster_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    # Hermeticity: on a GitHub runner RUNNER_TEMP is set and would retarget the
    # default workdir — pin it off so this test never writes outside tmp_path.
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    assert kd.main([]) == 2
    assert "--clusters-config" in capsys.readouterr().err


def test_main_refuses_missing_clusters_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.delenv("RUNNER_TEMP", raising=False)  # hermeticity (see above)
    # The chain records the setup refusal as evidence → exit 1 (not a guard 2).
    assert kd.main(["--clusters-config", str(tmp_path / "absent.yaml")]) == 1
