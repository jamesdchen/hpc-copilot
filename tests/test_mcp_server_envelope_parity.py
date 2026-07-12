"""MCP envelope parity with the Bash-transport autofetch hooks (architect memo §1).

The two ``PostToolUse`` autofetch hooks — ``decision_rendezvous_autofetch`` (inject
the ``brief`` a parked ``block-drive`` tick wrote) and ``skill_return_autofetch``
(inject a sub-skill's committed return envelope) — are **Bash-transport
compensations**: over the CLI the verb's payload only reaches the agent as stdout
it can miss, so a hook re-injects it. Over MCP that gap does not exist — the
envelope IS the structured ``tools/call`` result. The architect memo settled the
relay as ENVELOPE PARITY, not server-side injection: adding an injector to
``mcp_server.py`` would duplicate mechanism (one-definition rule) to solve a
transport problem MCP does not have.

These tests PIN that parity by construction, so the "do not build injection"
verdict stays honest:

* a parked ``block-drive`` ``tools/call`` result carries every field
  ``decision_rendezvous_autofetch.build_hook_output`` would inject — the ``brief``,
  the ``next_block`` hint (``next_verb``), and the awaiting marker
  (``action == "awaiting_decision"``) — read straight off the structured envelope;
* a ``fetch-skill-return`` ``tools/call`` result carries the committed return
  envelope byte-for-byte identical to what ``skill_return_autofetch`` injects (the
  parent's MCP-direct read of the sub-skill's payload);
* the server adds NO injection of its own (no ``additionalContext`` /
  ``hookSpecificOutput`` field, no duplicated brief) — the digest rides once, in
  the envelope. This is the "no duplication when the hook path also runs" contract:
  the server does not re-implement the hook, so a hook running over Bash transport
  cannot double with a server injection.

If a parity assertion ever fails because a field is MISSING from the envelope, the
memo's fix is to enrich the **block-drive Result model** (make the envelope carry
it), NEVER to add an injector to the server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent._kernel.extension import mcp_server as M
from hpc_agent._kernel.hooks import decision_rendezvous_autofetch as rendezvous
from hpc_agent._kernel.hooks import skill_return_autofetch as skillret
from hpc_agent._kernel.registry.primitive import get_registry
from hpc_agent.state.journal import mark_pending_decision, upsert_run
from hpc_agent.state.run_record import RunRecord

_RUN_ID = "run-parity"
_BLOCK = "s2"
_WORKFLOW = "submit"
_NEXT_VERB = "s3"
_BRIEF = {"proposal": "canary looks good", "cost": 42}


@pytest.fixture(autouse=True)
def _journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate the journal home so nothing leaks between tests / into the real one.
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_journal"))


def _curated_server() -> M.McpServer:
    # The REAL in-process runner (not a fake): these tests assert the envelope the
    # actual verb produces matches the hook, so the CLI must really run. Curated
    # is the surface the amplification loop uses and where block-drive is exposed.
    return M.McpServer(
        registry=get_registry(),
        catalog="curated",
        runner=M._in_process_cli_runner,
    )


def _full_server() -> M.McpServer:
    return M.McpServer(
        registry=get_registry(),
        allow_mutations=True,
        catalog="full",
        runner=M._in_process_cli_runner,
    )


def _record(exp: Path, run_id: str = _RUN_ID) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir=str(exp),
        status="in_flight",
    )


def _park(exp: Path, run_id: str = _RUN_ID) -> None:
    """Upsert an in-flight run and stamp its pending_decision marker + brief."""
    upsert_run(exp, _record(exp, run_id))
    mark_pending_decision(
        run_id,
        block=_BLOCK,
        workflow=_WORKFLOW,
        brief=_BRIEF,
        resume_cursor={
            "workflow": _WORKFLOW,
            "run_id": run_id,
            "next_verb": _NEXT_VERB,
            "current_verb": _BLOCK,
        },
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=exp,
    )


# ─── block-drive ⇄ decision_rendezvous_autofetch parity ──────────────────────


def test_parked_block_drive_result_carries_rendezvous_digest(tmp_path: Path) -> None:
    """A parked ``block-drive`` tool result carries every field the rendezvous
    autofetch hook would inject — brief, next_block hint, awaiting marker."""
    _park(tmp_path)
    result = _curated_server().call_tool(
        "block-drive", {"spec": {"run_id": _RUN_ID}, "experiment_dir": str(tmp_path)}
    )
    assert result["isError"] is False
    data = result["structuredContent"]["data"]
    # The awaiting marker, the next_block hint, and the brief — all present.
    assert data["action"] == "awaiting_decision"
    assert data["next_verb"] == _NEXT_VERB
    assert data["brief"] == _BRIEF

    # PARITY: the brief the hook would inject over Bash transport is byte-for-byte
    # the brief the MCP envelope already carries — so the hook is unnecessary here
    # by construction, not by omission.
    cmd = f"hpc-agent block-drive --run-id {_RUN_ID} --experiment-dir {tmp_path}"
    hook_out = rendezvous.build_hook_output(
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
    )
    assert hook_out is not None
    injected = json.loads(hook_out["hookSpecificOutput"]["additionalContext"])
    assert injected == data["brief"]


def test_block_drive_envelope_carries_the_digest_once_no_server_injection(
    tmp_path: Path,
) -> None:
    """No duplication: the server adds no injection field of its own and the brief
    rides exactly once (in ``data.brief``). The relay is the envelope, not a
    re-implemented hook — so a Bash-transport hook can never double with it."""
    _park(tmp_path)
    result = _curated_server().call_tool(
        "block-drive", {"spec": {"run_id": _RUN_ID}, "experiment_dir": str(tmp_path)}
    )
    # The MCP tool-result shape is exactly {content, structuredContent, isError} —
    # no hook-injection surface leaked into it.
    assert set(result) == {"content", "structuredContent", "isError"}
    assert "additionalContext" not in result
    assert "hookSpecificOutput" not in result
    # The brief appears once: under data.brief, not duplicated at the envelope top
    # level (the server did not lift/duplicate it the way an injector would).
    structured = result["structuredContent"]
    assert "brief" not in structured
    assert structured["data"]["brief"] == _BRIEF


def test_block_drive_content_text_is_the_structured_envelope(tmp_path: Path) -> None:
    """The ``content`` text block is just the serialized envelope — the same bytes
    ``structuredContent`` carries, so a client reading either recovers the brief;
    there is no separately-injected copy to reconcile."""
    _park(tmp_path)
    result = _curated_server().call_tool(
        "block-drive", {"spec": {"run_id": _RUN_ID}, "experiment_dir": str(tmp_path)}
    )
    text = result["content"][0]["text"]
    assert json.loads(text) == result["structuredContent"]


# ─── fetch-skill-return ⇄ skill_return_autofetch parity ──────────────────────

_SKILL = "hpc-status"
_RETURN_ENVELOPE = {
    "ok": True,
    "skill": _SKILL,
    "run_id": "run-xyz",
    "lifecycle_state": "complete",
    "next_step_hint": "aggregate",
}


def _commit_return(exp: Path) -> None:
    """Stage + emit a valid sub-skill return envelope, committing it to disk."""
    returns_dir = exp / ".hpc" / "_returns"
    returns_dir.mkdir(parents=True, exist_ok=True)
    (returns_dir / f"{_SKILL}.staged.json").write_text(json.dumps(_RETURN_ENVELOPE))
    emit = _full_server().call_tool(
        "emit-skill-return", {"skill": _SKILL, "experiment_dir": str(exp)}
    )
    assert emit["isError"] is False, emit


def test_fetch_skill_return_result_carries_the_committed_return(tmp_path: Path) -> None:
    """A ``fetch-skill-return`` tool result carries the committed return envelope
    byte-for-byte identical to what ``skill_return_autofetch`` would inject — the
    parent skill's MCP-direct read of the sub-skill's payload, no hook needed."""
    _commit_return(tmp_path)
    result = _full_server().call_tool(
        "fetch-skill-return",
        {"skill": _SKILL, "experiment_dir": str(tmp_path), "no_clear": True},
    )
    assert result["isError"] is False
    # The verb prints the envelope verbatim to stdout, so structuredContent IS the
    # sub-skill's return envelope (plus the runner's own exit_code annotation).
    structured = dict(result["structuredContent"])
    structured.pop("exit_code", None)

    injected = skillret.read_committed_envelope(tmp_path, _SKILL)
    assert injected is not None
    assert json.loads(injected) == structured


def test_emit_skill_return_result_carries_commit_receipt_not_payload(tmp_path: Path) -> None:
    """The honest boundary the memo names: over MCP the sub-skill's payload is
    delivered by ``fetch-skill-return`` (above), NOT echoed back by
    ``emit-skill-return`` — whose result is only the commit RECEIPT
    (skill / path / validated). This documents that the emit side is not the parity
    surface; the read verb is. (The unit brief's "emit-skill-return result carries
    the return" is superseded here: it carries the receipt, per the actual verb.)"""
    _commit_return(tmp_path)
    # _commit_return already emitted; re-emit is idempotent but the staged file is
    # gone, so read the receipt from a fresh stage+emit to inspect the shape.
    returns_dir = tmp_path / ".hpc" / "_returns"
    (returns_dir / f"{_SKILL}.staged.json").write_text(json.dumps(_RETURN_ENVELOPE))
    emit = _full_server().call_tool(
        "emit-skill-return", {"skill": _SKILL, "experiment_dir": str(tmp_path)}
    )
    data = emit["structuredContent"]["data"]
    assert set(data) == {"skill", "path", "validated"}
    assert data["skill"] == _SKILL
    assert data["validated"] is True
    # The receipt does NOT carry the sub-skill's return fields.
    assert "lifecycle_state" not in data
