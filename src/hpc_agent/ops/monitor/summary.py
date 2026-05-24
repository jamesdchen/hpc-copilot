"""``monitor-summary`` primitive — canonical user-facing tick summary.

Replaces the slash-command prose that walked the agent through framing
the per-tick / terminal report. Reads the run journal + the most
recent tick from ``.hpc/runs/<run_id>.monitor.jsonl`` and renders one
human-readable summary string the slash command prints verbatim.

Eliminates the failure mode where the agent's framing drifts from the
spec (different wording each tick, missed counts, inconsistent
phrasing of "complete" vs "done"). With this primitive, every tick's
report is byte-identical for the same input state.

Pure read-only function over the journal + tick log. Safe to call
from anywhere (slash command, external orchestrator, debug shell).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path

_TERMINAL_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {"complete", "failed", "abandoned", "timeout"}
)


def _read_last_tick(jsonl_path: Path) -> dict[str, Any] | None:
    """Return the most recent tick record, or None if the file is empty/absent."""
    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    last: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            last = rec
    return last


def _format_counts(summary: dict[str, int], total: int) -> str:
    """Render ``complete=4 running=2 pending=10 failed=0 / total=16``."""
    c = int(summary.get("complete") or 0)
    r = int(summary.get("running") or 0)
    p = int(summary.get("pending") or 0)
    f = int(summary.get("failed") or 0)
    return f"complete={c} running={r} pending={p} failed={f} / total={total}"


def _format_diff(diff: dict[str, Any]) -> str | None:
    """Render the ``newly_*`` fields of a tick's ``diff_from_prev`` block."""
    parts: list[str] = []
    nc = diff.get("newly_complete") or []
    nf = diff.get("newly_failed") or []
    nw = diff.get("newly_combined_waves") or []
    if nc:
        parts.append(f"+{len(nc)} complete")
    if nf:
        parts.append(f"+{len(nf)} failed")
    if nw:
        parts.append(f"combined waves {sorted(nw)}")
    return ", ".join(parts) if parts else None


@primitive(
    name="monitor-summary",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Render the canonical user-facing tick summary for a run. "
            "Reads .hpc/runs/<run_id>.monitor.jsonl + the run journal "
            "and returns {lifecycle_state, headline, body, armed_hint}. "
            "Slash command prints these verbatim."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--run-id",
                type=str,
                required=True,
                help="Run identifier (matches the .hpc/runs/<run_id>.json sidecar stem).",
            ),
        ),
    ),
    agent_facing=True,
)
def monitor_summary(
    experiment_dir: Path,
    *,
    run_id: str,
) -> dict[str, Any]:
    """Render the canonical user-facing summary for a run's most recent tick.

    Returns ``{lifecycle_state, headline, body, armed_hint, journal_missing}``:

    * ``lifecycle_state`` — one of the terminal states or ``in_flight``.
      Defaults to ``"abandoned"`` (closest semantic match — record gone)
      when ``journal_missing=True``.
    * ``journal_missing`` — True iff the journal record could not be
      loaded. Headline carries an explicit no-journal message in this
      case.
    * ``headline`` — single sentence the slash command prints first.
    * ``body`` — multi-line counts + diff + most-recent actions.
    * ``armed_hint`` — None when terminal (no further ticks needed);
      otherwise a one-line note reminding the slash command to
      schedule the next monitor tick (e.g. via a cron running
      ``hpc-campaign-driver`` or a re-invocation of ``/monitor-hpc``).

    Reads ``<experiment>/.hpc/runs/<run_id>.monitor.jsonl`` for the
    most recent tick. If the file is absent / empty, returns a minimal
    "no ticks yet" report rather than raising — the slash command may
    invoke this on the very first tick before any record landed.
    """
    if not run_id:
        raise errors.SpecInvalid("run_id must be a non-empty string")

    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, run_id)
    if record is None:
        # No journal — fall back to 'abandoned' (closest semantic match
        # in the canonical lifecycle_state_observable_with_timeout set)
        # and signal the absence via journal_missing=True so callers can
        # disambiguate from a real abandoned run.
        return {
            "lifecycle_state": "abandoned",
            "headline": f"no journal record found for run_id={run_id!r}",
            "body": "(submit the run first, or check ~/.claude/hpc/<repo_hash>/runs/)",
            "armed_hint": None,
            "journal_missing": True,
        }

    jsonl = experiment_dir / ".hpc" / "runs" / f"{run_id}.monitor.jsonl"
    last_tick = _read_last_tick(jsonl)

    summary = (last_tick or {}).get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    diff = (last_tick or {}).get("diff_from_prev") or {}
    actions = (last_tick or {}).get("actions") or []
    lifecycle = (last_tick or {}).get("lifecycle_state") or "in_flight"

    counts = _format_counts(summary, int(record.total_tasks))
    diff_str = _format_diff(diff) if isinstance(diff, dict) else None

    if lifecycle in _TERMINAL_LIFECYCLE_STATES:
        headline = f"run_id={run_id} reached terminal state: {lifecycle}"
    elif last_tick is None:
        headline = f"run_id={run_id} — first tick, no journal entry yet"
    else:
        headline = f"run_id={run_id} in flight — {counts}"

    body_lines: list[str] = [counts]
    if diff_str:
        body_lines.append(f"diff: {diff_str}")
    if actions:
        kinds = [str(a.get("kind") or "?") for a in actions if isinstance(a, dict)]
        if kinds:
            body_lines.append(f"actions: {', '.join(kinds)}")
    if record.combined_waves:
        body_lines.append(f"combined_waves: {sorted(record.combined_waves)}")
    if record.failed_waves:
        body_lines.append(f"failed_waves: {sorted(record.failed_waves)}")

    armed_hint = (
        None
        if lifecycle in _TERMINAL_LIFECYCLE_STATES
        else "next: schedule the next monitor tick (cron / re-invoke /monitor-hpc)"
    )

    return {
        "lifecycle_state": lifecycle,
        "headline": headline,
        "body": "\n".join(body_lines),
        "armed_hint": armed_hint,
        "journal_missing": False,
    }
