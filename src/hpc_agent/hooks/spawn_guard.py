"""``PreToolUse`` hook — the Claude Code adapter for the spawn contract.

Wired into ``settings.json`` against the ``Task``/``Agent`` tool. This
module is deliberately thin: it is the Claude Code *adapter* onto the
shared spawn contract. All the contract logic — what a spawn request
is, how it validates, how it renders — lives in
:mod:`hpc_agent.atoms.spawn_prompt`; this module only bridges the
Claude Code hook protocol to it.

The hook reads the tool-call event on stdin and classifies the
spawn's ``prompt``:

* An ``{"hpc_spawn": {...}}`` request — validate + render it via
  :func:`validate_and_render` and replace the ``prompt`` with the
  canonical text. An invalid request is denied.
* A hand-written prompt that imperatively invokes a workflow skill —
  deny it (:func:`is_unpinned_workflow_directive`): a workflow must go
  through the structured request, never a hand-written prompt.
* Anything else (Explore, general-purpose, ...) — pass through
  untouched.

Run as ``python3 -m hpc_agent.hooks.spawn_guard``. On a malformed event
or an internal logic error it falls back to "pass through" (exit 0, no
output) rather than wedging spawns. One case it cannot fail-open on: if
``hpc_agent`` itself fails to import, ``python3 -m`` exits non-zero
before this module runs.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from hpc_agent.atoms.spawn_prompt import (
    SpawnContractError,
    extract_spawn_payload,
    is_unpinned_workflow_directive,
    validate_and_render,
)


def _decision(
    permission: str,
    *,
    reason: str | None = None,
    updated_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": permission,
    }
    if reason is not None:
        inner["permissionDecisionReason"] = reason
    if updated_input is not None:
        inner["updatedInput"] = updated_input
    return {"hookSpecificOutput": inner}


def evaluate(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the hook output for *event*, or ``None`` to pass through.

    Split out from :func:`main` so it is unit-testable without stdio.
    """
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str):
        return None

    is_request, payload = extract_spawn_payload(prompt)
    if not is_request:
        # Not a spawn request. If the prompt nonetheless invokes a
        # workflow skill, it is an unpinned workflow run — deny it so a
        # workflow can only run through the structured request. Every
        # other spawn passes through.
        if is_unpinned_workflow_directive(prompt):
            return _decision(
                "deny",
                reason=(
                    "This Task prompt invokes an hpc-agent workflow skill but "
                    "is not an `hpc_spawn` request. A workflow must be "
                    'delegated as a Task prompt of the form {"hpc_spawn": '
                    '{"workflow": ..., "fields": ...}} — the spawn_guard hook '
                    "renders the canonical prompt from it. Do not hand-write "
                    "a workflow prompt."
                ),
            )
        return None

    try:
        rendered = validate_and_render(payload)
    except SpawnContractError as exc:
        return _decision("deny", reason=f"invalid hpc_spawn request: {exc}")

    updated = dict(tool_input)
    updated["prompt"] = rendered
    return _decision("allow", updated_input=updated)


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        # Can't parse our own input — fail open rather than block spawns.
        return 0
    if not isinstance(event, dict):
        return 0

    output = evaluate(event)
    if output is not None:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
