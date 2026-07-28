"""Mine Claude Code session transcripts for the hpc-agent forensic timeline.

``~/.claude/projects/<project-slug>/<session>.jsonl`` is the harness's own
append-only record of a session: every human turn, every tool call, every
tool result. This walker reduces one or more of those transcripts to the
timeline that matters for an experiment post-mortem:

* **human turns** — what the human actually typed (harness-injected user
  turns — task notifications, local-command echoes, system reminders — are
  dropped via the SAME public filter the utterance capture hook uses,
  :func:`hpc_agent.state.utterances.is_harness_injected`, so the walker and
  the trust log agree on what "human-typed" means);
* **harness verbs** — every hpc-agent touchpoint, exact-matched (no
  heuristics): MCP tool calls named ``mcp__hpc-agent__*`` and Bash commands
  invoking ``hpc-agent``;
* **off-script cluster mutations** — the class that motivated this tool
  (2026-07-27: the session went off-harness mid-run and the archaeology took
  a fresh session an hour): scheduler-mutating commands (``qsub``/``sbatch``/
  ``qdel``/``scancel``/…) that did NOT go through hpc-agent, whether composed
  by the model in a Bash tool call or typed by the human as a ``!``-prefix
  ``<bash-input>`` turn. These bypass every journal by construction — the
  transcript is their ONLY record, which is exactly why they get a section.

The reverse join: utterance records now carry the additive ``session`` key
(the capture hook stamps the payload's ``session_id``), so a journal record
points at its transcript file and this walker carries the timeline back.

Read-only, stdlib + hpc_agent imports only, UTF-8 output. Not a CLI verb by
design: it mines HARNESS-side artifacts on the operator's box (the same
family as the lint/build tools in this directory), not experiment state —
the CLI-verbs-over-internals doctrine (#200) covers agent-facing experiment
surfaces, and an agent has no business self-auditing its own transcript
mid-run (the walker's consumer is the human, or a fresh forensics session).

Usage::

    python scripts/mine_session_logs.py <transcript.jsonl> [...] [--format md|jsonl]
    python scripts/mine_session_logs.py --project-dir ~/.claude/projects/<slug> --last 2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# ── classification ────────────────────────────────────────────────────────────

#: Scheduler-mutating verbs: presence in a NON-hpc-agent command marks it
#: off-script. Deliberately scheduler-scoped (submit / kill / hold / alter):
#: read-only probes (qstat, squeue, sacct, tail) are normal manual telemetry
#: and would drown the signal.
_MUTATION_VERBS = re.compile(
    r"\b(qsub|sbatch|qdel|scancel|qhold|qrls|qalter"
    r"|scontrol\s+(?:hold|release|requeue|update))\b"
)

#: A mutation only counts when the command also reaches a remote shell — the
#: scheduler lives on the cluster, so a real off-script mutation rides ``ssh``
#: (first live walk: ``until grep -qE "...qsub|submitted..."`` log-tail
#: watchers matched the verb inside a grep PATTERN; requiring the transport
#: dropped all 30+ of those and kept every genuine hand-submit/kill). A
#: command that STARTS with the verb still counts (a Linux workstation with
#: local scheduler client tools).
_REMOTE_SHELL = re.compile(r"\bssh(?:\.exe)?\b", re.IGNORECASE)

#: Exact-match harness stamp: the MCP catalog prefix, and an hpc-agent CLI
#: invocation in a shell command (word-boundary; matches ``hpc-agent``,
#: ``hpc-agent.exe``, and ``-m hpc_agent`` module runs).
_MCP_PREFIX = "mcp__hpc-agent__"
_CLI_INVOCATION = re.compile(r"\bhpc-agent(?:\.exe)?\b|\B-m\s+hpc_agent\b")

#: ``!``-prefix shell turns the harness records as user content.
_BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


@dataclass
class Event:
    """One timeline row. ``kind`` ∈ human | harness_verb | off_script | bash."""

    ts: str
    kind: str
    detail: str
    session: str


def _iter_records(path: Path):
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn tail line never kills the walk


def _ts_of(rec: dict) -> str:
    ts = rec.get("timestamp")
    return ts if isinstance(ts, str) else ""


def _text_blocks(content) -> list[str]:
    """The text bodies of a user message's content (string or block list)."""
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    out.append(text)
    return out


