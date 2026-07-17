"""Boundary-pinning coverage for the reconcile orphan-recovery ladder + the
settle-arm harvest backstop (``ops/monitor/reconcile.py``).

This is safety-critical trust machinery: ``_recover_submitting`` decides whether
an orphaned ``submitting`` record is ADOPTED (its live cluster array reclaimed),
SAFE-RESUBMITTED (a fresh array minted), or LEFT-SUBMITTING (UNKNOWN). A wrong
adopt is a lost/duplicated array; a wrong resubmit duplicates a live array. The
U3-d/U3-e drills (``tests/ops/monitor/test_reconcile_submitting.py`` /
``tests/faultinject/test_submit_once.py``) assert the doctrine OUTCOME per rung.
This module adds the missing granularity: an assertion per DECISION BOUNDARY that
KILLS a surviving predicate/boundary mutant those doctrine tests would let pass —
each of the two adopt-gate conditions in ISOLATION, the exact-token keying that
stops a blind adopt of a present-but-wrong id, the two independent disjuncts of
the rung-2 Δ6 cross-check, and all three arms of ``_harvest_if_owed``.

Mirrors the landed ``tests/state/test_journal_coverage.py`` behaviour-pinning
style (each test names the mutant it kills). Cluster-free: the jobmap read, the
token query and the jobmap clear are routed through a patched ``remote.ssh_run``;
``read_announcements`` and ``harvest_on_terminal`` are monkeypatched.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent.infra.backends import get_backend_class
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.jobmap import parse_jobmap_read
from hpc_agent.ops.monitor import reconcile as R
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path, harvest_receipt_exists
from hpc_agent.state import run_record
from hpc_agent.state.journal import is_resubmittable_terminal, load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_ACK = "__HPC_JOBMAP_ACK__"
_SCHED_ACK = "__HPC_SCHED_ACK__=0"


# ── shared fixtures + builders (mirror test_reconcile_submitting.py) ───────────


@pytest.fixture
def exp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    e = tmp_path / "exp"
    e.mkdir()
    return e


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


def _proc(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _jobmap_stdout(*, token: str = "run-x#0", attempt: int = 0, wave_lines: list[str]) -> str:
    marker = f'{{"token":"{token}","state":"pending","attempt":{attempt},"waves":{{}}}}'
    return "\n".join([_ACK, marker, *wave_lines]) + "\n"


def _wave(wkey: str, rc: int, blob: str) -> str:
    return f"__HPC_JOBMAP_WAVE__ {wkey} {rc} {blob}".rstrip()


class _Router:
    """A patched ``ssh_run`` dispatching by command substring, recording calls."""

    def __init__(self, *, jobmap: object, token: object = None) -> None:
        self.jobmap = jobmap  # proc | Exception
        self.token = token  # proc | Exception for the squeue/qstat token query
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

    @property
    def reqsub_count(self) -> int:
        return sum(("sbatch" in c or "qsub " in c) for c in self.calls)

    @property
    def cleared_jobmap(self) -> bool:
        return any("rm -f" in c for c in self.calls)


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


def _parsed(*wave_lines: str) -> Any:
    return parse_jobmap_read(_jobmap_stdout(wave_lines=list(wave_lines)))


# ── A. the Δ4 adopt gate (_adoptable_wave_ids) — BOTH conditions, ISOLATED ─────
#
# A wave is adoptable ONLY when BOTH hold: recorded ``rc == 0`` AND the raw blob
# yields an id under ``JOB_ID_REGEX``. The U3-e drills conflate the two (the
# rc-nonzero drill also carries an empty/idless blob), so a mutant that drops the
# ``rc != 0`` guard survives them. These unit tests isolate each condition.


def test_adopt_gate_rc_zero_valid_id_adopts_and_extracts_group1() -> None:
    """rc==0 + a parseable blob → adopted, and the VALUE is the regex group(1)
    id, not the whole stdout blob. Kills ``match.group(1)`` → ``match.group(0)``
    (adopting the raw blob as a 'job id') and any inversion of the happy gate."""
    slurm = get_backend_class("slurm")
    out = R._adoptable_wave_ids(_parsed(_wave("wave-0", 0, "Submitted batch job 12345")), slurm)
    assert out == {"wave-0": "12345"}  # group(1), not "Submitted batch job 12345"


def test_adopt_gate_rc_nonzero_with_valid_id_is_rejected() -> None:
    """rc!=0 but a PERFECTLY parseable id blob → NOT adopted. Isolates the ``rc``
    condition: the only thing wrong is the rc, so this kills a mutant that removes
    or flips ``if rc != 0: continue`` (which the existing rc-nonzero drill — whose
    blob is also idless — does NOT, since its regex miss masks the rc gate)."""
    slurm = get_backend_class("slurm")
    out = R._adoptable_wave_ids(_parsed(_wave("wave-0", 1, "Submitted batch job 12345")), slurm)
    assert out == {}  # a confirmed failed dispatch is never adopted, even with an id


def test_adopt_gate_rc_zero_regex_miss_is_rejected() -> None:
    """rc==0 but the blob carries no parseable id → NOT adopted. Isolates the
    regex condition (rc is clean). Kills removal of the ``if match:`` guard (which
    would adopt a garbage/empty id or raise on ``match.group``)."""
    slurm = get_backend_class("slurm")
    out = R._adoptable_wave_ids(_parsed(_wave("wave-0", 0, "garbage-no-digits")), slurm)
    assert out == {}


def test_adopt_gate_mixed_waves_keeps_only_the_both_true_wave() -> None:
    """Three waves — (rc0,valid), (rc1,valid), (rc0,garbage) — yield ONLY the
    first. Pins the per-wave conjunction: dropping EITHER condition would let a
    second wave through, so this kills both gate mutants at once and pins the
    id-value mapping."""
    slurm = get_backend_class("slurm")
    out = R._adoptable_wave_ids(
        _parsed(
            _wave("wave-0", 0, "Submitted batch job 111"),
            _wave("wave-1", 1, "Submitted batch job 222"),
            _wave("wave-2", 0, "garbage-none"),
        ),
        slurm,
    )
    assert out == {"wave-0": "111"}


def test_adopt_promotes_sorted_deduped_ids(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two adoptable waves whose ids arrive high-then-low (222, 111) promote to a
    SORTED, de-duplicated ``job_ids``. Pins ``sorted(set(adoptable.values()))`` in
    the adopt rung — kills a mutant that drops ``sorted`` (order leak) or ``set``
    (duplicate ids)."""
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(
            0,
            _jobmap_stdout(
                wave_lines=[
                    _wave("wave-1", 0, "Submitted batch job 222"),
                    _wave("wave-0", 0, "Submitted batch job 111"),
                ]
            ),
        )
    )
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=True)
    out = _recover(exp)
    assert out.status == "in_flight"
    assert out.job_ids == ["111", "222"]  # sorted + unique, not ["222","111"]
    assert router.reqsub_count == 0  # adopt, never re-dispatch


