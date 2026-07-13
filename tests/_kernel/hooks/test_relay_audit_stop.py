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

    monkeypatch.setattr("hpc_agent.ops.decision.journal.verify_relay.verify_relay", _fake_verify)
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

    monkeypatch.setattr(
        "hpc_agent.ops.decision.journal.verify_relay.verify_notebook_relay", _counting
    )

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


# ─── the relay-due discharge pass (the omission gate) ────────────────────────
#
# notebook-status journals a relay-due marker on a TERMINAL verdict; the SAME
# stop that audits contradictions also enforces the omission side: an
# undischarged marker whose key tokens (state word / module sha12) are absent
# from the final text blocks the stop once, verbatim-ready. Each of the three
# safety properties gets a fires-AND-passes pair below: block-once
# (stop_hook_active — the sibling guards' seam), fail-open (corrupt marker /
# raising loader → the stop proceeds), narrow set (pinned in
# tests/ops/test_notebook_status.py::test_non_terminal_status_sets_no_marker).

_SHA12 = "abcdef012345"


def _seed_relay_due(exp: Path, *, state: str = "passed") -> dict:
    """Journal one relay-due marker (the notebook-status terminal write)."""
    from hpc_agent.state import notebook_audit as nb

    record = nb.record_relay_due(exp, audit_id=_NB_AUDIT, state=state, module_sha=_SHA12 + "0" * 52)
    assert record is not None
    resolved = record["resolved"]
    assert isinstance(resolved, dict)
    return resolved


def _discharges(exp: Path) -> list[dict]:
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.decision_journal import read_decisions

    return [
        r
        for r in read_decisions(exp, "notebook", _NB_AUDIT)
        if r.get("block") == nb.RELAY_DISCHARGE_BLOCK
    ]


def test_blocks_on_undischarged_terminal_state(tmp_path: Path) -> None:
    """The omission fires: a terminal `passed` the final text never carried
    blocks the stop with the verbatim-ready reason (tonight's proving-run
    shape: notebook-status computed `passed`, the human never saw it)."""
    _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, "All wrapped up here; ending the turn.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    assert (
        f"unrelayed terminal state: notebook-status = passed @ {_SHA12} — "
        "relay it verbatim before closing." in out["reason"]
    )
    assert _discharges(tmp_path) == []  # a block never discharges anything


