"""``verify-relay`` — deterministic audit of the agent's relay vs. the journal.

The machine counterpart to conduct rule 10 — "never relay numbers/state that
don't match the journal" (``docs/design/proving-run-2-hardening.md`` §6). The
doctrine already holds the LLM to relaying only code-digested briefs, but the
*relay itself* is unguarded: a rounded number, a swapped run-id, or a stale
state claim ("running" when the journal recorded "failed" — proving run #3)
still reaches the durable record. This verb closes that seam by *deterministic
code auditing the LLM against the durable record* — the inversion of an
LLM-audits-LLM reviewer, and the project's moat stated as a feature.

Home — ``ops/decision/`` (not ``ops/monitor/``): rules 9 (provenance gate) and
10 (this) are the paired trust-seam checks at the human decision / relay
boundary, and rule 9 already lives beside the decision journal
(``ops/decision/journal.py``'s ``append-decision``). The monitor domain
observes run *lifecycle*; this observes the agent's outgoing *message*, and its
primary authoritative source is the decision journal. So it belongs here.

It is a pure AUDIT: it reads durable records, never writes, and never blocks a
turn itself — it returns a verdict. Hook-level enforcement is a staged
follow-up, out of scope for this MVP.

Claim extraction & the heuristics (the bar is USEFUL-conservative, not perfect
— prefer flagging to missing):

* **Run-id / job-id tokens (checked first).** A token is "run-id-like" when it
  equals the run in scope, starts with ``run-``, is timestamp-shaped
  (``\\d{8}-\\d{6}…``), or carries a hyphen + a digit and is >= 8 chars. Each is
  matched against the authoritative id set (scope run_id + sidecar/record
  run_id, job_ids, parent_run_ids) by exact match or shared prefix (a short-sha
  reference passes). A run-id-like token matching nothing → ``run_id``
  mismatch. Standalone digit runs (>= 5 digits) are treated as job-id claims
  ONLY when the run has recorded job_ids to compare against (else they fall
  through to number checking). The character spans of every id token are then
  excluded from number extraction, so the digits inside a run-id never
  masquerade as a numeric claim.

* **Numbers.** ``\\d[\\d,]*(?:\\.\\d+)?%?`` — ints, floats, percentages,
  comma-grouped values (commas normalized away, ``%`` stripped). A claim passes
  on an exact normalized-string match, on float equality (so ``95`` == ``95.0``),
  or — for a DECIMAL claim only — when it is a string-prefix of a longer source
  value (pure truncation like ``3.14`` of ``3.1411``). A rounding that changes a
  digit (``3.15`` vs ``3.1411``) is NOT a prefix and IS flagged; the
  ``.``-required guard stops ``1`` from "truncating" ``128``. A number that
  matches no source number → ``number`` mismatch carrying the nearest source
  value. A number when the records carry NO comparable number at all →
  ``unverifiable`` (flagged, never silently passed).

  Conversational numbers are filtered BEFORE counting/flagging: an integer that
  is a line-start ``N.`` list marker, and any number whose nearest preceding
  non-space char is ``~`` (``check back in ~2 minutes``).

* **State words.** ``running / in_flight / complete / failed / pending /
  timeout / abandoned`` (+ synonyms) plus the verification phrases ``canary
  green`` and ``verified``. Each is mapped to a canonical family and compared to
  the run's recorded state (``RunRecord.status``, falling back to a sidecar
  ``status`` field). A lifecycle claim whose family differs from the recorded
  family → ``state`` mismatch carrying the recorded state. A verification claim
  (``verified`` / ``canary green``) passes only when its literal token is
  evidenced in some source (e.g. ``evidence_digest={"canary": "green"}``), else
  it is flagged. A state claim with no recorded state to check against at all →
  ``unverifiable``.

``sources_consulted`` names only the durable records actually found and read
(decision journal, run sidecar, RunRecord, per-run briefs), so a run with no
records honestly reports the empty/short list rather than a fabricated one.

The per-run briefs log (``<experiment>/.hpc/runs/<run_id>.briefs.jsonl``) is
read TOLERANTLY — another agent owns its creation; this verb never creates or
writes it, and a missing/partial file is simply skipped.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.verify_relay import (
    RelayMismatch,
    VerifyRelayInput,
    VerifyRelayResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from collections.abc import Iterable

# ── number extraction ─────────────────────────────────────────────────────────

# Ints, floats, comma-grouped values, and trailing-``%`` percentages.
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


def _normalize_num(raw: str) -> str:
    """Strip grouping commas and a trailing ``%`` — the compare-normal form."""
    return raw.replace(",", "").rstrip("%")


def _is_identifier_like(s: str) -> bool:
    """True for run-id / job-id / date-shaped strings (digit + hyphen).

    Such strings carry digits that are NOT numeric claims (``run-1``,
    ``20260703-141500-ab``), so their embedded numbers are excluded from the
    source-number pool to avoid a relay number spuriously "matching" them.
    """
    return "-" in s and bool(re.search(r"\d", s))


def _collect_source_numbers(obj: Any, strings: set[str], floats: list[float]) -> None:
    """Recursively gather every comparable number from a durable-record object.

    Scalar ints/floats contribute their value directly; strings contribute
    their embedded number tokens UNLESS the string is identifier-shaped (a
    run-id/job-id/date, whose digits are not numeric facts). Bools are skipped
    (``True``/``False`` are not numbers even though ``isinstance(True, int)``).
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        strings.add(_normalize_num(str(obj)))
        floats.append(float(obj))
        return
    if isinstance(obj, str):
        if _is_identifier_like(obj):
            return
        for m in _NUM_RE.finditer(obj):
            _add_num_token(m.group(0), strings, floats)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_source_numbers(v, strings, floats)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_source_numbers(v, strings, floats)


