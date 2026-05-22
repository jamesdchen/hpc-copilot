"""``PreToolUse`` hook — generate the prompt of every delegated workflow spawn.

Wired into ``settings.json`` against the ``Task``/``Agent`` tool. The
hook reads the tool-call event on stdin and classifies the spawn's
``prompt``:

* An ``{"hpc_spawn": {...}}`` JSON request — validate it (``workflow``
  is one of four, ``fields`` is an object) and replace the ``prompt``
  with the canonical text from :func:`render_spawn_prompt`. The prompt
  scaffold is generated here, by code, at spawn time; the agent only
  supplies the workflow name and the fields data. An invalid request
  is denied.
* A hand-written prompt that imperatively *invokes* a workflow skill
  (``invoke``/``run``/``execute`` the ``hpc-submit`` / ``hpc-status`` /
  ``hpc-aggregate`` / ``hpc-campaign`` skill) — deny it. A workflow run
  must go through the structured request; the invocation directive is
  the one thing such a bypass cannot omit. A mere *mention* of a skill
  (summarising or reading it) is not a directive and passes through.
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
import re
import sys
from typing import Any

from hpc_agent.atoms.spawn_prompt import WORKFLOW_SKILLS, render_spawn_prompt

# A spawn request is the whole Task prompt: a JSON object carrying this
# single key. Its payload is {workflow, experiment_dir?, fields?}.
_SPAWN_KEY = "hpc_spawn"
_ALLOWED_REQUEST_KEYS = frozenset({"workflow", "experiment_dir", "fields"})

# Sentinel: the prompt is not a spawn request at all (vs. a malformed
# one, which is a request that fails validation and gets denied).
_NOT_A_REQUEST: Any = object()

# A non-request prompt that *imperatively invokes* a workflow skill is an
# unpinned workflow run. Anchor on the directive grammar — an execution
# verb, "the", the `hpc-<wf>` skill name, "skill" — not on a bare
# skill-name mention. That keeps research / documentation spawns
# ("summarize the hpc-submit skill", "read skills/hpc-submit/SKILL.md")
# out of the deny while still catching every real invocation.
_WORKFLOW_DIRECTIVE_RE = re.compile(
    r"\b(?:invoke|run|execute)\s+the\s+[`*]?"
    r"hpc-(?:submit|status|aggregate|campaign)[`*]?\s+skill\b",
    re.IGNORECASE,
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


def _spawn_request(prompt: str) -> Any:
    """Extract the ``hpc_spawn`` payload from *prompt*.

    Returns the payload (any type — validation is the caller's job) when
    the prompt is a JSON object carrying the ``hpc_spawn`` key, else
    :data:`_NOT_A_REQUEST`.
    """
    stripped = prompt.strip()
    if not stripped.startswith("{"):
        return _NOT_A_REQUEST
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return _NOT_A_REQUEST
    if not isinstance(obj, dict) or _SPAWN_KEY not in obj:
        return _NOT_A_REQUEST
    return obj[_SPAWN_KEY]


def _validate(payload: Any) -> str | None:
    """Return a deny reason for *payload*, or ``None`` if it is valid."""
    if not isinstance(payload, dict):
        return f"`{_SPAWN_KEY}` must be a JSON object."
    extra = sorted(set(payload) - _ALLOWED_REQUEST_KEYS)
    if extra:
        return (
            f"unexpected key(s) in the {_SPAWN_KEY} request: {extra}; "
            f"allowed keys are {sorted(_ALLOWED_REQUEST_KEYS)}."
        )
    workflow = payload.get("workflow")
    if workflow not in WORKFLOW_SKILLS:
        return f"`workflow` must be one of {sorted(WORKFLOW_SKILLS)}; got {workflow!r}."
    if not isinstance(payload.get("fields", {}), dict):
        return "`fields` must be a JSON object."
    experiment_dir = payload.get("experiment_dir", ".")
    if not isinstance(experiment_dir, str) or "\n" in experiment_dir:
        return "`experiment_dir` must be a single-line string."
    return None


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

    payload = _spawn_request(prompt)
    if payload is _NOT_A_REQUEST:
        # Not a spawn request. If the prompt nonetheless invokes a
        # workflow skill, it is an unpinned workflow run — deny it so a
        # workflow can only run through the structured request. Every
        # other spawn passes through.
        if _WORKFLOW_DIRECTIVE_RE.search(prompt):
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

    reason = _validate(payload)
    if reason is not None:
        return _decision("deny", reason=f"invalid hpc_spawn request: {reason}")

    rendered = render_spawn_prompt(
        workflow=payload["workflow"],
        experiment_dir=payload.get("experiment_dir", "."),
        fields=payload.get("fields", {}),
    )
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
