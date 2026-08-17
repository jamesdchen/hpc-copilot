"""The checker-path authorship + fact-provenance gates (harness-contract
checker obligations 1 & 3).

The harness-contract audit ("The checker-path obligations", 2026-08-14) marked
two obligations DOCTRINE-ONLY with owed seams. These pin the seams that closed
them — and that each guard CAN FIRE (engineering-principles):

* obligation 3 — ``claimed_values`` / ``terminal_evidence`` are HUMAN-AUTHORED
  elicited inputs, held to the SAME derivation mechanism as
  ``goal``/``task_generator`` (``assert_elicited_value_human_authored``), wired
  at verify-reproduction's external-baseline intake and settle-run's intake;
* obligation 1 — every adoption fact carries a typed provenance annotation
  (``assert_adoption_fact_provenance``): human or observed, never the agent's
  own characterization. The seam is landed here for ``adopt-run``'s intake to
  consume.

Tiering mirrors the append-decision gate honestly: the utterance log present is
the LOCK (refusals below); no log is a DISCLOSED ``unverified_fallback``, never
a refusal (the settle-aggregate posture — pre-hook back-compat).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent.ops.decision.journal import (
    ELICITED_CHECKER_FIELDS,
    assert_adoption_fact_provenance,
    assert_elicited_value_human_authored,
)

if TYPE_CHECKING:
    from pathlib import Path


def _log_utterance(exp: Path, text: str) -> None:
    """Simulate the harness-side UserPromptSubmit capture for *exp*."""
    from hpc_agent.state.run_record import journal_dir
    from hpc_agent.state.utterances import append_utterance

    journal_dir(exp)  # the namespace a real state write would have created
    assert append_utterance(exp, text) is not None


# ── obligation 3: the elicited-value gate itself ──────────────────────────────


def test_field_set_names_the_two_checker_inputs() -> None:
    assert frozenset({"claimed_values", "terminal_evidence"}) == ELICITED_CHECKER_FIELDS


def test_unknown_field_is_a_programming_error(tmp_path: Path) -> None:
    """The field set is deliberate: an ad-hoc field name is refused at the
    call, not silently gated with invented semantics."""
    with pytest.raises(ValueError, match="ELICITED_CHECKER_FIELDS"):
        assert_elicited_value_human_authored(tmp_path, field="my_new_field", value="x")


def test_no_log_is_a_disclosed_fallback_never_a_refusal(tmp_path: Path) -> None:
    """Back-compat (the settle-aggregate posture): with no utterance log the
    gate passes but DISCLOSES the unverified tier — never a silent lock claim,
    never a pre-hook breakage."""
    for field, value in (
        ("claimed_values", {"gp.pi": 3.14}),
        ("terminal_evidence", "reporter RC=0 all-2700"),
    ):
        out = assert_elicited_value_human_authored(tmp_path, field=field, value=value)
        assert out["evidence_source"] == "unverified_fallback"


def test_empty_value_is_not_a_commit(tmp_path: Path) -> None:
    _log_utterance(tmp_path, "anything at all")
    out = assert_elicited_value_human_authored(tmp_path, field="claimed_values", value={})
    assert out["evidence_source"] == "empty_value_not_gated"


def test_claimed_values_lock_accepts_human_stated_numbers(tmp_path: Path) -> None:
    _log_utterance(tmp_path, "the paper claims QLIKE 0.234 and RMSE 1.5 on the test split")
    out = assert_elicited_value_human_authored(
        tmp_path, field="claimed_values", value={"qlike": 0.234, "rmse": 1.5}
    )
    assert out["evidence_source"] == "harness_captured"
    assert out["numbers"] == {"0.234": "verbatim", "1.5": "verbatim"}


def test_claimed_values_lock_refuses_a_number_the_human_never_stated(tmp_path: Path) -> None:
    """THE fire path (obligation 3): a harness relaying its own harvested
    number as the claim is refused, the token named, the authorship-missing
    marker attached (a freshly typed human claim resolves it)."""
    _log_utterance(tmp_path, "the paper claims QLIKE 0.234")
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_elicited_value_human_authored(
            tmp_path, field="claimed_values", value={"qlike": 0.999}
        )
    msg = str(ei.value)
    assert "claimed_values is human-authored" in msg
    assert "0.999" in msg  # the underivable token is named
    assert "checker obligation 3" in msg
    assert getattr(ei.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_terminal_evidence_lock_accepts_overlapping_words(tmp_path: Path) -> None:
    _log_utterance(tmp_path, "the foreground reporter finished RC=0, result tree on disk")
    out = assert_elicited_value_human_authored(
        tmp_path, field="terminal_evidence", value="reporter RC=0; result tree on disk"
    )
    assert out["evidence_source"] == "harness_captured"
    assert out["rule"] == "word_overlap"
    assert "reporter" in out["matched_words"]


def test_terminal_evidence_lock_refuses_agent_composed_text(tmp_path: Path) -> None:
    """The other fire path: terminal evidence sharing no words with anything
    the human typed reads as the harness's own characterization — refused."""
    _log_utterance(tmp_path, "please settle the run when done")
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_elicited_value_human_authored(
            tmp_path,
            field="terminal_evidence",
            value="scheduler accounting confirms successful completion",
        )
    assert "terminal_evidence is human-authored" in str(ei.value)
    assert getattr(ei.value, "failure_features", None) == {"authorship_evidence": "missing"}