def _add_num_token(raw: str, strings: set[str], floats: list[float]) -> None:
    norm = _normalize_num(raw)
    strings.add(norm)
    with contextlib.suppress(ValueError):
        floats.append(float(norm))


def _match_number(raw: str, source_strings: set[str], source_floats: list[float]) -> bool:
    """True iff the relay number *raw* is supported by some source number."""
    norm = _normalize_num(raw)
    if norm in source_strings:
        return True
    try:
        val = float(norm)
    except ValueError:
        # Unparseable numeric token — do not flag (nothing to compare).
        return True
    if any(f == val for f in source_floats):
        return True
    # Truncation tolerance: a DECIMAL claim that is a string-prefix of a longer
    # source value (``3.14`` of ``3.1411``). Requiring the ``.`` stops ``1``
    # from "truncating" ``128``; requiring a strictly longer source stops the
    # exact case (already handled) from double-counting.
    if "." in norm:
        for s in source_strings:
            if len(s) > len(norm) and s.startswith(norm):
                return True
    return False


def _nearest_number(raw: str, source_floats: list[float]) -> str | None:
    """The source number closest to *raw*, as a string, or None if no numbers."""
    if not source_floats:
        return None
    try:
        val = float(_normalize_num(raw))
    except ValueError:
        return None
    nearest = min(source_floats, key=lambda f: abs(f - val))
    # Render an integral float without the ``.0`` tail so it reads like the
    # source (``128`` not ``128.0``).
    return str(int(nearest)) if nearest == int(nearest) else str(nearest)


# ── run-id / job-id extraction ─────────────────────────────────────────────────

# Tokens with internal ``._-`` separators — the run-id-shaped candidates.
_IDENT_RE = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+")
_JOB_ID_RE = re.compile(r"\d{5,}")
_TS_PREFIX_RE = re.compile(r"\d{8}-\d{6}")


