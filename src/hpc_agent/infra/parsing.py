"""Shared scheduler-output parsers.

This module consolidates the small, permissive parsers that earlier lived
inline (and duplicated) in ``infra/inspect.py``, ``infra/backends/query.py``,
``infra/gpu.py``, and ``orchestrator/constraints.py``.

Design rules
------------

- **stdlib-only** — these helpers are imported by modules that ship to the
  cluster, so they must not pull in third-party deps.
- **permissive** — every parser degrades to ``None`` / ``0`` rather than
  raising on garbage input. Schedulers vary across versions; we surface
  a partial answer instead of refusing to parse.
- **single-source** — once a regex / format spec has settled here, callers
  must import it (no copy-pasting a "small fix" back into inspect.py).
"""

from __future__ import annotations

__all__ = [
    "to_int",
    "to_int_or_none",
    "to_float_or_none",
    "parse_mem_to_gb",
    "parse_mem_to_mb",
    "parse_walltime_to_sec",
    "parse_sacct_pipe_row",
    "parse_qstat_columns",
    "FAILURE_CATEGORY_PATTERNS",
    "categorize_failure",
]

import re
from typing import Any

# ---------------------------------------------------------------------------
# Numeric coercion
# ---------------------------------------------------------------------------


def to_int(value: str | None, default: int = 0) -> int:
    """Best-effort int parse — returns *default* on any failure.

    Accepts trailing ``.0`` (some sacct formats emit it). This is the
    "lossy but safe" form used by happy-path stat aggregation; if you
    need a sentinel for "missing" use :func:`to_int_or_none`.
    """
    if value is None:
        return default
    s = value.strip() if isinstance(value, str) else str(value).strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        # ``float("Infinity")`` succeeds but ``int(inf)`` raises
        # ``OverflowError`` — catch both to keep the documented
        # "returns default on failure" contract.
        try:
            return int(float(s))
        except (ValueError, OverflowError):
            return default


def to_int_or_none(s: Any) -> int | None:
    """Parse a leading signed integer prefix from *s*, else ``None``.

    Used by the snapshot parsers where a missing field is a meaningful
    "unknown" — distinct from the zero default of :func:`to_int`.
    """
    if s is None:
        return None
    text = str(s).strip()
    if not text:
        return None
    m = re.match(r"-?\d+", text)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def to_float_or_none(s: Any) -> float | None:
    """Best-effort float parse, else ``None``."""
    if s is None:
        return None
    text = str(s).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Memory tokens
# ---------------------------------------------------------------------------


def parse_mem_to_gb(s: str | None, *, cpus: int | None = None) -> float | None:
    """Parse a SLURM/SGE memory token (e.g. ``128G``, ``1024M``) -> GB.

    Accepts an optional trailing ``B`` (``128GB``); unit defaults to MB
    when absent (matches SLURM's default sacct ``ReqMem`` formatting).

    Recognises SLURM's per-CPU (``c``) and per-node (``n``) suffixes
    after the unit: ``1000Mc`` means 1000MB per CPU. When ``cpus`` is
    provided and the suffix is ``c``, the result is scaled accordingly.
    When ``cpus`` is None the per-CPU value is returned (caller must
    multiply); this matches the historical behaviour where the suffix
    was ignored.
    """
    if not s:
        return None
    m = re.match(r"(\d+(?:\.\d+)?)\s*([KMGTkmgt])?[bB]?([cn])?", s.strip())
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "M").upper()
    per = m.group(3)
    factor = {"K": 1 / (1024 * 1024), "M": 1 / 1024, "G": 1.0, "T": 1024.0}.get(unit, 1 / 1024)
    result = val * factor
    if per == "c" and cpus is not None and cpus > 0:
        result *= cpus
    return round(result, 3)


def parse_mem_to_mb(s: str | None) -> int | None:
    """Parse a memory token to integer MB (rounded)."""
    gb = parse_mem_to_gb(s)
    if gb is None:
        return None
    return int(round(gb * 1024))


