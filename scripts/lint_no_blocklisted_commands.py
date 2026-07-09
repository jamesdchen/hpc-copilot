r"""CI lint: no harness-block-listed command in agent-facing prose.

Sibling to ``lint_no_raw_ssh.py``. Where that one keeps a raw-ssh affordance out
of the agent-facing surfaces, this one keeps out every command shape that the
**Claude Code harness blocks** — because an agent driving a campaign
*autonomously* that emits one stalls on a non-bypassable permission prompt with
no human to approve. A stall mid-run is unrecoverable: it is the death sentence.

Two block lists (both observed in the operator's ``~/.claude/settings.json`` +
the auto-mode classifier):

* **Auto-mode classifier hard-blocks** — ``python -c`` / ``bash -c`` (arbitrary
  code), command substitution ``$(...)``, pipes (``|``), and background
  concurrency (``cmd &``). Refused *regardless of the allow-list*.
* **Explicit ``permissions.deny``** — ``scancel`` ``qdel`` ``rm -rf`` ``rm -r``
  ``kill -9`` ``chmod -R`` ``chown -R`` ``module purge`` ``conda remove``
  ``conda env remove`` ``pip uninstall`` ``git push --force/-f``.

Chaining (``&&`` / ``;`` / ``||``) — the surface matters
--------------------------------------------------------

The auto-mode classifier splits a command on ``&&`` / ``;`` / ``||`` and checks
each segment against the allow-list, so ``hpc-agent x && hpc-agent y`` (every
segment ``hpc-agent`` or ``git``) is permitted — and the orchestrator SKILLs
deliberately use it (see each SKILL's "Execution style"). So on a **SKILL**,
chaining is flagged ONLY when a segment is *not* an allow-listed command.

The **``hpc-worker``** agent is stricter: its invoke-only PreToolUse hook rejects
*any* ``&&`` / ``|`` / ``;`` / ``$(`` (one ``hpc-agent`` / ``git`` call per Bash
block, so the orchestrator can parse each envelope). So in a **worker_prompt**,
ALL chaining is flagged — author-time enforcement of what the hook enforces at
run-time, because a chained worker command is a hard mid-run failure.

Per the determinism principle (``docs/internals/engineering-principles.md`` "The
determinism boundary"), the fix is to **remove the affordance** and **mechanize**
the rule — this lint is how "removed" is enforced.

False-positive classes excluded (the SKILLs legitimately name these to forbid
them, and document scheduler placeholders): a bare keyword/operator with no real
operand (`` `bash -c` ``, `` `&&` `` as a *noun*); a ``|`` inside a
``<sge|slurm>`` / ``[--a|--b]`` placeholder; a markdown-escaped ``\|`` in a table
cell; ``$(`` / ``|`` inside a non-shell fenced block (e.g. a ``int | str`` Python
hint); and ``&`` / chaining on a line that does NOT invoke ``hpc-agent`` /
``git`` (a pedagogical ``cmd1 & cmd2 & wait`` counter-example, or a prose string
with a ``;``) — only a real framework-command line is a runnable instruction.

Scope
-----

* ``src/hpc_agent/slash_commands/skills/**/SKILL.md``
* ``src/hpc_agent/_kernel/extension/worker_prompts/*.md``

A genuine human-debug doc adds a cited ``(path, category)`` entry to
:data:`ALLOWLIST`. Every violation prints ``path:lineno: blocked <category>:
...`` and the script exits 1. Fire path:
``tests/scripts/test_lint_no_blocklisted_commands.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src"

_SKILL_GLOB = "hpc_agent/slash_commands/skills/*/SKILL.md"
_WORKER_PROMPT_GLOB = "hpc_agent/_kernel/extension/worker_prompts/*.md"

# Cited exemptions: ``(scan-root-relative path, category)`` for a genuine
# human-debug doc that must show a blocked command.
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # The /release skill is a HUMAN-run release procedure (halts before
        # every outward step; no autonomous worker executes it). Its
        # build-purge one-liner (`python -c "import shutil; ..."`) and the
        # WSL install (`wsl.exe -- bash -lc 'pip install ...'`) are
        # interactive human idioms, not worker instructions. Ported from
        # ~/.claude/skills/release 2026-07-04.
        ("hpc_agent/slash_commands/skills/release/SKILL.md", "python -c"),
        ("hpc_agent/slash_commands/skills/release/SKILL.md", "bash -c"),
    }
)

# Commands whose chaining the auto-mode classifier permits (each segment is
# itself allow-listed). A SKILL chain composed only of these is not flagged.
_ALLOWED_CHAIN_CMDS = frozenset({"hpc-agent", "git"})

# Fenced-block languages that are shell contexts (so ``$(`` / ``|`` / ``&&`` are
# real operators, not a Python ``int | str`` or a Makefile ``$(VAR)``). The empty
# string covers inline spans and untagged fences.
_SHELL_LANGS = frozenset(
    {"", "bash", "sh", "shell", "console", "shell-session", "sh-session", "shellsession", "zsh"}
)

# --- Checked in EVERY code span (keyword + REAL argument = invocation). The arg
# is ``\s+[^`\s]`` — a real token, not the closing backtick — so the bare noun
# `` `bash -c` `` / `` `python -c` `` (warning prose) is not matched. ---
_ARBITRARY_CODE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("python -c", re.compile(r"(?<![\w-])python3?\s+-c\s+[^`\s]")),
    ("bash -c", re.compile(r"(?<![\w-])(?:bash|sh|zsh|dash)\s+-[a-z]*c\b\s+[^`\s]")),
)
# Explicit ``permissions.deny`` verbs, as a runnable invocation.
_DENY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("scancel", re.compile(r"(?<![\w-])scancel\b")),
    ("qdel", re.compile(r"(?<![\w-])qdel\b")),
    ("rm -r", re.compile(r"(?<![\w-])rm\s+-[a-zA-Z]*r")),
    ("kill -9", re.compile(r"(?<![\w-])kill\s+-9\b")),
    ("chmod -R", re.compile(r"(?<![\w-])chmod\s+-R\b")),
    ("chown -R", re.compile(r"(?<![\w-])chown\s+-R\b")),
    ("module purge", re.compile(r"(?<![\w-])module\s+purge\b")),
    ("conda remove", re.compile(r"(?<![\w-])conda\s+(?:env\s+)?remove\b")),
    ("pip uninstall", re.compile(r"(?<![\w-])pip\s+uninstall\b")),
    ("git push --force", re.compile(r"(?<![\w-])git\s+push\b[^`\n]*\s(?:--force|-f)\b")),
)
# --- Checked per-line in shell-context spans, on the CLEANED line (delimiters /
# placeholders / escaped ``\|`` removed). Always a violation, both surfaces. ---
_ALWAYS_SHELL: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("command substitution $()", re.compile(r"\$\(")),
    ("pipe |", re.compile(r"[^\s|]\s*\|\s*[^\s|]")),
)
# Background ``&`` and chaining (``&&`` / ``;`` / ``||``) are checked ONLY on a
# line that invokes ``hpc-agent`` / ``git`` — a real framework command. A
# pedagogical counter-example (``cmd1 & cmd2 & wait``) or a prose string that
# happens to contain a ``;`` is not a runnable framework instruction.
_FRAMEWORK_CMD = re.compile(r"(?<![\w-])(?:hpc-agent|git)\b")
_BACKGROUND_RE = re.compile(r"[^\s&]\s*&(?!&)")
_CHAIN_RE = re.compile(r"&&|\|\||;")

_FENCED_RE = re.compile(r"```([^\n`]*)\n.*?```", re.DOTALL)
_INLINE_RE = re.compile(r"`[^`\n]+`")
_PLACEHOLDER_RE = re.compile(r"<[^>\n]*>|\[[^\]\n]*\]")


def _code_spans(text: str) -> list[tuple[int, str, str]]:
    """Return ``(start_offset, span_text, lang)`` for every code span."""
    spans: list[tuple[int, str, str]] = []
    for m in _FENCED_RE.finditer(text):
        info = (m.group(1) or "").strip().split()[:1]
        spans.append((m.start(), m.group(0), (info[0].lower() if info else "")))
    for m in _INLINE_RE.finditer(text):
        spans.append((m.start(), m.group(0), ""))
    return spans


def _clean_line_for_shell_ops(line: str) -> str:
    """Strip code-span delimiters, doc placeholders, and markdown-escaped
    operators so only *real* shell operators remain on this line."""
    s = re.sub(r"```[^\n]*", "", line)  # fence info-string / ``` markers
    s = s.replace("`", " ")  # inline / closing delimiters
    s = _PLACEHOLDER_RE.sub(" ", s)  # <sge|slurm>, [--a|--b]
    return s.replace(r"\|", " ").replace(r"\&", " ")  # escaped md operators


def _lineno(text: str, abs_off: int) -> int:
    return text.count("\n", 0, abs_off) + 1


def lint_file(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(lineno, category, message)`` per blocked command in *path*."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    is_worker = "worker_prompts" in path.parts
    findings: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str]] = set()

    def _record(lineno: int, category: str, snippet: str) -> None:
        if (lineno, category) in seen:
            return
        seen.add((lineno, category))
        findings.append(
            (
                lineno,
                category,
                f"blocked `{category}` in agent-facing prose ({snippet.strip()[:80]!r}). "
                f"An autonomous worker that runs this stalls on a non-bypassable "
                f"permission prompt. Use a single `hpc-agent <verb>` (or `git`) call; "
                f"read files with the Read/Grep/Glob tools.",
            )
        )

    for start, span, lang in _code_spans(text):
        # Keyword invocations: exact line from the match offset, in any span.
        for category, rx in (*_ARBITRARY_CODE, *_DENY):
            m = rx.search(span)
            if m:
                _record(_lineno(text, start + m.start()), category, span)
        if lang not in _SHELL_LANGS:
            continue
        # Shell operators + chaining: per-line on the cleaned line, exact line no.
        base = start
        for raw_line in span.split("\n"):
            cleaned = _clean_line_for_shell_ops(raw_line)
            for category, rx in _ALWAYS_SHELL:
                if rx.search(cleaned):
                    _record(_lineno(text, base), category, raw_line)
            # ``&`` / chaining only count on a real framework-command line.
            if _FRAMEWORK_CMD.search(cleaned):
                if _BACKGROUND_RE.search(cleaned):
                    _record(_lineno(text, base), "background &", raw_line)
                if _CHAIN_RE.search(cleaned):
                    tokens = [seg.split()[0] for seg in _CHAIN_RE.split(cleaned) if seg.split()]
                    if len(tokens) >= 2:
                        all_allowed = all(t in _ALLOWED_CHAIN_CMDS for t in tokens)
                        if is_worker:
                            _record(
                                _lineno(text, base),
                                "chaining (worker is invoke-only)",
                                raw_line,
                            )
                        elif not all_allowed:
                            _record(
                                _lineno(text, base),
                                "chaining a non-allow-listed command",
                                raw_line,
                            )
            base += len(raw_line) + 1  # + newline

    findings.sort(key=lambda f: (f[0], f[1]))
    return findings


def iter_targets(scan_root: Path) -> list[Path]:
    out: list[Path] = []
    for glob in (_SKILL_GLOB, _WORKER_PROMPT_GLOB):
        out.extend(p for p in sorted(scan_root.glob(glob)) if p.is_file())
    return out


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for path in iter_targets(root):
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, category, hint in lint_file(path):
            if (rel, category) in ALLOWLIST:
                continue
            try:
                disp = path.resolve().relative_to(REPO).as_posix()
            except ValueError:
                disp = path.as_posix()
            print(f"{disp}:{lineno}: {hint}")
            failures += 1
    if failures:
        print(
            f"\n{failures} harness-block-listed command(s) in agent-facing prose. "
            f"An autonomous driver that emits one stalls on a non-bypassable prompt. "
            f"Author every agent step as a single `hpc-agent <verb>` / `git` call (or "
            f"an all-`hpc-agent`/`git` `&&` chain in a SKILL) and read files with "
            f"Read/Grep/Glob. A genuine human-debug doc adds a cited (path, category) "
            f"entry to ALLOWLIST in scripts/lint_no_blocklisted_commands.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
