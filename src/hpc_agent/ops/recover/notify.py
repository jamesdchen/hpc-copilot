"""OS notification for the scheduled ``doctor`` watchdog (§5).

When the OS-scheduled ``doctor`` scan finds a stalled/orphaned run it must
*surface* the drafted re-arm proposal, not print JSON into a scheduler log
nobody reads (design §5: "Either side dying is loud"). This module raises a
dependency-free OS notification carrying the proposal summary.

Doctrine: **notify only, never act.** The notification is a surfacing of the
already-drafted proposal — a successor session (or the human) answers `y`/nudge.
The watchdog never restarts anything.

Mechanism (chosen for reliability without third-party deps):

* **Windows** — ``msg.exe <user> <text>``: non-blocking, ships on Windows
  Pro/Enterprise, no module install. A modal ``MessageBox`` would wedge a
  headless scheduled task, and a real toast needs an AppId-registered module —
  both worse than ``msg`` for an unattended firing. On Home editions ``msg`` is
  absent, so we fall back to the loud log file (below).
* **POSIX** — ``notify-send`` when present (the ``libnotify`` CLI, on virtually
  every desktop), else the loud log file.
* **Fallback (any platform)** — append to ``<journal_home>/doctor.alerts.log``.
  This is the guaranteed floor: it always surfaces the alert *somewhere* durable
  even with no desktop session, no ``msg``, no ``notify-send``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.state.run_record import journal_dir

_log = logging.getLogger(__name__)

_NOTIFY_TIMEOUT_SEC = 10

# Alert-record ``kind`` tags — the second component of the dedup identity.
_KIND_STALL = "stall"
_KIND_ALERT = "alert"

_ALERTS_LOG_NAME = "doctor.alerts.log"
# Acknowledgment watermark for the alert log (proving run #3: the watchdog
# detected the stall and wrote the alert, but nothing DELIVERED it — detection
# without delivery is silence). The log itself is an append-only audit trail
# and is never truncated; "acknowledged" means a status surface has shown the
# alert to the human once, recorded as the ISO timestamp of the newest alert
# surfaced. An alert is unacknowledged while its leading timestamp is newer
# than the watermark.
_ALERTS_WATERMARK_NAME = "doctor.alerts.seen"


def _alerts_paths(experiment_dir: Path) -> tuple[Path, Path]:
    """``(log_path, watermark_path)`` for *experiment_dir* — WITHOUT creating.

    Deliberately not :func:`journal_dir` (which mkdirs the namespace + writes
    ``repo.json``): the readers below run from status snapshots and from a
    SessionStart hook that fires in ANY repo, and a read must never scaffold a
    journal namespace for an arbitrary cwd (proving-run #3 finding g — leaked
    ``<repo_hash>/`` dirs). The writer (:func:`_append_alert_log`) keeps using
    the creating ``journal_dir``.
    """
    from hpc_agent.state.run_record import current_homedir, repo_hash

    base = current_homedir() / repo_hash(experiment_dir)
    return base / _ALERTS_LOG_NAME, base / _ALERTS_WATERMARK_NAME


def _parse_alert_line(line: str) -> tuple[str, str] | None:
    """Parse one alert-log line into ``(ts, message)``, tolerant of BOTH formats.

    * **New format** — a JSON object carrying at least ``ts`` + ``message`` (the
      canonical dedup write path, :func:`_append_alert_log`). Extra identity
      fields (``alert_id`` / ``run_id`` / ``kind`` / ``since``) are ignored here:
      the returned shape stays exactly ``(ts, message)`` so every consumer that
      spreads it (``doctor``'s ``AlertRecord(**a)``, ``extra="forbid"``) is
      unaffected.
    * **Legacy format** — a bare ``<iso-ts> <message>`` line (the pre-dedup
      writer). Kept readable forever so an existing log survives the format flip.

    Returns ``None`` for a blank / torn / structurally-unparseable line so a
    broken alert channel never breaks a status read (the fail-open posture).
    """
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return None  # torn / corrupt JSON line — skip, never raise
        if not isinstance(obj, dict):
            return None
        ts = obj.get("ts")
        message = obj.get("message")
        if isinstance(ts, str) and isinstance(message, str) and message.strip():
            return ts, message.strip()
        return None
    # Legacy plaintext: "<ts> <message>".
    ts_str, sep, message = stripped.partition(" ")
    if not sep or not message.strip():
        return None
    return ts_str, message.strip()


def read_unacknowledged_alerts(experiment_dir: Path) -> list[dict[str, str]]:
    """Alerts in ``doctor.alerts.log`` newer than the acknowledgment watermark.

    Returns ``[{"ts": <iso>, "message": <text>}, ...]`` in log (chronological)
    order — exactly the two keys every consumer expects (``doctor``'s
    ``AlertRecord(**a)`` forbids extras, the alert-count hook + snapshot brief
    read ``ts``/``message``, the attention queue peeks both). Each line is parsed
    dual-format via :func:`_parse_alert_line` (JSON records first, legacy
    plaintext as a tolerant fallback). Fail-open everywhere: a missing log, an
    unreadable log or watermark, or a line that does not parse / lacks a parseable
    ISO timestamp yields no alerts / skips the line — a broken alert channel must
    never break a status read. Never mutates anything (the log is an audit trail;
    acknowledgment is the separate :func:`acknowledge_alerts` watermark write).
    """
    try:
        log_path, watermark_path = _alerts_paths(experiment_dir)
        if not log_path.is_file():
            return []
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return []

    watermark = None
    try:
        if watermark_path.is_file():
            watermark = parse_iso_utc_or_none(
                watermark_path.read_text(encoding="utf-8", errors="replace").strip()
            )
    except OSError:
        watermark = None  # corrupt/unreadable watermark → treat all as new

    alerts: list[dict[str, str]] = []
    for line in lines:
        parsed = _parse_alert_line(line)
        if parsed is None:
            continue
        ts_str, message = parsed
        ts = parse_iso_utc_or_none(ts_str)
        if ts is None:
            continue  # corrupt line — skip, never raise
        if watermark is not None and ts <= watermark:
            continue  # already surfaced once
        alerts.append({"ts": ts_str, "message": message})
    return alerts


def newest_alert_ts(experiment_dir: Path) -> str | None:
    """The newest alert timestamp recorded in the log, or ``None`` if none/unreadable.

    Scans the RAW log (NOT watermark-filtered, dual-format) so the ``alerts-ack``
    verb can advance the watermark past EVERY recorded alert — an already-acked
    alert must never lower the ack target. Fail-open: a missing / unreadable /
    empty log yields ``None``.
    """
    try:
        log_path, _ = _alerts_paths(experiment_dir)
        if not log_path.is_file():
            return None
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return None
    newest_ts: str | None = None
    newest_dt = None
    for line in lines:
        parsed = _parse_alert_line(line)
        if parsed is None:
            continue
        ts_str, _message = parsed
        dt = parse_iso_utc_or_none(ts_str)
        if dt is None:
            continue
        if newest_dt is None or dt > newest_dt:
            newest_dt = dt
            newest_ts = ts_str
    return newest_ts


def acknowledge_alerts(experiment_dir: Path, *, up_to_ts: str) -> None:
    """Advance the acknowledgment watermark to *up_to_ts* (ISO-8601 UTC).

    Monotonic: a watermark already at or past *up_to_ts* is left alone, so a
    stale snapshot can never resurrect acknowledged alerts. The log itself is
    never touched (audit trail). Fail-open: an unparsable *up_to_ts* or an
    unwritable watermark is a silent no-op — the alert simply stays "new".
    """
    new_dt = parse_iso_utc_or_none(up_to_ts)
    if new_dt is None:
        return
    try:
        _, watermark_path = _alerts_paths(experiment_dir)
        if watermark_path.is_file():
            current = parse_iso_utc_or_none(
                watermark_path.read_text(encoding="utf-8", errors="replace").strip()
            )
            if current is not None and current >= new_dt:
                return
        watermark_path.write_text(up_to_ts + "\n", encoding="utf-8")
    except (OSError, ValueError):
        return


def summarize_proposals(proposals: list[dict[str, Any]]) -> str:
    """One-line, human-facing summary of the drafted re-arm proposals.

    Leads with the first stalled run's drafted proposal (already authored by
    ``doctor``); a ``(+N more)`` suffix counts the rest so a single toast never
    truncates the tail into silence.
    """
    if not proposals:
        return "hpc-agent doctor: no stalled runs."
    head = proposals[0]
    run_id = head.get("run_id", "?")
    since = head.get("last_tick_at") or "an unknown time"
    text = f"hpc-agent doctor: driver stalled since {since}, run {run_id} — re-arm?"
    extra = len(proposals) - 1
    if extra > 0:
        text += f" (+{extra} more stalled)"
    return text


def _alert_identity(
    *,
    run_id: str | None,
    kind: str,
    since: str | None,
    message: str,
    next_tick_due: str | None = None,
    status: str | None = None,
    awaiting_since: str | None = None,
) -> str:
    """Stable dedup identity for one live alert — ``<run_id>|<kind>|<subject>``.

    The subject is the stall's stable ``since`` (``last_tick_at``) when present —
    so every 15-min watchdog re-tick for the SAME stall (identity fixed, only the
    leading ``ts`` varies) replays to a no-op.

    When ``since`` is None the rendered message varies ONLY by ``run_id`` (the
    ``summarize_proposals`` template fills the unknown time with a constant), so
    hashing it would collapse two DISTINCT stalls of the same run onto one
    identity — the second alert would dedup away and never land. The subject is
    then a short hash of the stall's own durable distinguishing fields
    (``next_tick_due`` — the missed deadline, fixed while the driver stays dead
    and re-stamped on re-arm — plus ``status`` and the parked marker's
    ``awaiting_since`` when a caller carries one): identical across re-ticks of
    the SAME stall, distinct across DIFFERENT stalls of the same run. The
    message hash remains the final fallback when no distinguishing field is
    available (the free-form ``kind=alert`` path), preserving its dedup-by-text.
    """
    if since:
        subject = since
    else:
        token = "|".join(part for part in (next_tick_due, status, awaiting_since) if part)
        material = token if token else message
        subject = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{run_id or ''}|{kind}|{subject}"


def _append_alert_log(
    message: str,
    *,
    experiment_dir: Path,
    kind: str,
    run_id: str | None = None,
    since: str | None = None,
    next_tick_due: str | None = None,
    status: str | None = None,
    awaiting_since: str | None = None,
) -> str:
    """Append one JSON alert record to the loud fallback log; return its path.

    Routes through the canonical JSONL-append seam
    (:func:`hpc_agent.infra.io.append_jsonl_line`) — the ONE flock + fsync +
    sort_keys, whole-line-atomic, append-only discipline every ledger shares — so
    a torn / interleaved final line can never strand a prior alert. The record is
    ``{alert_id, ts, run_id, kind, since, message}``; ``dedup_key=("alert_id",
    <identity>)`` makes each repeat tick for a live stall a REPLAY NO-OP (one
    durable line per identity, never 55 near-identical lines for one stall).

    ``fsync_required=False`` keeps the notifier's never-raise delivery floor (an
    fsync OSError on a full disk is suppressed, io.py); the whole append is
    additionally wrapped best-effort so no I/O failure can raise out of the
    delivery path (the ``harvest_guard._write_marker`` precedent — the seam CAN
    raise OSError, and a watchdog notification must never crash on a bad log).
    """
    log_path = journal_dir(experiment_dir) / _ALERTS_LOG_NAME
    alert_id = _alert_identity(
        run_id=run_id,
        kind=kind,
        since=since,
        message=message,
        next_tick_due=next_tick_due,
        status=status,
        awaiting_since=awaiting_since,
    )
    record: dict[str, Any] = {
        "alert_id": alert_id,
        "ts": utcnow_iso(),
        "run_id": run_id or "",
        "kind": kind,
        "since": since or "",
        "message": message,
    }
    try:
        append_jsonl_line(
            log_path,
            record,
            fsync_required=False,
            dedup_key=("alert_id", alert_id),
        )
    except Exception as exc:  # noqa: BLE001 — notifier's never-raise delivery floor
        with contextlib.suppress(Exception):
            _log.warning("notify: could not append alert record to %s: %s", log_path, exc)
    return str(log_path)


def _log_stall_proposals(proposals: list[dict[str, Any]], *, experiment_dir: Path) -> str:
    """Append ONE deduped JSON record per stalled-run proposal; return the log path.

    Each proposal carries its own identity (``run_id`` + ``last_tick_at``), so a
    per-proposal record deduplicates on ``<run_id>|stall|<last_tick_at>`` — the
    per-stall granularity the summary line lacked. When ``last_tick_at`` is
    absent the identity anchors on the proposal's durable stall fields
    (``next_tick_due`` / ``status`` / ``awaiting_since``) instead of the rendered
    message — which varies only by ``run_id`` and would collapse two distinct
    stalls of one run onto a single dedup identity. The degenerate no-proposals
    case still writes one summary line so an empty notification is never silent.
    """
    if not proposals:
        return _append_alert_log(
            summarize_proposals(proposals), experiment_dir=experiment_dir, kind=_KIND_STALL
        )
    log_path = str(journal_dir(experiment_dir) / _ALERTS_LOG_NAME)
    for proposal in proposals:
        run_id = str(proposal.get("run_id") or "") or None
        since_val = proposal.get("last_tick_at")
        since = str(since_val) if since_val else None
        due_val = proposal.get("next_tick_due")
        next_tick_due = str(due_val) if due_val else None
        status_val = proposal.get("status")
        status = str(status_val) if status_val else None
        awaiting_val = proposal.get("awaiting_since")
        awaiting_since = str(awaiting_val) if awaiting_val else None
        log_path = _append_alert_log(
            summarize_proposals([proposal]),
            experiment_dir=experiment_dir,
            kind=_KIND_STALL,
            run_id=run_id,
            since=since,
            next_tick_due=next_tick_due,
            status=status,
            awaiting_since=awaiting_since,
        )
    return log_path


def _try_run(argv: list[str]) -> bool:
    """Run *argv* best-effort; True iff it exists and exits 0."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_NOTIFY_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def raise_alert_notification(text: str, *, experiment_dir: Path) -> dict[str, Any]:
    """Surface a free-form *text* alert the same way as a stall proposal.

    The delivery floor for any overnight/watchdog event that is not a stalled-run
    proposal — most sharply the overnight self-heal FAIL-LOUD (a campaign reconcile
    chain that could not be revived within its heal cap). Always writes the loud
    fallback log (the durable floor the human reads on waking), then fires the
    platform notifier when one is available. Returns the same
    ``{mechanism, delivered, text, log_path}`` delivery record as
    :func:`raise_stall_notification`. Best-effort, never acts on the cluster.
    """
    log_path = _append_alert_log(text, experiment_dir=experiment_dir, kind=_KIND_ALERT)
    mechanism = "logfile"
    if os.name == "nt":
        user = os.environ.get("USERNAME") or "*"
        if _try_run(["msg", user, text]):
            mechanism = "msg"
    elif shutil.which("notify-send") and _try_run(["notify-send", "hpc-agent doctor", text]):
        mechanism = "notify-send"
    return {"mechanism": mechanism, "delivered": True, "text": text, "log_path": log_path}


def raise_stall_notification(
    proposals: list[dict[str, Any]], *, experiment_dir: Path
) -> dict[str, Any]:
    """Surface *proposals* as an OS notification. Best-effort, never acts.

    Always writes the loud fallback log (the durable floor), then additionally
    fires the platform notifier when one is available. Returns the delivery
    record ``{mechanism, delivered, text, log_path}`` — ``mechanism`` is the
    richest channel that fired (``msg`` / ``notify-send`` / ``logfile``).
    """
    text = summarize_proposals(proposals)
    log_path = _log_stall_proposals(proposals, experiment_dir=experiment_dir)

    mechanism = "logfile"
    if os.name == "nt":
        user = os.environ.get("USERNAME") or "*"
        if _try_run(["msg", user, text]):
            mechanism = "msg"
    elif shutil.which("notify-send") and _try_run(["notify-send", "hpc-agent doctor", text]):
        mechanism = "notify-send"

    return {
        "mechanism": mechanism,
        "delivered": True,
        "text": text,
        "log_path": log_path,
    }
