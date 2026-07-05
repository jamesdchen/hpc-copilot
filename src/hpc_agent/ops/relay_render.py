"""``render_relay`` — CODE renders the human-facing relay one-liner from a
block's OWN structured evidence (submit S1–S4 briefs; status snapshot/watch
digests). The agent relays the returned string VERBATIM.

Wave 5, finding 15 (``docs/design/proving-run-5-hardening.md`` §5.3): the
driving agent used to RECONSTRUCT the human-facing relay from memory — it
relayed "canary green / verified / 20" (a *stale* state plus the MAIN array's
task count bled into the 1-task canary summary) against a journal reading
``complete`` / 16 / 1 task. The relay-audit ``Stop`` hook (conduct rule 10,
:mod:`hpc_agent.ops.decision.verify_relay`) caught it — but at ~6 correction
round-trips.

The fix removes the reconstruction: code renders the relay from the block's own
structured fields and the agent relays it verbatim. Because the string *is* the
journal's rendering it cannot contradict the journal, so the relay-audit hook
becomes a near-silent backstop and the correction round-trips vanish.

Two invariants are load-bearing:

* **Canary anti-bleed.** A canary is a ONE-task probe by construction, so the S2
  canary summary renders ``"canary 1 task"`` LITERALLY and NEVER interpolates
  the main array's ``total_tasks`` (which rides ``brief["cost_estimate"]``) — the
  exact finding-15 bleed.
* **Freshness.** ``status_blocks`` renders this FRESH from the journal digest on
  every snapshot, so the agent's correct move is "relay what the snapshot returns
  NOW", not a brief it cached across a journal transition.

Both surfaces route through this ONE renderer so their wording agrees. Pure
string work — no SSH, no journal reads, no ``_wire`` import: the caller hands in
the already-digested ``brief`` dict and receives the line.
"""

from __future__ import annotations

from typing import Any

__all__ = ["render_relay"]

# The per-task count keys a record's digested ``summary`` carries (mirrors
# ``status_blocks._COUNT_KEYS`` / the reporter's TaskStatus values). Rendered in
# this order so the human-facing line is stable.
_COUNT_KEYS: tuple[str, ...] = ("complete", "running", "pending", "failed", "unknown")


def render_relay(block: str, stage_reached: str, brief: dict[str, Any] | None) -> str:
    """Render the human-facing relay line for *(block, stage_reached)* from *brief*.

    ``block`` is the block terminator literal — ``s1``..``s4`` for the submit
    blocks, ``snapshot`` / ``watch`` for the status blocks. ``brief`` is the
    block's code-digested evidence (its OWN structured fields). Returns the
    one-line relay the agent forwards verbatim, or ``""`` for an unknown
    ``(block, stage)`` (a caller that can't render a line surfaces the empty
    string rather than a fabricated one).
    """
    b = brief or {}
    if block in ("s1", "s2", "s3", "s4"):
        return _render_submit(block, stage_reached, b)
    if block == "snapshot":
        return _render_snapshot(b)
    if block == "watch":
        return _render_watch(stage_reached, b)
    return ""


# ── shared vocabulary ────────────────────────────────────────────────────────


def _line(
    state: str, cluster: str | None, run_id: str | None, extra: str = "", tail: str = ""
) -> str:
    """Compose ``"<state>[ on <cluster>][ (run <id>[, <extra>])]<tail>"``.

    The one wording both submit and status relays share, so the two surfaces
    read the same. ``extra`` rides inside the parenthesis beside the run id (the
    canary's ``"canary 1 task"`` note); ``tail`` is the trailing clause.
    """
    where = f" on {cluster}" if cluster else ""
    inner: list[str] = []
    if run_id:
        inner.append(f"run {run_id}")
    if extra:
        inner.append(extra)
    detail = f" ({', '.join(inner)})" if inner else ""
    return f"{state}{where}{detail}{tail}"


def _summary_from(last_status: Any) -> dict[str, int]:
    """Project a record's ``last_status`` into stable per-task counts.

    Accepts either the reporter's flat counts dict or a ``{"summary": {...}}``
    nesting (the monitor-flow envelope carries either shape), dropping any
    non-numeric bookkeeping fields — the same tolerance as
    ``status_blocks._summary_of``.
    """
    if not isinstance(last_status, dict):
        return {}
    inner = last_status.get("summary")
    src = inner if isinstance(inner, dict) else last_status
    out: dict[str, int] = {}
    for key in _COUNT_KEYS:
        val = src.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = int(val)
    return out