def test_relayed_state_word_discharges_and_passes(tmp_path: Path) -> None:
    """The state word in the final text (case-insensitive substring) discharges
    the marker — an appended record echoing the marker key, the marker itself
    untouched — and the stop proceeds. A later token-absent stop stays silent:
    the obligation is closed."""
    marker = _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, f"Audit {_NB_AUDIT}: the module PASSED.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None

    discharges = _discharges(tmp_path)
    assert len(discharges) == 1
    resolved = discharges[0]["resolved"]
    assert resolved["record_kind"] == marker["record_kind"]
    assert resolved["audit_id"] == marker["audit_id"]
    assert resolved["key_tokens"] == marker["key_tokens"]
    assert resolved["created_at"] == marker["created_at"]
    assert resolved["discharged_at"]

    # Discharged → a later stop with no tokens in the text passes silently.
    later = _transcript(tmp_path, "Nothing new to report.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, later)) is None
    assert len(_discharges(tmp_path)) == 1  # and nothing is double-discharged


def test_sha12_token_alone_discharges(tmp_path: Path) -> None:
    """ANY key token discharges — the module sha12 identifies the verdict as
    well as the state word does."""
    _seed_relay_due(tmp_path, state="failed")
    transcript = _transcript(tmp_path, f"Verdict for module @ {_SHA12} relayed.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None
    assert len(_discharges(tmp_path)) == 1


def test_relay_due_block_once_via_stop_hook_active(tmp_path: Path) -> None:
    """Block-once (the sibling Stop guards' seam, reused exactly): the second
    stop — the hook-forced continuation — passes even when the token is STILL
    absent. The same marker never blocks twice in a row."""
    _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, "Still not relaying anything relevant.")
    # First stop fires...
    first = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert first is not None and first["decision"] == "block"
    # ...the forced continuation passes, token absent or not.
    second = relay_audit_stop.build_hook_output(
        _payload(tmp_path, transcript, stop_hook_active=True)
    )
    assert second is None


def test_forced_stop_still_discharges_a_corrected_relay(tmp_path: Path) -> None:
    """The stop_hook_active continuation NEVER blocks, but a corrected relay at
    that very stop still closes its own obligation (the discharge pass runs
    before the forced-pass return)."""
    _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, f"notebook-status = passed @ {_SHA12}.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript, stop_hook_active=True))
    assert out is None
    assert len(_discharges(tmp_path)) == 1


def test_fail_open_on_corrupt_marker_lines(tmp_path: Path) -> None:
    """A corrupt marker line (garbage resolved shape) and a non-JSON line must
    never block or crash the stop — the Option-3 failure class: a hook that can
    wedge a session on one bad record."""
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.decision_journal import append_decision, decisions_path

    # A relay-due record whose resolved shape is garbage (key_tokens not a list).
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_NB_AUDIT,
        block=nb.RELAY_DUE_BLOCK,
        response=nb.RELAY_DUE_RESPONSE,
        resolved={"record_kind": "notebook-status", "key_tokens": 42},
    )
    # Plus a raw non-JSON line appended straight into the journal file.
    path = decisions_path(tmp_path, "notebook", _NB_AUDIT)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{this is not json\n")

    transcript = _transcript(tmp_path, "Ending the turn; nothing relayed.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_fail_open_when_marker_load_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ANY exception in marker load/parse/check → the hook allows the stop,
    even with a genuinely undischarged marker pending."""
    _seed_relay_due(tmp_path, state="passed")

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("journal store exploded")

    monkeypatch.setattr("hpc_agent.state.notebook_audit.read_undischarged_relay_markers", _boom)
    transcript = _transcript(tmp_path, "Ending the turn; nothing relayed.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_omission_combines_with_contradiction_findings(tmp_path: Path) -> None:
    """A stop can owe BOTH corrections: a contradicted run state and an
    unrelayed terminal — one block carries both reasons."""
    _seed_run(tmp_path, status="failed")
    _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} is running.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None and out["decision"] == "block"
    assert "relay audit" in out["reason"]
    assert "unrelayed terminal state" in out["reason"]


# ─── render relay-due markers (the omission gate's SECOND producer) ──────────
#
# notebook-audit-view arms a per-section marker (record_kind
# "notebook-audit-view") whose single key token is the section's view_sha12 —
# the render-file address. The SAME discharge pass enforces it: the sha12 must
# reach the human (a render delivered as an unread file link is not a relay).
# The producer side is pinned in
# tests/ops/test_notebook_audit_view_relay_due.py; here we pin block + discharge.

_VIEW_SHA12 = "76a31b89d7ac"


def _seed_render_relay_due(exp: Path, *, sha12: str = _VIEW_SHA12) -> dict:
    """Journal one render-relay-due marker (the notebook-audit-view write)."""
    from hpc_agent.state import notebook_audit as nb

    record = nb.record_scope_relay_due(
        exp,
        scope_kind="notebook",
        scope_id=_NB_AUDIT,
        record_kind=nb.RENDER_RELAY_DUE_RECORD_KIND,
        key_tokens=[sha12],
    )
    assert record is not None
    resolved = record["resolved"]
    assert isinstance(resolved, dict)
    return resolved


def test_blocks_on_unrelayed_render_view_sha(tmp_path: Path) -> None:
    """A canonical human-required render whose view_sha12 never reached the human
    blocks the stop — the one-token marker names just the sha to relay (no
    dangling '@ ?'), the record_kind naming the render."""
    _seed_render_relay_due(tmp_path)
    transcript = _transcript(tmp_path, "Sent the render files along; wrapping up.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    assert (
        f"unrelayed terminal state: notebook-audit-view = {_VIEW_SHA12} — "
        "relay it verbatim before closing." in out["reason"]
    )
    assert _discharges(tmp_path) == []


def test_relayed_view_sha_discharges_and_passes(tmp_path: Path) -> None:
    """The view_sha12 in the final text (case-insensitive substring) discharges
    the render marker and the stop proceeds; a later token-absent stop stays
    silent (the obligation is closed)."""
    marker = _seed_render_relay_due(tmp_path)
    transcript = _transcript(tmp_path, f"section feature-construction (view_sha {_VIEW_SHA12}).")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None

    discharges = _discharges(tmp_path)
    assert len(discharges) == 1
    resolved = discharges[0]["resolved"]
    assert resolved["record_kind"] == marker["record_kind"] == "notebook-audit-view"
    assert resolved["key_tokens"] == marker["key_tokens"] == [_VIEW_SHA12]
    assert resolved["discharged_at"]

    later = _transcript(tmp_path, "Nothing new to report.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, later)) is None
    assert len(_discharges(tmp_path)) == 1  # not double-discharged


# ─── G1: the paraphrase pass (relayed diffs must be verbatim render content) ──


def _seed_render(exp: Path, audit_id: str, slug: str, sha12: str, body: str) -> None:
    """Write a content-addressed trusted-display render under .hpc/renders."""
    rdir = exp / ".hpc" / "renders" / audit_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{slug}.{sha12}.md").write_text(body, encoding="utf-8")
    # A notebook journal so the audit id is discoverable/mentioned.
    ndir = exp / ".hpc" / "notebooks"
    ndir.mkdir(parents=True, exist_ok=True)
    (ndir / f"{audit_id}.decisions.jsonl").write_text("", encoding="utf-8")


_RENDER_BODY = """## section: feature-construction  [tier: human_required]

### diff-from-template

```diff
--- template:feature-construction
+++ source:feature-construction
+kept = [r for r in rows if float(r[1]) > threshold]
+print(f"kept={len(kept)}")
```
"""


def test_paraphrase_blocks_on_retyped_diff_line(tmp_path: Path) -> None:
    _seed_render(tmp_path, "run10", "feature-construction", "abc123def456", _RENDER_BODY)
    # The relay invents a diff line NOT in the render (a paraphrase); it names
    # the audit id (run10) so the audit is attributable, and "section" so the
    # diff block reads as audit context.
    relay = (
        "run10 feature-construction section diff:\n\n"
        "```diff\n+kept = [r for r in rows if r[1] > THE_MEAN]  # reworded\n```\n"
    )
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, _transcript(tmp_path, relay)))
    assert out is not None
    assert out["decision"] == "block"
    assert "not found in any current render" in out["reason"]


def test_paraphrase_passes_when_diff_is_verbatim(tmp_path: Path) -> None:
    _seed_render(tmp_path, "run10", "feature-construction", "abc123def456", _RENDER_BODY)
    relay = (
        "run10 feature-construction section diff:\n\n"
        "```diff\n+kept = [r for r in rows if float(r[1]) > threshold]\n"
        '+print(f"kept={len(kept)}")\n```\n'
    )
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, _transcript(tmp_path, relay)))
    assert out is None  # every relayed line is verbatim render content


def test_paraphrase_ignores_non_audit_diff_blocks(tmp_path: Path) -> None:
    _seed_render(tmp_path, "run10", "feature-construction", "abc123def456", _RENDER_BODY)
    # A git-style diff with NO audit vocabulary near it must not be checked,
    # even though the relay names the audit id elsewhere.
    relay = "run10 status: I also edited the config:\n\n```diff\n+alpha = 2.0\n-alpha = 1.0\n```\n"
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, _transcript(tmp_path, relay)))
    assert out is None


# ─── item 2: sign-off echo detection (laundered authorship) ──────────────────
#
# A journaled notebook-sign-off whose `response` echoes a PRIOR assistant line
# is laundered authorship — the model drafted the words the human pasted.


def _seed_signoff(exp: Path, response: str, *, audit_id: str = _NB_AUDIT) -> None:
    """Journal ONE notebook-sign-off with a caller-chosen response utterance,
    creating the audit journal (so the audit is discoverable)."""
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.decision_journal import append_decision

    append_decision(
        exp,
        scope_kind="notebook",
        scope_id=audit_id,
        block=nb.SIGN_OFF_BLOCK,
        response=response,
        resolved={
            "audit_id": audit_id,
            "section": "load-data",
            "section_sha": "s",
            "view_sha": "v",
        },
    )


def _two_turn_transcript(tmp_path: Path, prior_assistant: str, final_text: str) -> Path:
    """user → assistant(prior) → user → assistant(final): the drafting turn is
    a NON-final assistant message; the final relay is separate."""
    path = tmp_path / "transcript.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "draft a sign-off?"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": prior_assistant}]}},
        {"type": "user", "message": {"role": "user", "content": "done, signed"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": final_text}]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _echo_provenance_records(exp: Path, audit_id: str = _NB_AUDIT) -> list[dict]:
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.decision_journal import read_decisions

    return [
        r
        for r in read_decisions(exp, "notebook", audit_id)
        if r.get("block") == nb.ECHO_PROVENANCE_BLOCK
    ]


def test_echo_is_journal_only_provenance_never_blocks(tmp_path: Path) -> None:
    """RE-RULED 2026-07-10: a model-drafted attestation the human pasted is
    JOURNAL-ONLY provenance — no block, no surfaced nag, one deduped record."""
    _seed_signoff(tmp_path, "I reviewed the load-data section and the parse looks correct.")
    transcript = _two_turn_transcript(
        tmp_path,
        "You could sign off with: I reviewed the load-data section and the parse looks correct.",
        "The section is signed off. Ending the turn.",
    )
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is None  # never blocks, never appends
    records = _echo_provenance_records(tmp_path)
    assert len(records) == 1
    resolved = records[0]["resolved"]
    assert resolved["audit_id"] == _NB_AUDIT
    assert "model-composed wording" in resolved["detail"]

    # Idempotent: a second stop over the same state writes NO duplicate.
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None
    assert len(_echo_provenance_records(tmp_path)) == 1


def test_echo_passes_on_original_human_utterance(tmp_path: Path) -> None:
    """A human-authored sign-off with no prior assistant echo does not fire —
    the assistant only asked a logistics question, never drafted the words."""
    _seed_signoff(tmp_path, "Checked the load-data parse against my notes; approving it.")
    transcript = _two_turn_transcript(
        tmp_path,
        "The load-data render is ready for your review whenever you are.",
        "Recorded your sign-off. Ending the turn.",
    )
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None
    assert _echo_provenance_records(tmp_path) == []


def test_echo_ignores_final_message_quoting_the_response(tmp_path: Path) -> None:
    """The FINAL relay legitimately quoting the response back is not laundering
    (only a PRIOR assistant line is) — no false block."""
    _seed_signoff(tmp_path, "I reviewed the load-data section and the parse looks correct.")
    # The ONLY assistant text carrying the response is the final relay itself.
    transcript = _two_turn_transcript(
        tmp_path,
        "The load-data render is ready for your review.",
        "Recorded the human sign-off: 'I reviewed the load-data section and the "
        "parse looks correct.' Ending the turn.",
    )
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None
    assert _echo_provenance_records(tmp_path) == []


def test_echo_ignores_short_response(tmp_path: Path) -> None:
    """A short attestation ('y') is below the length floor — never flagged even
    if it appears verbatim in a prior assistant line."""
    _seed_signoff(tmp_path, "y")
    transcript = _two_turn_transcript(
        tmp_path, "Just reply y to sign off.", "Signed. Ending the turn."
    )
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


# ─── item 5: decision-state claims (an unjournaled decision EVENT) ───────────
#
# Run #11: "your y is revoked and nothing has advanced" with ZERO journal
# record of the revocation. A decision-state verb about a NAMED scope must be
# supported by that scope's decision journal.


def test_state_claim_fires_on_unjournaled_revocation(tmp_path: Path) -> None:
    """A standing greenlight ('y' committed via submit-s1) that the relay
    falsely calls revoked → block: no supporting revocation record."""
    _seed_run(tmp_path, status="failed")  # submit-s1 response 'y' stands as latest
    transcript = _transcript(tmp_path, f"Run {RUN_ID}'s y is revoked and nothing has advanced.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    assert RUN_ID in out["reason"]
    assert "revoked/superseded" in out["reason"]
    assert "standing greenlight" in out["reason"]


def test_state_claim_passes_when_greenlight_record_exists(tmp_path: Path) -> None:
    """A positive decision-state claim ('greenlit'/'journaled') about a scope
    that HAS a committed 'y' greenlight is supported → no block."""
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} was greenlit and the decision is journaled.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_state_claim_silent_when_no_scope_named(tmp_path: Path) -> None:
    """A scope-less decision-state claim is a deliberate miss (conservative):
    the verb is attributable to no journaled scope."""
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(tmp_path, "Your y is revoked and nothing has advanced.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_state_claim_passes_on_journaled_supersession(tmp_path: Path) -> None:
    """A run genuinely superseded — ``superseded_by`` stamped on the run record
    and settled abandoned via ``mark_run``, exactly what ``ops/supersession``
    leaves behind — is truthfully relayed as superseded → no block. The decision
    journal's standing launch greenlight is the approval that STARTED the run,
    not a contradiction of its later journaled closure."""
    from hpc_agent.state.journal import mark_run, update_run_record

    _seed_run(tmp_path, status="in_flight")

    def _stamp(r):  # the ops/supersession.supersede_run record shape
        r.superseded_by = "pi-sweep-v2"
        r.superseded_at = "2026-07-12T00:00:00+00:00"
        r.last_status = {**(r.last_status or {}), "verdict_reason": "superseded_by=pi-sweep-v2"}

    update_run_record(tmp_path, RUN_ID, _stamp)
    mark_run(tmp_path, RUN_ID, status="abandoned")

    transcript = _transcript(tmp_path, f"Run {RUN_ID} was superseded by pi-sweep-v2.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


def test_state_claim_ignores_verb_on_a_different_line(tmp_path: Path) -> None:
    """Proximity guard: a revocation verb about something unrelated, on a
    different line from the run id, does not fire (no false block)."""
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(
        tmp_path,
        f"Run {RUN_ID} completed cleanly across 10 tasks.\n"
        "Separately, the stale API token was revoked by the admin.",
    )
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None


# ─── the COMPLETER (D1–D4): capability-gated append instead of bounce ─────────
#
# Dark by default: with no capability declared, every test ABOVE exercises the
# REJECTOR verbatim. Activating HPC_STOP_HOOK_APPEND flips the completer on — code
# APPENDS what it holds via `systemMessage` and the stop PROCEEDS, bouncing only
# on a poisoned decision. `..._ON_BLOCK` confirms the harness displays a
# systemMessage on a BLOCKED stop (D2's discharge-gating).


def _activate_completer(monkeypatch: pytest.MonkeyPatch, *, on_block: bool = False) -> None:
    monkeypatch.setenv("HPC_STOP_HOOK_APPEND", "1")
    if on_block:
        monkeypatch.setenv("HPC_STOP_HOOK_APPEND_ON_BLOCK", "1")
    else:
        monkeypatch.delenv("HPC_STOP_HOOK_APPEND_ON_BLOCK", raising=False)


def _pending_brief(exp: Path, *, content: str, ts: str = "2099-01-01T00:00:00+00:00") -> None:
    """A run brief with NO subsequent committed y (ts far-future) → still pending."""
    from hpc_agent.state.decision_briefs import append_brief

    append_brief(exp, run_id=RUN_ID, block="s2", ts=ts, brief={"proposal": content})


# --- omission class → append the owed verdict, no bounce (D3/D4) --------------


def test_completer_appends_owed_terminal_verdict_no_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate_completer(monkeypatch)
    _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, "All wrapped up here; ending the turn.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert "decision" not in out  # the omission bounce is KILLED
    msg = out["systemMessage"]
    assert "passed" in msg and _SHA12 in msg
    assert "model-untouched" in msg
    # completer-discharged (D3): provenance is "completer", not "relay".
    discharges = _discharges(tmp_path)
    assert len(discharges) == 1
    assert discharges[0]["resolved"]["discharged_by"] == "completer"
    # the obligation is closed — a later token-absent stop is silent.
    later = _transcript(tmp_path, "Nothing new to report.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, later)) is None
    assert len(_discharges(tmp_path)) == 1


def test_completer_found_token_still_discharges_as_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token the MODEL relayed discharges as `relay` even in completer mode —
    the provenance split (D3) is honest about who the human saw the verdict from."""
    _activate_completer(monkeypatch)
    _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, f"Audit {_NB_AUDIT}: the module PASSED.")
    assert relay_audit_stop.build_hook_output(_payload(tmp_path, transcript)) is None
    discharges = _discharges(tmp_path)
    assert len(discharges) == 1
    assert discharges[0]["resolved"]["discharged_by"] == "relay"


def test_completer_appends_render_by_view_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: a render view-marker's owed artifact is the trusted render file's own
    content, selected BY view_sha12 in its filename — verbatim by construction."""
    _activate_completer(monkeypatch)
    _seed_render(tmp_path, _NB_AUDIT, "feature-construction", _VIEW_SHA12, _RENDER_BODY)
    _seed_render_relay_due(tmp_path)  # marker key_tokens=[_VIEW_SHA12]
    transcript = _transcript(tmp_path, "Sent the render files along; wrapping up.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert "decision" not in out
    msg = out["systemMessage"]
    assert "code-appended render" in msg
    assert "kept = [r for r in rows if float(r[1]) > threshold]" in msg  # render body, verbatim
    assert _discharges(tmp_path)[0]["resolved"]["discharged_by"] == "completer"


def test_completer_render_over_cap_degrades_to_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4 cap: an over-cap render degrades to the token floor + a file reference,
    never inlining the whole body."""
    _activate_completer(monkeypatch)
    big = "# section\n" + ("x" * 9000)
    _seed_render(tmp_path, _NB_AUDIT, "feature-construction", _VIEW_SHA12, big)
    _seed_render_relay_due(tmp_path)
    transcript = _transcript(tmp_path, "wrapping up.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None and "decision" not in out
    msg = out["systemMessage"]
    assert "exceeds the append cap" in msg
    assert _VIEW_SHA12 in msg
    assert "xxxxxxxx" not in msg  # the body was NOT inlined


# --- violation class → append correction (no pending decision) ---------------


def test_completer_appends_correction_no_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule-10 contradiction with NO pending brief → the correction is appended
    UNDER the claim (journal value authoritative); the stop proceeds."""
    _activate_completer(monkeypatch)
    _seed_run(tmp_path, status="failed")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} is running; it used 999 core-hours.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert "decision" not in out  # no pending decision → no bounce
    msg = out["systemMessage"]
    assert "relay correction" in msg
    assert RUN_ID in msg
    assert "running" in msg  # the model's claim is quoted


def test_completer_mode_echo_is_journal_only_no_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RE-RULED 2026-07-10: echo is journal-only provenance in the COMPLETER
    mode too — no systemMessage, no bounce, one deduped provenance record."""
    _activate_completer(monkeypatch)
    _seed_signoff(tmp_path, "I reviewed the load-data section and the parse looks correct.")
    transcript = _two_turn_transcript(
        tmp_path,
        "You could sign off with: I reviewed the load-data section and the parse looks correct.",
        "The section is signed off. Ending the turn.",
    )
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is None  # nothing surfaced in either mode
    assert len(_echo_provenance_records(tmp_path)) == 1


# --- the poisoned-decision test (the surviving bounce) -----------------------


def test_completer_bounces_on_poisoned_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contradicted claim that feeds a still-PENDING brief (claim tokens
    intersect the brief content) bounces — a footnote is not enough under a
    pending proposal."""
    _activate_completer(monkeypatch, on_block=True)
    _seed_run(tmp_path, status="failed")
    _pending_brief(tmp_path, content="resume at 999 core-hours")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} used 999 core-hours; ending.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    assert "poisoned decision" in out["reason"]
    assert RUN_ID in out["reason"]
    assert "999" in out["reason"]


def test_completer_not_poisoned_when_brief_greenlit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief with a SUBSEQUENT committed y (the initial submit-s1 y, ts after
    the brief) is greenlit, not pending → the finding appends, never bounces."""
    _activate_completer(monkeypatch)
    _seed_run(tmp_path, status="failed")  # submit-s1 y stands at a 2026 ts
    _pending_brief(tmp_path, content="resume at 999 core-hours", ts="2000-01-01T00:00:00+00:00")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} used 999 core-hours; ending.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert "decision" not in out  # greenlit brief → not poisoned → correction append
    assert "relay correction" in out["systemMessage"]


# --- D2 composition + discharge-gated-on-confirmed-display --------------------


def test_completer_composes_block_and_append_when_block_display_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed class with confirmed blocked-display: ONE output carries the poisoned
    bounce AND the completions' systemMessage; the completed omission is
    discharged and NOT re-stated in the block reason."""
    _activate_completer(monkeypatch, on_block=True)
    _seed_run(tmp_path, status="failed")
    _pending_brief(tmp_path, content="resume at 999 core-hours")
    _seed_relay_due(tmp_path, state="passed")  # an omission on the notebook journal
    transcript = _transcript(tmp_path, f"Run {RUN_ID} used 999 core-hours; ending.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    assert "poisoned decision" in out["reason"]
    assert "systemMessage" in out
    assert "passed" in out["systemMessage"]  # the omission rode along, code-appended
    assert "unrelayed" not in out["reason"]  # completed findings not re-stated
    # the omission was completer-discharged (confirmed display).
    discharges = _discharges(tmp_path)
    assert len(discharges) == 1
    assert discharges[0]["resolved"]["discharged_by"] == "completer"


def test_completer_defers_completions_when_block_display_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed class with UNCONFIRMED blocked-display: completions DEFER to the
    post-continuation stop — the block fires alone, the marker stays owed (not
    discharged on a possibly-swallowed systemMessage)."""
    _activate_completer(monkeypatch, on_block=False)
    _seed_run(tmp_path, status="failed")
    _pending_brief(tmp_path, content="resume at 999 core-hours")
    _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} used 999 core-hours; ending.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    assert "systemMessage" not in out  # completions deferred
    assert _discharges(tmp_path) == []  # the marker is NOT discharged — still owed


def test_completer_forced_stop_appends_and_never_bounces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop_hook_active forced continuation runs completions (they never block)
    and a poisoned finding does NOT bounce (loop-safe: block-once)."""
    _activate_completer(monkeypatch)
    _seed_run(tmp_path, status="failed")
    _pending_brief(tmp_path, content="resume at 999 core-hours")
    transcript = _transcript(tmp_path, f"Run {RUN_ID} used 999 core-hours; ending.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript, stop_hook_active=True))
    assert out is not None
    assert "decision" not in out  # forced → poisoned does not re-bounce
    assert "relay correction" in out["systemMessage"]


def test_completer_dark_default_is_rejector_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With NO capability declared (the default landing) the same omission that
    the completer would append instead BOUNCES — rejector-identical."""
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND", raising=False)
    _seed_relay_due(tmp_path, state="passed")
    transcript = _transcript(tmp_path, "All wrapped up here; ending the turn.")
    out = relay_audit_stop.build_hook_output(_payload(tmp_path, transcript))
    assert out is not None
    assert out["decision"] == "block"
    assert "systemMessage" not in out
    assert _discharges(tmp_path) == []  # a rejector bounce discharges nothing
