"""Backend scheduler-state parsing/classification (#157).

The post-submit ``verify-submitted`` verb needs more than the alive-check's
"still in the queue?" — it needs each job's *state* so it can flag an SGE
``Eqw`` (error) or a held job that a plain alive-check reports as merely
present. These cover the pure, scheduler-shape-only backend helpers.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.backends import get_backend_class

SGE = get_backend_class("sge")
SLURM = get_backend_class("slurm")
PBSPRO = get_backend_class("pbspro")
TORQUE = get_backend_class("torque")


def test_sge_build_scheduler_state_cmd() -> None:
    assert SGE.build_scheduler_state_cmd([]) == "true"
    cmd = SGE.build_scheduler_state_cmd(["123"])
    # Sentinel-ack (positive-evidence rule): the query ends by echoing an
    # affirmative token with the scheduler command's rc, replacing ``|| true``.
    assert "qstat -u" in cmd and "|| true" not in cmd
    assert "__HPC_SCHED_ACK__=$?" in cmd


def test_sge_parse_scheduler_states_picks_state_column() -> None:
    out = (
        "job-ID  prior   name    user   state submit/start at      queue\n"
        "-----------------------------------------------------------------\n"
        "12345 0.50500 myjob   alice  Eqw   05/28/2026 12:00:00  all.q\n"
        "12346 0.50500 myjob   alice  r     05/28/2026 12:00:00  all.q\n"
        "12347 0.50500 myjob   alice  qw    05/28/2026 12:00:00\n"
    )
    states = SGE.parse_scheduler_states(out, ["12345", "12346", "12347", "99999"])
    # 99999 is absent from the output → omitted (caller treats as gone).
    assert states == {"12345": "Eqw", "12346": "r", "12347": "qw"}


def test_sge_classify_scheduler_state() -> None:
    assert SGE.classify_scheduler_state("Eqw") == "error"
    assert SGE.classify_scheduler_state("Er") == "error"
    assert SGE.classify_scheduler_state("hqw") == "held"
    assert SGE.classify_scheduler_state("r") == "alive"
    assert SGE.classify_scheduler_state("qw") == "alive"
    assert SGE.classify_scheduler_state("t") == "alive"


def test_slurm_build_scheduler_state_cmd() -> None:
    assert SLURM.build_scheduler_state_cmd([]) == "true"
    cmd = SLURM.build_scheduler_state_cmd(["12345", "12346"])
    assert "squeue" in cmd and "%T" in cmd and "|| true" not in cmd
    assert "__HPC_SCHED_ACK__=$?" in cmd


def test_scheduler_query_ran_positive_evidence() -> None:
    """The sentinel-ack transport verdict: presence of the ack proves the query
    RAN; absence is UNKNOWN, never 'no jobs'. SGE/PBS additionally require rc 0
    (qstat exits 0 on an empty queue); SLURM accepts any rc (squeue exits
    non-zero once queried ids have left the queue)."""
    # Ack present, rc 0: ran, and the ack line is stripped from the body (the
    # surrounding line structure — incl. the row's own trailing newline — is
    # preserved, since parse_scheduler_states/parse_alive_output splitline it).
    clean, ok = SGE.scheduler_query_ran("12345 r\n__HPC_SCHED_ACK__=0\n")
    assert ok is True and "__HPC_SCHED_ACK__" not in clean and clean == "12345 r\n"
    # SGE with a non-zero ack = qstat itself failed → UNKNOWN.
    _, ok = SGE.scheduler_query_ran("__HPC_SCHED_ACK__=1\n")
    assert ok is False
    # No ack at all (silent/truncated read) = UNKNOWN for every family.
    _, ok = SGE.scheduler_query_ran("")
    assert ok is False
    _, ok = SLURM.scheduler_query_ran("")
    assert ok is False
    # SLURM: ack present but non-zero rc (all jobs left the queue) still ran.
    clean, ok = SLURM.scheduler_query_ran("__HPC_SCHED_ACK__=1\n")
    assert ok is True and clean == ""


@pytest.mark.parametrize("cls", [PBSPRO, TORQUE])
def test_pbs_scheduler_query_ran_treats_nonzero_rc_as_ran(cls) -> None:
    """#5: PBS queries EXPLICIT ids (``qstat -t <id>``) and qstat exits non-zero
    once a queried id has left the queue ('Unknown Job Id' / 'job has finished').
    That non-zero rc is the EXPECTED result of a finished job, not a binary
    failure — so, like SLURM, ack PRESENCE proves the query ran and the kept
    rows stay parseable. The old SGE rc==0 rule pinned every finished PBS run at
    UNKNOWN forever."""
    # A still-running row plus a non-zero rc (some ids already gone):
    clean, ok = cls.scheduler_query_ran("123.svr R workq\n__HPC_SCHED_ACK__=153\n")
    assert ok is True
    assert clean == "123.svr R workq\n" and "__HPC_SCHED_ACK__" not in clean
    # Every job finished: empty body + non-zero rc still counts as 'ran'.
    _, ok = cls.scheduler_query_ran("__HPC_SCHED_ACK__=35\n")
    assert ok is True
    # No ack at all = silent/truncated channel → UNKNOWN, never 'all terminal'.
    _, ok = cls.scheduler_query_ran("")
    assert ok is False


@pytest.mark.parametrize("cls", [PBSPRO, TORQUE, SLURM])
def test_explicit_id_query_missing_binary_rc_is_unknown(cls) -> None:
    """#F35 fire path: rc 126/127 (binary not found / not executable) is the
    shell's OWN 'the scheduler command never ran' code — a missing ``squeue`` /
    ``qstat`` on a non-login shell, or a down-daemon that never launched — and
    must read UNKNOWN, not 'queue empty'. A finished/absent id never yields
    126/127 (its rc is 1/35/153), so the finished-id rule (#5) is untouched:
    those still count as 'ran'."""
    # Missing / non-executable binary + empty stdout → UNKNOWN (guard fires).
    for rc in (126, 127):
        _, ok = cls.scheduler_query_ran(f"__HPC_SCHED_ACK__={rc}\n")
        assert ok is False, f"rc {rc} must read UNKNOWN for {cls.scheduler_name}"
    # Regression: the finished-id rcs the #5 fix depends on stay 'ran'.
    for rc in (1, 35, 153):
        _, ok = cls.scheduler_query_ran(f"__HPC_SCHED_ACK__={rc}\n")
        assert ok is True, f"finished-id rc {rc} must stay 'ran' for {cls.scheduler_name}"


def test_slurm_builders_thread_federated_cluster_M_flag() -> None:
    """#F37: the liveness / state / cancel builders emit ``-M <cluster>`` for a
    federated SLURM job (the ``sbatch --clusters=`` member), so the probe/kill
    reach the cluster the job actually lives on. Default (no cluster) stays
    byte-identical — the non-federated path is unchanged."""
    ack = '; echo "__HPC_SCHED_ACK__=$?"'
    # Threaded cluster → -M appears; the rest of the command is unchanged.
    assert (
        SLURM.build_alive_check_cmd(["1", "2"], cluster="gpu")
        == "squeue -M gpu -j 1,2 -h -o '%i' 2>/dev/null" + ack
    )
    assert (
        SLURM.build_scheduler_state_cmd(["1"], cluster="gpu")
        == "squeue -M gpu -j 1 -h -o '%i %T' 2>/dev/null" + ack
    )
    assert SLURM.build_cancel_cmd(["1", "2"], cluster="gpu") == "scancel -M gpu 1 2"
    # Default (no federation) is byte-identical to the pre-#F37 command.
    assert SLURM.build_alive_check_cmd(["1", "2"]) == "squeue -j 1,2 -h -o '%i' 2>/dev/null" + ack
    assert SLURM.build_cancel_cmd(["1", "2"]) == "scancel 1 2"
    # PBS/SGE ignore the kwarg (no cross-cluster routing here) — still no -M.
    assert "-M" not in PBSPRO.build_alive_check_cmd(["12345"], cluster="gpu")
    assert "-M" not in SGE.build_cancel_cmd(["1"], cluster="gpu")


def test_sge_classify_suspended_states_are_held() -> None:
    """#F40: SGE SUSPENDED tokens (routine under subordinate-queue preemption)
    are not progressing — bucket held (→ PENDING via batch_status), matching the
    SLURM branch's SUSPENDED/STOPPED -> held. Lowercase 't' (transferring, a
    RUNNING substate) must stay alive."""
    from hpc_agent._kernel.contract.vocabulary import TaskStatus

    for tok in ("s", "S", "T", "ts", "tS"):
        assert SGE.classify_scheduler_state(tok) == "held", tok
    # 't' (transferring) and 'r' (running) stay alive — the fix must not
    # over-capture the running substates.
    assert SGE.classify_scheduler_state("t") == "alive"
    assert SGE.classify_scheduler_state("r") == "alive"
    # End-to-end through batch_status: a suspended task reports PENDING (waiting),
    # not RUNNING (the mislabel that hid a stalled task all campaign).
    assert SGE.batch_status({"12345": "s"}) == {"12345": TaskStatus.PENDING.value}
    assert SGE.batch_status({"12345": "r"}) == {"12345": TaskStatus.RUNNING.value}


def test_slurm_parse_scheduler_states() -> None:
    out = "12345 RUNNING\n12346 PENDING\n12347 FAILED\n"
    states = SLURM.parse_scheduler_states(out, ["12345", "12346", "12347", "88"])
    assert states == {"12345": "RUNNING", "12346": "PENDING", "12347": "FAILED"}


def test_slurm_parse_scheduler_states_array_suffix() -> None:
    # squeue may print array task ids like 12345_3; the base id is matched.
    out = "12345_0 RUNNING\n12345_1 PENDING\n"
    states = SLURM.parse_scheduler_states(out, ["12345"])
    assert states == {"12345": "PENDING"}  # last line wins; both map to base id


def test_slurm_classify_scheduler_state() -> None:
    assert SLURM.classify_scheduler_state("FAILED") == "error"
    assert SLURM.classify_scheduler_state("NODE_FAIL") == "error"
    assert SLURM.classify_scheduler_state("OUT_OF_MEMORY") == "error"
    assert SLURM.classify_scheduler_state("RUNNING") == "alive"
    assert SLURM.classify_scheduler_state("PENDING") == "alive"
