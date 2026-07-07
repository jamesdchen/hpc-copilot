"""Tests for the relay-audit ``Stop`` hook (conduct rule 10, staged → active).

``verify-relay`` existed but nothing made a driving agent run it. The hook
audits the FINAL assistant text (from the transcript) against the journal for
every journaled run the text names, and blocks the stop once — loop-safe via
``stop_hook_active`` — with the itemized contradiction summary. Covers: fires
on a synthetic mismatched relay (stale state + wrong number), silent on a
clean relay, silent when the repo has no hpc journal, silent when no journaled
run is mentioned, the ``unverifiable``-not-surfaced policy, loop safety, and
the stdin entrypoint.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import relay_audit_stop

RUN_ID = "pi-run-1"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _seed_run(exp: Path, *, status: str = "failed") -> None:
    """A journaled run: RunRecord (journal home) + decision journal (repo)."""
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        exp,
        RunRecord(
            run_id=RUN_ID,
            profile="p",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/remote",
            job_name="j",
            job_ids=["13610902"],
            total_tasks=10,
            submitted_at="2026-07-03T00:00:00+00:00",
            experiment_dir=str(exp),
            status=status,
        ),
    )
    append_decision(
        exp,
        scope_kind="run",
        scope_id=RUN_ID,
        block="submit-s1",
        response="y",
        evidence_digest={"canary": "green", "core_hours": 128},
    )


def _transcript(tmp_path: Path, final_text: str) -> Path:
    """A minimal session transcript ending in one assistant text message."""
    path = tmp_path / "transcript.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "status?"}},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": final_text}]},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _payload(exp: Path, transcript: Path, **extra: object) -> dict:
    return {"cwd": str(exp), "transcript_path": str(transcript), **extra}


# ─── fires on a mismatched relay ─────────────────────────────────────────────


def test_blocks_once_on_stale_state_and_wrong_number(tmp_path: Path) -> None:
    """The proving-run-#3 shape: journal says failed, relay says running —
    plus a number the records never carried."""
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} is running; it used 999 core-hours.")

    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    reason = out["reason"]
    assert "relay audit" in reason
    assert RUN_ID in reason
    assert "running" in reason  # the contradicted state claim is itemized
    assert "999" in reason  # the contradicted number claim is itemized
    assert "verify-relay" in reason  # names the verb to re-check with


def test_loop_safe_on_stop_hook_active(tmp_path: Path) -> None:
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} is running.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript, stop_hook_active=True))
    assert out is None


# ─── silent passes (fail-open everywhere) ────────────────────────────────────


def test_silent_on_clean_relay(tmp_path: Path) -> None:
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(
        tmp_path, f"Run {RUN_ID} has failed after 128 core-hours across 10 tasks."
    )
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_silent_when_no_hpc_journal(tmp_path: Path) -> None:
    """A repo with no journal namespace is not an hpc repo — and the check
    must not scaffold one (no-scaffold rule)."""
    home = tmp_path / "journal"
    transcript = _transcript(tmp_path, f"Run {RUN_ID} is running; it used 999 core-hours.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path / "repo", transcript)) is None
    assert not home.exists() or not any(home.iterdir())


def test_silent_when_final_text_names_no_journaled_run(tmp_path: Path) -> None:
    """Claims are only attributable to a run the relay names."""
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(tmp_path, "All tests pass; 42 files changed, everything running.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_unverifiable_claims_are_not_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kind filter: ``unverifiable`` claims (a number the records simply
    never saw — test counts, line numbers) are a verb-level concern, not a
    contradiction; the hook must not nag every turn end over them."""
    from hpc_agent._wire.queries.verify_relay import RelayMismatch, VerifyRelayResult

    _seed_run(tmp_path, status="failed")

    def _fake_verify(**_kw: object) -> VerifyRelayResult:
        return VerifyRelayResult(
            clean=False,
            claims_checked=1,
            mismatches=[
                RelayMismatch(
                    claim="42",
                    kind="unverifiable",
                    detail="numeric claim '42' has no comparable value in any durable record",
                    nearest_source_value=None,
                )
            ],
            sources_consulted=["run_record"],
        )

    monkeypatch.setattr("hpc_agent.ops.decision.verify_relay.verify_relay", _fake_verify)
    transcript = _transcript(tmp_path, f"Run {RUN_ID}: 42 files changed.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_silent_on_missing_transcript_or_malformed_payload(tmp_path: Path) -> None:
    _seed_run(tmp_path, status="failed")
    assert relay_audit_stop.build_hook_output(None) is None
    assert relay_audit_stop.build_hook_output({"cwd": str(tmp_path)}) is None
    assert (
        relay_audit_stop.build_hook_output(
            _payload(tmp_path, tmp_path / "no-such-transcript.jsonl")
        )
        is None
    )


# ─── transcript parsing ──────────────────────────────────────────────────────


def test_final_text_is_trailing_assistant_run_only(tmp_path: Path) -> None:
    """Only the final reply is audited — an earlier (superseded) assistant
    message with a stale claim does not fire the hook."""
    _seed_run(tmp_path, status="failed")
    path = tmp_path / "transcript.jsonl"
    lines = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"Run {RUN_ID} is running."}]},
        },
        {"type": "user", "message": {"content": "and now?"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"Run {RUN_ID} has failed."}]},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, path)) is None