# ---------------------------------------------------------------------------
# Walltime / elapsed
# ---------------------------------------------------------------------------


def parse_walltime_to_sec(s: str | None) -> int:
    """Parse a SLURM-style walltime / elapsed string to seconds.

    Accepts ``SS``, ``MM:SS``, ``HH:MM:SS``, and ``D-HH:MM:SS`` (with an
    optional ``.frac`` tail). Returns 0 on parse failure — same
    permissive posture the inline copies had.
    """
    if not s:
        return 0
    text = s.strip()
    if not text:
        return 0
    # D-HH:MM:SS (with optional fractional seconds)
    m = re.match(r"^(?:(?P<d>\d+)-)?(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})(?:\.\d+)?$", text)
    if m:
        days = int(m.group("d") or 0)
        return days * 86400 + int(m.group("h")) * 3600 + int(m.group("m")) * 60 + int(m.group("s"))
    # MM:SS
    m = re.match(r"^(\d+):(\d{2})$", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # SS (bare integer). ``str.isdigit()`` is unicode-aware and accepts
    # superscripts / circled digits / etc. that ``int()`` then rejects;
    # restrict to ASCII to keep the "degrades to 0" contract intact.
    if text.isascii() and text.isdigit():
        return int(text)
    return 0


# ---------------------------------------------------------------------------
# sacct pipe-delimited rows
# ---------------------------------------------------------------------------


def parse_sacct_pipe_row(parts: list[str], format_spec: list[str]) -> dict[str, str]:
    """Map a sacct ``-P``/``--parsable2`` row onto its declared columns.

    *parts* is the ``line.split("|")`` result; *format_spec* is the list
    of column names passed to sacct's ``--format=`` flag (in the same
    order). Trailing columns missing from *parts* are filled with the
    empty string so callers can ``row[col]`` without a length check.

    The function does **not** filter step rows or terminal states —
    those policy decisions belong to the caller. It exists to centralise
    the "ordinal -> name" mapping that the inline copies previously
    open-coded as ``parts[3]``, ``parts[4]`` etc., which made adding a
    new ``--format`` field a multi-file edit.
    """
    out: dict[str, str] = {}
    for i, name in enumerate(format_spec):
        if i < len(parts):
            out[name] = parts[i].strip()
        else:
            out[name] = ""
    return out


# ---------------------------------------------------------------------------
# Failure-category fingerprinting (regex catalog)
# ---------------------------------------------------------------------------

# Patterns that strongly identify a failure category, ordered most-specific
# first. Matched case-insensitively against the joined log tail.  The first
# hit wins. Subject-level orchestration (in
# :mod:`hpc_agent.runner.failures`) layers exit-code overrides and the
# richer :func:`hpc_agent.runner.failure_signatures.classify` catalog on
# top of this primitive bucket.
FAILURE_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # The campus user got bumped, not failed. Match the dispatcher's
    # SIGTERM-trap stderr line so the cluster groups all preempted
    # tasks together and the harness can resubmit cleanly.
    (
        "preempted",
        re.compile(r"\[hpc-agent\] SIGTERM received; cluster preemption imminent"),
    ),
    ("gpu_oom", re.compile(r"cuda(?: out of memory|.*OOM)|torch\.cuda\.OutOfMemoryError", re.I)),
    # Pattern kept identical to the ``system_oom`` row of
    # ``failure_signatures.CATALOG`` so the two classifiers cannot
    # disagree -- a kernel ``oom-kill:`` line (no "oom-killer" token)
    # must not classify as ``unknown`` here while the catalog tags it
    # ``system_oom`` and recommends increase-mem.
    (
        "system_oom",
        re.compile(r"oom-kill|out of memory.*killed|\bMemoryError\b|killed.*signal 9", re.I),
    ),
    (
        # Narrowed to scheduler-specific markers -- bare ``walltime`` or
        # ``signal SIGTERM 15`` collide with preemption (which the
        # scheduler also delivers via SIGTERM). The exit-code 130/143
        # override in the runner's orchestrator routes confirmed SIGTERM
        # cases to ``preempted``.
        "walltime",
        # Kept in sync with the ``walltime`` row of
        # ``failure_signatures.CATALOG`` (scheduler-specific markers
        # only -- no bare ``walltime`` token, which collides with
        # preemption) so the two classifiers cannot disagree.
        re.compile(
            r"DUE TO TIME LIMIT|CANCELLED.*TIME LIMIT|"
            r"wall.?time.*expired|wall.?time.*exceeded|"
            r"Time limit exceeded|h_rt.*exceeded|qmaster enforced h_rt",
            re.I,
        ),
    ),
    (
        "node_failure",
        re.compile(
            r"NODE_FAIL|node failed|connection (closed|reset by peer)|ssh: connect.*refused", re.I
        ),
    ),
    ("import_error", re.compile(r"\bImportError\b|\bModuleNotFoundError\b", re.I)),
    ("file_not_found", re.compile(r"\bFileNotFoundError\b|No such file or directory", re.I)),
    ("permission_denied", re.compile(r"\bPermissionError\b|Permission denied", re.I)),
    ("disk_full", re.compile(r"No space left on device|\bENOSPC\b", re.I)),
    ("python_traceback", re.compile(r"^Traceback \(most recent call last\):", re.I | re.M)),
)