# ── B. rung 1a adopt — Δ6 census cross-check is POSITIVE-ONLY ──────────────────
#
# The marker id is already positive evidence; the announce census is a
# best-effort confirmation that must NEVER un-adopt. Pins that a dir-absent or a
# severed census still leaves the run in_flight (the adopt stands).


def test_adopt_stands_when_census_dir_absent(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adopt succeeds; the Δ6 census reads dir-absent (present=False). The run
    STAYS ``in_flight`` (the marker id stands) and records the cross-check note.
    Kills a mutant that gates adoption on a present census — that would abandon a
    live array whose announce dir hasn't been read yet."""
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(
            0, _jobmap_stdout(wave_lines=[_wave("wave-0", 0, "Submitted batch job 12345")])
        )
    )
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=False)
    out = _recover(exp)
    assert out.status == "in_flight"
    assert out.job_ids == ["12345"]
    assert out.last_status.get("announce_crosscheck") == "dir-absent"


def test_adopt_stands_when_census_severed(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Δ6 census RAISES (severed). Adoption is unaffected — still ``in_flight``,
    crosscheck ``unavailable``. Kills a mutant that lets the census exception
    propagate (aborting the adopt) or un-adopt on failure."""
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(
            0, _jobmap_stdout(wave_lines=[_wave("wave-0", 0, "Submitted batch job 12345")])
        )
    )
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=ConnectionError("census severed"))
    out = _recover(exp)
    assert out.status == "in_flight"
    assert out.job_ids == ["12345"]
    assert out.last_status.get("announce_crosscheck") == "unavailable"


# ── C. rung 1b disambiguate (_disambiguate_by_token) — exact-token keying ──────
#
# The adopt-by-token lookup is keyed on the run's EXACT ``run_id#attempt`` token.
# The load-bearing safety property: a present-but-WRONG token is NOT adopted (that
# would reclaim another run's live array). And SEVERED (raise / no-ack) must be
# distinguished from a CLEAN MISS (never settle on absence).


def test_token_query_hit_adopts_the_exact_matching_id(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token map holds the run's token AND a decoy foreign token; the adopt
    picks the id bound to the run's OWN token (999), never the decoy (777). Kills
    a mutant that returns any/first map value instead of ``token_map.get(token)``."""
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_lines=[])),  # pending, no adoptable id
        token=_proc(0, "777|other-run#0\n999|run-x#0\n" + _SCHED_ACK + "\n"),
    )
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=True)
    out = _recover(exp)
    assert out.status == "in_flight"
    assert out.job_ids == ["999"]  # the run's token, not the decoy 777


