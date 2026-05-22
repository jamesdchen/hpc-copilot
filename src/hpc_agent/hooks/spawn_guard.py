"""``PreToolUse`` hook — pin the prompt of every delegated workflow spawn.

Wired into ``settings.json`` against the ``Task``/``Agent`` tool. The
hook reads the tool-call event on stdin and classifies the spawn's
``prompt``:

* A bare ``spec://<sha256>`` reference — resolve
  ``.hpc/spawn/<sha256>.json``, verify the file's SHA-256 still equals
  the reference, and rewrite the call's ``prompt`` to the canonical
  text stored inside. The model's authored bytes never reach the
  subagent: the only thing it controls is a 64-hex-char hash, which
  either resolves to a code-written spec file or is denied.
* A hand-written prompt that imperatively *invokes* a workflow skill
  (``invoke``/``run``/``execute`` the ``hpc-submit`` / ``hpc-status`` /
  ``hpc-aggregate`` / ``hpc-campaign`` skill) — deny it. A workflow run
  must be pinned; the invocation directive is the one thing a bypass
  cannot omit, so its presence in a raw prompt means an unpinned
  workflow spawn. A mere *mention* of a skill (summarising or reading
  it) is not a directive and passes through. The deny points the
  caller at ``hpc-agent build-spawn-prompt``.
* Anything else (Explore, general-purpose, ...) — pass through
  untouched.

Run as ``python3 -m hpc_agent.hooks.spawn_guard``. It always exits 0 —
the decision travels in the JSON written to stdout, never the exit
code, so a hook-internal hiccup degrades to "allow unchanged" rather
than wedging every subagent spawn.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_REF_RE = re.compile(r"^spec://([0-9a-f]{64})$")

# A non-spec prompt that *imperatively invokes* a workflow skill is an
# unpinned workflow run. Anchor on the directive grammar — an execution
# verb, "the", the `hpc-<wf>` skill name, "skill" — not on a bare
# skill-name mention. That keeps research / documentation spawns
# ("summarize the hpc-submit skill", "read skills/hpc-submit/SKILL.md")
# out of the deny while still catching every real invocation, including
# a verbatim paste of the canonical generated prompt.
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

    match = _REF_RE.match(prompt.strip())
    if match is None:
        # Not a pinned spec reference. If the prompt nonetheless invokes
        # a workflow skill, it is an unpinned workflow spawn — deny it
        # so the only way to run a workflow is the deterministic,
        # code-generated path. Every other spawn passes through.
        if _WORKFLOW_DIRECTIVE_RE.search(prompt):
            return _decision(
                "deny",
                reason=(
                    "This Task prompt invokes an hpc-agent workflow skill but "
                    "is not a content-addressed spec reference. Workflow runs "
                    "must be delegated deterministically: call `hpc-agent "
                    "build-spawn-prompt` and pass the `spec://<sha>` token it "
                    "returns as the Task prompt — not a hand-written one."
                ),
            )
        return None
    sha = match.group(1)

    spec_path = Path.cwd() / ".hpc" / "spawn" / f"{sha}.json"
    if not spec_path.is_file():
        return _decision(
            "deny",
            reason=(
                f"spawn spec {sha} not found under .hpc/spawn/. Re-run "
                "`hpc-agent build-spawn-prompt` to regenerate it."
            ),
        )

    raw = spec_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        return _decision(
            "deny",
            reason=(
                f"spawn spec {sha} failed its integrity check — the file's "
                "hash no longer matches its name. It was edited after "
                "generation; regenerate with `hpc-agent build-spawn-prompt`."
            ),
        )

    try:
        canonical_prompt = json.loads(raw)["prompt"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return _decision(
            "deny",
            reason=f"spawn spec {sha} is malformed (no usable `prompt`).",
        )
    if not isinstance(canonical_prompt, str):
        return _decision(
            "deny",
            reason=f"spawn spec {sha} is malformed (no usable `prompt`).",
        )

    updated = dict(tool_input)
    updated["prompt"] = canonical_prompt
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
