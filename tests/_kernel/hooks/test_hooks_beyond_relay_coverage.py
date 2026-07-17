"""Behavior-pinning battery for three harness hooks — the boundaries the
existing suites leave un-pinned (audit unit 4b, "beyond relay coverage").

The sibling suites (``test_scheduler_write_fence``, ``test_stop_multiplex``,
``test_skill_return_stop_guard``) pin the common paths. This file adds
MUTATION-KILLING pins for the exact boundaries the audit calls out and the
siblings only cover coarsely:

* **scheduler write fence** — each TRIP condition asserted by the EXACT verb it
  returns (not merely ``is not None``), guard-can-fire for the wrapper-skip /
  substitution / transport branches, plus the adjacent non-trip; and the
  PUBLIC delegate ``fenced_in_command`` (the sanctioned in-process entrypoint),
  which the sibling suite never calls.
* **stop-multiplex arbitration** — which stop WINS when several fire, and the
  dispatch ORDER (priority) source of truth, pinned exactly.
* **skill-return stop guard** — the completer's degrade/mixed invariants
  (guard-can-fire for the ``model_fetch`` branch and the "never claim an
  un-injected fetch" fall-through), which the sibling only exercises in its
  all-or-nothing forms.

Each test's docstring names the concrete mutant it kills.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import skill_return_stop_guard as guard
from hpc_agent._kernel.hooks import stop_multiplex as m
from hpc_agent._kernel.hooks.scheduler_write_fence import (
    FENCED,
    _fenced_in_command,
    fenced_in_command,
)
from hpc_agent.cli.skill_returns import _committed_path

# ══════════════════════════════════════════════════════════════════════════════
# 1. scheduler write fence — TRIP conditions by EXACT verb + adjacent non-trip
# ══════════════════════════════════════════════════════════════════════════════
#
# The sibling suite asserts only ``_fenced_in_command(cmd) is not None`` over a
# BLOCKED list. These pin the EXACT verb each branch returns, so a mutant that
# still trips but reports the wrong verb (or trips on the wrong branch) is
# caught, and they drive the PUBLIC entrypoint the conformance kit imports.


def test_public_delegate_matches_private_core_on_a_trip() -> None:
    """``fenced_in_command`` (PUBLIC, capability-6 reference entrypoint) returns
    the exact verb, identical to the package-private core.

    Kills: stubbing the delegate to ``return None`` / ``return`` (the sibling
    suite only ever calls ``_fenced_in_command`` and would stay green if the
    public delegate rotted); and any drift where the delegate stops forwarding
    to ``_fenced_in_command``.
    """
    cmd = "ssh hoffman2 qdel 13910281"
    assert fenced_in_command(cmd) == "qdel"
    assert fenced_in_command(cmd) == _fenced_in_command(cmd)


def test_public_delegate_clean_command_is_none() -> None:
    """``fenced_in_command`` returns ``None`` (not a truthy sentinel) for a
    read-only command.

    Kills: a delegate mutated to always-block (``return next(iter(FENCED))``);
    an inverted clean/dirty verdict.
    """
    assert fenced_in_command("qstat -u me") is None


def test_transport_recursion_returns_the_inner_verb_exactly() -> None:
    """The ssh-transport branch returns the SPECIFIC fenced verb executed
    remotely — ``qdel`` here, not just some fenced token.

    Kills: a recursion mutated to return the first member of ``FENCED`` (e.g.
    ``qalter``) instead of the token actually found; the ``head in
    (ssh, bash, ...)`` transport-set membership check being narrowed.
    """
    verb = _fenced_in_command("ssh host qdel 42")
    assert verb == "qdel"
    assert verb in FENCED


def test_wrapper_value_skip_can_fire_and_finds_the_real_verb() -> None:
    """Guard-can-fire for the wrapper option-value skip: ``nice -n 10 sbatch``
    skips the numeric level ``10`` and blocks on the REAL verb ``sbatch``.

    Kills: dropping the ``_WRAPPER_VALUE``/flag skip in ``_first_real_token`` —
    then ``10`` becomes the head, is not fenced, and the command passes UNFENCED
    (a consequence-bearing sbatch escapes). Asserting ``== "sbatch"`` (not
    ``is not None``) also kills a skip that overshoots onto the wrong token.
    """
    assert _fenced_in_command("nice -n 10 sbatch job.sh") == "sbatch"


def test_wrapper_around_readonly_is_the_adjacent_non_trip() -> None:
    """Adjacent non-trip boundary to the wrapper-skip: the SAME wrapper around a
    read-only command stays unfenced.

    Kills: a mutant that blocks whenever a skip-wrapper is present (over-fencing
    ``nice``/``timeout``/``stdbuf`` regardless of the wrapped verb).
    """
    assert _fenced_in_command("nice -n 10 python train.py") is None


def test_command_substitution_branch_returns_the_verb() -> None:
    """Guard-can-fire for ``_fenced_in_substitution``: ``echo $(qdel 5)`` runs
    ``qdel`` inside the substitution though the head is the innocent ``echo``.

    Kills: removing the substitution scan (head-only analysis would pass this);
    a depth-tracking off-by-one that never closes the group.
    """
    assert _fenced_in_command("echo $(qdel 5)") == "qdel"


def test_exec_wrapper_transparently_execs_the_verb() -> None:
    """Finding #24: ``exec qsub`` makes ``qsub`` the executing head.

    Kills: dropping ``exec``/``command`` from ``_SKIP_WRAPPERS`` — then ``exec``
    becomes the head, is not fenced, and the qsub escapes unfenced.
    """
    assert _fenced_in_command("exec qsub job.sh") == "qsub"


def test_background_operator_splits_segments_second_runs() -> None:
    """A bare ``&`` is a SEGMENT operator (backgrounding), not a redirection:
    ``sleep 1 & qsub job.sh`` runs ``qsub`` in the second segment.

    Kills: ``_is_redir_op`` mutated to treat a bare ``&`` as a redirection
    operator (it would swallow the following ``qsub`` as a redirect target and
    the fence would miss it); dropping ``&`` from ``_OPERATOR_TOKENS``.
    """
    assert _fenced_in_command("sleep 1 & qsub job.sh") == "qsub"


# ══════════════════════════════════════════════════════════════════════════════
# 2. stop-multiplex ARBITRATION — which stop wins, and the priority order
# ══════════════════════════════════════════════════════════════════════════════


def test_default_guard_order_is_the_pinned_priority() -> None:
    """The ONE in-code statement of Stop-guard PRIORITY: skill-return first,
    decision-rendezvous second, relay-audit last. First-block-wins means this
    tuple IS the arbitration order.

    Kills: reordering ``_DEFAULT_GUARDS`` (e.g. relay-audit ahead of
    skill-return) — a reorder would silently change which guard's block reason
    the agent sees when two fire on the same stop.
    """
    assert m._DEFAULT_GUARDS == (
        "hpc_agent._kernel.hooks.skill_return_stop_guard",
        "hpc_agent._kernel.hooks.decision_rendezvous_stop_guard",
        "hpc_agent._kernel.hooks.relay_audit_stop",
    )


def test_guard_modules_fallback_and_passthrough() -> None:
    """``_guard_modules`` maps empty argv to the default priority order and
    passes explicit args through unchanged (dispatch order == the args).

    Kills: swapping the ``args or _DEFAULT_GUARDS`` fallback (empty args would
    then dispatch nothing, or explicit args would be ignored in favour of the
    default) — either breaks the harness's ability to name the guard order.
    """
    assert m._guard_modules([]) == m._DEFAULT_GUARDS
    assert m._guard_modules(["g.x", "g.y"]) == ("g.x", "g.y")


def test_first_block_wins_over_a_later_block_regardless_of_order() -> None:
    """Exact arbitration: with TWO blocking guards, the EARLIER one's reason
    wins; the later block's reason is discarded.

    Kills: last-block-wins (``block = out`` without the ``block is None``
    guard) — the reason would flip to ``"B"``.
    """
    composed = m.compose_output(
        [
            {"decision": "block", "reason": "A", "systemMessage": "s0"},
            {"systemMessage": "s1"},
            {"decision": "block", "reason": "B", "systemMessage": "s2"},
        ]
    )
    assert composed is not None
    assert composed["decision"] == "block"
    assert composed["reason"] == "A"  # first block wins; B discarded


def test_system_messages_accumulate_across_the_winning_block() -> None:
    """A guard's ``systemMessage`` survives EVEN when it fires AFTER the winning
    block — accumulation is over ALL guards in dispatch order, not truncated at
    the first block.

    Kills: a ``break`` after the first block (``s2`` would be dropped); a
    reordering of the accumulation that no longer follows dispatch order.
    """
    composed = m.compose_output(
        [
            {"decision": "block", "reason": "A", "systemMessage": "s0"},
            {"systemMessage": "s1"},
            {"decision": "block", "reason": "B", "systemMessage": "s2"},
        ]
    )
    assert composed is not None
    assert composed["systemMessage"] == "s0\n\ns1\n\ns2"


def test_only_decision_block_wins_other_decisions_ignored() -> None:
    """Arbitration triggers on ``decision == "block"`` EXACTLY — a guard
    reporting any other ``decision`` value does not seize the stop.

    Kills: mutating the ``== "block"`` test to ``!= "block"`` or truthiness
    (``"approve"`` would then wrongly win) — the composed output must carry no
    ``decision`` key and only the surviving systemMessage.
    """
    composed = m.compose_output([{"decision": "approve"}, {"systemMessage": "x"}])
    assert composed == {"systemMessage": "x"}
    assert "decision" not in composed


def test_block_without_reason_omits_the_reason_key() -> None:
    """A block whose dict carries no ``reason`` composes to ``{"decision":
    "block"}`` with NO ``reason`` key (never ``reason: None``).

    Kills: unconditionally writing ``result["reason"] = block.get("reason")``
    (would emit ``reason: null`` and break the Stop-hook output contract).
    """
    assert m.compose_output([{"decision": "block"}]) == {"decision": "block"}


def test_all_none_and_empty_compose_to_none() -> None:
    """No block and no systemMessage → ``None`` (the stop proceeds silently).

    Kills: returning an empty ``{}`` instead of ``None`` — the caller prints
    ``"{}"`` and the harness sees spurious hook output every quiet turn.
    """
    assert m.compose_output([None, {}, {"decision": "approve"}]) is None


# ══════════════════════════════════════════════════════════════════════════════
# 3. skill-return stop guard — completer degrade/mixed invariants (guard-can-fire)
# ══════════════════════════════════════════════════════════════════════════════
#
# The sibling suite exercises the completer only all-injected or fully-dark.
# These pin the PARTIAL and DEGRADE branches: the "never claim an un-injected
# fetch" invariant (4) and the mixed ``model_fetch`` path.

_SKILL_A = "hpc-wrap-entry-point"
_SKILL_B = "hpc-classify-axis"
_ENVELOPE = {"ok": True, "skill": _SKILL_A, "entry_point_kind": "register_run"}


@pytest.fixture
def _completer_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Activate the completer split and isolate the breadcrumb home."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    monkeypatch.setenv("HPC_STOP_HOOK_APPEND", "1")
    monkeypatch.setenv("HPC_STOP_HOOK_APPEND_ON_BLOCK", "1")
    monkeypatch.setattr("hpc_agent.cli.skill_returns.known_return_dirs", lambda: [])


def _commit(exp: Path, skill: str, env: dict) -> Path:
    p = _committed_path(exp, skill)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(env), encoding="utf-8")
    return p


def _payload(exp: Path) -> dict:
    return {"hook_event_name": "Stop", "stop_hook_active": False, "cwd": str(exp)}


def test_completer_degrades_to_rejector_when_no_envelope_injectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _completer_env: None
) -> None:
    """Invariant 4: completer ACTIVE but every envelope read fails → the guard
    falls through to the REJECTOR (model-fetch instructions, no ``systemMessage``)
    and clears NOTHING — it must never claim a fetch it did not perform.

    Kills: ``_completer_output`` returning a truthy output when ``injected`` is
    empty (claiming an un-injected fetch); ``build_hook_output`` not falling
    through to ``_rejector_output`` on the ``None`` completer result — the
    committed file would be silently orphaned with no fetch instruction.
    """
    committed = _commit(tmp_path, _SKILL_A, _ENVELOPE)
    monkeypatch.setattr(
        "hpc_agent._kernel.hooks.skill_return_autofetch.read_committed_envelope",
        lambda _dir, _skill: None,
    )

    out = guard.build_hook_output(_payload(tmp_path))

    assert out is not None
    assert out["decision"] == "block"
    assert "systemMessage" not in out  # nothing was injected in code
    assert f"fetch-skill-return --skill {_SKILL_A}" in out["reason"]
    assert committed.exists()  # a degraded completer clears nothing


def test_completer_mixed_injects_readable_and_leaves_unreadable_on_fetch_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _completer_env: None
) -> None:
    """Guard-can-fire for the ``model_fetch`` branch: one envelope reads, one
    does not. The readable one is injected (systemMessage) and its file cleared;
    the unreadable one stays on the model-fetch path (its file kept, its fetch
    command still in the reason).

    Kills: a completer that injects ALL-or-NOTHING (dropping the per-skill
    ``model_fetch`` accumulation) — the unreadable skill would either be lost
    (no fetch instruction) or wrongly reported as fetched; and a clear that
    fires for the unreadable skill too.
    """
    committed_a = _commit(tmp_path, _SKILL_A, _ENVELOPE)
    committed_b = _commit(tmp_path, _SKILL_B, {"ok": True, "skill": _SKILL_B})
    from hpc_agent._kernel.hooks import skill_return_autofetch as af

    real_read = af.read_committed_envelope
    monkeypatch.setattr(
        af,
        "read_committed_envelope",
        lambda d, s: real_read(d, s) if s == _SKILL_A else None,
    )

    out = guard.build_hook_output(_payload(tmp_path))

    assert out is not None
    assert out["decision"] == "block"
    # Readable skill: injected in code, its committed file cleared.
    assert _SKILL_A in out["systemMessage"]
    assert not committed_a.exists()
    # Unreadable skill: still owed by the model, file preserved.
    assert f"fetch-skill-return --skill {_SKILL_B}" in out["reason"]
    assert committed_b.exists()


def test_rejector_join_order_follows_known_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejector chains multiple pending fetches with `` && `` in
    ``_KNOWN_SKILLS`` order (wrap-entry-point before classify-axis), so the
    agent runs them as one command line.

    Kills: emitting the fetches in arbitrary/commit order, or with a different
    separator (the composed shell line would break or reorder).
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    monkeypatch.setattr("hpc_agent.cli.skill_returns.known_return_dirs", lambda: [])
    _commit(tmp_path, _SKILL_B, {"ok": True, "skill": _SKILL_B})
    _commit(tmp_path, _SKILL_A, _ENVELOPE)

    out = guard.build_hook_output(_payload(tmp_path))

    assert out is not None
    reason = out["reason"]
    idx_a = reason.index(f"fetch-skill-return --skill {_SKILL_A}")
    idx_b = reason.index(f"fetch-skill-return --skill {_SKILL_B}")
    assert idx_a < idx_b  # _KNOWN_SKILLS order, not commit order
    assert " && " in reason  # chained into one command line