def test_token_query_foreign_token_only_is_clean_miss_safe_resubmit(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token query RAN and acked, but the map holds ONLY a foreign token — the
    run's own token is absent. That is a CLEAN MISS → safe-resubmit (abandoned),
    NEVER a blind adopt of the present-but-wrong id (777). The highest-value
    boundary: kills ``token_map.get(token)`` → ``next(iter(token_map.values()),
    None)``, which would reclaim another run's live array."""
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_lines=[])),
        token=_proc(0, "777|other-run#0\n" + _SCHED_ACK + "\n"),
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "abandoned"
    assert out.job_ids == []  # never adopted the foreign 777
    assert out.last_status.get("verdict_reason") == "submit_once_never_dispatched_safe_resubmit"


def test_token_query_raises_stays_submitting(exp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The token query CHANNEL raises (severed mid-query). UNKNOWN → stay
    ``submitting``, never a settle. Kills removal of the ``except Exception`` arm
    in ``_disambiguate_by_token`` (the U3-e drills only exercise the rc-255 leg,
    not a raised transport error on the token query)."""
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_lines=[])),
        token=ConnectionError("token channel dropped"),
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "submitting"
    assert not router.cleared_jobmap  # UNKNOWN never clears the marker


def test_token_query_no_ack_is_unknown_not_a_miss(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc-0 output that VISIBLY carries the run's token but has NO scheduler ack →
    the query did not run to completion → UNKNOWN (stay submitting), NOT a miss and
    NOT an adopt of the visible id. Pins that the ack gate is checked BEFORE the
    token lookup — kills removal of the ``if not ran_ok`` guard, which would read a
    truncated channel as authoritative."""
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_lines=[])),
        token=_proc(0, "999|run-x#0\n"),  # token present but NO __HPC_SCHED_ACK__ line
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "submitting"  # UNKNOWN, not an adopt of 999, not a miss
    assert out.job_ids == []
    assert not router.cleared_jobmap


# ── D. rung 2 (_rung2_never_dispatched) — the Δ6 disjunction, each disjunct ─────
#
# Marker dir absent under a clean read → candidate "never dispatched". Δ6 refuses
# to trust that absence unless the announce census is ALSO absent:
#   stay-submitting if  announce.present  OR  announced > 0
# The U3-e drill sets BOTH true at once; these isolate each disjunct so a mutant
# that drops one is caught.


