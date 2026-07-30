"""The consent-forwarding ``PreToolUse`` hook (attended-latency plan).

The hook reads the SAME journal ``ops/block_gate`` reads and forwards what is
already there to Claude Code's permission layer, so a greenlight the human
typed in-band is not re-asked by the auto-mode classifier. What is pinned here:

* **allow** on a live journaled greenlight, and on a live standing consent —
  both driven through REAL journal bytes (``append_decision`` + a real run
  sidecar), never a mocked probe: the whole point is that the hook agrees with
  the gate about what the journal says;
* **ask** with no greenlight, and ask when the consent died on a spec change
  (the ``cmd_sha`` leg) — the two ways consent evaporates;
* **``append-decision`` / ``kill`` ALWAYS ask**, even with a live consent on
  file for the same scope (a consent-commit and a destructive verb can never be
  pre-authorized);
* **fail CLOSED to ask** — malformed stdin, a non-object payload, an
  unidentifiable tool, a missing run_id, a raising probe;
* **the gated set DERIVES from ``block_chain.GATED_BLOCKS``** — the mutation:
  a fake verb added to that constant is covered with no edit to the hook;
* query verbs get NO decision (the harness's own flow is left untouched).

TOY VOCABULARY ONLY: widget runs.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._kernel.hooks import consent_forward as hook
from hpc_agent.infra import block_chain
from hpc_agent.infra.time import utcnow
from hpc_agent.ops import overnight
from hpc_agent.state import decision_journal as sdj
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "widget-run-77"
_CMD_SHA = "a3f2c9d1beef00112233"


@pytest.fixture
def experiment_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    exp = tmp_path / "exp"
    exp.mkdir()
    return exp


# ── real journal bytes ────────────────────────────────────────────────────────


def _greenlight(experiment_dir: Path, verb: str, *, response: str = "y") -> None:
    """Journal a REAL decision record whose ``resolved.next_block`` names *verb*."""
    sdj.append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        block="submit-s1",
        response=response,
        resolved={"next_block": verb},
    )


def _sidecar(experiment_dir: Path, *, cmd_sha: str = _CMD_SHA) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python widget.py",
        result_dir_template="results/{task_id}",
        task_count=10,
        tasks_py_sha="",
    )


def _consent(experiment_dir: Path, *, cmd_sha: str = _CMD_SHA) -> None:
    """Journal a REAL standing-consent record bound to *cmd_sha*."""
    sdj.append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        block=overnight.OVERNIGHT_CONSENT_BLOCK,
        response="let it run overnight to the widget canary, cap 50 dollars",
        resolved={
            "expires_at": str((utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")),
            "budget_cap": 50.0,
            "walltime_cap": 3600,
            "cmd_sha": cmd_sha,
            "wake": {"kind": "status-watch", "run_id": _RUN_ID},
        },
    )


def _payload(verb: str, experiment_dir: Path | None, **spec_extra: Any) -> dict[str, Any]:
    """A PreToolUse payload shaped like the MCP server's own tool input."""
    tool_input: dict[str, Any] = {"spec": {"submit": {"submit": {"run_id": _RUN_ID}}}}
    tool_input["spec"].update(spec_extra)
    if experiment_dir is not None:
        tool_input["experiment_dir"] = str(experiment_dir)
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": f"mcp__hpc-agent__{verb}",
        "tool_input": tool_input,
        "cwd": str(experiment_dir) if experiment_dir is not None else "",
    }


def _decision(output: dict[str, Any] | None) -> str | None:
    if output is None:
        return None
    inner = output["hookSpecificOutput"]
    assert inner["hookEventName"] == "PreToolUse"
    assert inner["permissionDecisionReason"]
    return str(inner["permissionDecision"])


def _reason(output: dict[str, Any] | None) -> str:
    assert output is not None, "expected a decision envelope, got pass-through"
    return str(output["hookSpecificOutput"]["permissionDecisionReason"])


# ── the forwarding path ───────────────────────────────────────────────────────


def test_live_greenlight_is_forwarded_as_allow(experiment_dir: Path) -> None:
    """The exhibit, fixed: a journaled `y` naming submit-s2 → the harness allows."""
    _greenlight(experiment_dir, "submit-s2")
    out = hook.build_hook_output(_payload("submit-s2", experiment_dir))
    assert _decision(out) == "allow"
    reason = _reason(out)
    # The reason NAMES the journaled record (block, ts, scope) rather than
    # asserting a conclusion of the hook's own.
    assert "submit-s1" in reason  # the record's block
    assert f"run:{_RUN_ID}" in reason
    assert "ts=" in reason


def test_no_greenlight_asks(experiment_dir: Path) -> None:
    out = hook.build_hook_output(_payload("submit-s2", experiment_dir))
    assert _decision(out) == "ask"
    assert "no journaled greenlight" in _reason(out)


