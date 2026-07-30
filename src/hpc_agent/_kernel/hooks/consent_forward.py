"""``PreToolUse`` hook — forward the JOURNAL's consent to the harness permission layer.

The 2026-07-30 exhibit (attended-latency plan): the human journals a greenlight
in hpc-agent — a real ``append-decision`` record whose ``resolved.next_block``
names ``submit-s2`` — and then Claude Code's auto-mode permission classifier
blocks the ``mcp__hpc-agent__submit-s2`` call anyway, because IN-BAND consent is
INVISIBLE to the harness. The human is asked a second time for a decision they
already made, in their own words, seconds earlier. That second ask is pure
attended latency.

The rejected fix is a standing allowlist (``mcp__hpc-agent__submit-s2`` always
allowed): that trades one round-trip for a permanently open boundary and makes
the harness layer lie about every FUTURE call, including the ones nobody
greenlit. The fix that keeps the boundary is this hook: at ``PreToolUse`` time,
READ the journal the gate itself reads and forward what is already there —
``allow`` when a live greenlight/standing consent covers exactly this verb for
exactly this run, ``ask`` otherwise. The harness stops re-asking a question the
journal already answered, and asks every question it has not.

Wiring: a ``command`` hook in ``hooks.PreToolUse`` with matcher
``mcp__hpc-agent__.*`` (the profile's :class:`~hpc_agent.harness_profile.ToolClass`
``OWN_TOOLS``), installed by :func:`hpc_agent.agent_assets.install_agent_assets`
behind a bash ``case`` pre-filter so the query verbs never pay an interpreter
start. Receives the PreToolUse payload as JSON on stdin.

The output envelope
===================

This is the repo's FIRST ``permissionDecision`` hook. The existing PreToolUse
hook (:mod:`scheduler_write_fence`) uses the OTHER PreToolUse channel — ``exit
2`` + stderr, which BLOCKS — so there is no in-repo precedent for the JSON form
to copy. The shape below follows Claude Code's documented PreToolUse protocol,
and is structurally the same ``hookSpecificOutput`` envelope the repo's
PostToolUse hooks (:mod:`skill_return_autofetch`,
:mod:`decision_rendezvous_autofetch`) already emit, with the event name and the
decision fields swapped in::

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "allow" | "ask",
                            "permissionDecisionReason": "..."}}

Emitting NO output (``None`` / empty stdout, exit 0) leaves the call to the
harness's own permission flow — that is how a verb this hook has no opinion
about passes through untouched.

Why this hook FAILS CLOSED — and why that INVERTS the other hooks
==================================================================

Every other hook in this package fails OPEN: :mod:`scheduler_write_fence`
returns 0 on a malformed payload, the autofetch hooks emit nothing on a bad
read, the Stop guards let the turn end rather than wedge it. Their failure mode
is "the guard did not fire", and the thing that keeps running is a CLI turn — a
dead lint guard costs a missed catch, never a cluster action.

This hook's decisions run the other way. Its non-firing state is not "nothing
happens" but "the harness asks the human", and its firing state includes
``allow`` — a decision that SPENDS the boundary. So every unknown here resolves
to ``ask``:

* unparseable stdin, or a payload that is not a JSON object → ``ask``;
* a tool shape this hook cannot read (no ``tool_name``, no resolvable
  ``experiment_dir``, zero or ambiguous ``run_id``) → ``ask``;
* a tool that is definitively NOT ours (no ``mcp__hpc-agent__`` prefix) → NO
  decision. This is the one unknown that is not an ask: "not our call" is a
  confident classification, not a failure to classify, and volunteering an
  ``ask`` on another server's tools would be an overreach. Nothing is forwarded
  on that path either way;
* a missing, empty, or corrupt journal → ``ask`` (``read_decisions`` skips
  corrupt lines, so a corrupt greenlight simply is not found);
* ANY exception anywhere in the decision path → ``ask``.

A dead hook must never mean a silent allow. ``ask`` is exactly the status quo
(the human sees the normal permission prompt), so the worst failure this hook
can have is to be useless — never permissive. It also never emits ``deny``:
refusing a mis-sequenced verb is the GATE's job at execution
(``ops/block_gate.assert_greenlit_or_consented``), where the refusal message can
name the predecessor brief and remediate itself. A ``deny`` here would replace a
self-remediating refusal with an opaque harness block.

What this hook FORWARDS — and what it therefore relies on
==========================================================

This hook adds no trust: it forwards trust the journal already carries. That
makes the JOURNAL's integrity the thing the ``allow`` rests on, and the journal
is agent-writable — an agent with ``Write``/``Bash`` can append a greenlight
record around ``append-decision``'s authorship gates rather than through them.
The harness permission prompt this hook suppresses was, incidentally,
defense-in-depth against exactly that: a human eyeball on the call even when a
"greenlight" was on file. Suppressing it removes that incidental backstop for
the gated verbs.

Stated here deliberately, not fixed here: closing it means journal INTEGRITY
(e.g. attestation-signed greenlights, or a tamper-evident record chain), which
is a ruling about the journal's trust model, not a property of a permission
hook. Note that this hook's own design already keeps the blast radius bounded —
``append-decision`` (the verb that mints consent) and ``kill`` always ask, and
the ``allow`` only ever covers a verb whose own gate re-reads the same record.

The decision table
==================

===========================  ==================================================
verb class                   decision
===========================  ==================================================
gated (``GATED_BLOCKS``)     ``allow`` iff a journaled greenlight or a live
                             standing consent covers it; else ``ask``
``append-decision``          ALWAYS ``ask`` — hard-coded
``kill``                     ALWAYS ``ask`` — hard-coded
every other verb             no decision (pass through untouched)
===========================  ==================================================

The gated set is DERIVED from :data:`hpc_agent.infra.block_chain.GATED_BLOCKS`
at call time, never copied into a list here: that constant is the census of
"verbs whose op body calls the greenlight gate", and a hand list would forward
consent for a verb the gate does not check (or fail to forward it for a newly
gated one).

``append-decision`` is hard-coded to ``ask`` because it is the verb that COMMITS
consent. Authorizing a consent-commit from consent already on file is the
laundering shape in miniature — the machine would be voting itself a decision.
``kill`` is hard-coded to ``ask`` because it is destructive and irreversible;
the visible boundary IS the point.

Bash is deliberately NOT matched. The equivalent ``hpc-agent submit-s2 --spec
…`` CLI form would need command-line parsing to recover the verb and spec, and
the only Bash-parsing hook here (:mod:`scheduler_write_fence`) analyses
scheduler command POSITION, not hpc-agent verb arguments — a second, subtly
different parser is precisely the drift this repo refuses. A CLI-form call
therefore just gets the normal prompt.
"""