# ── obligation 1: the adoption-fact provenance gate ───────────────────────────


def test_unattributed_fact_is_refused(tmp_path: Path) -> None:
    """THE fire path (obligation 1): a fact with NO provenance annotation is
    exactly the round-tripped-guess laundering channel — refused with the
    authorship marker."""
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_adoption_fact_provenance(tmp_path, facts={"run_id": "exp-1"}, provenance={})
    msg = str(ei.value)
    assert "unattributed" in msg and "run_id" in msg
    assert getattr(ei.value, "failure_features", None) == {"authorship_evidence": "missing"}


@pytest.mark.parametrize("kind", ["agent", "llm", "inferred", ""])
def test_non_human_non_observed_kind_is_refused(tmp_path: Path, kind: str) -> None:
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_adoption_fact_provenance(
            tmp_path,
            facts={"command": "python train.py"},
            provenance={"command": {"kind": kind}},
        )
    assert "laundering" in str(ei.value)


def test_observed_without_via_is_refused(tmp_path: Path) -> None:
    """An observation with no record of WHAT was read is a characterization."""
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_adoption_fact_provenance(
            tmp_path,
            facts={"job_ids": ["4242"]},
            provenance={"job_ids": {"kind": "observed"}},
        )
    assert "'via'" in str(ei.value)


def test_observed_with_named_source_passes_and_is_disclosed(tmp_path: Path) -> None:
    out = assert_adoption_fact_provenance(
        tmp_path,
        facts={"job_ids": ["4242"], "remote_path": "/scratch/me/exp"},
        provenance={
            "job_ids": {"kind": "observed", "via": "squeue -j 4242"},
            "remote_path": {"kind": "observed", "via": "ls /scratch/me/exp"},
        },
    )
    assert out["facts"]["job_ids"] == {"kind": "observed", "via": "squeue -j 4242"}
    assert out["facts"]["remote_path"]["via"] == "ls /scratch/me/exp"


def test_human_kind_faces_the_derivation_lock(tmp_path: Path) -> None:
    """A 'human'-attributed fact whose tokens the human never typed is refused
    under the lock; the same fact passes once the human actually stated it."""
    _log_utterance(tmp_path, "adopt my run: I ran python train.py with 20 seeds")
    ok = assert_adoption_fact_provenance(
        tmp_path,
        facts={"command": "python train.py"},
        provenance={"command": {"kind": "human"}},
    )
    assert ok["facts"]["command"]["kind"] == "human"
    assert ok["facts"]["command"]["evidence_source"] == "harness_captured"
    with pytest.raises(errors.SpecInvalid):
        assert_adoption_fact_provenance(
            tmp_path,
            facts={"command": "mpirun ./solver --steps 999"},
            provenance={"command": {"kind": "human"}},
        )