def test_a_nudge_is_not_a_greenlight(experiment_dir: Path) -> None:
    """A recorded nudge (``response != "y"``) never authorizes the boundary."""
    _greenlight(experiment_dir, "submit-s2", response="no — halve the widget grid")
    assert _decision(hook.build_hook_output(_payload("submit-s2", experiment_dir))) == "ask"


def test_a_greenlight_for_another_verb_does_not_carry(experiment_dir: Path) -> None:
    """Consent is per-VERB: the S3 `y` never pre-authorizes S4."""
    _greenlight(experiment_dir, "submit-s3")
    assert _decision(hook.build_hook_output(_payload("submit-s4", experiment_dir))) == "ask"
    assert _decision(hook.build_hook_output(_payload("submit-s3", experiment_dir))) == "allow"


def test_live_standing_consent_is_forwarded_as_allow(experiment_dir: Path) -> None:
    """Overnight mode: no greenlight, but a live consent names this boundary."""
    _sidecar(experiment_dir)
    _consent(experiment_dir)
    out = hook.build_hook_output(_payload("submit-s3", experiment_dir))
    assert _decision(out) == "allow"
    assert "standing consent" in _reason(out)


def test_consent_that_died_on_spec_change_asks(experiment_dir: Path) -> None:
    """Consent dies on spec change — and the hook must die with it.

    The consent is bound to one ``cmd_sha``; the run's sidecar now carries a
    different one (a regenerated grid). The gate would refuse, so the hook must
    not pre-authorize.
    """
    _sidecar(experiment_dir, cmd_sha="deadbeef99887766")
    _consent(experiment_dir, cmd_sha=_CMD_SHA)
    out = hook.build_hook_output(_payload("submit-s3", experiment_dir))
    assert _decision(out) == "ask"
    assert "spec-changed" in _reason(out)


def test_consent_never_carries_a_boundary_it_does_not_name(experiment_dir: Path) -> None:
    """A live run consent covers submit-s3 only — submit-s2 still asks.

    ``submit-s2`` is consumable under NO consent, so the flat refusal is the
    honest one.
    """
    _sidecar(experiment_dir)
    _consent(experiment_dir)
    out = hook.build_hook_output(_payload("submit-s2", experiment_dir))
    assert _decision(out) == "ask"
    assert "boundary-not-consumable" in _reason(out)


@pytest.mark.parametrize("verb", ["submit-s4", "aggregate-run"])
def test_conditionally_consumable_boundary_names_the_real_state(
    experiment_dir: Path, verb: str
) -> None:
    """A clean-terminal-conditional boundary must NOT be called un-consumable.

    ``submit-s4`` / ``aggregate-run`` DO auto-advance under a standing consent —
    behind clean-predecessor evidence the caller derives and this read-only
    probe cannot see. Reporting them as ``boundary-not-consumable`` would tell
    the human their overnight consent can never cover the harvest, which is
    false; the ask must say the evidence is invisible HERE, not absent.
    """
    from hpc_agent.ops.block_gate import probe_authorization

    _sidecar(experiment_dir)
    _consent(experiment_dir)

    probe = probe_authorization(experiment_dir, run_id=_RUN_ID, verb=verb)
    assert probe.authorized is False
    assert probe.reason == "predecessor-evidence-not-visible-to-probe"

    reason = _reason(hook.build_hook_output(_payload(verb, experiment_dir)))
    assert "predecessor finished clean" in reason
    assert "Not a refusal" in reason
    assert "boundary-not-consumable" not in reason


def test_the_probe_never_ledgers_a_consumption(experiment_dir: Path) -> None:
    """A permission CHECK must not burn the boundary's one audit line.

    ``assert_greenlit_or_consented`` ledgers the auto-advance in the same breath
    as passing it; the hook's read-only probe must not — the agent may never
    follow through on the call.
    """
    _sidecar(experiment_dir)
    _consent(experiment_dir)
    assert _decision(hook.build_hook_output(_payload("submit-s3", experiment_dir))) == "allow"
    assert overnight.read_consumption_ledger(experiment_dir, "run", _RUN_ID) == []


# ── the always-ask verbs ──────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", sorted(hook.ALWAYS_ASK_VERBS))
def test_always_ask_verbs_ask_even_with_live_consent(experiment_dir: Path, verb: str) -> None:
    """append-decision / kill can never be pre-authorized — hard-coded.

    Seeded with BOTH a live standing consent and a greenlight naming the verb:
    the strongest possible journal state must still produce ``ask``.
    """
    _sidecar(experiment_dir)
    _consent(experiment_dir)
    _greenlight(experiment_dir, verb)
    out = hook.build_hook_output(_payload(verb, experiment_dir))
    assert _decision(out) == "ask"
    assert "ALWAYS asks" in _reason(out)


