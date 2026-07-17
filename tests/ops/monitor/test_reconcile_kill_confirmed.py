"""A kill-confirmed run settles TERMINAL from the kill evidence, reporter-free.

Proving run #5, finding 14: ``kill`` confirms the scheduler jobs gone
(``record_kill_confirmed``) then routes through ``reconcile`` to settle the
record terminal. But reconcile's settle branch is guarded on ``not
reporter_failed`` — so when a broken cluster env crashes the per-task reporter,
a KILL-CONFIRMED run used to stay ``in_flight`` (surfaced ``unable_to_verify``),
block the next submit, and force the driver to hand-choreograph
reconcile→supersede.

A kill-confirmed run is terminal by the KILL evidence alone; the reporter's
per-task counts are irrelevant to a deliberate kill. Reconcile now short-circuits
to the SAME ``abandoned`` verdict ``classify.settle`` yields for a killed run when
the reporter DOES work — one verdict across both reporter-healthy and
reporter-broken paths. The short-circuit keys STRICTLY on kill-confirmation, so a
non-kill (or partial-kill) reporter failure still routes through
``unable_to_verify``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.monitor import reconcile as recon
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.state.journal import is_kill_confirmed, load_run, upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(
    run_id: str,
    *,
    status: str = "in_flight",
    job_ids=("100", "200"),
    total_tasks: int = 4,
    kill_confirmed_at: str | None = None,
    kill_confirmed_job_ids=(),
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=total_tasks,
        submitted_at="2026-07-05T00:00:00Z",
        experiment_dir="/exp",
        status=status,
        kill_confirmed_at=kill_confirmed_at,
        kill_confirmed_job_ids=list(kill_confirmed_job_ids),
    )


def _stub_reporter_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alive-check runs clean (jobs gone), but the per-task reporter crashes.

    This is the broken-env signature: the alive probe is plain shell (needs no
    conda env), while the reporter shells the cluster-side reduce module under a
    bare login-node python that has no ``hpc_agent`` → ``RemoteCommandFailed``.
    """

    def _status(**_kw):
        raise errors.RemoteCommandFailed(
            "status reporter failed (rc=127): python: command not found"
        )

    monkeypatch.setattr(recon, "_ssh_status_report", _status)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: set())


def _count_harvests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _fake(experiment_dir, run_id, *, terminal_cause, record=None, **_kw):
        calls.append(terminal_cause)
        # Mirror the real guard's LAST step: append a durable receipt to the
        # ledger, so the reconcile backstop's receipt-derived idempotency holds
        # (a real harvest ALWAYS writes a marker — a receipt-less fake would let
        # the terminal-with-no-receipt backstop re-fire on every re-reconcile).
        append_jsonl_line(
            harvest_marker_path(experiment_dir, run_id),
            {"run_id": run_id, "terminal_cause": terminal_cause, "harvest_ok": True},
        )
        return {}

    monkeypatch.setattr(recon, "harvest_on_terminal", _fake)
    return calls


# ---------------------------------------------------------------------------
# The regression target: kill-confirmed + a DEAD reporter still settles terminal.
# ---------------------------------------------------------------------------


def test_kill_confirmed_reporter_dead_settles_abandoned(tmp_path, monkeypatch):
    """REGRESSION-PIN (finding 14): today this run stays ``in_flight``. A
    kill-confirmed run with a crashed reporter must settle terminal ``abandoned``
    from the kill evidence alone, carrying the reporter-independent reason."""
    harvests = _count_harvests(monkeypatch)
    upsert_run(
        tmp_path,
        _record(
            "killed_r1",
            job_ids=["100", "200"],
            kill_confirmed_at="2026-07-05T01:00:00Z",
            kill_confirmed_job_ids=["100", "200"],
        ),
    )
    _stub_reporter_dead(monkeypatch)

    result = recon.reconcile(tmp_path, "killed_r1", scheduler="sge")

    # Settled terminal (mark_run → abandoned), NOT stranded in_flight.
    assert result.status == "abandoned"
    last = result.last_status or {}
    assert last["verdict_reason"] == "killed_confirmed_reporter_independent"
    # Crucially NOT masked as unverifiable: the short-circuit runs BEFORE the
    # unable_to_verify marking, so the envelope surfaces the terminal state.
    assert last.get("verify_state") != "unable_to_verify"
    envelope = recon._reconcile_envelope(result)
    assert envelope["lifecycle_state"] == "abandoned"
    # The guaranteed harvest fired once on the in_flight→abandoned transition.
    assert harvests == ["abandoned"]


