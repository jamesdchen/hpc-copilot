"""``PreToolUse`` hook — pin the prompt of every delegated workflow spawn.

Wired into ``settings.json`` against the ``Task``/``Agent`` tool. The
hook reads the tool-call event on stdin and acts only on calls whose
``prompt`` is a bare ``spec://<sha256>`` reference — every other
subagent spawn (Explore, general-purpose, ...) passes through
untouched.

For a spec reference it resolves ``.hpc/spawn/<sha256>.json``, verifies
the file's SHA-256 still equals the reference, and rewrites the tool
call's ``prompt`` to the canonical text stored inside. The model's
authored bytes never reach the subagent: the only thing it controls is
a 64-hex-char hash, which either resolves to a code-written spec file
or is denied.

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
        # Not a delegated-workflow spawn — leave it alone.
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