def _classify_command(command: str) -> str | None:
    """``harness_verb`` for an hpc-agent CLI call, ``off_script`` for a bare
    scheduler mutation, ``None`` for everything else (read probes, local
    work)."""
    if _CLI_INVOCATION.search(command):
        return "harness_verb"
    if _MUTATION_VERBS.search(command) and (
        _REMOTE_SHELL.search(command) or _MUTATION_VERBS.match(command.lstrip())
    ):
        return "off_script"
    return None


def _compact(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


def walk_transcript(path: Path) -> list[Event]:
    """Reduce one transcript to its forensic timeline (chronological)."""
    from hpc_agent.state.utterances import is_harness_injected

    session = path.stem
    events: list[Event] = []
    for rec in _iter_records(path):
        rec_type = rec.get("type")
        msg = rec.get("message") or {}
        content = msg.get("content")
        ts = _ts_of(rec)
        if rec_type == "user" and not rec.get("isMeta"):
            for text in _text_blocks(content):
                stripped = _SYSTEM_REMINDER_RE.sub("", text).strip()
                if not stripped:
                    continue
                # ``!``-prefix shell turns: user-typed, but they are COMMANDS —
                # classify like any other shell command (the 07-27 hand-sbatch
                # class), and never count them as prose utterances.
                bash_inputs = _BASH_INPUT_RE.findall(stripped)
                if bash_inputs:
                    for cmd in bash_inputs:
                        kind = _classify_command(cmd) or "bash"
                        events.append(Event(ts, kind, _compact(cmd), session))
                    continue
                if is_harness_injected(stripped):
                    continue  # notifications / echoes — not human-typed
                events.append(Event(ts, "human", _compact(stripped, 400), session))
        elif rec_type == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "")
                tool_input = block.get("input") or {}
                if name.startswith(_MCP_PREFIX):
                    events.append(
                        Event(ts, "harness_verb", name.removeprefix(_MCP_PREFIX), session)
                    )
                elif name == "Bash" and isinstance(tool_input, dict):
                    command = str(tool_input.get("command") or "")
                    cmd_kind = _classify_command(command)
                    if cmd_kind:
                        events.append(Event(ts, cmd_kind, _compact(command), session))
    return events


def _span(events: list[Event]) -> str:
    stamped = [e.ts for e in events if e.ts]
    if not stamped:
        return "(no timestamps)"
    return f"{min(stamped)} → {max(stamped)}"


def render_markdown(events: list[Event]) -> str:
    """The human-facing report: summary, the off-script section, the timeline."""
    lines = ["# Session forensic timeline", ""]
    lines.append(f"- span: {_span(events)}")
    for kind, label in [
        ("human", "human turns"),
        ("harness_verb", "harness verbs (hpc-agent)"),
        ("off_script", "OFF-SCRIPT cluster mutations"),
    ]:
        lines.append(f"- {label}: {sum(1 for e in events if e.kind == kind)}")
    off = [e for e in events if e.kind == "off_script"]
    if off:
        lines += [
            "",
            "## OFF-SCRIPT cluster mutations",
            "",
            "Scheduler-mutating commands that bypassed hpc-agent — no journal",
            "record exists for these; this transcript is their only trace.",
            "",
        ]
        lines += [f"- `{e.ts}` [{e.session}] `{e.detail}`" for e in off]
    lines += ["", "## Timeline", ""]
    tag = {"human": "HUMAN", "harness_verb": "VERB ", "off_script": "OFF! ", "bash": "bash "}
    lines += [f"- `{e.ts}` {tag[e.kind]} {e.detail}" for e in events]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcripts", nargs="*", type=Path, help="transcript .jsonl paths")
    parser.add_argument(
        "--project-dir", type=Path, help="a ~/.claude/projects/<slug> dir to scan"
    )
    parser.add_argument(
        "--last", type=int, default=0, help="with --project-dir: only the N most recent"
    )
    parser.add_argument("--format", choices=("md", "jsonl"), default="md")
    args = parser.parse_args(argv)

    paths = list(args.transcripts)
    if args.project_dir:
        found = sorted(
            args.project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        paths += found[: args.last] if args.last else found
    if not paths:
        parser.error("no transcripts given (pass paths or --project-dir)")

    events: list[Event] = []
    for path in paths:
        events.extend(walk_transcript(path))
    events.sort(key=lambda e: (e.ts, e.session))

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if args.format == "jsonl":
        for event in events:
            print(json.dumps(asdict(event), ensure_ascii=False))
    else:
        print(render_markdown(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
