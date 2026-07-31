"""``monitor-summary`` primitive — canonical user-facing tick summary.

Replaces the slash-command prose that walked the agent through framing
the per-tick / terminal report. Reads the run journal + the most
recent tick from the **journal** runs dir
(``~/.claude/hpc/<repo_hash>/runs/<run_id>.monitor.jsonl`` — the same
path the tick writers append to, resolved via
``ops/monitor/tick_log._tick_log_path``), NOT the cluster sidecar
``<experiment_dir>/.hpc/runs/`` path, and renders one human-readable
summary string the slash command prints verbatim.

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
    # cumulative S2 SLO fields (s2-readiness pillar 6, reduced by
    # ``state/s2_slo.py``). All three are CUMULATIVE: each is a total measured
    # over the whole attended stretch of one run — elapsed seconds from the
    # journaled y to array-accepted, the human decision records that stretch
    # cost, and the readiness ledger's age at fire. None is a per-tick change,
    # so none may ever acquire the ``+`` delta marker: a "+41 seconds" reading
    # of a total elapsed time is exactly the ``told 0 · complete 39/40``
    # confusion class this registry exists to prevent. The names are the
    # ``state/s2_slo.SLO_FIELDS`` tuple — one list, two consumers.
    "y_to_array_accepted_seconds": "cumulative",
    "first_y_to_array_accepted_seconds": "cumulative",
    "interventions_count": "cumulative",
    "readiness_age_at_fire_seconds": "cumulative",
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


def format_slo(slo: Any) -> str | None:
    """Render the S2 SLO line for a run, or ``None`` when nothing was measurable.

    One line, composed from the ``state/s2_slo.S2Slo`` reducer's fields — every
    one routed through :func:`_render_scalar` so the cumulative-vs-delta contract
    (and its lint) covers the SLO exactly as it covers the counts. A field the
    reducer could not measure is OMITTED rather than printed as ``0``: an
    unmeasured latency and a zero latency are different facts, and the whole
    point of pillar 6 is that the scorecard is honest.

    PUBLIC because it is the ONE definition of this line. Pillar 6 says the S2
    scorecard "rides run telemetry and the morning brief", so ``status-snapshot``
    and the overnight brief render it too (``ops/status_blocks``,
    ``ops/overnight``) — by calling this, never by re-composing it. A second
    renderer would be a second definition of the SLO the moment either side
    gained a field, and two surfaces disagreeing about a scorecard is worse than
    one surface not carrying it. (Promoted from ``_format_slo``; the underscore
    was the only thing making a copy easier than a call.)
    """
    if not slo.measured:
        return None
    # Spelled out field by field rather than looped over ``SLO_FIELDS``: the
    # lint's render scan matches a STRING LITERAL first argument, so a loop over
    # names would silently drop these out of static coverage. Same reason
    # ``_format_kill_count`` is called with literals below.
    parts: list[str] = []
    if slo.y_to_array_accepted_seconds is not None:
        parts.append(_render_scalar("y_to_array_accepted_seconds", slo.y_to_array_accepted_seconds))
    # Only when it DIFFERS: for a run that was never re-driven the two latencies
    # are equal, and printing both would imply a distinction that is not there.
    if slo.redriven:
        parts.append(
            _render_scalar(
                "first_y_to_array_accepted_seconds", slo.first_y_to_array_accepted_seconds
            )
            + " (incl. re-drives)"
        )
    # A count of zero is a real measurement ("no human stops"), not an absence —
    # rendered whenever the line renders at all.
    parts.append(_render_scalar("interventions_count", slo.interventions_count))
    if slo.readiness_age_at_fire_seconds is not None:
        # Marked because it is NOT a stamp taken at fire time: the ledger keeps
        # only the latest atom per identity, so this is reconstructed after the
        # fact and can be older than what the fire actually consulted. A number
        # that looks measured but was inferred is worse than one that says so.
        parts.append(
            _render_scalar("readiness_age_at_fire_seconds", slo.readiness_age_at_fire_seconds)
            + " (reconstructed)"
        )
    return ", ".join(parts)


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


def _format_harvest_lag(
    experiment_dir: Path,
    run_id: str,
    summary: dict[str, Any],
    total: int,
    actions: list[Any],
) -> str | None:
    """Render the U4 pull-lag line, or ``None`` when there is no mirror to describe.

    ``N complete, M pulled locally`` — the honest answer to "why aren't you
    streaming the results back?". *M* is counted off the LOCAL per-task mirror
    (zero SSH), so it reports what is genuinely readable right now rather than
    what some earlier tick claimed to have pulled.

    Rendered ONLY when the mirror directory exists. A run reduced by a
    cluster-side combiner legitimately never pulls per-task sidecars, and
    printing "2100 complete, 0 pulled locally" there would invent a shortfall
    that isn't one. A pause (breaker cooldown, opt-out) recorded on the latest
    tick is appended, because a stalled stream that reads as merely "behind" is
    the dishonest half of this disclosure.
    """
    from hpc_agent.ops import aggregate_flow as aggregate_flow_module
    from hpc_agent.ops.monitor.stream_harvest import render_stream_lag
    from hpc_agent.state.runs import read_run_sidecar, resolved_summary_artifact

    mirror = aggregate_flow_module.per_task_results_mirror(experiment_dir, run_id)
    if not mirror.is_dir():
        return None
    try:
        sidecar: dict[str, Any] | None = read_run_sidecar(experiment_dir, run_id)
    except Exception:  # noqa: BLE001 — a disclosure line never raises
        sidecar = None
    mirrored = aggregate_flow_module._mirrored_task_count(
        mirror, resolved_summary_artifact(sidecar)
    )
    line = render_stream_lag(int(summary.get("complete") or 0), mirrored, total)
    paused = next(
        (
            str(a.get("reason") or "")
            for a in actions
            if isinstance(a, dict) and a.get("kind") == "incremental_harvest_paused"
        ),
        "",
    )
    return f"{line} — streaming PAUSED ({paused})" if paused else line


@primitive(
    name="monitor-summary",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Render the canonical user-facing tick summary for a run. "
            "Reads the journal runs dir's <run_id>.monitor.jsonl (the tick "
            "writers' path) + the run journal and returns "
            "{lifecycle_state, headline, body, armed_hint}. "
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

    Reads the journal runs dir's ``<run_id>.monitor.jsonl`` (resolved via
    ``ops/monitor/tick_log._tick_log_path`` — the SAME path the tick
    writers append to, under ``~/.claude/hpc/<repo_hash>/runs/``; NOT the
    cluster sidecar ``<experiment_dir>/.hpc/runs/`` path) for the most
    recent tick. If the file is absent / empty, returns a minimal
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

    # Read the tick log from the SAME journal-runs-dir path the writers use
    # (ops/monitor/tick_log._tick_log_path). The old manual
    # ``<experiment>/.hpc/runs/`` (cluster sidecar) path never met a real
    # writer, so a terminal run summarized as in_flight with a "schedule the
    # next tick" hint — the run-#8 arm-a-cron-on-a-finished-run class.
    from hpc_agent.ops.monitor.tick_log import _tick_log_path

    jsonl = _tick_log_path(experiment_dir, run_id)
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
    # U4 incremental harvest: how much of the finished work is already readable
    # LOCALLY. Bytes only — this line says nothing about what the results mean
    # and confers no licence to aggregate a partial set.
    harvest_line = _format_harvest_lag(
        experiment_dir, run_id, summary, int(record.total_tasks), list(actions)
    )
    if harvest_line:
        body_lines.append(f"harvest: {harvest_line}")
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
    # s2-readiness pillar 6: the S2 SLO as ONE line, reduced from records that
    # already exist (the decision journal's y + this record's accept stamp +
    # the readiness ledger's own atom stamps). Absent when nothing was
    # measurable — an unmeasured SLO prints nothing rather than zeros.
    from hpc_agent.state.s2_slo import slo_for_run

    slo_line = format_slo(slo_for_run(experiment_dir, run_id, record))
    if slo_line:
        body_lines.append(f"slo: {slo_line}")

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
