"""Tests for the ``program-verify`` query verb (program-level reproduction, phase 1).

Exercises the projection over RECORDED reproduction judgments: the explicit-list
happy path with synthetic receipts (all four per-constituent classes), the
program roll-up fold ordering, no-evidence constituents NAMED not guessed, the
seeded-from-table path (incl. the G4a-proxy disclosure), the write-once manifest
idempotency + content-drift delta, render determinism, and the no-mutation pin.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.program_verify import ProgramVerifySpec
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.program_verify import (
    CLASS_INCOMPARABLE,
    CLASS_MISMATCH,
    CLASS_NONE,
    CLASS_REPRODUCED,
    CLASS_STALE_IDENTITY,
    _classify_receipt,
    _fold_constituent,
    _identity_drift,
    program_verify,
)
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_TS = "2026-07-18T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _sidecar(exp: Path, run_id: str, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "cmd_sha": f"cmd-{run_id}",
        "hpc_agent_version": "9.9.9",
        "submitted_at": _TS,
        "executor": "python train.py",
        "result_dir_template": "results/{task_id}",
        "task_count": 1,
        "tasks_py_sha": f"tsha-{run_id}",
        "cluster": "hoffman2",
        "profile": "p",
    }
    kwargs.update(over)
    write_run_sidecar(exp, **kwargs)


def _receipt(original: str, repro: str, overall: str, **extra: Any) -> dict[str, Any]:
    """A synthetic reproduction receipt in the shape program-verify reads."""
    receipt: dict[str, Any] = {
        "ts": _TS,
        "receipt_kind": "reproduction",
        "schema_version": 2,
        "original": {"run_id": original, "cmd_sha": f"cmd-{original}"},
        "repro": {"run_id": repro, "cmd_sha": f"cmd-{original}"},
        "overall": overall,
        "per_key": [
            {
                "key": "gp.pi",
                "verdict": "match" if overall in ("match", "auto_cleared") else overall,
            }
        ],
        "sources": {},
    }
    receipt.update(extra)
    return receipt


def _write_receipt(exp: Path, repro: str, receipt: dict[str, Any]) -> None:
    append_jsonl_line(exp / "_aggregated" / repro / "reproduction_receipts.jsonl", receipt)


def _persist_agg(exp: Path, run_id: str, contributing: list[str]) -> Path:
    agg = exp / "_aggregated" / run_id / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    agg.write_text(
        json.dumps(
            {
                "aggregated_metrics": {},
                "provenance": {
                    "source": "local_reduce",
                    "reduced_at": _TS,
                    "contributing_run_ids": list(contributing),
                    "hpc_agent_version": "9.9.9",
                },
            }
        ),
        encoding="utf-8",
    )
    return agg


def _seed_four_classes(exp: Path) -> list[str]:
    """A/B/C reproduced/mismatch/incomparable; D has no receipt at all."""
    for c in ("aaa", "bbb", "ccc", "ddd"):
        _sidecar(exp, c)
    _write_receipt(exp, "aaa-repro", _receipt("aaa", "aaa-repro", "auto_cleared"))
    _write_receipt(exp, "bbb-repro", _receipt("bbb", "bbb-repro", "mismatch"))
    _write_receipt(exp, "ccc-repro", _receipt("ccc", "ccc-repro", "incomparable"))
    return ["aaa", "bbb", "ccc", "ddd"]


# --------------------------------------------------------------------------- #
# explicit-list happy path — all four per-constituent classes
# --------------------------------------------------------------------------- #
def test_explicit_list_all_four_classes(tmp_path: Path) -> None:
    run_ids = _seed_four_classes(tmp_path)
    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=run_ids))

    by_id = {c.run_id: c for c in res.constituents}
    assert by_id["aaa"].classification == CLASS_REPRODUCED
    assert by_id["bbb"].classification == CLASS_MISMATCH
    assert by_id["ccc"].classification == CLASS_INCOMPARABLE
    assert by_id["ddd"].classification == CLASS_NONE

    assert res.seed_kind == "explicit"
    assert res.recipe_signature is None
    assert res.reproduced_count == 1
    assert res.total == 4
    # roll-up folds to the most severe constituent (a recorded mismatch).
    assert res.overall == CLASS_MISMATCH
    assert res.needs_decision is True


def test_no_evidence_constituent_is_named_not_guessed(tmp_path: Path) -> None:
    _sidecar(tmp_path, "ddd")
    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["ddd"]))
    (c,) = res.constituents
    assert c.classification == CLASS_NONE
    assert c.receipt_count == 0
    assert c.driving_receipt is None
    assert c.reason == "no reproduction receipt on record"


def test_constituent_carries_identity_and_driving_receipt(tmp_path: Path) -> None:
    _sidecar(tmp_path, "aaa")
    _write_receipt(
        tmp_path,
        "aaa-repro",
        _receipt("aaa", "aaa-repro", "mismatch", diverged_stage="normalize"),
    )
    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["aaa"]))
    (c,) = res.constituents
    assert c.cmd_sha == "cmd-aaa"
    assert c.tasks_py_sha == "tsha-aaa"
    assert c.executor == "python train.py"
    assert c.repro_run_ids == ["aaa-repro"]
    assert c.driving_receipt is not None
    assert c.diverged_stage == "normalize"
    # reason reads off the receipt's own keys (overall + counts), never a metric value.
    assert "overall=mismatch" in c.reason


# --------------------------------------------------------------------------- #
# roll-up fold ordering
# --------------------------------------------------------------------------- #
def test_constituent_fold_ordering() -> None:
    # a recorded mismatch dominates a matching sibling (never hidden).
    assert _fold_constituent([CLASS_REPRODUCED, CLASS_MISMATCH]) == CLASS_MISMATCH
    # a clean reproduction stands over a weaker incomparable attempt.
    assert _fold_constituent([CLASS_REPRODUCED, CLASS_INCOMPARABLE]) == CLASS_REPRODUCED
    # incomparable when that is all there is.
    assert _fold_constituent([CLASS_INCOMPARABLE]) == CLASS_INCOMPARABLE
    # no receipts → no reproduction on record.
    assert _fold_constituent([]) == CLASS_NONE


def test_program_fold_no_reproduction_outranks_incomparable(tmp_path: Path) -> None:
    # reproduced + no-reproduction + incomparable, no mismatch → overall is the
    # most severe: no_reproduction_on_record (outranks incomparable).
    _sidecar(tmp_path, "aaa")
    _sidecar(tmp_path, "ccc")
    _sidecar(tmp_path, "ddd")
    _write_receipt(tmp_path, "aaa-repro", _receipt("aaa", "aaa-repro", "auto_cleared"))
    _write_receipt(tmp_path, "ccc-repro", _receipt("ccc", "ccc-repro", "incomparable"))
    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["aaa", "ccc", "ddd"]))
    assert res.overall == CLASS_NONE
    assert res.needs_decision is True


def test_program_fully_reproduced_does_not_need_decision(tmp_path: Path) -> None:
    _sidecar(tmp_path, "aaa")
    _sidecar(tmp_path, "bbb")
    _write_receipt(tmp_path, "aaa-repro", _receipt("aaa", "aaa-repro", "auto_cleared"))
    _write_receipt(tmp_path, "bbb-repro", _receipt("bbb", "bbb-repro", "match"))
    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["aaa", "bbb"]))
    assert res.overall == CLASS_REPRODUCED
    assert res.needs_decision is False
    assert res.reproduced_count == 2


# --------------------------------------------------------------------------- #
# needs_verdict human-clearance (the fingerprint admission join)
# --------------------------------------------------------------------------- #
def test_needs_verdict_uncleared_is_incomparable(tmp_path: Path) -> None:
    _sidecar(tmp_path, "aaa")
    receipt = _receipt("aaa", "aaa-repro", "needs_verdict")
    # no admitted fingerprint sample for the pair → not cleared.
    assert _classify_receipt(tmp_path, receipt) == CLASS_INCOMPARABLE


def test_needs_verdict_human_cleared_is_reproduced(tmp_path: Path, monkeypatch) -> None:
    _sidecar(tmp_path, "aaa")
    receipt = _receipt("aaa", "aaa-repro", "needs_verdict")
    # a recorded HUMAN acceptance (the admission join) clears the needs_verdict.
    monkeypatch.setattr("hpc_agent.ops.program_verify._pair_admitted", lambda *a, **k: True)
    assert _classify_receipt(tmp_path, receipt) == CLASS_REPRODUCED


# --------------------------------------------------------------------------- #
# FIX 1 — identity-drift disclosure (evidence_stale_identity)
# --------------------------------------------------------------------------- #
def test_identity_drift_helper_compares_only_shared_legs() -> None:
    # a moved code leg is a mismatch, both shas carried.
    mismatched, unrecorded = _identity_drift(
        {"cmd_sha": "c1", "tasks_py_sha": "OLD"},
        {"cmd_sha": "c1", "tasks_py_sha": "NEW", "executor": "python train.py"},
    )
    assert mismatched == [("tasks_py_sha", "OLD", "NEW")]
    # executor is receipt-absent → unrecorded (never invented into drift); data_sha
    # is present-only so its absence is silent.
    assert unrecorded == ["executor"]

    # identical shared legs → nothing mismatched.
    m2, u2 = _identity_drift(
        {"cmd_sha": "c1", "tasks_py_sha": "t1", "executor": "e"},
        {"cmd_sha": "c1", "tasks_py_sha": "t1", "executor": "e"},
    )
    assert m2 == []
    assert u2 == []

    # a leg the SIDECAR lacks is unrecorded, not a false mismatch.
    m3, u3 = _identity_drift(
        {"cmd_sha": "c1", "tasks_py_sha": "t1"},
        {"cmd_sha": "c1", "executor": "e"},
    )
    assert m3 == []
    assert "tasks_py_sha" in u3


def test_moved_identity_is_evidence_stale_identity(tmp_path: Path) -> None:
    # the sidecar's code identity moved AFTER the receipt was earned.
    _sidecar(tmp_path, "aaa", tasks_py_sha="tsha-NEW")
    receipt = _receipt("aaa", "aaa-repro", "auto_cleared")
    receipt["original"]["tasks_py_sha"] = "tsha-OLD"  # receipt earned at the old sha
    _write_receipt(tmp_path, "aaa-repro", receipt)

    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["aaa"]))
    (c,) = res.constituents
    # a reproduced receipt at a superseded identity is NOT current evidence.
    assert c.classification == CLASS_STALE_IDENTITY
    assert "tasks_py_sha" in c.reason
    assert "tsha-OLD" in c.reason and "tsha-NEW" in c.reason
    assert res.overall == CLASS_STALE_IDENTITY
    assert res.needs_decision is True
    # the drift is carried up to the program gaps list, disclosed not papered.
    assert any(g["code"] == "constituent-evidence-stale-identity" for g in res.gaps)


def test_identical_identity_stays_reproduced_no_false_stale(tmp_path: Path) -> None:
    _sidecar(tmp_path, "aaa")  # cmd_sha=cmd-aaa, tasks_py_sha=tsha-aaa
    receipt = _receipt("aaa", "aaa-repro", "auto_cleared")
    receipt["original"]["tasks_py_sha"] = "tsha-aaa"  # matches the current sidecar
    _write_receipt(tmp_path, "aaa-repro", receipt)

    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["aaa"]))
    (c,) = res.constituents
    assert c.classification == CLASS_REPRODUCED  # identical legs → no stale
    assert CLASS_STALE_IDENTITY not in c.reason
    assert res.needs_decision is False
    assert not any(g["code"] == "constituent-evidence-stale-identity" for g in res.gaps)


def test_missing_receipt_leg_is_unrecorded_not_stale(tmp_path: Path) -> None:
    # the receipt records ONLY cmd_sha (an old shape / executor never on the
    # receipt); the sidecar's tasks_py_sha differs, but a missing receipt leg must
    # NEVER manufacture a stale — it is disclosed "unrecorded" instead.
    _sidecar(tmp_path, "aaa", tasks_py_sha="tsha-DIFFERENT")
    _write_receipt(tmp_path, "aaa-repro", _receipt("aaa", "aaa-repro", "auto_cleared"))

    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["aaa"]))
    (c,) = res.constituents
    assert c.classification == CLASS_REPRODUCED  # no false stale from a missing leg
    assert "identity leg tasks_py_sha unrecorded" in c.reason
    assert "identity leg executor unrecorded" in c.reason
    assert res.gaps == []  # nothing stale → no drift gap


# --------------------------------------------------------------------------- #
# FIX 2 — a claim-check receipt is NEVER reproduction evidence
# --------------------------------------------------------------------------- #
def test_claim_check_receipt_is_no_reproduction_no_admission_leak(
    tmp_path: Path, monkeypatch
) -> None:
    _sidecar(tmp_path, "aaa")
    # a claim-check receipt in the shape verify_reproduction._run_claim_check writes:
    # kind="claim-check", a `claim` block + `repro` identity, NO `original`. Even
    # placed in the reproduction ledger, the receipt_kind filter must exclude it.
    claim_receipt: dict[str, Any] = {
        "ts": _TS,
        "receipt_kind": "claim-check",
        "schema_version": 1,
        "claim": {
            "claimed_values": {"gp.pi": 3.14},
            "tolerance": None,
            "claimed_data_sha": None,
        },
        "repro": {"run_id": "aaa", "cmd_sha": "cmd-aaa"},
        "per_key": [{"key": "gp.pi", "verdict": "match"}],
        "overall": "match",
        "consistency": "the claim is consistent with a fresh observed run",
        "drift_disclosure": None,
        "sources": {"repro_artifact": "x"},
    }
    _write_receipt(tmp_path, "aaa", claim_receipt)  # into reproduction_receipts.jsonl
    append_jsonl_line(
        tmp_path / "_aggregated" / "aaa" / "claim_check_receipts.jsonl", claim_receipt
    )

    # a claim-check is NOT reproduction evidence: the fingerprint admission join
    # must never be consulted for it (no admission leak).
    def _boom(*a: Any, **k: Any) -> bool:
        raise AssertionError("admission join consulted for a claim-check receipt")

    monkeypatch.setattr("hpc_agent.ops.program_verify._pair_admitted", _boom)

    res = program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["aaa"]))
    (c,) = res.constituents
    assert c.classification == CLASS_NONE
    assert c.receipt_count == 0
    assert c.driving_receipt is None
    assert c.reason == "no reproduction receipt on record"


# --------------------------------------------------------------------------- #
# seeded-from-table path (extract-recipe reuse) + G4a proxy disclosure
# --------------------------------------------------------------------------- #
def test_seeded_from_table_resolves_and_signs(tmp_path: Path) -> None:
    _sidecar(tmp_path, "solo")
    agg = _persist_agg(tmp_path, "solo", ["solo"])
    _write_receipt(tmp_path, "solo-repro", _receipt("solo", "solo-repro", "auto_cleared"))

    res = program_verify(tmp_path, spec=ProgramVerifySpec(aggregate_path=str(agg)))
    assert res.seed_kind == "aggregate"
    assert res.resolved_run_ids == ["solo"]
    assert res.recipe_signature and len(res.recipe_signature) == 64
    assert res.gaps == []
    assert res.constituents[0].classification == CLASS_REPRODUCED
    assert res.overall == CLASS_REPRODUCED


def test_seeded_old_shape_table_discloses_g4a_gap(tmp_path: Path) -> None:
    # a pre-Task-1 table: no contributing_run_ids → extract-recipe discloses the
    # G4a table->run-set-link gap and resolves an empty set; program-verify passes
    # the gap through, disclosed not papered.
    agg = tmp_path / "_aggregated" / "legacy" / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    agg.write_text(
        json.dumps({"aggregated_metrics": {}, "provenance": {"source": "local_reduce"}}),
        encoding="utf-8",
    )
    res = program_verify(tmp_path, spec=ProgramVerifySpec(aggregate_path=str(agg)))
    codes = {g["code"] for g in res.gaps}
    assert "table-run-set-link-absent" in codes
    assert res.resolved_run_ids == []
    assert res.overall == CLASS_NONE


def test_campaign_seed_resolves_run_set(tmp_path: Path) -> None:
    from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    def _seed(run_id: str) -> None:
        _sidecar(tmp_path, run_id, campaign_id="camp")
        upsert_run(
            tmp_path,
            RunRecord(
                run_id=run_id,
                profile="p",
                cluster="hoffman2",
                ssh_target="host",
                remote_path="/remote",
                job_name="job",
                job_ids=["1"],
                total_tasks=1,
                submitted_at=_TS,
                experiment_dir="/exp",
                campaign_id="camp",
            ),
        )
        append_jsonl_line(
            harvest_marker_path(tmp_path, run_id),
            {"run_id": run_id, "harvested_at": _TS, "harvest_ok": True},
        )

    _seed("camp-good-11111111")
    res = program_verify(tmp_path, spec=ProgramVerifySpec(campaign_id="camp"))
    assert res.seed_kind == "campaign"
    assert "camp-good-11111111" in res.resolved_run_ids


# --------------------------------------------------------------------------- #
# write-once manifest — idempotency + content-drift delta
# --------------------------------------------------------------------------- #
def test_manifest_write_once_idempotent(tmp_path: Path) -> None:
    _seed_four_classes(tmp_path)
    spec = ProgramVerifySpec(run_ids=["aaa", "bbb", "ccc", "ddd"])
    r1 = program_verify(tmp_path, spec=spec)
    assert r1.manifest_path is not None
    manifest_file = tmp_path / ".hpc" / "provenance" / f"program-{r1.program_signature[:12]}.json"
    assert manifest_file.is_file()
    before = manifest_file.read_bytes()

    r2 = program_verify(tmp_path, spec=spec)
    assert r2.manifest_path == r1.manifest_path
    assert r2.program_signature == r1.program_signature
    assert r2.manifest_delta is None  # write-once: no rewrite, no drift
    assert manifest_file.read_bytes() == before

    # the signed manifest is self-attesting (reuses provenance-manifest's signing
    # helper) + carries identity only (no verdict — so re-runs stay idempotent).
    from hpc_agent.ops.provenance_manifest import manifest_signature

    written = json.loads(before.decode("utf-8"))
    body = {k: v for k, v in written.items() if k != "signature"}
    assert manifest_signature(body) == written["signature"] == r1.program_signature
    assert "constituents" not in written and "overall" not in written


def test_manifest_content_drift_writes_new_file_and_discloses(tmp_path: Path) -> None:
    _seed_four_classes(tmp_path)
    spec = ProgramVerifySpec(run_ids=["aaa", "bbb", "ccc", "ddd"])
    r1 = program_verify(tmp_path, spec=spec)

    # drift a constituent's identity (a new code sha) → the fingerprint moves →
    # a new program signature → a NEW write-once file + a disclosed delta.
    _sidecar(tmp_path, "aaa", tasks_py_sha="tsha-aaa-DRIFTED")
    r2 = program_verify(tmp_path, spec=spec)
    assert r2.program_signature != r1.program_signature
    assert r2.manifest_path != r1.manifest_path
    assert r2.manifest_delta is not None
    assert "content drifted" in r2.manifest_delta


# --------------------------------------------------------------------------- #
# mirror drift pins: the CLASS_* vocabulary <-> the wire Literal, the
# receipt-reason counting grammar <-> verify_reproduction._render_reason, and
# the render discipline <-> recipe_render.render_recipe
# --------------------------------------------------------------------------- #
def test_class_constants_match_the_wire_literal() -> None:
    """The ops-side CLASS_* constants are a deliberate twin of the wire
    ``ConstituentClassification`` Literal — adding/renaming a class is a
    reviewed vocabulary change in BOTH places, pinned equal here."""
    from typing import get_args

    from hpc_agent._wire.queries.program_verify import ConstituentClassification

    ops_classes = {
        CLASS_REPRODUCED,
        CLASS_MISMATCH,
        CLASS_INCOMPARABLE,
        CLASS_STALE_IDENTITY,
        CLASS_NONE,
    }
    assert ops_classes == set(get_args(ConstituentClassification))


def test_receipt_reason_counting_matches_verify_reproduction() -> None:
    """``_receipt_reason`` counts per-key verdicts with the SAME grammar
    ``verify_reproduction._render_reason`` renders ("N matched, M mismatched,
    K incomparable of T") — the two render paths must not drift apart."""
    from hpc_agent.ops.program_verify import _receipt_reason
    from hpc_agent.ops.verify_reproduction import _render_reason as vr_render_reason

    per_key = [
        {"key": "a", "verdict": "match"},
        {"key": "b", "verdict": "match"},
        {"key": "c", "verdict": "mismatch"},
        {"key": "d", "verdict": "incomparable"},
    ]
    receipt = {"overall": "mismatch", "per_key": per_key}
    fragment = "2 matched, 1 mismatched, 1 incomparable of 4"
    assert fragment in _receipt_reason(receipt, CLASS_MISMATCH, "repro-1")
    assert fragment in vr_render_reason(per_key, "mismatch")


def test_render_discipline_lockstep_with_recipe_render() -> None:
    """``_render_markdown`` shares ``recipe_render.render_recipe``'s render
    discipline (the MIRROR pin), asserted on BOTH sides so a drift in either
    render fails here:

    * determinism — byte-stable across two renders of the same input, and no
      wall-clock content (a timestamped header on EITHER side trips the regex);
    * identity — the seed ref is named in the heading;
    * counting — a counted ``## ... (N)`` section header is present;
    * disclosure — a gap renders as ``- **<code>** — <detail>``.
    """
    from hpc_agent.ops.program_verify import _render_markdown
    from hpc_agent.ops.recipe_render import render_recipe

    recipe = {
        "seed_kind": "aggregate",
        "seed_ref": "table-1",
        "recipe_signature": "sig",
        "minimal_run_ids": ["aaa"],
        "runs": [{"run_id": "aaa", "cmd_sha": "c1"}],
        "excluded": [],
        "rederivation_steps": [{"verb": "aggregate-run"}],
        "receipts": [],
        "gaps": [{"code": "g1", "detail": "first gap"}],
    }
    result_fields: dict[str, Any] = {
        "seed_kind": "explicit",
        "seed_ref": "table-1",
        "resolved_run_ids": ["aaa"],
        "constituents": [
            {
                "run_id": "aaa",
                "classification": CLASS_REPRODUCED,
                "receipt_count": 1,
                "fingerprint_samples": 2,
                "reason": "r",
            }
        ],
        "gaps": [{"code": "g1", "detail": "first gap"}],
        "reproduced_count": 1,
        "total": 1,
        "overall": CLASS_REPRODUCED,
    }

    recipe_md = render_recipe(recipe)
    program_md = _render_markdown(result_fields)

    # Determinism, both sides: byte-stable for a given input, no wall-clock.
    assert render_recipe(recipe) == recipe_md
    assert _render_markdown(result_fields) == program_md
    stamp = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
    assert not stamp.search(recipe_md)
    assert not stamp.search(program_md)

    # Identity: the seed ref is named in the heading of BOTH renders.
    assert "`table-1`" in recipe_md.splitlines()[0]
    assert "`table-1`" in program_md.splitlines()[0]

    # Counting: BOTH renders carry a counted section header (``## ... (N)``).
    counted = re.compile(r"^## .+ \(\d+\)$", re.MULTILINE)
    assert counted.search(recipe_md)
    assert counted.search(program_md)

    # Disclosure: BOTH renders emit a gap as ``- **<code>** — <detail>``.
    assert "- **g1** — first gap" in recipe_md
    assert "- **g1** — first gap" in program_md


# --------------------------------------------------------------------------- #
# render determinism + no mutation
# --------------------------------------------------------------------------- #
def test_render_is_byte_stable_across_two_runs(tmp_path: Path) -> None:
    _seed_four_classes(tmp_path)
    spec = ProgramVerifySpec(run_ids=["aaa", "bbb", "ccc", "ddd"])
    a = program_verify(tmp_path, spec=spec)
    b = program_verify(tmp_path, spec=spec)
    assert a.markdown == b.markdown
    assert a.markdown.startswith("# Program-verify")
    assert a.reason == b.reason


def test_verb_never_mutates_run_state(tmp_path: Path) -> None:
    _seed_four_classes(tmp_path)
    # snapshot every sidecar + every reproduction receipt ledger.
    watched = list((tmp_path / ".hpc" / "runs").glob("*.json"))
    watched += list((tmp_path / "_aggregated").glob("*/reproduction_receipts.jsonl"))
    before = {p: p.read_bytes() for p in watched}

    program_verify(tmp_path, spec=ProgramVerifySpec(run_ids=["aaa", "bbb", "ccc", "ddd"]))

    for p, data in before.items():
        assert p.read_bytes() == data, f"{p} was mutated by a read-only projection"


# --------------------------------------------------------------------------- #
# spec validation
# --------------------------------------------------------------------------- #
def test_exactly_one_program_identity_source() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProgramVerifySpec()
    with pytest.raises(ValidationError):
        ProgramVerifySpec(run_ids=["a"], campaign_id="c")


def test_bad_aggregate_seed_refuses(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        program_verify(tmp_path, spec=ProgramVerifySpec(aggregate_path="/no/such/table.json"))
