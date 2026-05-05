"""Stop hook: enforce the /monitor-hpc exit contract.

Reads a Stop-hook payload on stdin (Claude Code's hook input), and if
the most recent user message was a ``/monitor-hpc`` invocation, checks
that the agent's reply ends with the required ``armed:`` line. Returns
a ``decision: block`` JSON if the line is missing so the agent is
re-prompted to comply with the slash command's exit contract.

Hook input shape (per Claude Code docs):

    {
      "session_id": "...",
      "transcript_path": "/path/to/session.jsonl",
      "cwd": "...",
      "permission_mode": "default",
      "hook_event_name": "Stop",
      "stop_reason": "end_turn|max_tokens|tool_use|stop_sequence",
      "output": "<assistant text, possibly empty>",
      "tool_uses": [...]
    }

The transcript is a JSONL file the hook reads itself. Each line is one
message envelope; ``role`` is ``user`` / ``assistant`` and ``content``
is either a string or a list of content blocks.

Output:

    {} or no output                          -> allow stop
    {"decision": "block", "reason": "..."}   -> block stop with reason

Wire-up: see :func:`claude_hpc.hooks.monitor_armed_check.settings_entry`
or run ``hpc-mapreduce hook-install`` to add it to ~/.claude/settings.json.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "ARMED_RE",
    "USER_INVOCATION_RE",
    "main",
    "settings_entry",
]

# Pattern enforced by the monitor-hpc spec. Mechanism is one of cron / loop / none
# (ScheduleWakeup is intentionally absent — it is not a documented public Claude
# Code tool).
ARMED_RE = re.compile(
    r"^armed:\s+(cron|loop|none)\s+run_id=\S+\s+cadence=\d+s\s+reason=",
    re.MULTILINE,
)

USER_INVOCATION_RE = re.compile(r"(?:^|\s)/monitor-hpc(?:\s|$)")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Return parsed records from *path*; ignore parse errors line-by-line."""
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _content_text(content: Any) -> str:
    """Flatten a message's ``content`` field to plain text.

    Claude Code transcripts use either a bare string or the Anthropic
    block format (a list of ``{"type": "text", "text": "..."}`` and
    other block types). Tool-use / tool-result blocks are skipped.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(str(block.get("text", "")))
    return "\n".join(texts)


def _last_message_of_role(transcript: list[dict[str, Any]], role: str) -> str:
    for entry in reversed(transcript):
        if entry.get("role") == role:
            return _content_text(entry.get("content"))
    return ""


def settings_entry(
    *, command: str = "python -m claude_hpc.hooks.monitor_armed_check"
) -> dict[str, Any]:
    """Return the JSON entry for ~/.claude/settings.json's ``hooks.Stop`` array.

    Stable shape so :func:`hook_install` can detect prior installs and
    avoid duplicates. Override *command* to point at a custom interpreter
    (e.g. when the global ``python`` doesn't have claude-hpc installed).
    """
    return {"type": "command", "command": command}


def main() -> int:
    """Hook entry point. Read stdin, decide block-or-allow, exit 0."""
    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Malformed input from Claude Code is not the agent's fault.
        # Allow stop; this hook is enforcement, not validation of the host.
        return 0
    if not isinstance(payload, dict):
        return 0

    transcript_path = payload.get("transcript_path")
    transcript: list[dict[str, Any]] = []
    if isinstance(transcript_path, str) and transcript_path:
        transcript = _read_jsonl(Path(transcript_path))

    user_text = _last_message_of_role(transcript, "user")
    if not USER_INVOCATION_RE.search(user_text):
        # Not a /monitor-hpc turn — nothing to enforce.
        return 0

    inline_output = payload.get("output")
    assistant_text = ""
    if isinstance(inline_output, str) and inline_output.strip():
        assistant_text = inline_output
    if not assistant_text:
        assistant_text = _last_message_of_role(transcript, "assistant")

    if ARMED_RE.search(assistant_text):
        return 0  # contract met

    decision = {
        "decision": "block",
        "reason": (
            "/monitor-hpc spec violation: every invocation must emit a final "
            "line of the form `armed: <cron|loop|none> run_id=<X> cadence=<Y>s "
            'reason="<short>"` (see slash_commands/commands/monitor-hpc.md, '
            "Step 5: Required final line). Restart Step 5 — pick CronCreate "
            "(default) or /loop, then emit the line as the very last line of "
            "your response."
        ),
    }
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
