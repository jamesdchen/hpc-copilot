"""Severed / truncated READ drills — the positive-evidence refusal doctrine.

Every remote reader in the stack is ack-gated: a clean-looking rc-0 read whose
positive-evidence ack line is ABSENT is a silently-truncated / never-run channel
and MUST raise or degrade to UNKNOWN — never parse-and-trust the truncated bytes,
never read absence as a settled "zero rows / all terminal / all present". This is
the F3 rule and the run-12 finding-24 (NAT idle-drop) defense.

Audit rows drilled (``docs/plans/transport-robustness-2026-07-17/AUDIT.md`` §7):
  * "rc-0 read but drop the ack line" → ``ssh_status_report`` RAISES
  * "Drop the scheduler-query ack" → ``ssh_batch_scheduler_states`` → SshUnreachable
  * census sever / truncation → ``announce`` readers refuse (§3f, F4)
  * ``verify_per_task_outputs`` ack-gate (§3g)
"""

from __future__ import annotations

import json

import pytest

from hpc_agent.errors import RemoteCommandFailed, SshUnreachable
from hpc_agent.infra import cluster_status
from hpc_agent.ops.aggregate import runner as agg_runner
from hpc_agent.ops.monitor import announce

from .conftest import proc

_STATUS_RUN = "hpc_agent.infra.cluster_status.remote.ssh_run"
_ANNOUNCE_RUN = "hpc_agent.ops.monitor.announce.remote.ssh_run"
_RUNNER_RUN = "hpc_agent.ops.aggregate.runner.remote.ssh_run"

_OK_ENVELOPE = json.dumps({"summary": {}, "tasks": {}, "rollup": {}, "errors": []})


def test_status_report_rc0_no_ack_raises(garble_at) -> None:
    """AUDIT §7 'rc-0 read but drop the ack line' → ``ssh_status_report`` RAISES.

    A severed channel can deliver a clean rc-0 read that never carried the
    reporter to completion (the ack echo is the LAST line, so it survives only a
    complete read). Its absence is positive proof of truncation → refuse to parse,
    raise transient so every consumer routes to UNKNOWN — never a settled "the
    reporter emitted nothing" verdict (F3).
    """
    garble_at(_STATUS_RUN, return_value=proc(0, stdout=_OK_ENVELOPE))  # JSON, but NO ack line
    with pytest.raises(RemoteCommandFailed, match="ack|truncat|severed"):
        cluster_status.ssh_status_report(
            ssh_target="h", remote_path="/p", run_id="r", job_ids=["1"], job_name="j"
        )


def test_status_report_channel_sever_propagates_not_swallowed(sever_at) -> None:
    """A hard mid-op sever (ssh_run raises OSError) PROPAGATES out of the reader.

    The reader does not catch-and-default: a severed transport surfaces as an
    exception the poll loop classifies UNKNOWN, never a fabricated empty report.
    """
    sever_at(_STATUS_RUN, exc=OSError, message="connection reset by peer")
    with pytest.raises(OSError):
        cluster_status.ssh_status_report(
            ssh_target="h", remote_path="/p", run_id="r", job_ids=["1"], job_name="j"
        )


class _FakeBackend:
    """Minimal backend surface for ``ssh_batch_scheduler_states`` — the SUT is the
    refusal logic in ``cluster_status``, not any real scheduler parser."""

    @classmethod
    def build_scheduler_state_cmd(cls, job_ids):  # noqa: ANN001, ANN206
        return "qstat -u $USER"

    @classmethod
    def scheduler_query_ran(cls, stdout):  # noqa: ANN001, ANN206
        # No positive-evidence ack token present in this (truncated) read.
        return stdout, False

    @classmethod
    def parse_scheduler_states(cls, stdout, job_ids):  # noqa: ANN001, ANN206
        raise AssertionError("must not parse states from an un-acked read")


def test_scheduler_states_rc0_no_ack_is_unreachable(garble_at) -> None:
    """AUDIT §7 'Drop the scheduler-query ack' → SshUnreachable, never 'all terminal'.

    Reading a silent/truncated scheduler query as 'every job left the queue' would
    flip a fleet of LIVE runs to terminal on one blip. Missing ack ⇒ UNKNOWN.
    """
    garble_at(_STATUS_RUN, return_value=proc(0, stdout="job output without the ack"))
    with pytest.raises(SshUnreachable, match="ack|absence|terminal"):
        cluster_status.ssh_batch_scheduler_states(
            ssh_target="h",
            backend_cls=_FakeBackend,  # type: ignore[arg-type]  # SUT is the refusal logic
            job_ids=["101", "102"],
        )


def test_census_transport_sever_raises_not_zero_rows(garble_at) -> None:
    """AUDIT §3f census: an ssh TRANSPORT failure (rc 255) RAISES — never read a
    connectivity blip as 'nothing announced' (a spurious zero that could mis-settle).
    """
    garble_at(_ANNOUNCE_RUN, return_value=proc(255, stderr="ssh: connect to host ... refused"))
    with pytest.raises(RemoteCommandFailed):
        announce.read_announcements(ssh_target="h", remote_path="/p", run_id="r", task_count=20)


def test_census_truncated_read_refuses_present_zero(garble_at) -> None:
    """AUDIT §3f census: an rc-0 read whose ack is absent (truncation) degrades to
    ``present:0`` with ``missing == task_count`` — NEVER a spurious 'all complete'.

    The doctrine is 'no per-task census' (the caller falls through to the legacy
    path), never 'every task done on zero evidence'.
    """
    # rc 0, but the __HPC_ANNOUNCE_ACK__ line never arrived (severed after cd).
    garble_at(_ANNOUNCE_RUN, return_value=proc(0, stdout="complete=99\nfailed=99\n"))
    result = announce.read_announcements(
        ssh_target="h", remote_path="/p", run_id="r", task_count=20
    )
    assert result["present"] == 0
    assert result["complete"] == 0  # the un-acked "99" is REFUSED, not trusted
    assert result["announced"] == 0
    assert result["missing"] == 20  # every task still owed — never settled done


def test_announced_ids_no_ack_refuses_to_partition(garble_at) -> None:
    """AUDIT §3f: ``read_announced_task_ids`` with no ack ⇒ ``present is False`` and
    an EMPTY done-set — the caller REFUSES to partition, never 'all undone' (which
    would re-run every already-finished task) nor 'all done'.
    """
    garble_at(_ANNOUNCE_RUN, return_value=proc(0, stdout="task_0.complete\ntask_1.complete\n"))
    ids = announce.read_announced_task_ids(ssh_target="h", remote_path="/p", run_id="r")
    assert ids.present is False
    assert ids.done_ids == frozenset()  # un-acked marker lines are NOT trusted


def test_verify_per_task_outputs_rc0_no_ack_raises(garble_at, monkeypatch) -> None:
    """AUDIT §3g: the pre-reduce ``verify_per_task_outputs`` refuses to read a
    severed rc-0 silence as 'all outputs present' — it RAISES.
    """
    monkeypatch.setattr(agg_runner, "_read_remote_sidecar", lambda **_: {"wave_map": {}})
    monkeypatch.setattr(agg_runner, "_wave_task_ids", lambda *_: [0, 1])
    # The MISSING lines could look complete, but the ack echo never arrived.
    garble_at(_RUNNER_RUN, return_value=proc(0, stdout="MISSING:out/0.csv\n"))
    with pytest.raises(RemoteCommandFailed, match="ack|truncat|severed"):
        agg_runner.verify_per_task_outputs(
            ssh_target="h", remote_path="/p", run_id="r", wave=0, template="out/{task_id}.csv"
        )
