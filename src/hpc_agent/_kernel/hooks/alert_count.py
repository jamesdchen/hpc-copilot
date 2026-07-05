"""``SessionStart`` hook — surface the unacknowledged watchdog alert count.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.SessionStart``
array (see :func:`hpc_agent.agent_assets.install_agent_assets`). It receives the
SessionStart payload as JSON on **stdin** and may print a plain-text line on
**stdout**, which the harness injects into the session's context.

Why it exists
-------------
Proving run #3: the scheduled ``doctor`` watchdog DETECTED a stalled canary
driver and appended the drafted re-arm proposal to ``doctor.alerts.log`` — and
nobody saw it for hours, because nothing delivered the log to a surface the
human actually looks at. Detection without delivery is silence. This hook is
the cheapest delivery seam that exists: the moment a session starts in a repo
with unacknowledged alerts, the count (plus the newest alert line) lands in the
model's context, so the very first response can say "the watchdog flagged
something — run doctor / a status snapshot".

Notify only, never act (§5): the hook prints a count and points at ``doctor``;
it never re-arms, never acknowledges (the status-snapshot watermark owns
acknowledgment — see :mod:`hpc_agent.ops.recover.notify`), and never touches
the log.

Defensiveness
-------------
* The experiment dir is the payload's ``cwd`` (falling back to the process
  cwd) — a session started outside the experiment repo simply sees no alerts;
  the status snapshot and ``doctor`` remain the in-repo delivery surfaces.
* The alert read is fail-open and **non-creating**: a repo with no journal
  namespace gets none scaffolded (:func:`notify._alerts_paths`).
* For a malformed payload, an unreadable log, or zero alerts it is a clean
  no-op — prints nothing, exits ``0``. It never raises and never exits
  non-zero: a broken delivery hook must degrade to today's behaviour, not
  wedge session startup.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

__all__ = ["build_context_line", "main"]


def build_context_line(payload: Any) -> str | None:
    """Pure core: map a SessionStart *payload* to a context line, or ``None``.

    Returns ``None`` (→ caller prints nothing) when the payload carries no
    usable ``cwd`` fallback-able to the process cwd, or when that directory has
    no unacknowledged alerts. Otherwise a single human-facing line carrying the
    count, the newest alert, and the two surfaces that show/acknowledge them.
    """
    from hpc_agent.ops.recover.notify import read_unacknowledged_alerts

    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    cwd_dir = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())

    alerts = read_unacknowledged_alerts(cwd_dir)
    if not alerts:
        return None
    newest = alerts[-1]
    return (
        f"{len(alerts)} unacknowledged hpc-agent watchdog alert(s) for this repo "
        f"(newest: {newest['ts']} {newest['message']}) — run `hpc-agent doctor` "
        "for the drafted proposals; a status snapshot surfaces and acknowledges them."
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, maybe print, never crash.

    Mirrors the Stop-guard entrypoints: any unexpected error is swallowed and
    reported as a clean no-op (exit ``0``). ``argv`` is accepted for symmetry
    with the sibling hook entrypoints but is unused.
    """
    del argv
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else None
    except (json.JSONDecodeError, ValueError):
        payload = None

    try:
        line = build_context_line(payload)
    except Exception:
        return 0

    if line is not None:
        print(line, flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the harness
    raise SystemExit(main())