from __future__ import annotations

import json
import sys
from typing import Any

__all__ = [
    "ALWAYS_ASK_VERBS",
    "build_hook_output",
    "gated_verbs",
    "main",
    "verb_from_tool_name",
]

#: Verbs that ALWAYS ask, whatever the journal says (see the module docstring).
#: ``append-decision`` commits consent; ``kill`` destroys work.
ALWAYS_ASK_VERBS: frozenset[str] = frozenset({"append-decision", "kill"})

#: The EXACT MCP tool-name prefix Claude Code gives OUR projected server's tools
#: (``mcp__hpc-agent__<verb>``). A tool name must carry this prefix verbatim
#: before any consent is forwarded for it.
#:
#: Checking the server segment — not just ``mcp__`` + a trailing verb — is
#: defense in depth. The installed matcher (``mcp__hpc-agent__.*``) already
#: means only our server's tools reach this hook, but a hook that trusted the
#: LAST ``__`` segment alone would forward hpc-agent's journal consent to
#: ``mcp__evil-server__submit-s3`` if it were ever invoked by hand, by a
#: broadened matcher, or by a future harness that routes differently. A
#: permission surface must not depend on its own matcher for correctness.
#:
#: ONE DEFINITION: this is the profile's ``ToolClass.OWN_TOOLS`` matcher minus
#: its trailing ``.*`` regex. It is restated here rather than imported because
#: :mod:`hpc_agent.harness_profile` is deliberately unreadable from the trust
#: path (``tests/contracts/test_harness_profile_boundary.py`` — no gate/verify/
#: journal module may read the activation profile), and this hook decides
#: permissions. The equality is a FIRED PIN instead:
#: ``tests/_kernel/hooks/test_consent_forward.py::
#: test_own_tool_prefix_matches_the_installed_matcher``, which derives the
#: matcher from ``ClaudeCodeProfile.matcher_string(ToolClass.OWN_TOOLS)`` and
#: goes red the moment the two drift.
_OWN_TOOL_PREFIX = "mcp__hpc-agent__"

_EVENT = "PreToolUse"


def gated_verbs() -> frozenset[str]:
    """The consent-gated block verbs, read LIVE from the chain's census.

    Resolved through the module attribute (not a from-import bound at import
    time) so the derivation is real: the hook covers exactly the verbs
    :data:`hpc_agent.infra.block_chain.GATED_BLOCKS` names, including one added
    after this module was written.
    """
    from hpc_agent.infra import block_chain

    return frozenset(block_chain.GATED_BLOCKS)