def _counts_phrase(summary: dict[str, int]) -> str:
    """Render the nonzero task counts as ``"16 complete, 3 failed"`` (or ``""``)."""
    parts = [f"{summary[k]} {k}" for k in _COUNT_KEYS if summary.get(k)]
    return ", ".join(parts)


def _brief_run_id(brief: dict[str, Any]) -> str | None:
    """The run id a block's brief carries, checked across the known shapes."""
    for key in ("run_id", "main_run_id", "canary_run_id"):
        val = brief.get(key)
        if isinstance(val, str) and val:
            return val
    resolve = brief.get("resolve")
    if isinstance(resolve, dict):
        val = resolve.get("run_id")
        if isinstance(val, str) and val:
            return val
    return None


def _brief_cluster(brief: dict[str, Any]) -> str | None:
    """The cluster a block's brief carries, checked across the known shapes."""
    val = brief.get("cluster")
    if isinstance(val, str) and val:
        return val
    resolved = brief.get("resolved")
    if isinstance(resolved, dict):
        cluster = resolved.get("cluster")
        if isinstance(cluster, str) and cluster:
            return cluster
    resolve = brief.get("resolve")
    if isinstance(resolve, dict):
        submit_spec = resolve.get("submit_spec")
        if isinstance(submit_spec, dict):
            cluster = submit_spec.get("cluster")
            if isinstance(cluster, str) and cluster:
                return cluster
    return None


# ── submit S1–S4 ─────────────────────────────────────────────────────────────


def _render_submit(block: str, stage: str, brief: dict[str, Any]) -> str:
    run_id = _brief_run_id(brief)
    cluster = _brief_cluster(brief)

    if stage == "detached":
        return _line(
            f"submit-{block} detached",
            cluster,
            run_id,
            tail="; the brief arrives on completion — read the journal.",
        )

    # S1 — resolve.
    if stage == "needs_resolution":
        n = len(brief.get("ambiguities") or [])
        return (
            f"{n} submit input(s) need a decision — review the recommendations and "
            "greenlight or nudge."
        )
    if stage == "resolved":
        return _line(
            "submit inputs resolved", cluster, run_id, tail=" — greenlight to stage & canary."
        )
    if stage == "prior_run_found":
        return _line("prior run found", cluster, run_id, tail=" — confirm resume vs fresh.")
    if stage == "needs_scaffold_interview":
        return _line(
            "resolve needs the scaffold interview",
            cluster,
            run_id,
            tail=" — supply the missing pieces.",
        )

    # S2 — stage & canary.
    if stage == "canary_verified":
        est = brief.get("est_core_hours")
        est_phrase = (
            f"{est:g} core-hours" if isinstance(est, (int, float)) and est else "unknown core-hours"
        )
        # The canary is a ONE-task probe by construction (finding-15 anti-bleed):
        # render "canary 1 task" LITERALLY. NEVER interpolate the main array's
        # total_tasks (brief["cost_estimate"]["total_tasks"]) — that count bleeding
        # into the 1-task canary summary was the exact finding-15 corruption.
        return _line(
            "canary green",
            cluster,
            run_id,
            extra="canary 1 task",
            tail=f"; est. {est_phrase} — greenlight to submit & watch.",
        )
    if stage == "canary_failed":
        if brief.get("canary_run_id") is None:
            return _line(
                "canary never entered the queue",
                cluster,
                run_id,
                tail=" — propose a fix before main.",
            )
        return _line(
            f"canary failed verification ({brief.get('failure_kind')})",
            cluster,
            run_id,
            tail=" — propose a fix before main.",
        )
    if stage == "deduped":
        return _line(
            "run already exists",
            cluster,
            run_id,
            tail=" — no fresh canary; confirm resume vs fresh.",
        )

    # S3 — submit & watch.
    if stage == "watching_terminal":
        summary = _summary_from(brief.get("last_status"))
        total = brief.get("total_tasks")
        complete = summary.get("complete", 0)
        tasks = f"{complete}/{total} tasks" if isinstance(total, int) else f"{complete} complete"
        return _line(
            "main array complete", cluster, run_id, tail=f": {tasks} — proceed to harvest."
        )
    if stage == "watching_timeout":
        return _line(
            "monitor budget hit",
            cluster,
            run_id,
            tail="; cluster jobs may run on — keep watching or stop?",
        )
    if stage == "watching_anomaly":
        lifecycle = brief.get("lifecycle_state") or "anomaly"
        esc = brief.get("escalation_reason") or "no escalation reason"
        return _line(
            f"main array {lifecycle}", cluster, run_id, tail=f" ({esc}) — propose recovery."
        )

    # S4 — harvest.
    if stage == "harvested":
        n = len(brief.get("results_table") or [])
        return _line(
            "harvest complete",
            cluster,
            run_id,
            tail=f": {n} result row(s) — review the table and choose an interpretation.",
        )
    if stage == "harvest_partial":
        n = len(brief.get("results_table") or [])
        return _line(
            "partial harvest",
            cluster,
            run_id,
            tail=f": {n} row(s), some waves escalated — review the table.",
        )

    return ""


