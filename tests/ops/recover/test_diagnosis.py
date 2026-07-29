"""Tests for the park-time diagnosis seam (diagnosis-request / attach-diagnosis).

Fire-proofs the doctrinal envelope end to end:

* the REQUEST is code-composed, parked-runs-only, reuses THE one catalog
  classifier over evidence the stores already hold, and names paths (never
  content);
* the ATTACH is shape-validated (closed category set), provenance-stamped
  agent by the state layer, overwrite-on-reattach, ungated;
* the SURFACES carry pointers + counts (park notice, doctor's parked note,
  the morning digest's parked section);
* and the SEPARATION contract: diagnosis content NEVER appears in the
  decision-brief provenance store or the answer menu's code-authored options.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._kernel.lifecycle.answer_menu import compose_answer_menu
from hpc_agent._wire.actions.attach_diagnosis import (
    AttachDiagnosisSpec,
    DiagnosisEvidenceExcerpt,
    DiagnosisProposedAction,
)
from hpc_agent._wire.queries.diagnosis_request import DiagnosisRequestSpec
from hpc_agent.infra.failure_signatures import CLASSIFIER_CATEGORIES
from hpc_agent.ops.recover.diagnosis import (
    attach_diagnosis,
    compose_diagnosis_request,
    diagnosis_request,
)
from hpc_agent.state.block_terminal import record_terminal
from hpc_agent.state.decision_briefs import append_brief, briefs_path, read_briefs
from hpc_agent.state.diagnosis import diagnosis_path, read_diagnosis
from hpc_agent.state.journal import mark_pending_decision, upsert_run
from hpc_agent.state.run_record import RunRecord, current_homedir

RUN = "run-diag"

#: A brief shaped like the S2 canary_failed park brief: the verify result's
#: failure_features (the stored evidence + stored classification) plus the
#: answer menu the driver composed at park (OVERRIDE advance = anomaly).
_ANOMALY_BRIEF = {
    "verify_result": {
        "failure_features": {
            "cluster_log_tail": "RuntimeError: CUDA out of memory. Tried to allocate 2.0 GiB",
            "log_path": "/remote/logs/task.err",
            "classified_error": {
                "error_class": "gpu_oom",
                "suggested_fix": {"action": "increase-mem-per-gpu", "factor": 1.5},
                "matched_pattern": "CUDA out of memory",
            },
        }
    },
    "answer_menu": {
        "options": [{"paste": "y", "kind": "advance", "override": True, "target": "submit-s3"}],
        "answer_line": "y",
        "summary": "parked at submit-s2 — OVERRIDE",
    },
}


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str = RUN) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-29T00:00:00+00:00",
        experiment_dir="/exp",
        status="in_flight",
    )


def _park(exp: Path, run_id: str = RUN, *, brief: dict | None = None) -> None:
    upsert_run(exp, _record(run_id))
    mark_pending_decision(
        run_id,
        block="submit-s2",
        workflow="submit",
        brief=_ANOMALY_BRIEF if brief is None else brief,
        resume_cursor={"workflow": "submit", "run_id": run_id, "next_verb": "submit-s3"},
        awaiting_since="2026-07-29T00:30:00+00:00",
        experiment_dir=exp,
    )


def _record_anomaly_terminal(exp: Path, run_id: str = RUN) -> None:
    record_terminal(
        exp,
        run_id=run_id,
        block="submit-s2",
        cmd_sha="sha-1",
        result_dump={
            "block": "s2",
            "stage_reached": "canary_failed",
            "needs_decision": True,
            "reason": "canary failed verification (dispatcher_failed); propose a fix before main.",
            "run_id": run_id,
            "brief": _ANOMALY_BRIEF,
        },
    )


def _spec(**overrides) -> AttachDiagnosisSpec:
    base = {
        "run_id": RUN,
        "classification": "gpu_oom",
        "evidence_excerpts": [{"path": "/logs/task.err", "lines": "CUDA out of memory"}],
        "proposed_actions": [
            {
                "label": "raise mem",
                "rationale": "gpu_oom signature over the canary tail",
                "suggested_response_text": "raise mem_mb 1.5x, then resubmit-failed",
            }
        ],
    }
    base.update(overrides)
    return AttachDiagnosisSpec.model_validate(base)


# ── the request composer ─────────────────────────────────────────────────────


class TestComposeDiagnosisRequest:
    def test_parked_anomaly_run_composes_the_full_request(self, tmp_path: Path) -> None:
        _park(tmp_path)
        _record_anomaly_terminal(tmp_path)
        # A worker log for this run in the journal's _detached dir.
        detached = current_homedir() / "_detached"
        detached.mkdir(parents=True, exist_ok=True)
        log = detached / f"submit-s2-{RUN}-abcd1234.log"
        log.write_text("worker log", encoding="utf-8")

        request = compose_diagnosis_request(tmp_path, RUN)
        assert request is not None
        assert request["run_id"] == RUN
        assert request["block"] == "submit-s2"
        assert request["workflow"] == "submit"
        assert request["stage_reached"] == "canary_failed"
        assert "canary failed verification" in (request["reason"] or "")
        assert request["is_anomaly"] is True
        # The closed vocabulary, verbatim from the one catalog source.
        assert request["categories"] == sorted(CLASSIFIER_CATEGORIES)
        # Paths only, existing files only — never content.
        assert str(log) in request["worker_logs"]
        assert request["read_paths"]["journal_record"].endswith(f"{RUN}.json")
        assert request["read_paths"]["block_terminal"].endswith(f"{RUN}.submit-s2.terminal.json")
        assert request["attach_target"].endswith(f"{RUN}.diagnosis.json")
        assert request["diagnosis_attached"] is False
        # Paths, classifications, and vocabulary — never the log CONTENT: the
        # raw tail the store holds does not ride the request (the investigator
        # reads it from the named files).
        assert "Tried to allocate" not in json.dumps(request)

    def test_signature_matches_reuse_the_one_classifier(self, tmp_path: Path) -> None:
        """The stored classified_error is relayed VERBATIM (tagged stored); a fresh
        classification of the same stored tail dedups onto the same error_class —
        one catalog, never a second matcher, never a duplicate row."""
        _park(tmp_path)
        _record_anomaly_terminal(tmp_path)
        request = compose_diagnosis_request(tmp_path, RUN)
        assert request is not None
        matches = request["signature_matches"]
        gpu = [m for m in matches if m["error_class"] == "gpu_oom"]
        assert len(gpu) == 1
        assert "(stored)" in gpu[0]["source"]
        assert gpu[0]["suggested_fix"] == {"action": "increase-mem-per-gpu", "factor": 1.5}

    def test_no_park_returns_none_and_the_verb_refuses(self, tmp_path: Path) -> None:
        upsert_run(tmp_path, _record())  # a live run, NOT parked
        assert compose_diagnosis_request(tmp_path, RUN) is None
        with pytest.raises(errors.PreconditionFailed):
            diagnosis_request(tmp_path, spec=DiagnosisRequestSpec(run_id=RUN))
        # A run with no record at all is equally not-parked.
        assert compose_diagnosis_request(tmp_path, "ghost") is None

    def test_anomaly_fallback_reads_the_menu_override_when_no_terminal(
        self, tmp_path: Path
    ) -> None:
        """No terminal record on disk (a synchronous park): the anomaly read
        falls back to the answer menu's OVERRIDE projection — still code-decided."""
        _park(tmp_path)  # _ANOMALY_BRIEF carries override: True; no terminal recorded
        request = compose_diagnosis_request(tmp_path, RUN)
        assert request is not None
        assert request["stage_reached"] is None
        assert request["is_anomaly"] is True
        # A plain greenlight park (no override) reads False.
        plain_brief = {"answer_menu": {"options": [{"paste": "y", "override": False}]}}
        _park(tmp_path, "run-plain", brief=plain_brief)
        plain = compose_diagnosis_request(tmp_path, "run-plain")
        assert plain is not None
        assert plain["is_anomaly"] is False

    def test_the_verb_returns_the_wire_shape(self, tmp_path: Path) -> None:
        _park(tmp_path)
        _record_anomaly_terminal(tmp_path)
        result = diagnosis_request(tmp_path, spec=DiagnosisRequestSpec(run_id=RUN))
        dumped = result.model_dump(mode="json")
        assert dumped["is_anomaly"] is True
        assert dumped["note"].startswith("code-composed diagnosis request")