def _is_run_id_like(token: str, scope_run_id: str) -> bool:
    if token == scope_run_id:
        return True
    if token.lower().startswith("run-"):
        return True
    if _TS_PREFIX_RE.match(token):
        return True
    return "-" in token and bool(re.search(r"\d", token)) and len(token) >= 8


def _id_matches(token: str, auth_ids: set[str]) -> bool:
    """True iff *token* names an authoritative id (exact or shared prefix)."""
    if token in auth_ids:
        return True
    for aid in auth_ids:
        # Short-sha / prefix reference either direction (>= 4 chars to be a
        # meaningful prefix, not a 1-char coincidence).
        if len(token) >= 4 and (aid.startswith(token) or token.startswith(aid)):
            return True
    return False


# ── state extraction ───────────────────────────────────────────────────────────

# Relay state phrase → canonical family. Multi-word phrases first in the regex.
_STATE_WORD_TO_FAMILY: dict[str, str] = {
    "canary green": "canary_green",
    "in_flight": "running",
    "in-flight": "running",
    "in flight": "running",
    "inflight": "running",
    "timed_out": "timeout",
    "timed out": "timeout",
    "running": "running",
    "complete": "complete",
    "completed": "complete",
    "finished": "complete",
    "succeeded": "complete",
    "success": "complete",
    "failed": "failed",
    "failure": "failed",
    "errored": "failed",
    "pending": "pending",
    "queued": "pending",
    "waiting": "pending",
    "timeout": "timeout",
    "abandoned": "abandoned",
    "verified": "verified",
}

# Recorded ``RunRecord.status`` (or sidecar status) → canonical family.
_STATUS_TO_FAMILY: dict[str, str] = {
    "in_flight": "running",
    "running": "running",
    "complete": "complete",
    "failed": "failed",
    "abandoned": "abandoned",
    "timeout": "timeout",
    "pending": "pending",
}

_LIFECYCLE_FAMILIES = frozenset(
    {"running", "complete", "failed", "pending", "timeout", "abandoned"}
)

