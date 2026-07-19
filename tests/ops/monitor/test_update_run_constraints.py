"""Tests for ``ops.monitor.update_constraints``.

The primitive is SSH-bound; tests mock ``ssh_run`` at the OS boundary
(``infra.remote.subprocess.run``-equivalent), keeping the function
under test unchanged. Real cluster integration is out of scope for
unit tests.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.update_run_constraints import (
    UpdateRunConstraintsSpec,
)
from hpc_agent.ops.monitor.update_constraints import update_run_constraints
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


_RUN_ID = "20260101-000000-aaaaaaa"


def _seed_sidecar(tmp_path: Path, *, job_ids: list[str], features: list[str] | None = None) -> None:
    write_run_sidecar(
        tmp_path,
        run_id=_RUN_ID,
        cmd_sha="a" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 .hpc/_hpc_dispatch.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
        job_ids=job_ids,
        constraints={"features": features} if features else None,
    )
    # ssh_target is NOT a v2 sidecar field — see _V2_CONFIG_FIELDS in
    # state/runs.py. The primitive resolves ssh_target from the journal
    # RunRecord, which submit_flow writes alongside the sidecar.
    upsert_run(
        tmp_path,
        RunRecord(
            run_id=_RUN_ID,
            profile="p",
            cluster="c",
            ssh_target="alice@cluster",
            remote_path="/remote",
            job_name="j",
            job_ids=list(job_ids),
            total_tasks=4,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(tmp_path.resolve()),
        ),
    )


def _ok_cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# The batched update collapses N per-job ``scontrol update`` calls into ONE
# login-shell round-trip; per-id outcomes ride the per-segment ack echoes
# (``__HPC_UPD__ <job_id> <rc>``), and the whole batch is wrapped in the
# canonical batch-level sentinel ack (``__HPC_UPD_BATCH__=<rc>`` trailing) that
# proves the batch shell ran to completion. The fake transport must therefore
# return the ack lines the real cluster shell would emit for each composed
# segment PLUS the trailing batch ack.
def _acks(*pairs: tuple[str, int]) -> str:
    """Build the fused command's stdout: one ``__HPC_UPD__ <id> <rc>`` per id,
    then the trailing batch-level ack (a completed batch shell)."""
    segs = "".join(f"__HPC_UPD__ {jid} {rc}\n" for jid, rc in pairs)
    return f"{segs}__HPC_UPD_BATCH__=0\n"


# ─── happy paths ──────────────────────────────────────────────────────


def test_set_features_batches_all_jobs_into_one_ssh_exec(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["12345", "12346"])
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_ok_cp(stdout=_acks(("12345", 0), ("12346", 0))),
    ) as mock_ssh:
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100", "l40s"]),
        )

    assert out.job_ids_updated == ["12345", "12346"]
    assert out.job_ids_failed == []
    assert out.new_features == ["a100", "l40s"]
    # The latency fix: N job ids collapse into ONE ssh round-trip, not N.
    assert mock_ssh.call_count == 1
    cmd = mock_ssh.call_args_list[0].args[0]
    # Fused command runs inside a single login shell so scontrol resolves off
    # the cluster's login PATH.
    assert cmd.startswith("bash -lc ")
    # One ``scontrol update`` segment per job id, each with its own ack echo.
    assert "scontrol update jobid=12345" in cmd
    assert "scontrol update jobid=12346" in cmd
    assert "__HPC_UPD__ 12345 $?" in cmd
    assert "__HPC_UPD__ 12346 $?" in cmd
    # Segments are ``;``-joined (never ``&&``): an early failure must not skip a
    # later id's update.
    assert " && scontrol" not in cmd
    # The ``|`` feature separator is a shell metacharacter, so the Features
    # expression is shell-quoted before it reaches scontrol.
    assert "a100|l40s" in cmd


def test_add_features_extends_existing_set(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["1"], features=["a100"])
    with patch("hpc_agent.infra.remote.ssh_run", return_value=_ok_cp(stdout=_acks(("1", 0)))):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, add_features=["l40s"]),
        )
    assert out.new_features == ["a100", "l40s"]


def test_add_features_dedupes(tmp_path: Path) -> None:
    """add_features=['a100'] when a100 already exists is a no-op (no
    duplicate in the new set), but the scontrol still runs."""
    _seed_sidecar(tmp_path, job_ids=["1"], features=["a100"])
    with patch("hpc_agent.infra.remote.ssh_run", return_value=_ok_cp(stdout=_acks(("1", 0)))):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, add_features=["a100"]),
        )
    assert out.new_features == ["a100"]


def test_sidecar_features_persisted_on_success(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["1"], features=["a100"])
    with patch("hpc_agent.infra.remote.ssh_run", return_value=_ok_cp(stdout=_acks(("1", 0)))):
        update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["v100"]),
        )
    sidecar = json.loads((tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json").read_text())
    assert sidecar["constraints"]["features"] == ["v100"]


# ─── failure paths ─────────────────────────────────────────────────────


def test_partial_failure_reports_per_job(tmp_path: Path) -> None:
    """One id's scontrol fails, the other succeeds — parsed from the per-segment
    acks of the SINGLE batched exec. A partially-failed batch must not report
    full success; the sidecar is updated when at least one job succeeded."""
    _seed_sidecar(tmp_path, job_ids=["1", "2"])
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_ok_cp(stdout=_acks(("1", 0), ("2", 1))),
    ) as mock_ssh:
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert mock_ssh.call_count == 1
    assert out.job_ids_updated == ["1"]
    assert out.job_ids_failed == ["2"]


def test_missing_ack_is_unknown_never_ok(tmp_path: Path) -> None:
    """An id whose ack line never came back (truncated / killed mid-batch) is
    UNKNOWN — reported failed, NEVER assumed ok. Here id 2's ack is absent."""
    _seed_sidecar(tmp_path, job_ids=["1", "2"])
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_ok_cp(stdout=_acks(("1", 0))),  # no line for "2"
    ):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert out.job_ids_updated == ["1"]
    assert out.job_ids_failed == ["2"]