def verb_from_tool_name(tool_name: str) -> str | None:
    """The hpc-agent verb an MCP *tool_name* invokes, or ``None``.

    ``mcp__hpc-agent__submit-s2`` → ``submit-s2``. Returns ``None`` for anything
    that is not a tool of OUR server — a Bash call, a bare tool, another
    server's tool (``mcp__evil-server__submit-s3``), or a malformed name with an
    empty server segment (``mcp____submit-s3``). The caller emits no decision in
    that case, so a foreign tool that merely ENDS in one of our verb names can
    never collect hpc-agent's journaled consent.
    """
    if not isinstance(tool_name, str) or not tool_name.startswith(_OWN_TOOL_PREFIX):
        return None
    verb = tool_name[len(_OWN_TOOL_PREFIX) :]
    # A verb segment must be a single leaf: no further ``__`` nesting, which
    # would mean the name was not shaped by our server's projection.
    return verb if verb and "__" not in verb else None


def _ask(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": _EVENT,
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def _allow(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": _EVENT,
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }


def _collect_values(node: Any, key: str, found: list[str]) -> None:
    """Depth-first collect every non-empty string value stored under *key*."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, str) and v:
                found.append(v)
            else:
                _collect_values(v, key, found)
    elif isinstance(node, list):
        for item in node:
            _collect_values(item, key, found)


def _sole_run_id(tool_input: dict[str, Any]) -> str | None:
    """The ONE ``run_id`` *tool_input* names, or ``None`` when it is not unique.

    The block specs nest ``run_id`` at different depths (``spec.submit.submit``
    for S2/S3, ``spec.aggregate`` for S4/aggregate-run), and mirroring those
    shapes here would be a second copy of the wire models. A scan for the key
    is shape-free and self-maintaining; ambiguity (two different run ids, or
    none) resolves to ``None`` → the caller asks.
    """
    found: list[str] = []
    _collect_values(tool_input, "run_id", found)
    unique = set(found)
    return found[0] if len(unique) == 1 else None


def _experiment_dir(payload: dict[str, Any], tool_input: dict[str, Any]) -> str | None:
    """The experiment repo the call targets: the spec's, else the session cwd.

    ``experiment_dir`` is an OPTIONAL top-level property of every MCP tool whose
    primitive takes one, defaulting (per the server's own schema prose) to the
    server's working directory — which for an in-session server is the session
    cwd the payload carries.
    """
    candidate = tool_input.get("experiment_dir") or payload.get("cwd")
    return candidate if isinstance(candidate, str) and candidate else None


def _decide(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The decision core (no exception handling — see :func:`build_hook_output`)."""
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        # We cannot even tell WHICH call this is. That is an unreadable payload,
        # not a foreign tool, so it takes the fail-closed branch.
        return _ask(
            "consent-forward: the payload carries no readable tool_name "
            f"({tool_name!r}), so the call cannot be identified. Failing CLOSED to "
            "the normal prompt — a hook that cannot identify a call must never "
            "authorize it."
        )
    verb = verb_from_tool_name(tool_name)
    if verb is None:
        # A tool that is definitively NOT ours (a Bash call, another MCP
        # server's tool, a malformed name). No opinion: emitting nothing leaves
        # the harness's own permission flow untouched. Asking here would be an
        # overreach — this hook has no standing over another server's tools, and
        # a hook that volunteered "ask" on every foreign call would inject
        # itself into permission flows it knows nothing about. It is not a
        # weakening either: no journaled consent is ever forwarded on this path,
        # which is the only thing this hook could get wrong.
        return None

    if verb in ALWAYS_ASK_VERBS:
        why = (
            "it COMMITS the human's consent; authorizing a consent-commit from consent "
            "already on file would let the machine vote itself a decision"
            if verb == "append-decision"
            else "it is destructive and irreversible — the visible boundary IS the point"
        )
        return _ask(
            f"consent-forward: `{verb}` ALWAYS asks, whatever the journal says, because "
            f"{why}. No journaled record can pre-authorize this verb."
        )

    if verb not in gated_verbs():
        # A query verb (or any ungated verb): this hook has no opinion. Emitting
        # nothing leaves the harness's own permission flow exactly as it was.
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return _ask(
            f"consent-forward: `{verb}` is a consent-gated block but its tool_input is "
            "unreadable, so the journaled record covering it cannot be identified. "
            "Failing CLOSED to the normal prompt."
        )

    experiment_dir = _experiment_dir(payload, tool_input)
    run_id = _sole_run_id(tool_input)
    if experiment_dir is None or run_id is None:
        missing = "experiment_dir" if experiment_dir is None else "a unique run_id"
        return _ask(
            f"consent-forward: `{verb}` is a consent-gated block but {missing} could not "
            "be resolved from the call, so this hook cannot look up the journaled "
            "decision that would cover it. Failing CLOSED to the normal prompt."
        )

    from pathlib import Path

    from hpc_agent.ops.block_gate import probe_authorization

    probe = probe_authorization(Path(experiment_dir), run_id=run_id, verb=verb)
    if probe.authorized:
        record = f"block={probe.block!r}, ts={probe.ts}, scope={probe.scope}"
        basis = (
            "the human's journaled greenlight"
            if probe.basis == "greenlight"
            else "a live standing consent (overnight mode)"
        )
        return _allow(
            f"consent-forward: {basis} already authorizes `{verb}` — journaled record "
            f"[{record}]. In-band consent lives in the hpc-agent decision journal, which "
            "the harness permission layer cannot see; this hook forwards it rather than "
            "re-asking a question the human already answered. The verb's own gate "
            "re-reads the SAME record at execution, so this is a disclosure of an "
            "existing decision, not a new grant."
        )
    if probe.reason == "predecessor-evidence-not-visible-to-probe":
        # Honest about WHICH refusal this is: not "your consent can never cover
        # this boundary" but "this read-only probe cannot see the clean-terminal
        # evidence the gate would accept", so the gate may well pass on the call
        # the human is about to approve.
        return _ask(
            f"consent-forward: `{verb}` is auto-advanceable under a standing consent "
            "ONLY behind evidence that its predecessor finished clean, and that "
            "evidence is derived at execution — this pre-flight probe cannot see it. "
            "Not a refusal: the verb's own gate may still pass. Asking because "
            "forwarding consent this hook cannot verify is the one thing it must "
            "never do."
        )
    return _ask(
        f"consent-forward: no journaled greenlight or live standing consent covers "
        f"`{verb}` for {probe.scope} (failing leg: {probe.reason}). The human has not "
        "authorized this boundary — surface the predecessor brief and take a real "
        "decision. (The verb's own gate would refuse this at execution; this hook only "
        "declines to pre-authorize it.)"
    )


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a PreToolUse *payload* to the hook-output dict, or ``None``.

    ``None`` means "no opinion" (a query verb) — the caller writes nothing and
    the harness's own permission flow decides. Every OTHER outcome, including
    every failure, is a concrete ``allow``/``ask`` envelope.

    Total by construction: any exception from the decision path (a bad payload
    shape, an unreadable journal, an import failure) is caught and converted to
    ``ask``. This is the fail-CLOSED posture the module docstring justifies —
    the inverse of the lint-style guards in this package, which fail OPEN.
    """
    if not isinstance(payload, dict):
        return _ask(
            "consent-forward: the PreToolUse payload was not a JSON object, so no "
            "journaled decision could be looked up. Failing CLOSED to the normal prompt."
        )
    try:
        return _decide(payload)
    except Exception as exc:  # noqa: BLE001 — a dead hook must never mean a silent allow
        return _ask(
            f"consent-forward: the consent lookup failed ({type(exc).__name__}: {exc}). "
            "Failing CLOSED to the normal prompt — a hook that cannot read the journal "
            "must never authorize a boundary on its behalf."
        )


def main() -> int:
    """Read the PreToolUse payload from stdin; emit the decision envelope.

    Always exits 0: this hook decides via the JSON ``permissionDecision``
    channel, never via the exit-2 BLOCK channel (which is
    :mod:`scheduler_write_fence`'s). An unreadable stdin still emits ``ask``.
    """
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001 — see the fail-closed posture above
        raw = ""
    try:
        payload: Any = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001 — see below; NOT just JSONDecodeError/ValueError
        # A deeply-nested payload makes ``json.loads`` itself raise
        # RecursionError, which is neither of those — it escaped as a traceback
        # and a non-zero exit, contradicting both "unparseable stdin still emits
        # ask" and "always exits 0". Any parse failure whatsoever is now an ask.
        payload = None
    try:
        output = build_hook_output(payload)
    except Exception:  # noqa: BLE001 — build_hook_output is total; belt and braces
        output = _ask(
            "consent-forward: the decision path failed unexpectedly. Failing CLOSED "
            "to the normal prompt."
        )
    if output is not None:
        try:
            sys.stdout.write(json.dumps(output))
        except Exception:  # noqa: BLE001 — an unwritable stdout is still not an allow
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