def test_human_kind_without_log_is_a_disclosed_fallback(tmp_path: Path) -> None:
    out = assert_adoption_fact_provenance(
        tmp_path,
        facts={"cluster": "hoffman2"},
        provenance={"cluster": {"kind": "human"}},
    )
    assert out["facts"]["cluster"] == {"kind": "human", "evidence_source": "unverified_fallback"}
    assert out["evidence_source"] == "unverified_fallback"


def test_empty_fact_values_are_never_gated(tmp_path: Path) -> None:
    """Absent optional facts (None / empty) are not commits — no annotation owed."""
    out = assert_adoption_fact_provenance(
        tmp_path,
        facts={"job_ids": None, "terminal_evidence": "", "resources": {}},
        provenance={},
    )
    assert out["facts"] == {}


def test_structurally_malformed_provenance_is_a_plain_refusal(tmp_path: Path) -> None:
    """A non-mapping provenance is structural misuse — refused WITHOUT the
    authorship marker (no typed utterance can fix a malformed call)."""
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_adoption_fact_provenance(
            tmp_path,
            facts={"run_id": "exp-1"},
            provenance="human",  # type: ignore[arg-type]
        )
    assert getattr(ei.value, "failure_features", None) is None


# ── wiring: settle-run gates terminal_evidence at intake ──────────────────────

_RUN_ID = "exp-abcd1234"
_HARVEST_SEAM = "hpc_agent.ops.monitor.harvest_guard.harvest_on_terminal"


def _seed_run(exp: Path, *, status: str = "in_flight") -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        exp,
        RunRecord(
            run_id=_RUN_ID,
            profile="exp",
            cluster="hoffman2",
            ssh_target="me@hoffman2.idre.ucla.edu",
            remote_path="/scratch/me/exp",
            job_name="exp",
            job_ids=["42"],
            total_tasks=2,
            submitted_at="2026-07-11T00:00:00+00:00",
            experiment_dir=str(exp),
            status=status,
            backend="sge",
        ),
    )


def test_settle_run_refuses_agent_composed_terminal_evidence(tmp_path: Path) -> None:
    """The wired guard 4 fires BEFORE anything is journaled or marked."""
    from hpc_agent._wire.workflows.settle_run import SettleRunInput
    from hpc_agent.ops.settle_run import settle_run
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.journal import load_run

    _seed_run(tmp_path)
    _log_utterance(tmp_path, "please settle the run")
    with pytest.raises(errors.SpecInvalid) as ei:
        settle_run(
            tmp_path,
            spec=SettleRunInput(
                run_id=_RUN_ID,
                status="complete",
                evidence="scheduler accounting confirms successful completion",
            ),
        )
    assert "terminal_evidence is human-authored" in str(ei.value)
    # Nothing was journaled and the status did not flip — the gate is at intake.
    assert read_decisions(tmp_path, "run", _RUN_ID) == []
    rec = load_run(tmp_path, _RUN_ID)
    assert rec is not None and rec.status == "in_flight"


def test_settle_run_accepts_human_stated_evidence_and_discloses_tier(tmp_path: Path) -> None:
    from hpc_agent._wire.workflows.settle_run import SettleRunInput
    from hpc_agent.ops.settle_run import settle_run
    from hpc_agent.state.decision_journal import read_decisions

    _seed_run(tmp_path)
    _log_utterance(tmp_path, "reporter finished RC=0, all 2 tasks, result tree on disk — settle it")
    with mock.patch(_HARVEST_SEAM, return_value={"harvest_ok": True}):
        res = settle_run(
            tmp_path,
            spec=SettleRunInput(
                run_id=_RUN_ID, status="complete", evidence="reporter RC=0; result tree on disk"
            ),
        )
    assert res.status == "complete"
    prov = read_decisions(tmp_path, "run", _RUN_ID)[0]["provenance"]
    assert prov["authorship"]["evidence_source"] == "harness_captured"