def test_append_decision_and_kill_reasons_state_why() -> None:
    """The reason names the CLASS, not just the refusal (the boundary is the point)."""
    commit = hook.build_hook_output(_payload("append-decision", None))
    assert "COMMITS" in _reason(commit)
    destructive = hook.build_hook_output(_payload("kill", None))
    assert "destructive" in _reason(destructive)


# ── pass-through: verbs this hook has no opinion about ────────────────────────


@pytest.mark.parametrize("verb", ["status-snapshot", "doctor", "notebook-lint", "submit-s1"])
def test_query_and_ungated_verbs_emit_no_decision(experiment_dir: Path, verb: str) -> None:
    assert hook.build_hook_output(_payload(verb, experiment_dir)) is None


def test_pass_through_writes_nothing_to_stdout(
    experiment_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_payload("status-snapshot", experiment_dir)))
    )
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


# ── the derivation: GATED_BLOCKS is the census ────────────────────────────────


def test_gated_set_is_read_from_block_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mutation: a verb added to GATED_BLOCKS is covered with NO edit here."""
    assert "widget-gate" not in hook.gated_verbs()
    monkeypatch.setattr(
        block_chain, "GATED_BLOCKS", frozenset({*block_chain.GATED_BLOCKS, "widget-gate"})
    )
    assert "widget-gate" in hook.gated_verbs()


def test_a_newly_gated_verb_is_covered_by_the_hook(
    experiment_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same mutation, end to end: the fake gated verb now asks (it was pass-through)."""
    assert hook.build_hook_output(_payload("widget-gate", experiment_dir)) is None
    monkeypatch.setattr(
        block_chain, "GATED_BLOCKS", frozenset({*block_chain.GATED_BLOCKS, "widget-gate"})
    )
    assert _decision(hook.build_hook_output(_payload("widget-gate", experiment_dir))) == "ask"
    # …and a journaled greenlight for it is forwarded, exactly like a real block.
    _greenlight(experiment_dir, "widget-gate")
    assert _decision(hook.build_hook_output(_payload("widget-gate", experiment_dir))) == "allow"


def test_hook_never_emits_deny(experiment_dir: Path) -> None:
    """Refusing a mis-sequenced verb is the GATE's job at execution, not the hook's."""
    seen = set()
    for verb in [*sorted(hook.gated_verbs()), *sorted(hook.ALWAYS_ASK_VERBS), "doctor"]:
        seen.add(_decision(hook.build_hook_output(_payload(verb, experiment_dir))))
    assert "deny" not in seen


# ── fail CLOSED to ask ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not-an-object",
        [],
        {},
        {"tool_name": None},
        {"tool_name": ""},
        {"tool_name": 42},
        {"tool_name": "mcp__hpc-agent__submit-s2"},  # no tool_input at all
        {"tool_name": "mcp__hpc-agent__submit-s2", "tool_input": "junk"},
    ],
)
def test_unknown_shapes_fail_closed_to_ask(payload: Any) -> None:
    """Every UNREADABLE shape asks — a dead hook must never mean a silent allow."""
    assert _decision(hook.build_hook_output(payload)) == "ask"


def test_a_non_mcp_tool_gets_no_decision() -> None:
    """A Bash call is not unreadable — it is confidently NOT ours, so: no opinion.

    Bash is not matched by the installed hook, and if it ever were, injecting an
    ``ask`` into every shell call would be an overreach rather than a safeguard.
    """
    payload = {"tool_name": "Bash", "tool_input": {"command": "hpc-agent submit-s2 --spec s"}}
    assert hook.build_hook_output(payload) is None


def test_missing_run_id_asks(experiment_dir: Path) -> None:
    payload = {
        "tool_name": "mcp__hpc-agent__submit-s2",
        "tool_input": {"experiment_dir": str(experiment_dir), "spec": {}},
    }
    out = hook.build_hook_output(payload)
    assert _decision(out) == "ask"
    assert "unique run_id" in _reason(out)


def test_ambiguous_run_id_asks(experiment_dir: Path) -> None:
    """Two DIFFERENT run ids in one call: the hook cannot know which was greenlit."""
    _greenlight(experiment_dir, "submit-s2")
    payload = {
        "tool_name": "mcp__hpc-agent__submit-s2",
        "tool_input": {
            "experiment_dir": str(experiment_dir),
            "spec": {
                "submit": {"submit": {"run_id": _RUN_ID}},
                "other": {"run_id": "widget-run-99"},
            },
        },
    }
    assert _decision(hook.build_hook_output(payload)) == "ask"


def test_no_experiment_dir_anywhere_asks() -> None:
    payload = {
        "tool_name": "mcp__hpc-agent__submit-s2",
        "tool_input": {"spec": {"submit": {"submit": {"run_id": _RUN_ID}}}},
        "cwd": "",
    }
    out = hook.build_hook_output(payload)
    assert _decision(out) == "ask"
    assert "experiment_dir" in _reason(out)


