"""Real-world scheduler stdout fed through the parsers.

Agent A (TEST safety net) — table-driven coverage of the messy stdout
the schedulers actually emit (warning-prefixed submit lines, multi-line
``qstat -u`` output with headers and an ``Eqw`` job, a preempted SLURM
job, empty output). Drives all three parse stages: job-id parse,
alive/state parse, and classify.

Resolved through ``get_backend_class(...)`` so the same fixtures will
exercise the profile-driven engine once the spine integrates it.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.backends import get_backend_class


def _slurm():
    return get_backend_class("slurm")


def _sge():
    return get_backend_class("sge")


# ---------------------------------------------------------------------------
# Job-id parse from submit stdout
# ---------------------------------------------------------------------------

_SLURM_SUBMIT_FIXTURES = [
    ("clean", "Submitted batch job 12345\n", "12345"),
    (
        "warning_prefix",
        "sbatch: warning: 30% of nodes pre-empt; Submitted batch job 12345\n",
        "12345",
    ),
    (
        "multiline_warnings",
        "sbatch: warning: can't honor --mem\nsbatch: info: account=foo\nSubmitted batch job 88\n",
        "88",
    ),
]

_SGE_SUBMIT_FIXTURES = [
    ("single", 'Your job 777 ("probe") has been submitted\n', "777"),
    (
        "array",
        'Your job-array 12345.1-10:1 ("probe") has been submitted\n',
        "12345",
    ),
]


@pytest.mark.parametrize(("label", "stdout", "expected"), _SLURM_SUBMIT_FIXTURES)
def test_slurm_job_id_parse(label, stdout, expected):
    m = _slurm().JOB_ID_REGEX.search(stdout)
    assert m is not None and m.group(1) == expected


@pytest.mark.parametrize(("label", "stdout", "expected"), _SGE_SUBMIT_FIXTURES)
def test_sge_job_id_parse(label, stdout, expected):
    m = _sge().JOB_ID_REGEX.search(stdout)
    assert m is not None and m.group(1) == expected


def test_empty_submit_output_does_not_match():
    assert _slurm().JOB_ID_REGEX.search("") is None
    assert _sge().JOB_ID_REGEX.search("") is None


# ---------------------------------------------------------------------------
# squeue alive + state output (SLURM), incl. a preempted job
# ---------------------------------------------------------------------------

# `squeue -h -o '%i'` — bare ids, one per line, array members suffixed.
_SQUEUE_ALIVE = "100_1\n100_2\n200\n"

# `squeue -h -o '%i %T'` with a preempted job in the mix.
_SQUEUE_STATES = "100 RUNNING\n200 PENDING\n300 PREEMPTED\n"


def test_slurm_alive_parse_with_array_members():
    cls = _slurm()
    assert cls.parse_alive_output(_SQUEUE_ALIVE, ["100", "200", "999"]) == {"100", "200"}


def test_slurm_states_then_classify_preempted_is_error():
    cls = _slurm()
    states = cls.parse_scheduler_states(_SQUEUE_STATES, ["100", "200", "300"])
    assert states == {"100": "RUNNING", "200": "PENDING", "300": "PREEMPTED"}
    classified = {jid: cls.classify_scheduler_state(s) for jid, s in states.items()}
    assert classified == {"100": "alive", "200": "alive", "300": "error"}


def test_slurm_empty_squeue_output():
    cls = _slurm()
    assert cls.parse_alive_output("", ["1", "2"]) == set()
    assert cls.parse_scheduler_states("", ["1", "2"]) == {}


# ---------------------------------------------------------------------------
# qstat -u $USER output (SGE): header rows + an Eqw job
# ---------------------------------------------------------------------------

_QSTAT_U = """job-ID  prior   name       user         state submit/start at     queue
-----------------------------------------------------------------------------
   100 0.50500 cpu_array  alice        r     05/30/2026 10:00:00 all.q@n1
   200 0.50500 cpu_array  alice        qw    05/30/2026 10:01:00
   300 0.50500 cpu_array  alice        Eqw   05/30/2026 10:02:00
   400 0.50500 cpu_array  alice        hqw   05/30/2026 10:03:00
"""


def test_sge_alive_parse_skips_headers():
    cls = _sge()
    assert cls.parse_alive_output(_QSTAT_U, ["100", "200", "300", "400"]) == {
        "100",
        "200",
        "300",
        "400",
    }


def test_sge_states_then_classify():
    cls = _sge()
    states = cls.parse_scheduler_states(_QSTAT_U, ["100", "200", "300", "400"])
    assert states == {"100": "r", "200": "qw", "300": "Eqw", "400": "hqw"}
    classified = {jid: cls.classify_scheduler_state(s) for jid, s in states.items()}
    assert classified == {
        "100": "alive",
        "200": "alive",
        "300": "error",
        "400": "held",
    }


def test_sge_state_parse_ignores_unrelated_jobs():
    cls = _sge()
    # Only ask about 300; the other rows must be dropped.
    assert cls.parse_scheduler_states(_QSTAT_U, ["300"]) == {"300": "Eqw"}


def test_sge_empty_qstat_output():
    cls = _sge()
    assert cls.parse_alive_output("", ["100"]) == set()
    assert cls.parse_scheduler_states("", ["100"]) == {}