# ─── entrypoint ──────────────────────────────────────────────────────────────


def test_main_prints_block_json_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} is running.")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(tmp_path, transcript))))
    assert relay_audit_stop.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert RUN_ID in out["reason"]


def test_main_is_a_clean_noop_on_garbage_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert relay_audit_stop.main() == 0
    assert capsys.readouterr().out == ""


# ─── notebook-audit relay (T11) ──────────────────────────────────────────────

_NB_AUDIT = "demo-audit"

_NB_SOURCE = """# %%
# hpc-audit-section: load-data
import pandas as pd
data = pd.read_csv("in.csv")

# %%
# hpc-audit-section: fit-model
model = fit(data)
"""


def _seed_notebook(exp: Path, *, sign: str | None = "load-data") -> None:
    """A discoverable notebook audit: source + template + interview.json, and (to
    make the journal file exist for discovery) a sign-off of *sign* if given."""
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.audit_source import parse_percent_source
    from hpc_agent.state.decision_journal import append_decision

    (exp / "source.py").write_text(_NB_SOURCE, encoding="utf-8")
    (exp / "template.py").write_text(_NB_SOURCE, encoding="utf-8")
    (exp / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": _NB_AUDIT,
                }
            }
        ),
        encoding="utf-8",
    )
    if sign is not None:
        sha = next(
            s.section_sha for s in parse_percent_source(_NB_SOURCE).sections if s.slug == sign
        )
        append_decision(
            exp,
            scope_kind="notebook",
            scope_id=_NB_AUDIT,
            block=nb.SIGN_OFF_BLOCK,
            response=f"reviewed the {sign} section",
            resolved={"audit_id": _NB_AUDIT, "section": sign, "section_sha": sha, "view_sha": "v1"},
        )


def test_blocks_on_wrong_notebook_status(tmp_path: Path) -> None:
    """load-data is signed_current; the relay calls it unsigned → state block.

    Also exercises the pre-submit repo shape: no run was ever submitted, so the
    hook proceeds on the notebook journal alone."""
    _seed_notebook(tmp_path, sign="load-data")
    transcript = _transcript(tmp_path, f"For audit {_NB_AUDIT}: the load-data section is unsigned.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    assert _NB_AUDIT in out["reason"]
    assert "load-data" in out["reason"]


def test_notebook_loop_safe_on_stop_hook_active(tmp_path: Path) -> None:
    _seed_notebook(tmp_path, sign="load-data")
    transcript = _transcript(tmp_path, f"Audit {_NB_AUDIT}: load-data is unsigned.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript, stop_hook_active=True))
    assert out is None


def test_silent_on_correct_notebook_status(tmp_path: Path) -> None:
    _seed_notebook(tmp_path, sign="load-data")
    transcript = _transcript(tmp_path, f"Audit {_NB_AUDIT}: load-data is signed_current.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_no_notebook_work_when_no_audit_mentioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run path is completely unaffected: a relay naming a run but no audit
    does ZERO notebook work (verify_notebook_relay is never called), and the run
    contradiction still fires."""
    _seed_run(tmp_path, status="failed")
    _seed_notebook(tmp_path, sign="load-data")  # a discoverable audit is present...

    calls = {"n": 0}

    def _counting(*_a: object, **_k: object) -> object:
        calls["n"] += 1
        raise AssertionError("verify_notebook_relay must not run when no audit is named")

    monkeypatch.setattr("hpc_agent.ops.decision.verify_relay.verify_notebook_relay", _counting)

    # ...but the relay names only the run, not the audit id.
    transcript = _transcript(tmp_path, f"Run {RUN_ID} is running.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None and out["decision"] == "block"  # run state mismatch still fires
    assert RUN_ID in out["reason"]
    assert calls["n"] == 0


def test_notebook_unverifiable_source_not_surfaced(tmp_path: Path) -> None:
    """A discoverable audit whose .py source cannot resolve (no interview.json)
    yields only unverifiable claims → no block (the hook's kind filter)."""
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.audit_source import parse_percent_source
    from hpc_agent.state.decision_journal import append_decision

    # Journal file exists (discoverable) but there is NO interview.json/source.
    sha = next(
        s.section_sha for s in parse_percent_source(_NB_SOURCE).sections if s.slug == "load-data"
    )
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_NB_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response="reviewed load-data",
        resolved={
            "audit_id": _NB_AUDIT,
            "section": "load-data",
            "section_sha": sha,
            "view_sha": "v",
        },
    )
    transcript = _transcript(tmp_path, f"Audit {_NB_AUDIT}: load-data is auto_cleared.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None
