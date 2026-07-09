"""verify-relay: ISO date/timestamp quotes are not run-id or number claims.

The proving-run-#3 false-positive class, recurred live (audit 2026-07-09): a
faithful relay quoting the journal's OWN timestamps ("submitted at
2026-07-03T00:00:00+00:00") tripped the run-id contradiction — the
``_IDENT_RE`` fragment ``2026-07-03T00`` is hyphen+digit and >= 8 chars, so it
read run-id-like, matched nothing in ``auth_ids``, and the Stop hook (run_id ∈
``_CONTRADICTION_KINDS``) blocked the turn. The ``:``-split time components
additionally leaked bare digit runs ("22", "05") into the number pass, which
the source pool can never verify (identifier-shaped source strings are
excluded from it). Same fix shape as the verb-vocabulary and decimal-fraction
exemptions: the whole ISO span is consumed and audited as neither.

Mirrors the fixtures in ``tests/ops/test_verify_relay.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent._wire.queries.verify_relay import VerifyRelayInput, VerifyRelayResult
from hpc_agent.ops.decision.verify_relay import verify_relay
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "run-1"


def _seed(tmp_path: Path) -> None:
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=RUN_ID,
        block="submit-s1",
        response="y",
        evidence_digest={"canary": "green", "core_hours": 128},
    )
    write_run_sidecar(
        tmp_path,
        run_id=RUN_ID,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-03T00:00:00+00:00",
        executor="python3 .hpc/_hpc_dispatch.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=10,
        tasks_py_sha="b" * 64,
    )


def _run(tmp_path: Path, relay: str) -> VerifyRelayResult:
    return verify_relay(
        experiment_dir=tmp_path,
        spec=VerifyRelayInput(run_id=RUN_ID, relay_text=relay),
    )


def test_journal_timestamp_quote_not_flagged_as_run_id(tmp_path: Path) -> None:
    """The live reproduction: quoting the sidecar's own ``submitted_at``."""
    _seed(tmp_path)
    out = _run(
        tmp_path,
        "Run run-1 was submitted at 2026-07-03T00:00:00+00:00 and used 128 core-hours.",
    )
    assert out.clean is True
    assert [m for m in out.mismatches if m.kind == "run_id"] == []


def test_timestamp_variants_not_flagged(tmp_path: Path) -> None:
    """Bare date, Z-offset, non-zero and fractional times: none reads as an id
    claim, and the time components never leak into the number pass."""
    _seed(tmp_path)
    for relay in (
        "Submitted on 2026-07-03.",
        "Checked at 2026-07-05T01:00:00Z.",
        "Last tick 2026-07-03T14:22:05+00:00.",
        "Started 2026-07-03T00:00:00.123456+00:00.",
    ):
        out = _run(tmp_path, relay)
        assert out.mismatches == [], relay


def test_run_id_timestamp_shape_still_flagged(tmp_path: Path) -> None:
    """Counter: the exemption is the ISO hyphenated-date dialect ONLY — the
    run-id timestamp shape (``\\d{8}-\\d{6}``, no date hyphens) stays a run-id
    claim and still fires when it matches nothing."""
    _seed(tmp_path)
    out = _run(tmp_path, "See run 20990703-141500-ab.")
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "20990703-141500-ab"


def test_date_prefixed_identifier_still_flagged(tmp_path: Path) -> None:
    """Counter: a token that merely EMBEDS a date but continues as an
    identifier ('2026-07-03-alpha') is not a timestamp quote — still audited
    as a run-id claim."""
    _seed(tmp_path)
    out = _run(tmp_path, "Results for 2026-07-03-alpha are ready.")
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "2026-07-03-alpha"


def test_wrong_run_id_and_number_still_flagged_alongside_timestamp(tmp_path: Path) -> None:
    """Counter: the timestamp exemption must not swallow REAL contradictions
    in the same relay."""
    _seed(tmp_path)
    out = _run(
        tmp_path,
        "At 2026-07-03T00:00:00+00:00 run-2 consumed 256 core-hours.",
    )
    assert out.clean is False
    assert [m.claim for m in out.mismatches if m.kind == "run_id"] == ["run-2"]
    assert [m.claim for m in out.mismatches if m.kind == "number"] == ["256"]
