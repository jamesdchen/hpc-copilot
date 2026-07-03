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
from typing import TYPE_CHECKING, Any, Literal, get_args

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire._shared import LifecycleStateTerminal
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path

# Derived from the LifecycleStateTerminal Literal (the SoT in _wire/_shared.py)
# so the terminal-state set stays in lock-step instead of being re-hardcoded.
_TERMINAL_LIFECYCLE_STATES: frozenset[str] = frozenset(get_args(LifecycleStateTerminal))

FieldKind = Literal["cumulative", "delta", "label"]

# Single source of truth for telemetry-field legibility (design §5). Every
# field emitted in the tick record (``ops/monitor/tick_log.py::_append_tick``)
# and every count field the renderers below consume is declared here with its
# kind:
#
#   * ``cumulative`` — a running total, a snapshot of the whole run so far
#     (``complete=39`` of ``total=40``).
#   * ``delta``      — a per-tick change since the previous tick (``+0`` newly
#     complete → the "told 0" reading of the same underlying quantity).
#   * ``label``      — identifier / lifecycle state / scheduling metadata,
#     neither a running total nor a per-tick change.
#
# Rendering routes through :func:`_render_scalar`, which derives the marker
# from the declared kind — so a cumulative count can never masquerade as a
# delta and a delta always carries its ``+`` marker. The lint
# ``scripts/lint_telemetry_labels.py`` fails CI if any emitted field is absent
# here: that is the mechanized form of the ``told 0 · complete 39/40``
# confusion contract (a cumulative read as a delta, or vice-versa).
FIELD_KIND: dict[str, FieldKind] = {
    # cumulative running totals — the ``summary`` block + the derived total
    "complete": "cumulative",
    "running": "cumulative",
    "pending": "cumulative",
    "failed": "cumulative",
    "total": "cumulative",
    # cumulative kill counts — the §5 first-class kill telemetry, rendered from
    # the run record's kill ledger (``kill_requested_job_ids`` /
    # ``kill_confirmed_job_ids``). Both are running totals ("N requested, M
    # confirmed gone"), never per-tick deltas.
    "kill_requested": "cumulative",
    "kill_confirmed": "cumulative",
    # per-tick deltas — the ``diff_from_prev`` block
    "newly_complete": "delta",
    "newly_failed": "delta",
    "newly_combined_waves": "delta",
    # labels / metadata — top-level tick-record fields that are neither a
    # running total nor a per-tick change (``summary`` / ``diff_from_prev`` are
    # the containers that hold the cumulative / delta blocks respectively).
    "tick_id": "label",
    "run_id": "label",
    "summary": "label",
    "diff_from_prev": "label",
    "preflight": "label",
    "actions": "label",
    "lifecycle_state": "label",
    "next_tick_seconds": "label",
    "console_emitted": "label",
}

_DELTA_MARKER = "+"


def _render_scalar(name: str, value: object) -> str:
    """Render one telemetry scalar with the marker its declared kind requires.

    The *kind* (from :data:`FIELD_KIND`), not the call site, fixes the marker:
    a ``cumulative`` field renders ``name=value`` and can never acquire the
    ``+`` delta marker; a ``delta`` field renders ``+value label`` and can
    never lose it. This is the runtime half of the cumulative-vs-delta
    contract that ``scripts/lint_telemetry_labels.py`` enforces statically —
    the ``told 0 · complete 39/40`` confusion class.

    A field absent from the registry (or one declared ``label``) is not a
    renderable scalar and raises, mirroring the lint's fire condition at
    runtime for a field that slipped past CI.
    """
    kind = FIELD_KIND.get(name)
    if kind == "cumulative":
        return f"{name}={value}"
    if kind == "delta":
        # The delta label is the cumulative field it tracks (``newly_complete``
        # → ``complete``), so ``+N complete`` pairs visually with ``complete=M``.
        label = name.removeprefix("newly_")
        return f"{_DELTA_MARKER}{value} {label}"
    raise errors.SpecInvalid(
        f"telemetry field {name!r} has kind {kind!r}; only cumulative/delta "
        f"fields render as scalars — declare it in FIELD_KIND"
    )