def test_rung2_census_present_announced_zero_refuses_resubmit(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker absent, census present=True but announced=0 → stay ``submitting``.
    Isolates the ``present`` disjunct (announced is 0). Kills dropping
    ``announce.get('present')`` from the Δ6 guard."""
    upsert_run(exp, _sub_record())
    router = _Router(jobmap=_proc(0, ""))  # rc0, no ack ⇒ submit dir absent
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=True, announced=0)
    out = _recover(exp)
    assert out.status == "submitting"
    assert not router.cleared_jobmap


def test_rung2_census_absent_but_tasks_announced_refuses_resubmit(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker absent, census present=False but announced=5 (a non-shared-FS split:
    the dispatcher started and tasks announced, yet the marker dir reads absent) →
    stay ``submitting``. Isolates the ``announced > 0`` disjunct. Kills dropping
    ``int(announce.get('announced',0)) > 0`` — that mutant would resubmit a
    demonstrably LIVE array."""
    upsert_run(exp, _sub_record())
    router = _Router(jobmap=_proc(0, ""))
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=False, announced=5)
    out = _recover(exp)
    assert out.status == "submitting"
    assert not router.cleared_jobmap


def test_rung2_both_absent_is_the_only_safe_resubmit(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ONLY marker-absent AND announce-absent AND announced==0 safe-resubmits
    (abandoned + jobmap cleared). Pins that the resubmit path requires the FULL
    conjunction of absences and carries the never-dispatched reason."""
    upsert_run(exp, _sub_record())
    router = _Router(jobmap=_proc(0, ""))
    _patch_ssh(monkeypatch, router)
    _patch_announce(monkeypatch, present=False, announced=0)
    out = _recover(exp)
    assert out.status == "abandoned"
    assert router.cleared_jobmap
    assert out.last_status.get("verdict_reason") == "submit_once_never_dispatched_safe_resubmit"


# ── E. safe-resubmit (_safe_resubmit) — transition + clear + attempt discipline ─


def test_safe_resubmit_preserves_attempt_for_next_submit_bump(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting from attempt=2, safe-resubmit transitions to a resubmittable
    terminal WITHOUT mutating ``attempt`` on the record — the +1 is minted by the
    NEXT submit (``allocate_attempt`` → 3). Uses a non-zero starting attempt so it
    kills an ``allocate_attempt`` mutant that always returns 1 (the existing drill
    starts at 0 and cannot distinguish +1 from a constant 1)."""
    from hpc_agent.ops.submit.runner import allocate_attempt

    upsert_run(exp, _sub_record(attempt=2))
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_lines=[])),
        token=_proc(0, _SCHED_ACK + "\n"),  # ran, token absent → clean miss
    )
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "abandoned"
    assert is_resubmittable_terminal(out)
    assert int(out.attempt) == 2  # the record's attempt is NOT bumped here
    assert allocate_attempt(out) == 3  # the next submit mints attempt+1