def test_settle_run_without_log_is_unchanged_and_discloses_fallback(tmp_path: Path) -> None:
    """Pre-hook behavior preserved: no utterance log → the settle proceeds,
    the fallback tier is on the record (never silent)."""
    from hpc_agent._wire.workflows.settle_run import SettleRunInput
    from hpc_agent.ops.settle_run import settle_run
    from hpc_agent.state.decision_journal import read_decisions

    _seed_run(tmp_path)
    with mock.patch(_HARVEST_SEAM, return_value={"harvest_ok": True}):
        res = settle_run(
            tmp_path,
            spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="proven on disk"),
        )
    assert res.status == "complete"
    prov = read_decisions(tmp_path, "run", _RUN_ID)[0]["provenance"]
    assert prov["authorship"]["evidence_source"] == "unverified_fallback"


# ── wiring: verify-reproduction gates claimed_values at external-baseline intake ──


def _fresh_observed_run(exp: Path, metrics: dict[str, Any]) -> None:
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        exp,
        run_id="repro-run",
        cmd_sha="a" * 64,
        hpc_agent_version="0.11.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python train.py",
        result_dir_template="results/{task_id}",
        task_count=1,
        tasks_py_sha="b" * 64,
    )
    agg = exp / "_aggregated" / "repro-run"
    agg.mkdir(parents=True, exist_ok=True)
    (agg / "metrics_aggregate.json").write_text(
        json.dumps({"run_id": "repro-run", "aggregated_metrics": metrics}), encoding="utf-8"
    )


def _claim_spec(claimed_values: dict[str, float]) -> Any:
    from hpc_agent._wire.queries.verify_reproduction import (
        ExternalBaseline,
        VerifyReproductionSpec,
    )

    return VerifyReproductionSpec(
        repro_run_id="repro-run",
        external_baseline=ExternalBaseline(claimed_values=claimed_values),
    )


def test_claim_check_refuses_a_fabricated_claim_before_any_receipt(tmp_path: Path) -> None:
    """The wired obligation-3 gate: a claimed value the human never stated is
    refused at intake — no claim-check receipt is written."""
    from hpc_agent.ops.verify_reproduction import verify_reproduction

    _fresh_observed_run(tmp_path, {"gp": {"pi": 3.14159}})
    _log_utterance(tmp_path, "the paper claims pi 3.14159")
    with pytest.raises(errors.SpecInvalid) as ei:
        verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 2.71828}))
    assert "claimed_values is human-authored" in str(ei.value)
    assert not (tmp_path / "_aggregated" / "repro-run" / "claim_check_receipts.jsonl").exists()


def test_claim_check_happy_path_with_human_authored_claim_is_unchanged(tmp_path: Path) -> None:
    """The human stated the claim → the lock accepts and the claim-check runs
    to its normal match verdict (the pinned consistency sentence)."""
    from hpc_agent.ops.verify_reproduction import CLAIM_CONSISTENT_SENTENCE, verify_reproduction

    _fresh_observed_run(tmp_path, {"gp": {"pi": 3.14159}})
    _log_utterance(tmp_path, "claim-check this: the paper claims pi 3.14159")
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    assert res.stage_reached == "match"
    assert res.reason == CLAIM_CONSISTENT_SENTENCE


def test_claim_check_without_log_is_unchanged(tmp_path: Path) -> None:
    """Pre-hook back-compat: no utterance log → external-baseline mode behaves
    byte-identically to before the gate (the disclosed fallback tier)."""
    from hpc_agent.ops.verify_reproduction import CLAIM_CONSISTENT_SENTENCE, verify_reproduction

    _fresh_observed_run(tmp_path, {"gp": {"pi": 3.14159}})
    res = verify_reproduction(tmp_path, spec=_claim_spec({"gp.pi": 3.14159}))
    assert res.stage_reached == "match"
    assert res.reason == CLAIM_CONSISTENT_SENTENCE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