#: Human phrasing per kill-count field (§5 kill semantics: "N requested, N
#: confirmed gone"). Keyed by the FIELD_KIND field name so the lint's render-fn
#: scan of :func:`_format_kill_count` reaches a declared telemetry field.
_KILL_PHRASE: dict[str, str] = {
    "kill_requested": "requested",
    "kill_confirmed": "confirmed gone",
}


def _format_kill_count(field: str, value: int) -> str:
    """Render one cumulative kill-count field with its human phrasing.

    Like :func:`_render_scalar`, the *kind* comes from :data:`FIELD_KIND` — a
    kill count is a cumulative running total and can never be read as a per-tick
    delta. Routing through this named helper (not an inline f-string) is what lets
    ``scripts/lint_telemetry_labels.py`` see the field and require its
    declaration; an undeclared kill field raises, mirroring the lint at runtime.
    """
    kind = FIELD_KIND.get(field)
    if kind != "cumulative":
        raise errors.SpecInvalid(
            f"kill telemetry field {field!r} has kind {kind!r}; kill counts are "
            "cumulative running totals — declare it cumulative in FIELD_KIND"
        )
    return f"{value} {_KILL_PHRASE[field]}"


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
    """Render ``complete=4 running=2 pending=10 failed=0 / total=16``.

    Every field is a *cumulative* running total; routing through
    :func:`_render_scalar` keeps the delta marker off them (FIELD_KIND
    declares each ``"cumulative"``).
    """
    c = int(summary.get("complete") or 0)
    r = int(summary.get("running") or 0)
    p = int(summary.get("pending") or 0)
    f = int(summary.get("failed") or 0)
    return (
        f"{_render_scalar('complete', c)} {_render_scalar('running', r)} "
        f"{_render_scalar('pending', p)} {_render_scalar('failed', f)} "
        f"/ {_render_scalar('total', total)}"
    )


def _format_diff(diff: dict[str, Any]) -> str | None:
    """Render the ``newly_*`` fields of a tick's ``diff_from_prev`` block.

    Each per-tick delta routes through :func:`_render_scalar`, whose marker is
    fixed by FIELD_KIND — a delta always carries the ``+`` marker and can never
    be misread as a cumulative count (the ``told 0 · complete 39/40`` class).
    """
    parts: list[str] = []
    nc = diff.get("newly_complete") or []
    nf = diff.get("newly_failed") or []
    nw = diff.get("newly_combined_waves") or []
    # monitor_flow stores newly_complete / newly_failed as a length-1
    # list whose single element is the delta count (see
    # monitor_flow._tick: ``diff[f"newly_{key}"] = [cur - prv]``).
    # Use the value, not the list length.
    if nc:
        parts.append(_render_scalar("newly_complete", int(nc[0])))
    if nf:
        parts.append(_render_scalar("newly_failed", int(nf[0])))
    if nw:
        # newly_combined_waves is a *set* delta (wave IDs), not a count —
        # rendered as an explicit phrase, still declared ``delta`` in
        # FIELD_KIND so it can never be re-read as a cumulative snapshot.
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
    # ``lifecycle_state`` is read verbatim from the on-disk tick jsonl, whose
    # writer types it as a bare str. The output schema constrains it to the
    # observable-with-timeout enum, so coerce any out-of-enum value (legacy /
    # hand-edited / foreign / future-schema tick) to ``in_flight`` rather than
    # emit a value that fails output validation.
    lifecycle = (last_tick or {}).get("lifecycle_state") or "in_flight"
    if lifecycle not in (_TERMINAL_LIFECYCLE_STATES | {"in_flight"}):
        lifecycle = "in_flight"

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
    # §5 first-class kill telemetry: once a kill has been requested on this run,
    # surface the honest "N requested, M confirmed gone" from the journal's kill
    # ledger (M ≤ N — kill.py only counts scheduler-confirmed-gone job ids).
    if getattr(record, "kill_requested_at", None):
        n_req = len(record.kill_requested_job_ids)
        n_conf = len(record.kill_confirmed_job_ids)
        body_lines.append(
            f"kill: {_format_kill_count('kill_requested', n_req)}, "
            f"{_format_kill_count('kill_confirmed', n_conf)}"
        )

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
