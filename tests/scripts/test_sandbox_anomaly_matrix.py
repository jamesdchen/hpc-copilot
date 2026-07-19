"""Hermetic tests for ``scripts/sandbox_anomaly_matrix.py`` — the U6 anomaly arms.

Every test here is rung-1: no cluster, no docker, no subprocess, no socket —
canned briefs / journals / scheduler snapshots only. The pins cover the pure
contract the live matrix asserts against the CODE-RENDERED BRIEFS:

1. The §3 guard reuse (delegates to the U3 driver — no inline copy) + the
   ``main()`` refusal order (guard → scenario selection → cluster source).
2. The scancel/qdel command composition per scheduler family (the kill
   drill's ``--scheduler slurm|sge`` abstraction twin).
3. The scenario (a)-(d) brief-assertion shapes: ``canary_failed`` (failure
   kind + stderr tail on the brief), ``watching_anomaly`` (anomaly lifecycle,
   null successor), the reconcile terminal classification, the doctor re-arm
   proposal.
4. The namespace-coupling pin (U5.5's twin): a decoy stall in a SECOND
   namespace is invisible to a single-namespace scan and never re-armed by
   the fleet re-arm selection.
5. The alerts-ack watermark monotonicity + the attention-queue post-ack drop.
6. The matrix evidence/render shape (mirrors the U3 driver's).
7. The failing-executor variant name, pinned against the U1 fixture sibling.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "sandbox_anomaly_matrix", REPO_ROOT / "scripts" / "sandbox_anomaly_matrix.py"
)
assert _SPEC is not None and _SPEC.loader is not None
matrix = importlib.util.module_from_spec(_SPEC)
sys.modules["sandbox_anomaly_matrix"] = matrix
_SPEC.loader.exec_module(matrix)

driver = matrix.driver
SandboxRefusal = matrix.SandboxRefusal


# ── canned brief shapes (the relay-doctrine contracts the scenarios assert) ──


def _canary_failed_result() -> dict[str, Any]:
    return {
        "block": "s2",
        "stage_reached": "canary_failed",
        "needs_decision": True,
        "reason": "canary failed verification (traceback); propose a fix before main.",
        "run_id": "sandbox-u6-a-fail-deadbeef",
        "brief": {
            "verify_result": {
                "ok": False,
                "failure_kind": "traceback",
                "details": "canary task exited non-zero",
                "stderr_tail": "RuntimeError: sandbox failing-executor variant: every task fails",
            }
        },
        "next_block": None,
    }


def _watching_anomaly_result() -> dict[str, Any]:
    return {
        "block": "s3",
        "stage_reached": "watching_anomaly",
        "needs_decision": True,
        "reason": "main array reached 'abandoned' (no escalation reason); propose recovery.",
        "run_id": "sandbox-u6-b-watch-deadbeef",
        "brief": {"lifecycle_state": "abandoned", "main_job_ids": ["123"]},
        "next_block": None,
    }


def _reconcile_data() -> dict[str, Any]:
    return {
        "run_id": "sandbox-u6-b-watch-deadbeef",
        "lifecycle_state": "abandoned",
        "combined_waves": [],
        "failed_waves": [],
        "last_status": {"summary": {"done": 0, "running": 0, "failed": 8}},
    }


_SANDBOX_DIR = "/tmp/sandbox-anomaly/experiment-c"
_DECOY_DIR = "/tmp/sandbox-anomaly/experiment-decoy"
_RUN_ID = "sandbox-u6-c-stall-deadbeef"
_DECOY_RUN_ID = "sandbox-u6-c-decoy-999"


def _stalled_entry(run_id: str, experiment_dir: str | None) -> dict[str, Any]:
    where = f" [{experiment_dir}]" if experiment_dir else ""
    evidence: dict[str, Any] = {
        "last_tick_at": "2026-07-19T00:00:00Z",
        "next_tick_due": "2026-07-19T00:01:00Z",
        "now": "2026-07-19T01:00:00Z",
        "overdue_seconds": 3540,
    }
    if experiment_dir is not None:
        evidence["experiment_dir"] = experiment_dir
    return {
        "run_id": run_id,
        "status": "in_flight",
        "last_tick_at": "2026-07-19T00:00:00Z",
        "next_tick_due": "2026-07-19T00:01:00Z",
        "cluster": "slurmci",
        "ssh_target": "hpcuser@slurmci",
        "proposal": (
            f"driver stalled since 2026-07-19T00:00:00Z, status in_flight{where}: "
            "next tick was due 2026-07-19T00:01:00Z but has not fired. Re-arm the driver?"
        ),
        "evidence": evidence,
    }


def _doctor_data(*entries: dict[str, Any], needs_attention: bool = True) -> dict[str, Any]:
    return {
        "now": "2026-07-19T01:00:00Z",
        "needs_attention": needs_attention,
        "attention_summary": "1 stalled driver",
        "alerts": [],
        "stalled_count": len(entries),
        "stalled": list(entries),
        "parked_count": 0,
        "parked": [],
        "awaiting_advance_count": 0,
        "awaiting_advance": [],
        "open_ssh_circuits": [],
        "version_skew": None,
        "active_env_overrides": {},
    }


def _attention_data(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "computed_at": "2026-07-19T01:00:00Z",
        "items": list(items),
        "counts": {"informational": len(items)},
        "skipped": [],
        "render": "# attention queue",
    }


def _alert_item(ts: str) -> dict[str, Any]:
    return {
        "kind": "alert",
        "class": "informational",
        "subject": {"scope_kind": None, "scope_id": ts},
        "experiment_dir": _SANDBOX_DIR,
        "since": ts,
    }


# ── §3 guard reuse (delegation, never an inline copy) ───────────────────────


def test_guard_delegates_unset() -> None:
    with pytest.raises(SandboxRefusal, match="HPC_JOURNAL_DIR is unset"):
        matrix.require_journal_home({})


def test_guard_delegates_production_home() -> None:
    production = Path.home() / ".claude" / "hpc"
    with pytest.raises(SandboxRefusal, match="production journal home"):
        matrix.require_journal_home({"HPC_JOURNAL_DIR": str(production)})


def test_guard_accepts_ephemeral_tmp(tmp_path: Path) -> None:
    home = matrix.require_journal_home({"HPC_JOURNAL_DIR": str(tmp_path / "j")})
    assert home == (tmp_path / "j").resolve()


def test_guard_is_the_driver_guard_object() -> None:
    # The matrix reuses the driver's guard function (single definition) — no
    # inline copy that could drift past the shared sandbox guard.
    assert matrix.require_journal_home.__module__ == "sandbox_anomaly_matrix"
    assert matrix.SandboxRefusal is driver.SandboxRefusal


def test_matrix_binds_one_driver_object() -> None:
    # A SECOND by-path load converges on the one registered driver (sys.modules
    # probe) — the tests and the matrix share one driver instance.
    spec = importlib.util.spec_from_file_location(
        "_matrix_second_load", REPO_ROOT / "scripts" / "sandbox_anomaly_matrix.py"
    )
    assert spec is not None and spec.loader is not None
    second = importlib.util.module_from_spec(spec)
    sys.modules["_matrix_second_load"] = second  # importlib contract: register before exec
    try:
        spec.loader.exec_module(second)
    finally:
        sys.modules.pop("_matrix_second_load", None)
    assert second.driver is driver


# ── main() refusal order (each exits 2 before any cluster work) ─────────────


def test_main_refuses_without_journal_env(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.delenv("HPC_JOURNAL_DIR", raising=False)
    assert matrix.main([]) == 2
    assert "HPC_JOURNAL_DIR is unset" in capsys.readouterr().err


def test_main_refuses_unknown_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    assert matrix.main(["--scenarios", "a,z"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_main_refuses_without_cluster_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    assert matrix.main([]) == 2
    assert "--clusters-config" in capsys.readouterr().err


def test_main_setup_failure_flips_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    # A missing clusters config is a SETUP refusal: the evidence records it
    # (never silently passes) and the exit code is 1.
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    out = tmp_path / "evidence.json"
    rc = matrix.main(
        [
            "--clusters-config",
            str(tmp_path / "absent.yaml"),
            "--workdir",
            str(tmp_path),
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert evidence["verdict"] == "fail"
    assert "setup" in evidence["failed_steps"]
    assert "verdict FAIL" in capsys.readouterr().out


def test_parse_scenario_selection_dedup_preserves_order() -> None:
    assert matrix._parse_scenario_selection("b,a,b") == ["b", "a"]
    assert matrix._parse_scenario_selection("a,b,c,d") == ["a", "b", "c", "d"]


def test_parse_scenario_selection_refuses_unknown() -> None:
    with pytest.raises(SandboxRefusal, match="unknown scenario"):
        matrix._parse_scenario_selection("a,nope")


# ── scancel / qdel composition (the --scheduler family abstraction) ─────────


def test_compose_cancel_slurm() -> None:
    assert matrix.compose_cancel_command("slurm", ["123"]) == "scancel 123"
    assert matrix.compose_cancel_command("slurm", ["12", "34"]) == "scancel 12 34"


def test_compose_cancel_slurm_task_range() -> None:
    # scancel's array-subscript form, one call per id.
    assert (
        matrix.compose_cancel_command("slurm", ["12"], task_range="4,8,13-15")
        == "scancel 12_[4,8,13-15]"
    )
    assert (
        matrix.compose_cancel_command("slurm", ["12", "34"], task_range="1-3")
        == "scancel 12_[1-3] scancel 34_[1-3]"
    )


def test_compose_cancel_sge() -> None:
    assert matrix.compose_cancel_command("sge", ["123"]) == "qdel 123"
    assert matrix.compose_cancel_command("sge", ["12", "34"]) == "qdel 12 34"


def test_compose_cancel_sge_task_range() -> None:
    # SGE qdel -t takes ONE range over the addressed ids.
    assert (
        matrix.compose_cancel_command("sge", ["12", "34"], task_range="4-8") == "qdel 12 34 -t 4-8"
    )


def test_compose_cancel_refuses_unknown_scheduler() -> None:
    with pytest.raises(SandboxRefusal, match="no cancel grammar"):
        matrix.compose_cancel_command("lsf", ["123"])


def test_compose_cancel_refuses_empty_ids() -> None:
    with pytest.raises(SandboxRefusal, match="at least one job id"):
        matrix.compose_cancel_command("slurm", [])


# ── scenario (a): the canary_failed brief ────────────────────────────────────


def test_canary_failed_brief_good() -> None:
    assert matrix.assert_canary_failed_brief(_canary_failed_result()) == []


def test_canary_failed_brief_wrong_stage() -> None:
    result = _canary_failed_result()
    result["stage_reached"] = "canary_verified"
    problems = matrix.assert_canary_failed_brief(result)
    assert any("stage_reached" in p for p in problems)


def test_canary_failed_brief_requires_stderr_tail() -> None:
    result = _canary_failed_result()
    result["brief"]["verify_result"]["stderr_tail"] = ""
    problems = matrix.assert_canary_failed_brief(result)
    assert any("stderr_tail" in p for p in problems)


def test_canary_failed_brief_requires_failure_kind_named_in_reason() -> None:
    result = _canary_failed_result()
    result["reason"] = "canary failed; propose a fix."
    problems = matrix.assert_canary_failed_brief(result)
    assert any("failure kind" in p for p in problems)


def test_canary_failed_brief_requires_verdict_and_decision() -> None:
    result = _canary_failed_result()
    result["brief"]["verify_result"]["ok"] = True
    result["needs_decision"] = False
    problems = matrix.assert_canary_failed_brief(result)
    assert any("verify_result.ok" in p for p in problems)
    assert any("needs_decision" in p for p in problems)
    assert matrix.assert_canary_failed_brief({})  # non-matching shape is never a pass
    assert matrix.assert_canary_failed_brief({"brief": {}})  # no verify_result


# ── scenario (b): the watching_anomaly brief + reconcile classification ──────


def test_watching_anomaly_brief_good() -> None:
    assert matrix.assert_watching_anomaly_brief(_watching_anomaly_result()) == []


def test_watching_anomaly_brief_rejects_complete_lifecycle() -> None:
    result = _watching_anomaly_result()
    result["brief"]["lifecycle_state"] = "complete"
    problems = matrix.assert_watching_anomaly_brief(result)
    assert any("lifecycle_state" in p for p in problems)


def test_watching_anomaly_brief_rejects_successor() -> None:
    result = _watching_anomaly_result()
    result["next_block"] = {"verb": "submit-s4"}
    problems = matrix.assert_watching_anomaly_brief(result)
    assert any("next_block must be null" in p for p in problems)


def test_watching_anomaly_brief_wrong_stage_and_decision() -> None:
    result = _watching_anomaly_result()
    result["stage_reached"] = "watching_terminal"
    result["needs_decision"] = False
    problems = matrix.assert_watching_anomaly_brief(result)
    assert any("stage_reached" in p for p in problems)
    assert any("needs_decision" in p for p in problems)


def test_terminal_classification_good() -> None:
    assert (
        matrix.assert_terminal_classification_brief(
            _reconcile_data(), run_id="sandbox-u6-b-watch-deadbeef"
        )
        == []
    )


def test_terminal_classification_rejects_in_flight() -> None:
    data = _reconcile_data()
    data["lifecycle_state"] = "in_flight"
    problems = matrix.assert_terminal_classification_brief(data)
    assert any("in_flight" in p for p in problems)


def test_terminal_classification_run_id_and_status_shape() -> None:
    data = _reconcile_data()
    data["last_status"] = None
    problems = matrix.assert_terminal_classification_brief(data, run_id="other-run")
    assert any("run_id" in p for p in problems)
    assert any("last_status" in p for p in problems)


# ── scenario (c): doctor re-arm proposal + the namespace-coupling pin ────────


def test_doctor_proposal_good_single_namespace() -> None:
    data = _doctor_data(_stalled_entry(_RUN_ID, None))
    assert matrix.assert_doctor_proposal(data, run_id=_RUN_ID) == []


def test_doctor_proposal_fleet_names_namespace() -> None:
    data = _doctor_data(_stalled_entry(_RUN_ID, _SANDBOX_DIR))
    assert matrix.assert_doctor_proposal(data, run_id=_RUN_ID, namespace=_SANDBOX_DIR) == []


def test_doctor_proposal_namespace_not_named() -> None:
    # Fleet entry attributed to the DECOY dir while the sandbox dir is asked.
    data = _doctor_data(_stalled_entry(_RUN_ID, _DECOY_DIR))
    problems = matrix.assert_doctor_proposal(data, run_id=_RUN_ID, namespace=_SANDBOX_DIR)
    assert any("sandbox namespace" in p for p in problems)


def test_doctor_proposal_missing_run() -> None:
    data = _doctor_data(_stalled_entry("other-run", None))
    problems = matrix.assert_doctor_proposal(data, run_id=_RUN_ID)
    assert any("no stalled proposal" in p for p in problems)


def test_doctor_proposal_no_attention() -> None:
    data = _doctor_data(needs_attention=False)
    problems = matrix.assert_doctor_proposal(data, run_id=_RUN_ID)
    assert any("needs_attention" in p for p in problems)


def test_decoy_namespace_invisible_to_single_scan() -> None:
    # THE namespace-scoping pin (U5.5 twin): a single-namespace scan proposing
    # the sandbox run must NOT carry the decoy from the second namespace.
    data = _doctor_data(_stalled_entry(_RUN_ID, None))
    assert matrix.assert_run_not_proposed(data, run_id=_DECOY_RUN_ID) == []
    leaked = _doctor_data(_stalled_entry(_RUN_ID, None), _stalled_entry(_DECOY_RUN_ID, None))
    problems = matrix.assert_run_not_proposed(leaked, run_id=_DECOY_RUN_ID)
    assert any("decoy" in p for p in problems)


def test_filter_namespace_proposals_excludes_decoy() -> None:
    # The re-arm selection over a fleet scan: the sandbox entry is kept, the
    # decoy-namespace entry is dropped — a decoy alert is never re-armed.
    data = _doctor_data(
        _stalled_entry(_RUN_ID, _SANDBOX_DIR), _stalled_entry(_DECOY_RUN_ID, _DECOY_DIR)
    )
    selected = matrix.filter_namespace_proposals(data, _SANDBOX_DIR)
    assert [e["run_id"] for e in selected] == [_RUN_ID]
    # The mirror selection from the decoy side keeps only the decoy.
    decoy_selected = matrix.filter_namespace_proposals(data, _DECOY_DIR)
    assert [e["run_id"] for e in decoy_selected] == [_DECOY_RUN_ID]


def test_filter_keeps_unattributed_entries() -> None:
    # A single-namespace (non-fleet) entry carries no experiment_dir
    # attribution — it came from the scanned namespace, so it is kept.
    data = _doctor_data(_stalled_entry(_RUN_ID, None))
    selected = matrix.filter_namespace_proposals(data, _SANDBOX_DIR)
    assert [e["run_id"] for e in selected] == [_RUN_ID]


# ── scenario (d): alerts-ack watermark + attention-queue drop ────────────────


def test_watermark_from_none() -> None:
    assert matrix.advance_ack_watermark(None, "2026-07-19T01:00:00Z") == "2026-07-19T01:00:00Z"


def test_watermark_monotonic_never_regresses() -> None:
    current = "2026-07-19T02:00:00Z"
    stale = "2026-07-19T01:00:00Z"
    # A stale ack leaves the watermark where it is (nothing resurrected).
    assert matrix.advance_ack_watermark(current, stale) == current


def test_watermark_advances_forward_and_equal() -> None:
    older = "2026-07-19T01:00:00Z"
    newer = "2026-07-19T03:00:00Z"
    assert matrix.advance_ack_watermark(older, newer) == newer
    assert matrix.advance_ack_watermark(newer, newer) == newer


def test_watermark_refuses_unparsable_requested() -> None:
    with pytest.raises(SandboxRefusal, match="not ISO-8601"):
        matrix.advance_ack_watermark(None, "not-a-timestamp")


def test_watermark_tolerates_unparsable_current() -> None:
    requested = "2026-07-19T01:00:00Z"
    assert matrix.advance_ack_watermark("garbage", requested) == requested


def test_parse_iso_utc_handles_z_and_offset() -> None:
    z = matrix._parse_iso_utc("2026-07-19T01:02:03Z")
    offset = matrix._parse_iso_utc("2026-07-19T03:02:03+02:00")
    assert z is not None and offset is not None and z == offset
    assert matrix._parse_iso_utc("nope") is None
    assert matrix._parse_iso_utc(None) is None


def test_find_alert_items_and_no_alert_assertion() -> None:
    alert = _alert_item("2026-07-19T01:00:00Z")
    stalled_item = {
        "kind": "run-stalled",
        "class": "blocked",
        "subject": {"scope_kind": "run", "scope_id": _RUN_ID},
        "experiment_dir": _SANDBOX_DIR,
    }
    data = _attention_data(alert, stalled_item)
    alerts = matrix.find_alert_items(data)
    assert [a["subject"]["scope_id"] for a in alerts] == ["2026-07-19T01:00:00Z"]
    # Post-ack: the alert is gone (the run-stalled item may legitimately stay).
    assert matrix.assert_no_alert_items(_attention_data(stalled_item)) == []
    problems = matrix.assert_no_alert_items(data)
    assert any("still lists 1 alert" in p for p in problems)


# ── matrix evidence + render (mirrors the U3 driver shape) ───────────────────


def _row(step: str, passed: bool) -> dict[str, Any]:
    row: dict[str, Any] = driver.build_evidence_row(step, "where", "check", passed)
    return row


def test_matrix_evidence_verdict_pass() -> None:
    evidence = matrix.build_matrix_evidence(
        {"run_ref": "u6"},
        {"a": {"description": "arm a", "rows": [_row("a.x", True)]}},
    )
    assert evidence["verdict"] == "pass"
    assert evidence["failed_steps"] == []
    assert evidence["scenarios"]["a"]["verdict"] == "pass"
    assert evidence["kind"] == "sandbox-anomaly-matrix-evidence"
    assert len(evidence["rows"]) == 1


def test_matrix_evidence_verdict_fail_tags_scenario() -> None:
    evidence = matrix.build_matrix_evidence(
        {},
        {
            "a": {"description": "arm a", "rows": [_row("a.good", True)]},
            "b": {"description": "arm b", "rows": [_row("b.bad", False)]},
        },
    )
    assert evidence["verdict"] == "fail"
    assert evidence["failed_steps"] == ["b.bad"]
    assert evidence["scenarios"]["a"]["verdict"] == "pass"
    assert evidence["scenarios"]["b"]["verdict"] == "fail"
    assert evidence["scenarios"]["b"]["failed_steps"] == ["b.bad"]


def test_matrix_evidence_extra_blocks_reach_the_verdict() -> None:
    # A non-canonical block (the main() "setup" refusal) must NOT drop out of
    # the flat rows — its failure flips the verdict — and canonical scenarios
    # keep the a-d order ahead of it.
    evidence = matrix.build_matrix_evidence(
        {},
        {
            "setup": {"description": "refused", "rows": [_row("setup.x", False)]},
            "b": {"description": "arm b", "rows": [_row("b.x", True)]},
        },
    )
    assert list(evidence["scenarios"]) == ["b", "setup"]
    assert evidence["verdict"] == "fail"
    assert evidence["failed_steps"] == ["setup.x"]
    assert [r["step"] for r in evidence["rows"]] == ["b.x", "setup.x"]


def test_matrix_render_shape() -> None:
    evidence = matrix.build_matrix_evidence(
        {"run_ref": "u6", "cluster": "slurmci", "scheduler": "slurm", "scenarios": "a"},
        {"a": {"description": "failing canary", "rows": [_row("a.x", True)]}},
    )
    md = matrix.render_matrix_markdown(evidence)
    assert "# Sandbox anomaly matrix evidence (U6 — rung 2)" in md
    assert "## Scenario (a) — failing canary" in md
    assert "| Step | Where | Mechanical check | Pass |" in md
    assert "| a.x | where | check | yes |" in md
    assert "**Verdict: pass**" in md
    assert "Rung-2 jurisdiction" in md


def test_matrix_render_marks_failing_rows() -> None:
    evidence = matrix.build_matrix_evidence(
        {},
        {"b": {"description": "mid-watch cancel", "rows": [_row("b.anomaly", False)]}},
    )
    md = matrix.render_matrix_markdown(evidence)
    assert "| b.anomaly | where | check | **NO** |" in md
    assert "verdict: **fail**" in md


def test_matrix_render_includes_extra_blocks() -> None:
    evidence = matrix.build_matrix_evidence(
        {},
        {"setup": {"description": "matrix setup (refused)", "rows": [_row("setup", False)]}},
    )
    md = matrix.render_matrix_markdown(evidence)
    assert "## Scenario (setup) — matrix setup (refused)" in md
    assert "**Verdict: fail**" in md


# ── the failing-executor variant pin (against the U1 fixture sibling) ────────


def test_failing_variant_constant_matches_fixture_sibling() -> None:
    # Load the REAL U1 fixture (import-safe: its hpc_agent imports are inside
    # build_sandbox_experiment; no journal writes at import) and pin the
    # variant name the scenario-(a) arm passes.
    fixture = driver.load_sibling_module(driver.FIXTURE_MODULE_PATH, label="sandbox_fixture")
    variants = fixture._TRAIN_PY_BY_VARIANT
    assert matrix.FAILING_EXECUTOR_VARIANT in variants
    assert matrix.WORKING_EXECUTOR_VARIANT in variants
    failing_src = variants[matrix.FAILING_EXECUTOR_VARIANT]
    working_src = variants[matrix.WORKING_EXECUTOR_VARIANT]
    # The failing variant raises on every task (the canary_failed arm); the
    # working variant computes (the resubmit-FIXED arm).
    assert "raise RuntimeError" in failing_src
    assert "raise" not in working_src


# ── per-scenario context delegation ──────────────────────────────────────────


def test_scenario_context_delegates_to_chain_context(tmp_path: Path) -> None:
    ctx = driver.ChainContext(
        env={},
        journal_home=tmp_path,
        workdir=tmp_path,
        scratch=tmp_path / "specs",
        experiment_dir=None,
        cluster="slurmci",
        configured_clusters=["slurmci"],
        ssh_target="hpcuser@slurmci",
        backend="slurm",
        remote_path_stanza={},
        goal="g",
        run_name="r",
        run_ref="ref",
        wait_timeout=1,
        poll_interval=1,
        run_preflight=False,
        walltime_sec=60,
    )
    state = driver.ChainState()
    wrapped = matrix._ScenarioContext(ctx, state)
    assert wrapped.cluster == "slurmci"  # delegated read
    wrapped.experiment_dir = tmp_path / "exp"  # delegated write
    assert ctx.experiment_dir == tmp_path / "exp"
    assert wrapped.state is state  # the scenario's own evidence accumulator