# Longest phrases first so ``in flight`` wins over a bare ``flight`` fragment,
# and ``canary green`` over ``green`` alone (which we deliberately don't match).
_STATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(w) for w in sorted(_STATE_WORD_TO_FAMILY, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _classify_state(
    family: str,
    run_status_raw: str | None,
    run_status_family: str | None,
    source_text: str,
) -> tuple[str, str | None] | None:
    """Return ``(kind, nearest)`` for a state claim, or None when it passes."""
    if family in ("verified", "canary_green"):
        needle = "verified" if family == "verified" else "green"
        if needle in source_text:
            return None
        if run_status_family is None and not source_text:
            return ("unverifiable", None)
        return ("state", run_status_raw)
    # Lifecycle claim.
    if run_status_family is None:
        return ("unverifiable", None)
    if family == run_status_family:
        return None
    return ("state", run_status_raw)


# ── source loading ─────────────────────────────────────────────────────────────


def _load_briefs(experiment_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Read ``<exp>/.hpc/runs/<run_id>.briefs.jsonl`` tolerantly (may be absent).

    Never creates or writes the file — another agent owns brief persistence. A
    missing file, unreadable bytes, or an individually-corrupt line yields no
    records for that line rather than raising.
    """
    path = experiment_dir / ".hpc" / "runs" / f"{run_id}.briefs.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _dedupe_mismatches(items: Iterable[RelayMismatch]) -> list[RelayMismatch]:
    seen: set[tuple[str, str, str, str | None]] = set()
    out: list[RelayMismatch] = []
    for m in items:
        key = (m.claim, m.kind, m.detail, m.nearest_source_value)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


@primitive(
    name="verify-relay",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    # A pure read-only audit: re-running it is always safe (it writes nothing).
    idempotent=True,
    cli=CliShape(
        help=(
            "Audit an agent's draft relay text against a run's durable records "
            "(decision journal, sidecar, RunRecord, briefs). Deterministic "
            "claim-extraction — numbers, run/job ids, state words — diffed "
            "against the record. Returns a verdict; never blocks (conduct "
            "rule 10)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=VerifyRelayInput,
        schema_ref=SchemaRef(input="verify_relay"),
    ),
    agent_facing=True,
)
def verify_relay(*, experiment_dir: Path, spec: VerifyRelayInput) -> VerifyRelayResult:
    """Audit *spec.relay_text* against the durable records for *spec.run_id*.

    Deterministically extracts the factual claims (numbers, run/job ids, state
    words) from the relay and diffs each against the decision journal, run
    sidecar, RunRecord, and per-run briefs. Returns a verdict — ``clean`` plus
    the itemized mismatches; it never blocks the turn itself.

    Raises
    ------
    :class:`errors.SpecInvalid`
        Never raised for a well-formed spec; the run_id shape is enforced at
        the wire boundary. Declared for registry honesty.
    """
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    experiment_dir = Path(experiment_dir)
    run_id = spec.run_id
    relay = spec.relay_text or ""

    # ── load the authoritative sources (honest sources_consulted) ──────────────
    sources_consulted: list[str] = []
    source_objs: list[Any] = []

    journal_records = read_decisions(experiment_dir, "run", run_id)
    if journal_records:
        sources_consulted.append("decision_journal")
        source_objs.extend(journal_records)

    sidecar: dict[str, Any] | None
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, ValueError):
        sidecar = None
    if sidecar is not None:
        sources_consulted.append("run_sidecar")
        source_objs.append(sidecar)

    record = load_run(experiment_dir, run_id)
    record_dict: dict[str, Any] | None = None
    if record is not None:
        record_dict = dataclasses.asdict(record)
        sources_consulted.append("run_record")
        source_objs.append(record_dict)

    briefs = _load_briefs(experiment_dir, run_id)
    if briefs:
        sources_consulted.append("briefs")
        source_objs.extend(briefs)

    # ── build the compare pools ────────────────────────────────────────────────
    source_num_strings: set[str] = set()
    source_num_floats: list[float] = []
    for obj in source_objs:
        _collect_source_numbers(obj, source_num_strings, source_num_floats)
    has_source_numbers = bool(source_num_strings)

    source_text = " ".join(json.dumps(o, default=str) for o in source_objs).lower()

    run_status_raw: str | None = None
    if record is not None:
        run_status_raw = record.status
    elif sidecar is not None:
        raw = sidecar.get("status")
        run_status_raw = raw if isinstance(raw, str) and raw else None
    run_status_family = _STATUS_TO_FAMILY.get(run_status_raw or "")

    # Authoritative id set + recorded job ids.
    auth_ids: set[str] = {run_id}
    job_ids: set[str] = set()
    for src in (sidecar, record_dict):
        if not isinstance(src, dict):
            continue
        rid = src.get("run_id")
        if isinstance(rid, str) and rid:
            auth_ids.add(rid)
        for key in ("job_ids", "parent_run_ids"):
            vals = src.get(key)
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, (str, int)) and str(v):
                        auth_ids.add(str(v))
                        if key == "job_ids":
                            job_ids.add(str(v))

    mismatches: list[RelayMismatch] = []
    claims_checked = 0
    consumed_spans: list[tuple[int, int]] = []

    # ── (1) run-id / job-id tokens (first; their spans block number reads) ─────
    for m in _IDENT_RE.finditer(relay):
        token = m.group(0)
        if not _is_run_id_like(token, run_id):
            continue
        consumed_spans.append((m.start(), m.end()))
        claims_checked += 1
        if not _id_matches(token, auth_ids):
            mismatches.append(
                RelayMismatch(
                    claim=token,
                    kind="run_id",
                    detail=(
                        f"run-id-shaped token {token!r} matches no authoritative "
                        f"identifier for run {run_id!r}"
                    ),
                    nearest_source_value=run_id,
                )
            )

    # Standalone digit job-id claims — only when the run has recorded job_ids.
    if job_ids:
        for m in _JOB_ID_RE.finditer(relay):
            if _overlaps(m.start(), m.end(), consumed_spans):
                continue
            token = m.group(0)
            consumed_spans.append((m.start(), m.end()))
            claims_checked += 1
            if token not in job_ids:
                mismatches.append(
                    RelayMismatch(
                        claim=token,
                        kind="run_id",
                        detail=(
                            f"job-id-shaped token {token!r} is not among the run's "
                            f"recorded job ids {sorted(job_ids)}"
                        ),
                        nearest_source_value=", ".join(sorted(job_ids)),
                    )
                )

    # ── (2) numbers (skipping id spans + conversational uses) ──────────────────
    for m in _NUM_RE.finditer(relay):
        if _overlaps(m.start(), m.end(), consumed_spans):
            continue
        raw = m.group(0)
        if _is_conversational_number(relay, m.start(), m.end(), raw):
            continue
        claims_checked += 1
        if not has_source_numbers:
            mismatches.append(
                RelayMismatch(
                    claim=raw,
                    kind="unverifiable",
                    detail=(
                        f"numeric claim {raw!r} has no comparable value in any "
                        "durable record for the run"
                    ),
                    nearest_source_value=None,
                )
            )
            continue
        if not _match_number(raw, source_num_strings, source_num_floats):
            mismatches.append(
                RelayMismatch(
                    claim=raw,
                    kind="number",
                    detail=(
                        f"numeric claim {raw!r} matches no source number (nor a truncation of one)"
                    ),
                    nearest_source_value=_nearest_number(raw, source_num_floats),
                )
            )

    # ── (3) state words (deduped by phrase) ────────────────────────────────────
    seen_state: set[str] = set()
    for m in _STATE_RE.finditer(relay):
        phrase = re.sub(r"\s+", " ", m.group(0).lower())
        if phrase in seen_state:
            continue
        seen_state.add(phrase)
        family = _STATE_WORD_TO_FAMILY.get(phrase)
        if family is None:
            continue
        claims_checked += 1
        verdict = _classify_state(family, run_status_raw, run_status_family, source_text)
        if verdict is None:
            continue
        kind, nearest = verdict
        detail = (
            f"state claim {m.group(0)!r} has no comparable state in the record"
            if kind == "unverifiable"
            else (f"state claim {m.group(0)!r} contradicts the recorded state {run_status_raw!r}")
        )
        mismatches.append(
            RelayMismatch(
                claim=m.group(0),
                kind=kind,  # type: ignore[arg-type]
                detail=detail,
                nearest_source_value=nearest,
            )
        )

    mismatches = _dedupe_mismatches(mismatches)
    return VerifyRelayResult(
        clean=not mismatches,
        claims_checked=claims_checked,
        mismatches=mismatches,
        sources_consulted=sources_consulted,
    )


def _overlaps(start: int, end: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(start < e and s < end for s, e in spans)


def _is_conversational_number(text: str, start: int, end: int, raw: str) -> bool:
    """True for a number that is chatter, not a fact.

    Two pragmatic heuristics (documented in the module docstring):

    * a line-start ``N.`` list marker with an integer value 0-10;
    * any number whose nearest preceding non-space char is ``~`` (``~2 minutes``).
    """
    # ~-prefixed duration: nearest preceding non-space char is a tilde.
    j = start - 1
    while j >= 0 and text[j] == " ":
        j -= 1
    if j >= 0 and text[j] == "~":
        return True
    # Line-start ``N.`` list marker (integer 0-10 only).
    line_start = text.rfind("\n", 0, start) + 1
    before = text[line_start:start]
    if before.strip() == "" and end < len(text) and text[end] == "." and "." not in raw:
        try:
            if 0 <= int(raw) <= 10:
                return True
        except ValueError:
            return False
    return False
