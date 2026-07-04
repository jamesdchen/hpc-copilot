"""Direct-atom tests for the ``verify-relay`` primitive (conduct rule 10).

Seeds a tmp experiment dir with a decision journal + run sidecar (+ a
RunRecord for the state-word cases, mirroring the fixtures in
``test_decision_journal_primitives.py``), then drives the primitive with a
draft relay and asserts on the audit verdict. Covers: a clean relay passing, a
wrong number flagged with its nearest source value, a wrong state word, a wrong
run-id, conversational numbers not flagged, and the missing-sources /
unverifiable policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent._wire.queries.verify_relay import VerifyRelayInput, VerifyRelayResult
from hpc_agent.ops.decision.verify_relay import verify_relay
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "run-1"


def _seed_journal(tmp_path: Path, **evidence: object) -> None:
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=RUN_ID,
        block="submit-s1",
        response="y",
        evidence_digest=dict(evidence) or {"canary": "green", "core_hours": 128},
    )


def _seed_sidecar(tmp_path: Path, *, task_count: int = 10) -> None:
    write_run_sidecar(
        tmp_path,
        run_id=RUN_ID,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-03T00:00:00+00:00",
        executor="python3 .hpc/_hpc_dispatch.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=task_count,
        tasks_py_sha="b" * 64,
    )


def _seed_record(tmp_path: Path, *, status: str, job_ids: list[str] | None = None) -> None:
    upsert_run(
        tmp_path,
        RunRecord(
            run_id=RUN_ID,
            profile="p",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/remote",
            job_name="j",
            job_ids=job_ids if job_ids is not None else ["13610902"],
            total_tasks=10,
            submitted_at="2026-07-03T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status=status,
        ),
    )


def _run(tmp_path: Path, relay: str) -> VerifyRelayResult:
    return verify_relay(
        experiment_dir=tmp_path,
        spec=VerifyRelayInput(run_id=RUN_ID, relay_text=relay),
    )


# ── clean relay ────────────────────────────────────────────────────────────────


def test_clean_relay_passes(tmp_path: Path) -> None:
    _seed_journal(tmp_path, canary="green", core_hours=128)
    _seed_sidecar(tmp_path, task_count=10)
    _seed_record(tmp_path, status="failed")

    out = _run(
        tmp_path,
        "Run run-1 has failed. It consumed 128 core-hours across 10 tasks; the canary was green.",
    )
    assert out.clean is True
    assert out.mismatches == []
    assert out.claims_checked >= 3  # run-id, 128, 10, failed, canary-green
    assert "decision_journal" in out.sources_consulted
    assert "run_sidecar" in out.sources_consulted
    assert "run_record" in out.sources_consulted


# ── wrong number ───────────────────────────────────────────────────────────────


def test_wrong_number_flagged_with_nearest_source_value(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path, task_count=10)

    out = _run(tmp_path, "The run consumed 256 core-hours.")
    assert out.clean is False
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim == "256"
    assert num[0].nearest_source_value == "128"


def test_truncated_decimal_tolerated_but_rounding_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, metric=3.1411)
    _seed_sidecar(tmp_path)

    # Pure truncation passes.
    ok = _run(tmp_path, "The metric is 3.14.")
    assert [m for m in ok.mismatches if m.kind == "number"] == []
    # A rounding that changes a digit is flagged.
    bad = _run(tmp_path, "The metric is 3.15.")
    assert [m for m in bad.mismatches if m.kind == "number"]


# ── wrong state ────────────────────────────────────────────────────────────────


def test_wrong_state_word_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="failed")

    out = _run(tmp_path, "The job is still running.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "running"
    assert state[0].nearest_source_value == "failed"


# ── wrong run-id ───────────────────────────────────────────────────────────────


def test_wrong_run_id_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Results for run-2 are ready.")
    assert out.clean is False
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "run-2"
    assert rid[0].nearest_source_value == RUN_ID


def test_wrong_job_id_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="in_flight", job_ids=["13610902"])

    out = _run(tmp_path, "Scheduler job 99999999 is queued.")
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "99999999"


# ── conversational numbers ──────────────────────────────────────────────────────


def test_conversational_numbers_not_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path, task_count=10)

    relay = (
        "The plan has three steps:\n"
        "1. Stage the code.\n"
        "2. Submit the canary.\n"
        "3. Watch the array.\n"
        "Check back in ~2 minutes."
    )
    out = _run(tmp_path, relay)
    # No numeric/unverifiable mismatch from the list markers or the ~2.
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable")] == []


# ── missing sources / unverifiable policy ───────────────────────────────────────


def test_missing_sources_conversational_only_is_clean(tmp_path: Path) -> None:
    # No journal, no sidecar, no record — nothing to contradict.
    out = _run(tmp_path, "The run is being set up. Check back in ~2 minutes.")
    assert out.clean is True
    assert out.claims_checked == 0
    assert out.sources_consulted == []


def test_number_with_no_source_is_unverifiable(tmp_path: Path) -> None:
    # No sources at all, but the relay asserts a factual number.
    out = _run(tmp_path, "The run consumed 512 core-hours.")
    assert out.clean is False
    unv = [m for m in out.mismatches if m.kind == "unverifiable"]
    assert len(unv) == 1
    assert unv[0].claim == "512"
    assert unv[0].nearest_source_value is None
    assert out.sources_consulted == []


def test_scope_run_id_mention_passes(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    out = _run(tmp_path, "Run run-1 is in flight.")
    assert [m for m in out.mismatches if m.kind == "run_id"] == []