def test_safe_resubmit_clears_this_runs_jobmap_marker(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safe-resubmit transition issues an ``rm -f`` that names THIS run's
    jobmap (so a stale marker can never adopt onto the attempt+1 resubmit). Pins
    the ``_clear_jobmap`` fire targets the run_id."""
    upsert_run(exp, _sub_record())
    router = _Router(
        jobmap=_proc(0, _jobmap_stdout(wave_lines=[])),
        token=_proc(0, _SCHED_ACK + "\n"),
    )
    _patch_ssh(monkeypatch, router)
    _recover(exp)
    rm_calls = [c for c in router.calls if "rm -f" in c]
    assert rm_calls, "safe-resubmit must clear the jobmap"
    assert any("run-x" in c for c in rm_calls)


# ── F. rung 3 severed reads (_stay_submitting) — UNKNOWN, never a settle ───────


def test_jobmap_transport_rc_stays_submitting_with_reason(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A jobmap read transport failure (rc 255) → stay ``submitting`` with the
    recovery-UNKNOWN provenance, and NO jobmap clear. Kills a mutant that flips the
    ``proc.returncode != 0`` guard (treating a severed read as a clean 'no marker')
    and pins the exact ``verdict_reason``."""
    upsert_run(exp, _sub_record())
    router = _Router(jobmap=_proc(255, "", "connection reset"))
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "submitting"
    assert out.last_status.get("verdict_reason") == "recovery_unknown_recensus"
    assert "transport rc" in str(out.last_status.get("recovery_note", ""))
    assert not router.cleared_jobmap  # a severed read never settles/clears


def test_jobmap_read_raise_never_clears_or_settles(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RAISED jobmap read (channel exception) → stay ``submitting``; no clear, no
    re-qsub. Pins the ``except Exception`` arm returns the UNKNOWN posture rather
    than falling through to a settle."""
    upsert_run(exp, _sub_record())
    router = _Router(jobmap=ConnectionError("dropped"))
    _patch_ssh(monkeypatch, router)
    out = _recover(exp)
    assert out.status == "submitting"
    assert not router.cleared_jobmap
    assert router.reqsub_count == 0


# ── G. the settle-arm harvest backstop (_harvest_if_owed), all three arms ──────
#
# ``_harvest_if_owed`` fires ``harvest_on_terminal`` when the verdict TRANSITIONED
# (terminal_cause != pre_reconcile_status) OR — absent a transition — as a
# journal-evidence backstop when the run is terminal with NO harvest receipt. The
# harvest-backstop suite drives this through ``reconcile``; these pin the helper's
# own three branches directly, isolating the boundary each arm turns on.


def _count_harvests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``harvest_on_terminal`` with a counting fake that writes a REAL
    receipt (mirroring the guard's last step) so the receipt-derived idempotency
    is exercised through the true ledger."""
    causes: list[str] = []

    def _fake(
        experiment_dir: Path,
        run_id: str,
        *,
        terminal_cause: str,
        record: Any | None = None,
    ) -> dict[str, Any]:
        causes.append(terminal_cause)
        append_jsonl_line(
            harvest_marker_path(experiment_dir, run_id),
            {"run_id": run_id, "terminal_cause": terminal_cause, "harvest_ok": True},
        )
        return {"run_id": run_id, "terminal_cause": terminal_cause, "harvest_ok": True}

    monkeypatch.setattr(R, "harvest_on_terminal", _fake)
    return causes


def test_harvest_if_owed_transition_fires_even_with_receipt(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict TRANSITION (pre=complete → cause=failed, a legit downgrade) fires
    the harvest UNCONDITIONALLY — even though a prior 'complete' receipt is already
    on the ledger. Pins that the transition gate is checked FIRST and never
    receipt-gated: kills ``!=`` → ``==`` and kills folding the two gates into
    ``terminal_cause != pre AND not receipt_exists`` (which would drop this fire).
    Also pins the fired cause is ``terminal_cause`` (failed), not the prior."""
    append_jsonl_line(
        harvest_marker_path(exp, "r-downgrade"),
        {"run_id": "r-downgrade", "terminal_cause": "complete", "harvest_ok": True},
    )
    assert harvest_receipt_exists(exp, "r-downgrade")
    causes = _count_harvests(monkeypatch)
    R._harvest_if_owed(
        exp,
        "r-downgrade",
        terminal_cause="failed",
        record=None,  # type: ignore[arg-type]
        pre_reconcile_status="complete",
    )
    assert causes == ["failed"]


def test_harvest_if_owed_no_transition_no_receipt_backstops(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No transition (pre==cause==complete) AND no receipt on the ledger → the
    backstop fires exactly once (the mark→harvest death gap). Kills removal of the
    ``if not harvest_receipt_exists(...)`` backstop arm — without it a dropped
    guaranteed harvest is lost forever."""
    causes = _count_harvests(monkeypatch)
    assert not harvest_receipt_exists(exp, "r-gap")
    R._harvest_if_owed(
        exp,
        "r-gap",
        terminal_cause="complete",
        record=None,  # type: ignore[arg-type]
        pre_reconcile_status="complete",
    )
    assert causes == ["complete"]
    assert harvest_receipt_exists(exp, "r-gap")


def test_harvest_if_owed_no_transition_with_receipt_no_fire(
    exp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No transition AND a receipt already present → NO re-fire (idempotent both
    ways; each fire pays an rsync pull + reduce + append). Kills inverting the
    backstop predicate (``if not receipt`` → ``if receipt``)."""
    append_jsonl_line(
        harvest_marker_path(exp, "r-done"),
        {"run_id": "r-done", "terminal_cause": "complete", "harvest_ok": True},
    )
    causes = _count_harvests(monkeypatch)
    R._harvest_if_owed(
        exp,
        "r-done",
        terminal_cause="complete",
        record=None,  # type: ignore[arg-type]
        pre_reconcile_status="complete",
    )
    assert causes == []
