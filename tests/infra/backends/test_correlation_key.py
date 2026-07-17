"""U3-c — the run+attempt correlation key (submit-once Δ2 / OPEN-1(i)).

The token ``run_id#attempt`` rides a scheduler CONTEXT/COMMENT field — Slurm
``--comment``, SGE ``-ac HPC_TOKEN=…`` — NEVER ``job_name``. Pins:

* the flag emission per family + the flag-OFF byte-identity regression pin (the
  same discipline the jobmap marker weave carries in test_jobmap_weave);
* the rung-1b query command shape + the parse round-trip (token → base job id),
  including the ack-gated UNKNOWN posture reused verbatim from the liveness query.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.backends import get_backend_class
from hpc_agent.infra.backends.sge import SGEBackend
from hpc_agent.infra.backends.slurm import SlurmBackend


def _backend(family: str, tmp_path):
    if family == "slurm":
        return SlurmBackend(script=str(tmp_path / "j.slurm"), log_dir=str(tmp_path / "logs"))
    return SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))


_RUN_ENV = {"HPC_RUN_ID": "pi-train-d363e2a3", "HPC_SUBMIT_ATTEMPT": "2"}


# ── flag emission (the argv fragment) ─────────────────────────────────────────


def test_slurm_correlation_flags_are_comment() -> None:
    assert SlurmBackend.build_correlation_flags("pi-train-d363e2a3", 2) == [
        "--comment",
        "pi-train-d363e2a3#2",
    ]


def test_sge_correlation_flags_are_ac_context() -> None:
    assert SGEBackend.build_correlation_flags("pi-train-d363e2a3", 2) == [
        "-ac",
        "HPC_TOKEN=pi-train-d363e2a3#2",
    ]


def test_pbs_family_carries_no_correlation_flag() -> None:
    # PBS has no clean submit-time comment field — degrade to marker-only
    # recovery (never a duplicate), so the fragment is empty.
    for fam in ("pbspro", "torque"):
        assert get_backend_class(fam).build_correlation_flags("r", 0) == []


# ── the weave into _build_command: byte-identity OFF, injected ON ─────────────


@pytest.mark.parametrize(
    ("family", "flag"),
    [("slurm", "--comment"), ("sge", "-ac")],
)
def test_build_command_off_is_byte_identical(
    family: str, flag: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = _backend(family, tmp_path)
    # Same job_env, flag OFF vs ON: OFF carries NO correlation flag; ON splices
    # exactly the correlation fragment in before the script and nothing else
    # changes (the byte-identity regression pin — job_env is otherwise identical
    # so --export/-v content is the same on both).
    monkeypatch.delenv("HPC_SUBMIT_ONCE", raising=False)
    off = b._build_command("1-4", "job", dict(_RUN_ENV))
    assert flag not in off
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    on = b._build_command("1-4", "job", dict(_RUN_ENV))
    frag = type(b).build_correlation_flags("pi-train-d363e2a3", 2)
    assert on == [*off[:-1], *frag, off[-1]]


@pytest.mark.parametrize(
    ("family", "flag"),
    [("slurm", "--comment"), ("sge", "-ac")],
)
def test_build_command_on_injects_before_script(
    family: str, flag: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    b = _backend(family, tmp_path)
    cmd = b._build_command("1-4", "job", dict(_RUN_ENV))
    assert flag in cmd
    # The token carries run_id#attempt (attempt from HPC_SUBMIT_ATTEMPT).
    joined = " ".join(cmd)
    assert "pi-train-d363e2a3#2" in joined
    # Injected BEFORE the script (last arg) so the qsub grammar is undisturbed.
    assert cmd[-1] == b.script
    assert cmd.index(flag) < len(cmd) - 1
    # The token is NEVER in the job name (the -N/--job-name value stays "job").
    assert "pi-train-d363e2a3#2" not in "job"


def test_build_command_on_without_run_id_stays_identical(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_SUBMIT_ONCE", "1")
    b = _backend("slurm", tmp_path)
    assert "--comment" not in b._build_command("1-4", "job", {})


# ── rung-1b query command + parse round-trip ──────────────────────────────────


def test_slurm_token_query_cmd_reads_comment_column() -> None:
    cmd = SlurmBackend.build_token_query_cmd()
    assert "squeue" in cmd and "%k" in cmd  # %k = comment column
    assert '-u "$USER"' in cmd
    # Ack-wrapped (positive-evidence): a severed query reads UNKNOWN, not empty.
    assert "__HPC_SCHED_ACK__=" in cmd


def test_sge_token_query_cmd_dumps_qstat_j_context() -> None:
    cmd = SGEBackend.build_token_query_cmd()
    assert "qstat -u" in cmd and "qstat -j" in cmd  # context lives in -j detail
    assert "__HPC_SCHED_ACK__=" in cmd


def test_pbs_token_query_is_noop() -> None:
    cmd = get_backend_class("pbspro").build_token_query_cmd()
    assert "true" in cmd  # no token field on PBS


def test_slurm_parse_token_query_maps_token_to_base_id() -> None:
    stdout = (
        "12345_1|pi-train-d363e2a3#2\n"
        "12345_2|pi-train-d363e2a3#2\n"  # same array, same token → base 12345
        "67890|other-run#0\n"
        "\n"  # blank / header noise ignored
    )
    got = SlurmBackend.parse_token_query(stdout)
    assert got["pi-train-d363e2a3#2"] == "12345"
    assert got["other-run#0"] == "67890"


def test_sge_parse_token_query_reads_context_line() -> None:
    stdout = (
        "==============================================================\n"
        "job_number:                 987654\n"
        "owner:                      jc_905\n"
        "context:                    HPC_TOKEN=pi-train-d363e2a3#2,foo=bar\n"
        "==============================================================\n"
        "job_number:                 987655\n"
        "context:                    HPC_TOKEN=other-run#1\n"
    )
    got = SGEBackend.parse_token_query(stdout)
    assert got == {"pi-train-d363e2a3#2": "987654", "other-run#1": "987655"}


def test_parse_token_query_ignores_jobs_without_our_key() -> None:
    # A job carrying some OTHER context but not HPC_TOKEN contributes nothing.
    stdout = "job_number:  111\ncontext:  NABLA=1,other=x\n"
    assert SGEBackend.parse_token_query(stdout) == {}