def test_cwd_is_the_experiment_dir_fallback(experiment_dir: Path) -> None:
    """The MCP schema defaults experiment_dir to the server's cwd — so does the hook."""
    _greenlight(experiment_dir, "submit-s2")
    payload = {
        "tool_name": "mcp__hpc-agent__submit-s2",
        "tool_input": {"spec": {"submit": {"submit": {"run_id": _RUN_ID}}}},
        "cwd": str(experiment_dir),
    }
    assert _decision(hook.build_hook_output(payload)) == "allow"


def test_a_raising_probe_asks(experiment_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt/unreadable substrate must ask, never allow and never crash."""
    import hpc_agent.ops.block_gate as gate

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("journal is a directory")

    _greenlight(experiment_dir, "submit-s2")
    monkeypatch.setattr(gate, "probe_authorization", _boom)
    out = hook.build_hook_output(_payload("submit-s2", experiment_dir))
    assert _decision(out) == "ask"
    assert "OSError" in _reason(out)


# ── the stdin/stdout contract ─────────────────────────────────────────────────


def test_main_emits_the_allow_envelope_on_stdout(
    experiment_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _greenlight(experiment_dir, "submit-s2")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload("submit-s2", experiment_dir))))
    assert hook.main() == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize("raw", ["", "{not json", "null", "[]", '{"tool_input": }'])
def test_malformed_stdin_emits_ask(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    """Malformed stdin → ask, exit 0. Never a block, never a silent allow."""
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert hook.main() == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_deeply_nested_payload_emits_ask_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ~20k-deep payload makes ``json.loads`` raise RecursionError — still ask.

    RecursionError is neither ``JSONDecodeError`` nor ``ValueError``, so a
    narrower catch let it escape as a traceback and a non-zero exit: the outcome
    was still a prompt (the harness ignores a crashed hook), but the module's
    two stated guarantees — "unparseable stdin still emits ask" and "always
    exits 0" — were both false, and a fail-CLOSED hook whose docstring lies
    about its failure mode is the kind of guard nobody re-checks.
    """
    depth = 20_000
    raw = ('{"a":' * depth) + "1" + ("}" * depth)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))

    assert hook.main() == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_verb_from_tool_name_shapes() -> None:
    assert hook.verb_from_tool_name("mcp__hpc-agent__submit-s2") == "submit-s2"
    for bad in ("Bash", "", "mcp__hpc-agent", "mcp__hpc-agent__", "mcp__"):
        assert hook.verb_from_tool_name(bad) is None


@pytest.mark.parametrize(
    "spoof",
    [
        "mcp__evil-server__submit-s3",  # another server, our verb name
        "mcp____submit-s3",  # empty server segment
        "mcp__hpc-agent-evil__submit-s3",  # prefix-adjacent server name
        "mcp__hpc-agent__nested__submit-s3",  # extra __ nesting
    ],
)
def test_a_foreign_server_never_collects_our_consent(experiment_dir: Path, spoof: str) -> None:
    """Only OUR server's tools may be forwarded consent (defense in depth).

    Unreachable under the installed matcher (``mcp__hpc-agent__.*``), which is
    exactly why it is pinned: a permission surface must not depend on its own
    matcher for correctness. A hook that took the last ``__`` segment would hand
    hpc-agent's journaled greenlight to any tool whose name merely ENDS in one
    of our verbs.
    """
    _greenlight(experiment_dir, "submit-s3")  # a REAL, live greenlight on file
    assert hook.verb_from_tool_name(spoof) is None

    payload = {
        "tool_name": spoof,
        "tool_input": {
            "experiment_dir": str(experiment_dir),
            "spec": {"submit": {"submit": {"run_id": _RUN_ID}}},
        },
    }
    # NO decision at all: never an allow (the thing that matters), and never a
    # claim of authority over a tool that is not ours.
    assert hook.build_hook_output(payload) is None


def test_own_tool_prefix_matches_the_installed_matcher() -> None:
    """ONE DEFINITION, enforced: the prefix IS the profile's rendered matcher.

    The hook restates the prefix rather than importing
    :mod:`hpc_agent.harness_profile` (which the trust path may not read — see
    ``tests/contracts/test_harness_profile_boundary.py``), so the equality is
    pinned HERE. If the profile's ``OWN_TOOLS`` matcher ever changes, this goes
    red rather than the hook silently checking a prefix nothing installs.
    """
    from hpc_agent.harness_profile import ClaudeCodeProfile, ToolClass

    matcher = ClaudeCodeProfile.matcher_string(ToolClass.OWN_TOOLS)
    assert matcher == f"{hook._OWN_TOOL_PREFIX}.*"