# ── the attach channel ───────────────────────────────────────────────────────


class TestAttachDiagnosis:
    def test_roundtrip_and_provenance(self, tmp_path: Path) -> None:
        result = attach_diagnosis(tmp_path, spec=_spec())
        assert result.overwrote is False
        assert result.proposed_actions_count == 1
        assert Path(result.path).is_file()
        stored = read_diagnosis(tmp_path, RUN)
        assert stored is not None
        assert stored["provenance"]["authored_by"] == "agent"
        assert stored["classification"] == "gpu_oom"

    def test_reattach_overwrites(self, tmp_path: Path) -> None:
        attach_diagnosis(tmp_path, spec=_spec())
        result = attach_diagnosis(
            tmp_path, spec=_spec(classification="unmatched", proposed_actions=[])
        )
        assert result.overwrote is True
        stored = read_diagnosis(tmp_path, RUN)
        assert stored is not None
        assert stored["classification"] == "unmatched"
        assert stored["proposed_actions"] == []

    def test_classification_outside_the_closed_set_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(errors.SpecInvalid):
            attach_diagnosis(tmp_path, spec=_spec(classification="creative_new_category"))
        # "unknown" is the MATCHER's no-hit literal, not an investigator claim.
        with pytest.raises(errors.SpecInvalid):
            attach_diagnosis(tmp_path, spec=_spec(classification="unknown"))
        assert read_diagnosis(tmp_path, RUN) is None  # nothing was written

    def test_every_catalog_category_and_unmatched_are_accepted(self, tmp_path: Path) -> None:
        for category in [*sorted(CLASSIFIER_CATEGORIES), "unmatched"]:
            result = attach_diagnosis(tmp_path, spec=_spec(classification=category))
            assert result.classification == category

    def test_spec_cannot_smuggle_a_provenance(self) -> None:
        """extra='forbid': the wire shape has no provenance seat to forge."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AttachDiagnosisSpec.model_validate(
                {"run_id": RUN, "classification": "gpu_oom", "authored_by": "code"}
            )

    def test_corrupt_dossier_reads_fail_open(self, tmp_path: Path) -> None:
        attach_diagnosis(tmp_path, spec=_spec())
        diagnosis_path(tmp_path, RUN).write_text("{torn", encoding="utf-8")
        assert read_diagnosis(tmp_path, RUN) is None
        request_visible = compose_diagnosis_request  # corrupt ≠ crash anywhere
        _park(tmp_path)
        request = request_visible(tmp_path, RUN)
        assert request is not None
        assert request["diagnosis_attached"] is False


# ── the surfaces: pointers + counts, never content ───────────────────────────


class TestSurfacePointers:
    def test_park_notice_says_none_without_a_dossier(self, tmp_path: Path) -> None:
        from hpc_agent.ops.recover.notify import compose_park_notice

        text = compose_park_notice(
            {"run_id": RUN, "block": "submit-s2", "awaiting_since": "T", "brief": _ANOMALY_BRIEF}
        )
        assert "diagnosis: none" in text

    def test_park_notice_carries_the_pointer_when_attached(self, tmp_path: Path) -> None:
        from hpc_agent.ops.recover import notify
        from hpc_agent.state.diagnosis import diagnosis_pointer

        attach_diagnosis(tmp_path, spec=_spec())
        pointer = diagnosis_pointer(tmp_path, RUN)
        text = notify.compose_park_notice(
            {"run_id": RUN, "block": "submit-s2", "awaiting_since": "T", "brief": _ANOMALY_BRIEF},
            diagnosis=pointer,
        )
        assert "diagnosis: attached (1 proposed action(s), agent-authored, advisory)" in text
        assert f"{RUN}.diagnosis.json" in text
        # The pointer, not the content: the drafted action text never rides the push.
        assert "raise mem_mb 1.5x" not in text

    def test_raise_park_notification_reads_the_pointer_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hpc_agent.ops.recover import notify

        monkeypatch.setattr(notify, "_try_run", lambda argv: False)
        attach_diagnosis(tmp_path, spec=_spec())
        records = notify.raise_park_notification(
            [{"run_id": RUN, "block": "submit-s2", "awaiting_since": "T", "brief": {}}],
            experiment_dir=tmp_path,
        )
        assert len(records) == 1
        assert "diagnosis: attached (1 proposed action(s)" in records[0]["text"]

    def test_doctor_parked_note_carries_the_pointer(self, tmp_path: Path) -> None:
        from hpc_agent._wire.queries.doctor import DoctorSpec
        from hpc_agent.ops.recover.doctor import doctor

        _park(tmp_path)
        out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-29T01:00:00+00:00"))
        assert out["parked_count"] == 1
        assert out["parked"][0]["diagnosis"] == "none"
        assert out["parked"][0]["diagnosis_path"] is None

        attach_diagnosis(tmp_path, spec=_spec())
        out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-29T01:00:00+00:00"))
        note = out["parked"][0]
        assert note["diagnosis"].startswith("attached (1 proposed action(s), agent-authored")
        assert note["diagnosis_path"].endswith(f"{RUN}.diagnosis.json")
        # Pointer only — the dossier's drafted text never enters the doctor read.
        assert "raise mem_mb 1.5x" not in json.dumps(out)

    def test_morning_digest_parked_section_is_pointer_rows(self, tmp_path: Path) -> None:
        from hpc_agent.ops.status_blocks import _parked_brief_section

        now = "2026-07-29T01:00:00+00:00"
        assert _parked_brief_section(tmp_path, now) is None  # no parks → omitted key
        _park(tmp_path)
        rows = _parked_brief_section(tmp_path, now)
        assert rows is not None and len(rows) == 1
        assert rows[0]["run_id"] == RUN
        assert rows[0]["diagnosis"] == "none"
        attach_diagnosis(tmp_path, spec=_spec())
        rows = _parked_brief_section(tmp_path, now)
        assert rows is not None
        assert rows[0]["diagnosis"].startswith("attached (1 proposed action(s)")
        assert rows[0]["diagnosis_path"].endswith(f"{RUN}.diagnosis.json")
        assert "raise mem_mb 1.5x" not in json.dumps(rows)


# ── the separation contract: agent judgment never enters a trusted surface ──


SENTINEL = "DIAG-SENTINEL-9f3c"


class TestAdvisorySeparation:
    """Pin the trust boundary: attaching a diagnosis moves NOTHING but the
    diagnosis sidecar — the decision-brief provenance store and the answer
    menu's code-authored options are byte-identical before and after."""

    def _sentinel_spec(self) -> AttachDiagnosisSpec:
        return AttachDiagnosisSpec(
            run_id=RUN,
            classification="gpu_oom",
            evidence_excerpts=[DiagnosisEvidenceExcerpt(path="/logs/x", lines=f"lines {SENTINEL}")],
            proposed_actions=[
                DiagnosisProposedAction(
                    label=f"label {SENTINEL}",
                    rationale=f"rationale {SENTINEL}",
                    suggested_response_text=f"paste-me {SENTINEL}",
                )
            ],
        )

    def test_diagnosis_never_enters_the_decision_brief_store(self, tmp_path: Path) -> None:
        _park(tmp_path)
        append_brief(tmp_path, run_id=RUN, block="s2", brief=_ANOMALY_BRIEF)
        before = briefs_path(tmp_path, RUN).read_bytes()

        attach_diagnosis(tmp_path, spec=self._sentinel_spec())

        after = briefs_path(tmp_path, RUN).read_bytes()
        assert after == before  # the provenance journal is byte-identical
        assert SENTINEL not in json.dumps(read_briefs(tmp_path, RUN))

    def test_diagnosis_never_becomes_an_answer_menu_option(self, tmp_path: Path) -> None:
        """The menu's 'code-carried data only' rule means code-AUTHORED: after an
        attach, recomposing the boundary's menu from its brief yields options
        none of which carry the agent's drafted text — the mapping is not
        laundered in."""
        _park(tmp_path)
        attach_diagnosis(tmp_path, spec=self._sentinel_spec())

        menu = compose_answer_menu(
            brief=_ANOMALY_BRIEF,
            block="submit-s2",
            run_id=RUN,
            target="submit-s3",
            next_spec_sha="a" * 64,
            is_anomaly_terminator=True,
        )
        assert menu is not None
        assert SENTINEL not in json.dumps(menu)
        # And the composer is pure over the brief: same inputs, same menu,
        # regardless of what sits in the diagnosis sidecar.
        again = compose_answer_menu(
            brief=_ANOMALY_BRIEF,
            block="submit-s2",
            run_id=RUN,
            target="submit-s3",
            next_spec_sha="a" * 64,
            is_anomaly_terminator=True,
        )
        assert again == menu

    def test_attach_touches_only_the_diagnosis_sidecar(self, tmp_path: Path) -> None:
        """Filesystem-level pin: the ONLY .hpc/runs entry an attach creates or
        rewrites is <run_id>.diagnosis.json (+ its lock)."""
        _park(tmp_path)
        append_brief(tmp_path, run_id=RUN, block="s2", brief={"k": "v"})
        runs_dir = tmp_path / ".hpc" / "runs"
        before = {p.name: p.read_bytes() for p in runs_dir.iterdir() if p.is_file()}

        attach_diagnosis(tmp_path, spec=self._sentinel_spec())

        after = {p.name: p.read_bytes() for p in runs_dir.iterdir() if p.is_file()}
        new_or_changed = {
            name for name in after if name not in before or after[name] != before[name]
        }
        assert new_or_changed <= {f"{RUN}.diagnosis.json", f"{RUN}.diagnosis.json.lock"}
        assert f"{RUN}.diagnosis.json" in new_or_changed