def test_sentinel_ack_idiom_lockstep_with_scheduler_ack() -> None:
    """The per-segment ack (``_UPD_ACK_PREFIX``) shares the scheduler
    sentinel-ack idiom (``_engine._SCHED_ACK_PREFIX``) — the MIRROR pin,
    asserted on BOTH sides so a drift in either fails here: the ack's
    PRESENCE is the affirmative proof the remote segment ran; its ABSENCE is
    UNKNOWN, never "ok"."""
    from hpc_agent.infra.backends._engine import _SCHED_ACK_PREFIX
    from hpc_agent.infra.backends.slurm import SlurmBackend
    from hpc_agent.ops.monitor.update_constraints import _UPD_ACK_PREFIX, _parse_upd_acks

    # Engine side: a query stdout lacking the sentinel ack is UNKNOWN
    # (ran_ok=False), never "the query ran"; the ack-carried rc 0 runs ok.
    assert SlurmBackend.scheduler_query_ran("1|RUNNING\n") == ("1|RUNNING\n", False)
    assert SlurmBackend.scheduler_query_ran(f"1|RUNNING\n{_SCHED_ACK_PREFIX}0\n") == (
        "1|RUNNING\n",
        True,
    )

    # Update side: a per-segment rc-0 ack is the ONLY success signal; an id
    # whose ack line never came back is ABSENT (UNKNOWN), and a non-integer
    # rc token can never masquerade as a successful rc 0.
    acks = _parse_upd_acks(f"{_UPD_ACK_PREFIX} 1 0\n{_UPD_ACK_PREFIX} 2 nope\n")
    assert acks == {"1": 0}


def test_all_acks_missing_reports_no_success(tmp_path: Path) -> None:
    """A batch whose stdout carried NO acks at all (e.g. the login shell never
    ran) reports every id failed — no id is silently assumed updated."""
    _seed_sidecar(tmp_path, job_ids=["1", "2"])
    with patch("hpc_agent.infra.remote.ssh_run", return_value=_ok_cp(stdout="")):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert out.job_ids_updated == []
    assert out.job_ids_failed == ["1", "2"]


def test_missing_batch_ack_fails_the_whole_batch_closed(tmp_path: Path) -> None:
    """A severed channel (run-12 finding 24) delivers rc 0 with TRUNCATED
    stdout — here the per-segment acks arrived but the trailing batch-level ack
    never did, so the batch shell never provably ran to completion. The read
    must fail CLOSED: every id UNKNOWN/failed, never a settled outcome off a
    partial read (the update is idempotent, so a re-run converges)."""
    _seed_sidecar(tmp_path, job_ids=["1", "2"])
    truncated = "__HPC_UPD__ 1 0\n__HPC_UPD__ 2 0\n"  # no __HPC_UPD_BATCH__ trailer
    with patch("hpc_agent.infra.remote.ssh_run", return_value=_ok_cp(stdout=truncated)):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert out.job_ids_updated == []
    assert out.job_ids_failed == ["1", "2"]


