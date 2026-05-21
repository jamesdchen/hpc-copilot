"""Parsers for SLURM reservation + QOS-limit output.

Lesson 2 from the backfill session: free GPU count is not the same as
backfill-eligible. Nodes can sit idle but be reserved for higher-
priority work, and per-partition QOS gates can refuse a job even when
the partition has free slots. The framework's existing
:class:`ClusterSnapshot` exposes only resource-state info, so the
planner over-promises.

This module adds two pure parsers:

* :func:`parse_slurm_reservations` — ``scontrol show reservation`` →
  list of :class:`ReservationHold`.
* :func:`parse_sacctmgr_qos` — ``sacctmgr -P show qos`` → dict of
  ``qos_name`` → :class:`QosLimit`.

Pure stdlib; no SSH side effects. Callers (inspect/slurm.py) probe
the cluster, hand the raw text to these parsers, then attach the
parsed structures to :class:`ClusterSnapshot`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class ReservationHold:
    """One ``scontrol show reservation`` entry.

    Fields are best-effort: SLURM versions emit slightly different
    column sets, so anything missing collapses to ``None`` / empty.
    The planner subtracts ``nodes`` from the free-node pool when the
    reservation's window covers the proposed StartTime.
    """

    name: str
    nodes: tuple[str, ...] = ()
    start_iso: str | None = None
    end_iso: str | None = None
    users: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class QosLimit:
    """One ``sacctmgr show qos`` entry.

    The fields we care about for self-DOS detection (lesson 6) and
    backfill prediction (lesson 2): max jobs per user, max cpus per
    user, priority tier. Anything missing is None — every gate is
    independently advisory.
    """

    name: str
    max_jobs_per_user: int | None = None
    max_cpus_per_user: int | None = None
    max_submit_jobs_per_user: int | None = None
    priority: int | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)


# ─── reservation parser ────────────────────────────────────────────────


# scontrol output is a sequence of "key=value" tokens, possibly across
# multiple lines per reservation. Records are separated by blank lines
# OR by the ``ReservationName=`` token starting a new entry. Tokens
# can carry whitespace inside (``Users=alice,bob`` is fine; ``Nodes=cn[001-008]``
# stays as one token).

_KV_TOKEN = re.compile(r"(\w+)=(\S*)")


def _slurm_time_to_iso(token: str) -> str | None:
    """Parse SLURM's compact time format (``2026-04-15T03:00:00``) to
    an ISO string. Returns None on parse failure (token absent,
    relative ``NOW+...`` form, or unrecognised shape).

    SLURM emits reservation times in slurmctld's local timezone, NOT
    UTC. The TZ is read from ``HPC_SLURM_TZ`` (e.g. ``America/Los_Angeles``);
    when unset we tag as UTC for backwards compatibility — callers that
    compare against UTC ``now()`` on non-UTC clusters will be off by the
    cluster's offset.
    """
    if not token or token in {"Unknown", "N/A"}:
        return None
    try:
        dt = datetime.strptime(token, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    tz_name = os.environ.get("HPC_SLURM_TZ")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            return dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc).isoformat()
        except Exception:  # noqa: BLE001 — bad TZ falls back to UTC tag
            pass
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _split_csv(token: str) -> tuple[str, ...]:
    """Comma-separated SLURM token (Users / Accounts / Flags). Empty
    or ``(null)`` → empty tuple."""
    if not token or token in {"(null)", "None"}:
        return ()
    return tuple(part.strip() for part in token.split(",") if part.strip())


def _split_nodelist(token: str) -> tuple[str, ...]:
    """Split a SLURM ``Nodes=`` hostlist on top-level commas only.

    Commas inside ``[...]`` ranges (e.g. ``cn[001-003,005]``) are range
    separators, not nodelist separators — splitting on every comma the
    way :func:`_split_csv` does corrupts the bracketed token into
    invalid fragments (``cn[001-003`` / ``005]``). Ranges themselves are
    left unexpanded, consistent with :func:`held_node_set`.
    """
    if not token or token in {"(null)", "None"}:
        return ()
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in token:
        if ch == "[":
            depth += 1
            cur.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return tuple(p.strip() for p in parts if p.strip())


def _parse_one_reservation(record: str) -> ReservationHold | None:
    """Parse one reservation record (a chunk of key=value tokens)."""
    fields_kv: dict[str, str] = {}
    for k, v in _KV_TOKEN.findall(record):
        # Last write wins on dup keys — SLURM doesn't emit dupes for
        # the fields we care about.
        fields_kv[k] = v
    name = fields_kv.get("ReservationName")
    if not name:
        return None
    return ReservationHold(
        name=name,
        nodes=_split_nodelist(fields_kv.get("Nodes", "")),
        start_iso=_slurm_time_to_iso(fields_kv.get("StartTime", "")),
        end_iso=_slurm_time_to_iso(fields_kv.get("EndTime", "")),
        users=_split_csv(fields_kv.get("Users", "")),
        accounts=_split_csv(fields_kv.get("Accounts", "")),
        flags=_split_csv(fields_kv.get("Flags", "")),
    )


def parse_slurm_reservations(text: str) -> list[ReservationHold]:
    """Parse ``scontrol show reservation`` output.

    Permissive: never raises. Records the parser can't recognise are
    silently skipped (one bad record doesn't taint the whole list).
    Empty input → empty list.
    """
    if not text:
        return []
    # Records are separated by blank lines OR by ``ReservationName=``
    # at the start of a new line. Use blank-line splitting first; then
    # also split each chunk on ``ReservationName=`` boundaries to handle
    # the all-on-one-line form some SLURM versions emit.
    out: list[ReservationHold] = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        # Each chunk may still contain multiple records glued together.
        sub_records = re.split(r"(?=ReservationName=)", chunk)
        for record in sub_records:
            if "ReservationName=" not in record:
                continue
            parsed = _parse_one_reservation(record)
            if parsed is not None:
                out.append(parsed)
    return out


# ─── sacctmgr QOS parser ───────────────────────────────────────────────


def _coerce_int_or_none(token: str) -> int | None:
    """sacctmgr emits empty cells as empty strings. Parse a positive int
    or return None; treat ``-1`` (sentinel for "no limit") as None too."""
    if not token:
        return None
    try:
        n = int(token)
    except ValueError:
        return None
    return None if n < 0 else n


def parse_sacctmgr_qos(text: str) -> dict[str, QosLimit]:
    """Parse ``sacctmgr -P show qos`` output (pipe-separated).

    Format: header line names the columns; data rows are pipe-
    separated; missing values are empty cells. We pick the columns we
    care about (Name, MaxJobsPU, MaxCPUsPU, MaxSubmitJobsPU, Priority,
    Flags) by name so column order changes don't break the parse.

    Permissive: missing columns surface as None on the QosLimit;
    unparseable rows are skipped.
    """
    if not text:
        return {}
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    header = [col.strip() for col in lines[0].split("|")]
    name_to_idx = {col: i for i, col in enumerate(header)}
    if "Name" not in name_to_idx:
        # Without a name column we can't key the result.
        return {}

    def _cell(cells: list[str], col: str) -> str:
        i = name_to_idx.get(col)
        return cells[i].strip() if i is not None and i < len(cells) else ""

    out: dict[str, QosLimit] = {}
    for raw in lines[1:]:
        cells = raw.split("|")
        name = _cell(cells, "Name")
        if not name:
            continue
        out[name] = QosLimit(
            name=name,
            max_jobs_per_user=_coerce_int_or_none(_cell(cells, "MaxJobsPU")),
            max_cpus_per_user=_coerce_int_or_none(_cell(cells, "MaxCPUsPU")),
            max_submit_jobs_per_user=_coerce_int_or_none(_cell(cells, "MaxSubmitJobsPU")),
            priority=_coerce_int_or_none(_cell(cells, "Priority")),
            flags=_split_csv(_cell(cells, "Flags")),
        )
    return out


def reservations_active_at(
    reservations: list[ReservationHold],
    *,
    at_iso: str,
) -> list[ReservationHold]:
    """Subset of *reservations* whose [start, end) window covers *at_iso*.

    Convenience wrapper for the planner: hand it the parsed list and
    the proposed StartTime; it returns the holds that gate the slot.
    Reservations missing either start or end are treated as unbounded
    on that side (match the SLURM semantics where ``Unknown`` end
    means "until further notice").
    """

    def _to_dt(iso: str | None) -> datetime | None:
        if iso is None:
            return None
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        # Normalize to tz-aware UTC. ``_slurm_time_to_iso`` always tags
        # with UTC, but callers may pass naive timestamps for ``at_iso``;
        # without normalization the start <= target comparison raises
        # ``TypeError: can't compare offset-naive and offset-aware``.
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    target = _to_dt(at_iso)
    if target is None:
        return []

    out: list[ReservationHold] = []
    for r in reservations:
        start = _to_dt(r.start_iso)
        end = _to_dt(r.end_iso)
        if (start is None or start <= target) and (end is None or target < end):
            out.append(r)
    return out


def held_node_set(reservations: list[ReservationHold]) -> set[str]:
    """Flat set of node names across the input reservations.

    ``cn[001-003]``-style ranges are NOT expanded here — that's a SLURM
    hostlist-syntax problem and worth a separate helper. Callers that
    need expansion can use ``scontrol show hostnames``."""
    out: set[str] = set()
    for r in reservations:
        out.update(r.nodes)
    return out


__all__ = [
    "ReservationHold",
    "QosLimit",
    "parse_slurm_reservations",
    "parse_sacctmgr_qos",
    "reservations_active_at",
    "held_node_set",
]
