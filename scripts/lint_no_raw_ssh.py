"""CI lint: no raw ``ssh`` / ``scp`` / ``rsync`` in agent-facing prose.

Companion to ``lint_backend_boundary.py`` / ``lint_subject_imports.py``. Where
those keep core Python routed through its seams, this keeps the **agent-facing
surfaces** — SKILL bodies and worker prompts — from offering a *raw-ssh
affordance*. An agent that needs to touch the cluster must reach for a
throttled framework verb (``inspect-deployment``, ``batch-status``,
``reconcile``, ``check-preflight``, ``aggregate-flow`` …), never a bare
``ssh <host> "<cmd>"``.

Why it matters: raw ssh bypasses the #346 connection-storm hardening
(``ConnectTimeout`` / ``IdentitiesOnly`` / the per-host ``safe_interval``
throttle in ``infra/ssh_throttle.py``). Those guards only protect the cluster
if **all** SSH flows through ``infra.remote.ssh_run``; a raw-ssh side channel
reopens the hole that earned the CARC fail2ban ban. Per the determinism
principle (``docs/internals/engineering-principles.md`` "The determinism
boundary"), the fix is to **remove the affordance**, not to discourage it in
prose — and a lint is how "removed" is mechanized.

What it flags
-------------

A bare ``ssh`` / ``scp`` / ``rsync`` **invocation** appearing inside a code
span (inline ``` `...` ``` or a fenced ```` ``` ```` block) in a scanned file.
"Invocation" means the command keyword followed by an argument — i.e. the form
you would actually *run*:

    `ssh usc-discovery "ls /scratch1/..."`     ← flagged (raw remote exec)
    `rsync push`                               ← flagged (use a verb)

Deliberately NOT flagged, to avoid false positives on documentation:

* Plain prose mentions outside code spans ("raw ssh bypasses the guards") —
  only code spans are scanned, because that is where a runnable command lives.
* The bare word in backticks with no argument (``` `ssh` ```, ``` `rsync` ```)
  — a noun, not an invocation.
* Identifier forms — ``ssh_run(`` / ``ssh_target`` / ``rsync_push`` /
  ``ssh-add`` / ``ssh-keygen`` — the keyword must be a standalone token
  followed by whitespace.
* An angle-bracket *placeholder* destination (``ssh <host> echo ok``) — that
  is illustrative documentation of a verb's internal behavior, not an authored
  command for the agent to run.

Scope
-----

* ``src/hpc_agent/slash_commands/skills/**/SKILL.md``
* ``src/hpc_agent/_kernel/extension/worker_prompts/*.md``

A genuine human-debug doc that must show a raw ssh command adds a cited entry
to :data:`ALLOWLIST` (scan-root-relative path) — the same escape valve
``lint_backend_boundary.py`` uses. The lint targets *agent-facing* skill /
worker-prompt prose, not a human running ``! ssh …`` interactively.

Every violation surfaces a ``path:lineno: raw-ssh affordance: ...`` line and
the script exits 1. The fire path is exercised in
``tests/scripts/test_lint_no_raw_ssh.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src"

# Scan-root-relative globs for the agent-facing prose surfaces.
_SKILL_GLOB = "hpc_agent/slash_commands/skills/*/SKILL.md"
_WORKER_PROMPT_GLOB = "hpc_agent/_kernel/extension/worker_prompts/*.md"

# Cited exemptions: scan-root-relative paths of genuine human-debug docs that
# must show a raw ssh command. Add an entry only as a reviewed decision, with a
# comment citing why the throttled verb does not apply.
ALLOWLIST: frozenset[str] = frozenset(
    {
        # The /release skill is a HUMAN-run release procedure (its contract
        # halts before every outward step). Its Hoffman2 wheel install is a
        # one-off interactive scp/ssh by the human's own session — no
        # autonomous worker executes it, and no throttled verb ships wheels
        # to a conda env. Ported from ~/.claude/skills/release 2026-07-04.
        "hpc_agent/slash_commands/skills/release/SKILL.md",
    }
)

# An ``ssh`` / ``scp`` / ``rsync`` invocation: the keyword as a standalone token
# (not ``ssh_run`` / ``ssh-add`` / ``rsync_push`` — guarded by the lookbehind +
# the whitespace requirement) followed by an argument. ``(?!<)`` skips an
# angle-bracket placeholder destination (illustrative docs, not a real command).
_INVOCATION_RE = re.compile(r"(?<![\w-])(ssh|scp|rsync)\s+(?!<)\S")

# Code-span extractors: fenced blocks first (multi-line), then inline spans.
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_RE = re.compile(r"`[^`\n]+`")


def _code_spans(text: str) -> list[tuple[int, str]]:
    """Return ``(start_offset, span_text)`` for every code span in *text*.

    Both fenced blocks and inline spans, so a raw command is caught whether it
    is written ``` `ssh ...` ``` inline or inside a ```` ``` ```` block.
    """
    return [(m.start(), m.group(0)) for rx in (_FENCED_RE, _INLINE_RE) for m in rx.finditer(text)]


def lint_file(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` per raw-ssh affordance in *path*."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    findings: list[tuple[int, str]] = []
    seen: set[int] = set()
    for start, span in _code_spans(text):
        m = _INVOCATION_RE.search(span)
        if not m:
            continue
        # Line number of the match itself: count newlines up to its ABSOLUTE
        # offset (span start + match offset within the span). Counting only to
        # ``start`` and adding ``m.start()`` would add a char offset to a line
        # count — wrong for any match not at the very start of its span, and
        # badly wrong inside a multi-line fenced block.
        abs_off = start + m.start()
        lineno = text.count("\n", 0, abs_off) + 1
        if lineno in seen:
            continue
        seen.add(lineno)
        keyword = m.group(1)
        findings.append(
            (
                lineno,
                f"raw-ssh affordance: bare `{keyword}` invocation in agent-facing "
                f"prose ({span.strip()!r}). Use a throttled verb (inspect-deployment "
                f"/ batch-status / reconcile / check-preflight) so SSH stays inside "
                f"the connection-storm guards.",
            )
        )
    findings.sort(key=lambda f: f[0])
    return findings


def iter_targets(scan_root: Path) -> list[Path]:
    """Yield every scanned agent-facing markdown file under *scan_root*."""
    out: list[Path] = []
    for glob in (_SKILL_GLOB, _WORKER_PROMPT_GLOB):
        out.extend(p for p in sorted(scan_root.glob(glob)) if p.is_file())
    return out


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for path in iter_targets(root):
        try:
            # ``as_posix`` so the ALLOWLIST (forward-slash, scan-root-relative)
            # matches on Windows too — ``str(WindowsPath)`` yields backslashes
            # that would never compare equal to a cited entry.
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        if rel in ALLOWLIST:
            continue
        for lineno, hint in lint_file(path):
            try:
                disp = path.resolve().relative_to(REPO).as_posix()
            except ValueError:
                disp = path.as_posix()
            print(f"{disp}:{lineno}: {hint}")
            failures += 1
    if failures:
        print(
            f"\n{failures} raw-ssh affordance(s) in agent-facing prose. "
            f"Route cluster access through a throttled hpc-agent verb so all SSH "
            f"goes through infra.remote.ssh_run (ConnectTimeout / IdentitiesOnly / "
            f"safe_interval). A genuine human-debug doc adds a cited entry to "
            f"ALLOWLIST in scripts/lint_no_raw_ssh.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