def test_kill_confirmed_reporter_ok_still_terminal_no_double_harvest(tmp_path, monkeypatch):
    """When the reporter DOES work, the verdict is UNCHANGED (still ``abandoned``,
    the settle verdict for a killed mid-flight run) and the harvest fires exactly
    once — the short-circuit returns before the settle arm, so no double-harvest,
    and an idempotent re-reconcile does not re-fire."""
    harvests = _count_harvests(monkeypatch)
    upsert_run(
        tmp_path,
        _record(
            "killed_r2",
            job_ids=["300"],
            total_tasks=4,
            kill_confirmed_at="2026-07-05T01:00:00Z",
            kill_confirmed_job_ids=["300"],
        ),
    )
    # Reporter runs clean: killed mid-flight, nothing complete, nothing failed.
    report = {"summary": {"complete": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}}
    monkeypatch.setattr(recon, "_ssh_status_report", lambda **_kw: report)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: set())

    result = recon.reconcile(tmp_path, "killed_r2", scheduler="sge")
    assert result.status == "abandoned"
    assert (result.last_status or {})["verdict_reason"] == "killed_confirmed_reporter_independent"
    assert harvests == ["abandoned"]  # exactly one — no double-harvest

    # Idempotent re-reconcile: already abandoned == pre_reconcile_status → no re-fire.
    recon.reconcile(tmp_path, "killed_r2", scheduler="sge")
    assert harvests == ["abandoned"]


def test_non_kill_reporter_dead_stays_unable_to_verify(tmp_path, monkeypatch):
    """The short-circuit keys STRICTLY on kill-confirmation: a NON-kill run whose
    reporter crashed must still route through ``unable_to_verify`` (untouched
    behavior), never be settled by the kill short-circuit."""
    harvests = _count_harvests(monkeypatch)
    upsert_run(tmp_path, _record("live_r3", job_ids=["400", "500"]))  # no kill fields
    _stub_reporter_dead(monkeypatch)

    result = recon.reconcile(tmp_path, "live_r3", scheduler="sge")

    assert result.status == "in_flight"  # NOT settled
    assert (result.last_status or {}).get("verify_state") == "unable_to_verify"
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "unable_to_verify"
    assert harvests == []  # no terminal transition → no harvest


def test_partial_kill_reporter_dead_stays_unable_to_verify(tmp_path, monkeypatch):
    """A PARTIAL kill (confirmed-gone covers only SOME requested ids) is not
    kill-confirmed — the run is still live — so a crashed reporter routes through
    ``unable_to_verify``, never the terminal short-circuit."""
    upsert_run(
        tmp_path,
        _record(
            "partial_r4",
            job_ids=["100", "200"],
            kill_confirmed_at="2026-07-05T01:00:00Z",
            kill_confirmed_job_ids=["100"],  # 200 not confirmed gone → partial
        ),
    )
    _stub_reporter_dead(monkeypatch)

    result = recon.reconcile(tmp_path, "partial_r4", scheduler="sge")

    assert result.status == "in_flight"
    assert (result.last_status or {}).get("verify_state") == "unable_to_verify"
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "unable_to_verify"


# ---------------------------------------------------------------------------
# The predicate itself (journal.is_kill_confirmed).
# ---------------------------------------------------------------------------


def test_predicate_full_coverage_is_confirmed():
    rec = _record("x", kill_confirmed_at="t", job_ids=["1", "2"], kill_confirmed_job_ids=["1", "2"])
    assert is_kill_confirmed(rec) is True


def test_predicate_partial_coverage_is_not_confirmed():
    rec = _record("x", kill_confirmed_at="t", job_ids=["1", "2"], kill_confirmed_job_ids=["1"])
    assert is_kill_confirmed(rec) is False


def test_predicate_no_kill_confirmed_at_is_not_confirmed():
    rec = _record("x", job_ids=["1"], kill_confirmed_job_ids=["1"])  # no timestamp
    assert is_kill_confirmed(rec) is False


def test_predicate_no_job_ids_is_not_confirmed():
    rec = _record("x", kill_confirmed_at="t", job_ids=[], kill_confirmed_job_ids=[])
    assert is_kill_confirmed(rec) is False


def test_kill_confirmed_run_persisted_to_journal(tmp_path):
    """The kill fields round-trip through upsert/load so the predicate sees them."""
    upsert_run(
        tmp_path,
        _record(
            "persist_r5",
            kill_confirmed_at="2026-07-05T01:00:00Z",
            kill_confirmed_job_ids=["100", "200"],
        ),
    )
    loaded = load_run(tmp_path, "persist_r5")
    assert loaded is not None
    assert is_kill_confirmed(loaded) is True