def categorize_failure(content: str | None) -> str:
    """Map a stderr blob to one of :data:`FAILURE_CATEGORY_PATTERNS` or 'unknown'.

    Pure pattern match -- no exit-code awareness, no preemption override.
    Subject-level orchestration in :mod:`hpc_agent.runner.failures` wraps
    this with the exit-code 130/143 -> ``preempted`` override and the
    richer signature-catalog classifier.
    """
    if not content:
        return "unknown"
    for category, pat in FAILURE_CATEGORY_PATTERNS:
        if pat.search(content):
            return category
    return "unknown"


# ---------------------------------------------------------------------------
# qstat columnar output
# ---------------------------------------------------------------------------


def parse_qstat_columns(
    text: str,
    *,
    skip_prefixes: tuple[str, ...] = ("HOSTNAME", "---", "global", "queuename", "###"),
    require_min_cols: int = 1,
) -> list[list[str]]:
    """Tokenise an SGE columnar table (``qstat -f``, ``qhost``, ...) into rows.

    Drops blank lines and any line whose first token starts with one of
    *skip_prefixes* (covers headers, separators, and ``qhost``'s
    ``global`` summary row). Continuation / detail lines (those that
    start with whitespace) are returned with their leading whitespace
    intact in column 0 of the row's tokens — callers that want only the
    primary rows can filter on ``row[0].startswith`` or use
    :func:`iter_qstat_primary_rows` (a thin wrapper kept inline by each
    caller because the primary/detail boundary differs by command).

    Returns a list of column lists; never raises. *require_min_cols*
    drops rows shorter than that count (defaults to 1 so callers see
    every non-blank row by default).
    """
    rows: list[list[str]] = []
    if not text:
        return rows
    for raw in text.splitlines():
        if not raw.strip():
            continue
        # Detail lines (leading whitespace) are kept verbatim so the
        # caller can re-attach them to the previous primary row.
        leading_ws = raw[: len(raw) - len(raw.lstrip())]
        cols = raw.split()
        if not cols:
            continue
        first = cols[0]
        if any(first.startswith(p) for p in skip_prefixes):
            continue
        if leading_ws:
            # Mark continuation by preserving a sentinel empty leading element.
            cols = ["", *cols]
        if len(cols) < require_min_cols:
            continue
        rows.append(cols)
    return rows
