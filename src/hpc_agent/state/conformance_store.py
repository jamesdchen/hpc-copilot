"""The live-conformance ledger — one append-only store per registration (T3).

Design origin: ``docs/design/live-conformance.md`` (C-store, C1, C-compare).
This module is the **mechanics** of the conformance ledger: where live receipts
live, how one is appended un-fakeably, how the ledger is read tolerantly, and
how a query-time window is selected over it. It is the second consumer of the
``_aggregated/`` ledger idiom (``ops/verify_reproduction.py``'s reproduction
receipts), never a third storage invention — the append routes through the ONE
JSONL-append definition (:func:`hpc_agent.infra.io.append_jsonl_line`) and the
ONE attestation kernel (:func:`hpc_agent.state.attestation.bind`).

The ledger is **registration-scoped**:
``<experiment>/_aggregated/_conformance/<registration_id>.jsonl`` — one line per
observation receipt, keyed on the REGISTRATION (the sealed hypothesis under
test), not on a code identity (C-store). It is a durable SCIENTIFIC record that
must survive a decision-journal wipe, so it lives beside the aggregated metrics,
not in ``.hpc/``.

Record shape (schema_version 1 — the ``RECEIPT_SCHEMA_VERSION`` convention)::

    {"ts": "...", "schema_version": 1,
     "attestor": "code", "subject_kind": "conformance-observation",
     "subject_id": "<registration_id>",
     "content_sha": "<canonical sha over {payload, labels, observed_at}>",
     "registration": {"registration_id": "...", "dossier_sha": "...",
                      "status_at_record": "current|stale|revoked|superseded"},
     "observed_at": "<ISO ts the caller says the observation occurred>",
     "labels": {"<opaque>": "<opaque>"},
     "payload": {"<metric key>": 0.947, ...},
     "emitter": "<opaque caller-declared emitter id>"}

The append is fail-open for EVIDENCE (C-store): this store binds and appends any
well-formed observation regardless of the registration's reduced status — the
``status_at_record`` the CALLER stamps (T4) is validated for PRESENCE and
disclosed, never used to refuse. The REGISTRATION-exists check (an absent
registration is refused loudly) is the record VERB's job (T4); this store is
mechanics.

Pure store: no SSH, no ``_wire`` import, no scheduler. It reads and writes the
ledger file and routes shape/lock decisions through the shared kernels.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state import attestation, determinism, scopes

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "ATTESTOR",
    "SUBJECT_KIND",
    "conformance_ledger_path",
    "append_observation",
    "read_observations",
    "select_window",
]

_log = logging.getLogger(__name__)

#: The ledger schema version — bump on any record-shape change (the
#: ``RECEIPT_SCHEMA_VERSION`` convention shared with the reproduction receipt).
SCHEMA_VERSION = 1

#: Every observation is a CODE attestation (C1) — the payload sha is
#: server-recomputed and bound; truthfulness of the payload itself is
#: caller-attested (the F8 honesty).
ATTESTOR = "code"

#: The opaque attestation ``subject_kind`` every observation rides. Distinguishes
#: this subject class from notebook sections / reproduction receipts sharing the
#: attestation machinery; the kernel never interprets it.
SUBJECT_KIND = "conformance-observation"

# The registration sub-block's required non-empty string fields. ``status_at_record``
# is validated for PRESENCE only — its VALUE is the registration reduction's
# vocabulary (current/stale/revoked/superseded), stamped by the caller (T4), and
# the store never refuses on it (fail-open for evidence, C-store).
_REGISTRATION_FIELDS = ("registration_id", "dossier_sha", "status_at_record")


def conformance_ledger_path(experiment_dir: Path, registration_id: str) -> Path:
    """The append-only ledger path for *registration_id* (file may not exist).

    ``<experiment>/_aggregated/_conformance/<registration_id>.jsonl`` — the
    C-store home. The *registration_id* is slug-validated (it is a path segment)
    through the state layer's ONE slug class (:func:`scopes.validate_tag` →
    ``^[A-Za-z0-9._-]+$``), so it cannot escape the ``_conformance`` dir. Does
    not create the file; the append helper creates the parent dir on first write.

    Raises :class:`errors.SpecInvalid` on a non-slug id.
    """
    _validate_registration_id(registration_id)
    from pathlib import Path as _Path

    root = _Path(experiment_dir).resolve()
    return root / "_aggregated" / "_conformance" / f"{registration_id}.jsonl"


def append_observation(experiment_dir: Path, *, record: dict[str, Any]) -> dict[str, Any]:
    """Validate, bind, and append one conformance observation — un-fakeably.

    The one append path. Steps, in order:

    1. **Validate the C-store shape** — ``schema_version`` == 1, the attestation
       fields (``attestor`` == ``"code"``, ``subject_kind`` ==
       ``"conformance-observation"``, a non-empty ``subject_id``), the
       ``registration`` sub-block (``registration_id`` / ``dossier_sha`` /
       ``status_at_record`` all present and non-empty — ``status_at_record`` is
       the CALLER's stamp, validated for presence only), a non-empty
       ``observed_at``, and opaque flat ``payload`` / ``labels`` (dicts of
       scalars — identity/range/count-compared, never read for meaning).
    2. **Recompute the canonical sha server-side** over ``{payload, labels,
       observed_at}`` (:func:`_canonical_observation_sha`) and route the record
       through :func:`~hpc_agent.state.attestation.bind` with that recompute —
       the asserted ``content_sha`` must equal the fresh recompute, so a
       payload's sha cannot be asserted into existence (D5 lock 2).
    3. **Append ONE line** via the shared JSONL-append helper (advisory flock +
       fsync + append-only) — the sole side effect.

    ``ts`` is the ledger-append time: stamped server-side when absent (the
    receipt idiom), while ``observed_at`` stays the caller-attested observation
    time. This store is **fail-open for evidence** — it never consults the
    registration's live status to refuse; recording an observation against a
    stale/revoked/superseded registration is CORRECT (production is the
    experiment that never stops), with the reduced status disclosed in
    ``status_at_record``. The REGISTRATION-exists refusal is the record verb's
    job (T4).

    Returns the appended record (with ``ts`` stamped). Raises
    :class:`errors.SpecInvalid` on any shape violation or a content_sha that does
    not match the server recompute.
    """
    out = dict(record)
    if not isinstance(out.get("ts"), str) or not out["ts"]:
        out["ts"] = utcnow_iso()
    _validate_observation_shape(out)

    # Un-fakeable lock: recompute the payload sha server-side and bind (routes
    # through the ONE kernel; the asserted content_sha must match).
    recompute = _canonical_observation_sha(out["payload"], out["labels"], out["observed_at"])
    attestation.bind(out, recompute=recompute)

    path = conformance_ledger_path(experiment_dir, out["subject_id"])
    append_jsonl_line(path, out)
    return out


def read_observations(
    experiment_dir: Path, registration_id: str
) -> tuple[list[dict[str, Any]], int]:
    """Read a registration's ledger tolerantly → ``(records, skipped)``.

    Returns every observation record in append (chronological) order plus a count
    of TORN lines skipped (JSON-decode failures or non-object lines) — one bad
    line never strands the rest of a scientific record (the tolerant-read idiom).
    Blank lines are ignored and do NOT count as skipped. A missing ledger file
    returns ``([], 0)``.

    Raises :class:`errors.SpecInvalid` on a non-slug *registration_id* (via the
    path derivation).
    """
    path = conformance_ledger_path(experiment_dir, registration_id)
    records: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records, skipped
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("conformance_store: skipping unreadable %s (%s)", path, exc)
        return records, skipped
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.warning("conformance_store: torn line %d in %s (%s)", lineno, path, exc)
            skipped += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            skipped += 1
    return records, skipped


def select_window(
    records: Sequence[Mapping[str, Any]],
    *,
    since: str | None = None,
    until: str | None = None,
    last_n: int | None = None,
) -> list[dict[str, Any]]:
    """Select a live window over *records* — timestamp/count arithmetic ONLY.

    The query-time window selection hook (C-compare): it feeds T1's
    ``judge_window``. Two mutually-exclusive modes, no default — a caller MUST
    name a window:

    * **timestamp mode** — ``since`` (and optional ``until``): the half-open
      interval ``[since, until)`` over each record's ``observed_at``. ``since``
      is INCLUSIVE (``since <= observed_at``); ``until`` is EXCLUSIVE
      (``observed_at < until``) so adjacent windows never double-count a
      boundary record. ISO-8601 timestamps compare lexicographically (the
      ``as_of`` posture: compare timestamps, invent no parsing). A record
      missing ``observed_at`` is skipped.
    * **count mode** — ``last_n``: the trailing *last_n* records in append order
      (``records[-last_n:]``). Must be a positive int.

    ``last_n`` may NOT combine with ``since``/``until`` (a count window and a
    time window are different selections). Supplying neither, or both, is a loud
    refusal — core never picks, defaults, or "recommends" a window.

    Raises :class:`errors.SpecInvalid` on the exclusivity/emptiness guards or a
    malformed selector.
    """
    time_mode = since is not None or until is not None
    count_mode = last_n is not None
    if time_mode and count_mode:
        raise errors.SpecInvalid(
            "select_window: `last_n` is mutually exclusive with `since`/`until` — "
            "a count window and a time window are different selections; name exactly one."
        )
    if not time_mode and not count_mode:
        raise errors.SpecInvalid(
            "select_window: name a window — either `since` (with optional `until`) "
            "or `last_n`. Core never defaults a window (no invented span)."
        )

    if count_mode:
        if not isinstance(last_n, int) or isinstance(last_n, bool) or last_n < 1:
            raise errors.SpecInvalid(
                f"select_window: `last_n` must be a positive int; got {last_n!r}"
            )
        return [dict(r) for r in records[-last_n:]]

    if since is not None and not isinstance(since, str):
        raise errors.SpecInvalid(f"select_window: `since` must be an ISO string; got {since!r}")
    if until is not None and not isinstance(until, str):
        raise errors.SpecInvalid(f"select_window: `until` must be an ISO string; got {until!r}")

    out: list[dict[str, Any]] = []
    for record in records:
        observed_at = record.get("observed_at")
        if not isinstance(observed_at, str) or not observed_at:
            continue
        if since is not None and observed_at < since:
            continue
        if until is not None and observed_at >= until:
            continue
        out.append(dict(record))
    return out


# ── internals ────────────────────────────────────────────────────────────────


def _validate_registration_id(registration_id: Any) -> str:
    """Slug-validate a registration id — it is a path segment (reuse the ONE class)."""
    if not isinstance(registration_id, str):
        raise errors.SpecInvalid(
            f"conformance: registration_id must be a string; got {registration_id!r}"
        )
    try:
        scopes.validate_tag(registration_id)
    except errors.SpecInvalid as exc:
        raise errors.SpecInvalid(f"conformance: registration_id — {exc}") from exc
    return registration_id


def _canonical_observation_sha(
    payload: Mapping[str, Any], labels: Mapping[str, Any], observed_at: str
) -> str:
    """The observation ``content_sha`` — canonical JSON over ``{payload, labels, observed_at}``.

    The harness-contract sha form (``docs/internals/harness-contract.md``:
    ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)``,
    SHA-256 lowercase hex). The json+sha kernel is the ONE such canonicalization
    (:func:`state.determinism.canonical_sha`), reused here rather than a local
    copy; ``sort_keys`` neutralizes the assembly order so the digest is
    byte-identical to :func:`state.conformance.canonical_content_sha`.
    """
    return determinism.canonical_sha(
        {"labels": dict(labels), "observed_at": observed_at, "payload": dict(payload)}
    )


def _require_flat_scalar_dict(value: Any, *, what: str) -> dict[str, Any]:
    """Refuse anything but a flat dict of opaque scalars (identity/range/counted)."""
    if not isinstance(value, dict):
        raise errors.SpecInvalid(f"conformance: {what} must be an object; got {value!r}")
    for key, scalar in value.items():
        if not isinstance(key, str) or not key:
            raise errors.SpecInvalid(
                f"conformance: {what} keys must be non-empty strings; got {key!r}"
            )
        if isinstance(scalar, (dict, list)):
            raise errors.SpecInvalid(
                f"conformance: {what}[{key!r}] must be an opaque scalar, not a container; "
                f"got {type(scalar).__name__}"
            )
    return value


def _validate_observation_shape(record: dict[str, Any]) -> None:
    """Validate the C-store record shape (schema_version 1) or refuse loudly.

    Shape ONLY — never reads a payload key, label, or ``status_at_record`` value
    for meaning. The attestation fields (``attestor`` / ``subject_kind`` /
    ``subject_id`` / ``content_sha``) are re-checked by
    :func:`~hpc_agent.state.attestation.bind`; this pins the C-store-specific
    fields ``bind`` does not know: ``schema_version``, the literal
    ``attestor`` / ``subject_kind`` values, the ``registration`` sub-block,
    ``observed_at``, and the opaque ``payload`` / ``labels``.
    """
    if not isinstance(record, dict):
        raise errors.SpecInvalid(
            f"conformance: observation must be an object; got {type(record).__name__}"
        )

    if record.get("schema_version") != SCHEMA_VERSION:
        raise errors.SpecInvalid(
            f"conformance: schema_version must be {SCHEMA_VERSION}; got "
            f"{record.get('schema_version')!r}"
        )
    if record.get("attestor") != ATTESTOR:
        raise errors.SpecInvalid(
            f"conformance: attestor must be {ATTESTOR!r} (every observation is a code "
            f"attestation); got {record.get('attestor')!r}"
        )
    if record.get("subject_kind") != SUBJECT_KIND:
        got_kind = record.get("subject_kind")
        raise errors.SpecInvalid(
            f"conformance: subject_kind must be {SUBJECT_KIND!r}; got {got_kind!r}"
        )

    subject_id = record.get("subject_id")
    _validate_registration_id(subject_id)

    observed_at = record.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        raise errors.SpecInvalid(
            f"conformance: observed_at must be a non-empty ISO string; got {observed_at!r}"
        )

    # ``labels`` is optional and opaque; absent → {} (normalized in place so the
    # server sha is over a stable shape). ``payload`` is required and opaque.
    labels = record.get("labels")
    if labels is None:
        labels = {}
        record["labels"] = labels
    _require_flat_scalar_dict(labels, what="labels")

    if "payload" not in record:
        raise errors.SpecInvalid("conformance: payload is required")
    _require_flat_scalar_dict(record["payload"], what="payload")

    registration = record.get("registration")
    if not isinstance(registration, dict):
        raise errors.SpecInvalid(
            f"conformance: registration block must be an object; got {registration!r}"
        )
    for field in _REGISTRATION_FIELDS:
        val = registration.get(field)
        if not isinstance(val, str) or not val:
            raise errors.SpecInvalid(
                f"conformance: registration.{field} must be a non-empty string "
                f"(the caller stamps status_at_record — T4); got {val!r}"
            )
    if registration["registration_id"] != subject_id:
        raise errors.SpecInvalid(
            f"conformance: registration.registration_id {registration['registration_id']!r} must "
            f"match subject_id {subject_id!r} — the observation and its registration block name "
            "the same hypothesis."
        )

    emitter = record.get("emitter")
    if emitter is not None and (not isinstance(emitter, str) or not emitter):
        raise errors.SpecInvalid(
            f"conformance: emitter, when present, must be a non-empty string; got {emitter!r}"
        )