def test_fused_command_carries_the_batch_ack_echo(tmp_path: Path) -> None:
    """The composed command is wrapped by wrap_with_ack: a trailing
    ``echo "__HPC_UPD_BATCH__=$?"`` proving the batch shell reached its end."""
    _seed_sidecar(tmp_path, job_ids=["1"])
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_ok_cp(stdout=_acks(("1", 0))),
    ) as mock_ssh:
        update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    cmd = mock_ssh.call_args_list[0].args[0]
    assert "__HPC_UPD_BATCH__=$?" in cmd


def test_ssh_unreachable_marks_every_job_failed(tmp_path: Path) -> None:
    """The transport dies before any ack returns: EVERY id is failed (the whole
    batch is one round-trip now, so an SshUnreachable fails all of them)."""
    _seed_sidecar(tmp_path, job_ids=["1", "2"])
    with patch("hpc_agent.infra.remote.ssh_run", side_effect=errors.SshUnreachable("nope")):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert out.job_ids_updated == []
    assert out.job_ids_failed == ["1", "2"]


def test_single_job_unchanged_behaviour(tmp_path: Path) -> None:
    """A batch of one is trivially the same shape: ONE ssh exec, one segment,
    the id updated on its ack rc 0."""
    _seed_sidecar(tmp_path, job_ids=["42"])
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_ok_cp(stdout=_acks(("42", 0))),
    ) as mock_ssh:
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert mock_ssh.call_count == 1
    cmd = mock_ssh.call_args_list[0].args[0]
    assert cmd.startswith("bash -lc ")
    assert "scontrol update jobid=42" in cmd
    assert out.job_ids_updated == ["42"]
    assert out.job_ids_failed == []


# ─── spec invariants ──────────────────────────────────────────────────


def test_both_set_and_add_features_rejected(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["1"])
    # Mutex is enforced at spec construction (Pydantic model_validator);
    # the function-level guard remains as a redundant check.
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Pass exactly one"):
        UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a"], add_features=["b"])


def test_neither_set_nor_add_features_rejected(tmp_path: Path) -> None:
    # Validation is now enforced at the model boundary (Pydantic
    # validator) instead of only inside the runner — the surrounding
    # contract docs claim "validate before the runner sees it", so a
    # no-op spec must fail at model construction.
    from pydantic import ValidationError

    _seed_sidecar(tmp_path, job_ids=["1"])
    with pytest.raises(ValidationError, match="at least one"):
        UpdateRunConstraintsSpec(run_id=_RUN_ID)


def test_no_job_ids_in_sidecar_rejected(tmp_path: Path) -> None:
    """Sidecar without job_ids is half-baked (rsync/qsub failed before
    submit_and_record); refuse to update."""
    _seed_sidecar(tmp_path, job_ids=[])
    with pytest.raises(errors.SpecInvalid, match="no job_ids"):
        update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )


def test_feature_with_shell_metachar_rejected(tmp_path: Path) -> None:
    """Defence against shell injection through the scontrol command."""
    _seed_sidecar(tmp_path, job_ids=["1"])
    with pytest.raises(errors.SpecInvalid, match="contains characters outside"):
        update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100;rm -rf /"]),
        )


def test_malicious_job_id_refused_before_composing(tmp_path: Path) -> None:
    """A job id is command-substituted into the fused shell string, so the
    injection guard must reject a malicious id BEFORE it reaches the composed
    ``bash -lc`` command — it is dropped to ``failed`` and never appears in the
    exec, while the well-formed sibling id is still updated."""
    _seed_sidecar(tmp_path, job_ids=["12345", "1; rm -rf /"])
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_ok_cp(stdout=_acks(("12345", 0))),
    ) as mock_ssh:
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert mock_ssh.call_count == 1
    cmd = mock_ssh.call_args_list[0].args[0]
    # The malicious id never reaches the composed command.
    assert "rm -rf" not in cmd
    assert "1; rm -rf /" not in cmd
    assert "scontrol update jobid=12345" in cmd
    assert out.job_ids_updated == ["12345"]
    assert out.job_ids_failed == ["1; rm -rf /"]


def test_all_job_ids_malicious_makes_no_ssh_call(tmp_path: Path) -> None:
    """When every id fails the injection guard there is nothing safe to compose,
    so no ssh exec is dispatched and every id is reported failed."""
    _seed_sidecar(tmp_path, job_ids=["1|2", "$(whoami)"])
    with patch("hpc_agent.infra.remote.ssh_run", return_value=_ok_cp()) as mock_ssh:
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert mock_ssh.call_count == 0
    assert out.job_ids_updated == []
    assert out.job_ids_failed == ["1|2", "$(whoami)"]