# ── status snapshot / watch ──────────────────────────────────────────────────


def _row_line(row: dict[str, Any]) -> str:
    """Render one ``running_where`` digest row as a current-state clause.

    Reads each row's OWN ``status`` and ``summary`` — so a canary row shows the
    canary's 1-task counts and the parent shows the main array's, and neither
    count can bleed into the other (finding-15, the status-side guarantee). The
    ``status`` comes straight off the record, so a post-transition snapshot
    renders the NEW state, never a stale cached one.
    """
    run_id = row.get("run_id") or "?"
    cluster = row.get("cluster")
    kind = "canary" if row.get("is_canary") else "run"
    where = f" on {cluster}" if cluster else ""
    if row.get("is_superseded"):
        return f"{kind} {run_id} superseded by {row.get('superseded_by')}"
    status = row.get("status") or "unknown"
    counts = _counts_phrase(row.get("summary") or {})
    tail = f": {counts}" if counts else ""
    return f"{kind} {run_id} {status}{where}{tail}"


def _render_snapshot(brief: dict[str, Any]) -> str:
    """Render the snapshot relay FRESH from the journal digest.

    On a clean snapshot: one current-state clause per digested run. On an
    anomaly: the failed/abandoned runs with their proposed next action plus any
    stalled drivers — the exact evidence ``status-snapshot`` returns NOW.
    """
    anomalies = brief.get("anomalies") or []
    stalled = brief.get("stalled_runs") or []
    if anomalies or stalled:
        segs: list[str] = []
        for a in anomalies:
            if not isinstance(a, dict):
                continue
            action = (a.get("recommendation") or {}).get("action")
            segs.append(_row_line(a) + (f" — {action}" if action else ""))
        for s in stalled:
            if not isinstance(s, dict):
                continue
            due = s.get("next_tick_due")
            segs.append(
                f"run {s.get('run_id')} driver stalled" + (f" (next tick due {due})" if due else "")
            )
        return "; ".join(seg for seg in segs if seg) or "an anomaly needs a decision"

    rows = brief.get("running_where") or []
    lines = [_row_line(r) for r in rows if isinstance(r, dict)]
    return "; ".join(line for line in lines if line) or "no in-flight runs"


def _render_watch(stage: str, brief: dict[str, Any]) -> str:
    run_id = brief.get("run_id")
    prefix = f"run {run_id}" if run_id else "run"
    summary = brief.get("summary")
    counts = _counts_phrase(summary if isinstance(summary, dict) else {})
    if stage == "watch_terminal":
        tail = f": {counts}" if counts else ""
        return f"{prefix} complete{tail} — harvest guaranteed; hand off to harvest."
    if stage == "watch_timeout":
        return f"{prefix} monitor budget hit; cluster jobs may run on — keep watching or stop?"
    if stage == "watch_anomaly":
        lifecycle = brief.get("lifecycle_state") or "anomaly"
        esc = brief.get("escalation_reason") or "no escalation reason"
        return f"{prefix} {lifecycle} ({esc}) — review the evidence brief."
    return ""
