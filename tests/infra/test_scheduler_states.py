"""Backend scheduler-state parsing/classification (#157).

The post-submit ``verify-submitted`` verb needs more than the alive-check's
"still in the queue?" — it needs each job's *state* so it can flag an SGE
``Eqw`` (error) or a held job that a plain alive-check reports as merely
present. These cover the pure, scheduler-shape-only backend helpers.
"""

from __future__ import annotations

from hpc_agent.infra.backends import get_backend_class

SGE = get_backend_class("sge")
SLURM = get_backend_class("slurm")


def test_sge_build_scheduler_state_cmd() -> None:
    assert SGE.build_scheduler_state_cmd([]) == "true"
    cmd = SGE.build_scheduler_state_cmd(["123"])
    assert "qstat -u" in cmd and "|| true" in cmd


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
    assert "squeue" in cmd and "%T" in cmd and "|| true" in cmd


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
