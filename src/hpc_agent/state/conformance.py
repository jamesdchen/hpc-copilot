"""Live conformance — the pure judgment kernel (registration conformance).

Design origin: ``docs/design/live-conformance.md`` (Wave A, task T1). A
registration is a hypothesis; production is the experiment that never stops.
This module is the pure, I/O-free core that judges the live evidence stream
against the REGISTERED evidence: the observation record model + validation, the
canonical payload sha, the conformance-declaration validator (structure-only),
the baseline-row/envelope shapes, the window-selection arithmetic, and the ONE
comparator :func:`judge_window`.

Naming warning (recorded in the plan's C-verbs section): the package name
``conformance`` is ALSO claimed by the future HARNESS conformance kit
(``docs/design/conformance-kit.md``, ``src/hpc_agent/conformance/``) — a
DIFFERENT subject. THIS module is REGISTRATION conformance (the SPC watchdog).
The paths are disjoint and importable side by side; the collision is cognitive.

The design center is **statistical process control rebuilt on attestations**
(the plan's canonical lineage): the chart JUDGES, the operator ADJUSTS. Core
ships only the per-point and per-window comparison arithmetic (control LIMITS);
sequential run-rules, alarm policy, and every actuation are caller/pack
territory (control RULES), forever. This module OBSERVES and JUDGES; it never
actuates, never mutates a registration, never reaches an external system.

The honest comparison semantics (plan C-compare), verbatim:

* The registered side is a SEALED, point-in-time order-statistics envelope over
  the baseline rows — observed ``[min, max]`` plus a derived relative spread,
  labeled with its evidence ``n``. It never grows: live observations NEVER widen
  it (no admission path — re-baselining is re-registration, the full human bar).
* The live side is an explicit window selection over the receipt ledger; per
  declared key it reduces to its OWN order statistics + n + the distinct label
  sets observed.
* Only comparison arithmetic runs: range containment, ``window_n >=
  min_window_n``, ``baseline_n >= 3`` (the fingerprint's well-evidenced bar,
  reused — the ONLY mechanized numeric threshold). Thin/novel/incomparable
  evidence routes to the human in BOTH directions; a σ / fitted parameter is
  never fabricated. No control RULES over the verdict stream.

.. note:: **T1a re-point (one envelope definition).** The per-key order-statistics
   envelope is the SHARED helper ``state/determinism.py::order_statistics_envelope``
   (fingerprint T1 / the plan's T1a) so both the fingerprint reduction and
   :func:`judge_window` route through ONE definition — never a second
   min/max/spread implementation (enforcement row). Now that the determinism
   fingerprint has landed, :func:`_order_statistics_envelope` is a thin alias
   that wraps that shared ``(lo, hi, rel_spread)`` leg with this module's
   :class:`Envelope` evidence count ``n``. Both the baseline and the live-window
   envelopes route through it; :func:`judge_window` never re-inlines min/max. The
   route-through pin in the tests holds the invariant.

Pure, no I/O: this module reads no file, holds no SSH / ``_wire`` / scheduler
import, and — the plan's first-class agency boundary — reaches no broker,
instrument, or external system. It routes shared invariants through
``state/attestation.py`` and reuses ``state/registration.py``'s status
vocabulary; nothing else.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hpc_agent import errors
from hpc_agent.state import attestation, determinism, registration

__all__ = [
    "SCHEMA_VERSION",
    "ATTESTOR",
    "SUBJECT_KIND",
    "STATUS_AT_RECORD",
    "CONFORMING",
    "NEEDS_VERDICT",
    "NONCONFORMING",
    "TIERS",
    "WITHIN_ENVELOPE",
    "OUTSIDE_ENVELOPE",
    "INSUFFICIENT_WINDOW",
    "THIN_BASELINE",
    "KEY_NOVELTY",
    "LABEL_NOVELTY",
    "INCOMPARABLE",
    "TIER_REASONS",
    "WELL_EVIDENCED_MIN_N",
    "BaselineRef",
    "ConformanceDeclaration",
    "Envelope",
    "KeyVerdict",
    "ConformanceReport",
    "canonical_content_sha",
    "build_observation_record",
    "validate_observation",
    "to_attestation",
    "validate_declaration",
    "parse_baseline_rows",
    "judge_window",
]

# --- the record vocabulary (C-store) ----------------------------------------

#: The observation-record schema version. Bump on shape change (the
#: ``RECEIPT_SCHEMA_VERSION`` convention).
SCHEMA_VERSION = 1

#: Every observation is a CODE attestation (C1): the bind vouches for the exact
#: recorded bytes; truthfulness of the payload is the emitter's (the F8 honesty).
ATTESTOR = "code"

#: The opaque attestation ``subject_kind`` every conformance observation rides.
SUBJECT_KIND = "conformance-observation"

#: The registration statuses an observation may be stamped with at record time
#: (C-store: recording is fail-open — a stale/revoked/superseded registration is
#: RECORDED with its reduced status disclosed, never silently mixed; ``absent``
#: is refused loudly by the record verb, so it is not a recordable status).
#: Reuses ``state/registration.py``'s ONE status vocabulary — never a second set.
STATUS_AT_RECORD = frozenset(registration.STATUSES - {registration.ABSENT})

# --- the tier + tier_reason vocabulary (C2 / C-compare) ---------------------

#: Every declared key sits inside a well-evidenced envelope over a sufficient
#: window — mechanized, zero human attention (a DERIVED verdict, never stored).
CONFORMING = "conforming"

#: Routed to the human WITH calibrated, range-phrased evidence: thin baseline,
#: insufficient window, key/label novelty, or incomparable values.
NEEDS_VERDICT = "needs_verdict"

#: A window outside a well-evidenced envelope — a FINDING. It never mutates the
#: registration's status, revokes nothing, halts nothing (the agency boundary).
NONCONFORMING = "nonconforming"

#: Every overall tier the comparator can yield (mirrors the fingerprint tiers).
TIERS = frozenset({CONFORMING, NEEDS_VERDICT, NONCONFORMING})

#: --- per-key ``tier_reason`` vocabulary (C-compare's fold) ---
WITHIN_ENVELOPE = "within_envelope"
OUTSIDE_ENVELOPE = "outside_envelope"
INSUFFICIENT_WINDOW = "insufficient_window"
THIN_BASELINE = "thin_baseline"
KEY_NOVELTY = "key_novelty"
LABEL_NOVELTY = "label_novelty"
INCOMPARABLE = "incomparable"

#: The CLOSED per-key reason vocabulary. Equality-pinned in the tests.
TIER_REASONS = frozenset(
    {
        WITHIN_ENVELOPE,
        OUTSIDE_ENVELOPE,
        INSUFFICIENT_WINDOW,
        THIN_BASELINE,
        KEY_NOVELTY,
        LABEL_NOVELTY,
        INCOMPARABLE,
    }
)

#: The reasons that route to :data:`NEEDS_VERDICT` (never an auto verdict). The
#: fold: any :data:`OUTSIDE_ENVELOPE` → :data:`NONCONFORMING`; else any of these
#: → :data:`NEEDS_VERDICT`; else :data:`CONFORMING`.
_NEEDS_VERDICT_REASONS = frozenset(
    {INSUFFICIENT_WINDOW, THIN_BASELINE, KEY_NOVELTY, LABEL_NOVELTY, INCOMPARABLE}
)

#: The fingerprint's well-evidenced bar, REUSED (the ONE mechanized evidence
#: threshold — an existing vocabulary, not a new invention). A baseline with
#: fewer than this many observations for a key is THIN and routes to the human,
#: never auto-verdicts. This is the ONLY numeric threshold literal in the module
#: (AST-pinned in the tests).
WELL_EVIDENCED_MIN_N = 3


# --- the canonical payload sha (C-store; harness-contract form) --------------


def canonical_content_sha(
    payload: Mapping[str, Any], labels: Mapping[str, Any], observed_at: str
) -> str:
    """The observation's ``content_sha``: sha-256 over ``{payload, labels, observed_at}``.

    The canonical-JSON sha (C-store) recomputed SERVER-SIDE at append and bound
    via ``state/attestation.py::bind`` — a hash cannot be asserted into
    existence. Pure and deterministic: the same three inputs always yield the
    same hex digest. The json+sha kernel is the ONE harness-contract
    canonicalization (:func:`state.determinism.canonical_sha`), reused here
    rather than a local copy; only the ``{payload, labels, observed_at}``
    assembly lives at this seam.
    """
    return determinism.canonical_sha(
        {"payload": payload, "labels": labels, "observed_at": observed_at}
    )


# --- the observation record model + validation (C-store) --------------------


def build_observation_record(
    *,
    registration_id: str,
    dossier_sha: str,
    status_at_record: str,
    payload: Mapping[str, Any],
    observed_at: str,
    labels: Mapping[str, Any] | None = None,
    emitter: str | None = None,
    ts: str,
) -> dict[str, Any]:
    """Assemble one C-store observation record dict (the ledger line T3 appends).

    Computes the ``content_sha`` over ``{payload, labels, observed_at}`` via
    :func:`canonical_content_sha` (so the sha lives in the kernel, and T3's
    ``bind`` recompute is the same function), stamps the fail-open
    ``status_at_record``, and returns the record verbatim in the C-store shape.
    Validated as a side-effect (via :func:`validate_observation`) so a malformed
    assembly refuses at build time, not at read time.
    """
    labels = dict(labels or {})
    record: dict[str, Any] = {
        "ts": ts,
        "schema_version": SCHEMA_VERSION,
        "attestor": ATTESTOR,
        "subject_kind": SUBJECT_KIND,
        "subject_id": registration_id,
        "content_sha": canonical_content_sha(payload, labels, observed_at),
        "registration": {
            "registration_id": registration_id,
            "dossier_sha": dossier_sha,
            "status_at_record": status_at_record,
        },
        "observed_at": observed_at,
        "labels": labels,
        "payload": dict(payload),
        "emitter": emitter,
    }
    validate_observation(record)
    return record


@dataclass(frozen=True)
class _ValidatedObservation:
    """A validated observation record — the fields the comparator reads.

    ``payload`` / ``labels`` are OPAQUE (identity-compared, range-compared,
    counted — never read for meaning). ``registration`` is the C-store block.
    """

    registration_id: str
    content_sha: str
    observed_at: str
    payload: Mapping[str, Any]
    labels: Mapping[str, Any]
    emitter: str | None
    status_at_record: str
    dossier_sha: str


def validate_observation(record: Mapping[str, Any]) -> _ValidatedObservation:
    """Validate a C-store observation record dict, or refuse loudly.

    Enforces the record SHAPE only (the load-bearing C-store invariants):
    ``schema_version == 1``, the ``attestor``/``subject_kind`` literals, a
    non-empty ``subject_id`` / ``content_sha``, the ``registration`` block
    (``registration_id`` matching ``subject_id``, a non-empty ``dossier_sha``, a
    ``status_at_record`` in :data:`STATUS_AT_RECORD`), a non-empty ISO
    ``observed_at``, and opaque mapping ``labels`` / ``payload``. The shared
    invariants route through :func:`to_attestation` (the ONE attestation kernel)
    so an observation is a code attestation like every other trusted record.

    Never interprets ``payload`` / ``labels`` for meaning. Raises
    :class:`errors.SpecInvalid` naming the offending field.
    """
    if not isinstance(record, Mapping):
        raise errors.SpecInvalid(
            f"conformance observation: record must be a mapping; got {type(record).__name__}"
        )
    if record.get("schema_version") != SCHEMA_VERSION:
        raise errors.SpecInvalid(
            f"conformance observation: schema_version must be {SCHEMA_VERSION}; "
            f"got {record.get('schema_version')!r}"
        )
    subject_kind = record.get("subject_kind")
    if subject_kind != SUBJECT_KIND:
        raise errors.SpecInvalid(
            f"conformance observation: subject_kind must be {SUBJECT_KIND!r}; got {subject_kind!r}"
        )
    attestor = record.get("attestor")
    if attestor != ATTESTOR:
        raise errors.SpecInvalid(
            f"conformance observation: attestor must be {ATTESTOR!r} (an observation is a code "
            f"attestation — the bind vouches for the bytes, the emitter for the truth); "
            f"got {attestor!r}"
        )

    # Route the shared invariants (attestor literal, non-empty subject_id /
    # content_sha) through the ONE attestation kernel — never a re-inlined check.
    att = to_attestation(record)

    observed_at = record.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        raise errors.SpecInvalid(
            f"conformance observation: 'observed_at' must be a non-empty ISO string; "
            f"got {observed_at!r}"
        )

    reg = record.get("registration")
    if not isinstance(reg, Mapping):
        raise errors.SpecInvalid(
            f"conformance observation: 'registration' must be a mapping; got {reg!r}"
        )
    reg_id = reg.get("registration_id")
    if not isinstance(reg_id, str) or not reg_id:
        raise errors.SpecInvalid(
            f"conformance observation: registration.registration_id must be a non-empty "
            f"string; got {reg_id!r}"
        )
    if reg_id != att.subject_id:
        raise errors.SpecInvalid(
            f"conformance observation: registration.registration_id {reg_id!r} must equal "
            f"subject_id {att.subject_id!r} (the subject under test is the registration)"
        )
    dossier_sha = reg.get("dossier_sha")
    if not isinstance(dossier_sha, str) or not dossier_sha:
        raise errors.SpecInvalid(
            f"conformance observation: registration.dossier_sha must be a non-empty string; "
            f"got {dossier_sha!r}"
        )
    status_at_record = reg.get("status_at_record")
    if status_at_record not in STATUS_AT_RECORD:
        raise errors.SpecInvalid(
            f"conformance observation: registration.status_at_record must be one of "
            f"{sorted(STATUS_AT_RECORD)}; got {status_at_record!r}"
        )

    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise errors.SpecInvalid(
            f"conformance observation: 'payload' must be a mapping of opaque scalars; "
            f"got {payload!r}"
        )
    labels = record.get("labels", {})
    if not isinstance(labels, Mapping):
        raise errors.SpecInvalid(
            f"conformance observation: 'labels' must be a mapping when present; got {labels!r}"
        )
    emitter = record.get("emitter")
    if emitter is not None and (not isinstance(emitter, str) or not emitter):
        raise errors.SpecInvalid(
            f"conformance observation: 'emitter', when present, must be a non-empty string; "
            f"got {emitter!r}"
        )

    return _ValidatedObservation(
        registration_id=reg_id,
        content_sha=att.content_sha,
        observed_at=observed_at,
        payload=payload,
        labels=labels,
        emitter=emitter,
        status_at_record=status_at_record,
        dossier_sha=dossier_sha,
    )


def to_attestation(record: Mapping[str, Any]) -> attestation.Attestation:
    """Project an observation record to the shared :class:`attestation.Attestation`.

    The C1 route-through: an observation is a CODE attestation, so the shared
    invariants (the ``attestor`` literal, a non-empty ``subject_id`` /
    ``content_sha``) are enforced by ``state/attestation.py::validate`` — never a
    re-inlined check. The store layer's ``bind`` recomputes the ``content_sha``
    server-side; this projection only validates the shape.
    """
    return attestation.validate(
        {
            "attestor": record.get("attestor"),
            "subject_kind": record.get("subject_kind"),
            "subject_id": record.get("subject_id"),
            "content_sha": record.get("content_sha"),
        }
    )


# --- the conformance declaration (C-declare, structure-only) ----------------


@dataclass(frozen=True)
class BaselineRef:
    """The sealed baseline artifact reference (C-declare): ``{path, sha256}``.

    ``path`` is a relpath inside the sealed dossier; ``sha256`` is that entry's
    manifest sha. STRUCTURE-only here — the dossier-membership check (that the
    pair is a MEMBER of the dossier's manifest entries) is the append gate's
    recompute leg (plan T7), NOT this validator: the state substrate never
    imports ``ops``.
    """

    path: str
    sha256: str


@dataclass(frozen=True)
class ConformanceDeclaration:
    """A validated conformance declaration (C-declare), structure-only.

    * ``baseline`` — the sealed artifact carrying the registered samples.
    * ``keys`` — the caller-declared key set the comparator judges (opaque
      slugs). EMPTY → every key present in the baseline (disclosed at judge time).
    * ``min_window_n`` — the caller-declared live-window evidence floor (COUNTING
      against a caller number; core never picks it).
    * ``review_horizon`` — an optional caller-computed ISO timestamp (C4); core
      compares timestamps, never names a period.
    """

    baseline: BaselineRef
    keys: tuple[str, ...]
    min_window_n: int
    review_horizon: str | None = None


_DECLARATION_KEYS = frozenset({"baseline", "keys", "min_window_n", "review_horizon"})
_BASELINE_KEYS = frozenset({"path", "sha256"})


def validate_declaration(raw: Mapping[str, Any]) -> ConformanceDeclaration:
    """Validate a ``conformance`` declaration block, structure-only, or refuse.

    C-declare, verbatim: unknown keys in the block (or in ``baseline``) are a
    LOUD :class:`errors.SpecInvalid` (the R4 dangling-reference posture — an
    opted-in requirement core cannot check must never silently pass). ``keys``
    empty/absent means "every baseline key" (disclosed later, not defaulted
    here). ``min_window_n`` is a positive int (a caller number, never core's).
    ``review_horizon`` is an optional non-empty ISO string. NO dossier-membership
    check here — that is the append gate's recompute leg (T7).
    """
    if not isinstance(raw, Mapping):
        raise errors.SpecInvalid(
            f"conformance declaration: must be a mapping; got {type(raw).__name__}"
        )
    unknown = set(raw) - _DECLARATION_KEYS
    if unknown:
        raise errors.SpecInvalid(
            f"conformance declaration: unknown key(s) {sorted(unknown)} — an opted-in "
            f"requirement core cannot check must never silently pass; allowed keys are "
            f"{sorted(_DECLARATION_KEYS)}"
        )

    raw_baseline = raw.get("baseline")
    if not isinstance(raw_baseline, Mapping):
        raise errors.SpecInvalid(
            f"conformance declaration: 'baseline' must be a {{path, sha256}} mapping; "
            f"got {raw_baseline!r}"
        )
    unknown_baseline = set(raw_baseline) - _BASELINE_KEYS
    if unknown_baseline:
        raise errors.SpecInvalid(
            f"conformance declaration: baseline unknown key(s) {sorted(unknown_baseline)}; "
            f"allowed keys are {sorted(_BASELINE_KEYS)}"
        )
    path = raw_baseline.get("path")
    if not isinstance(path, str) or not path:
        raise errors.SpecInvalid(
            f"conformance declaration: baseline.path must be a non-empty string; got {path!r}"
        )
    sha256 = raw_baseline.get("sha256")
    if not isinstance(sha256, str) or not sha256:
        raise errors.SpecInvalid(
            f"conformance declaration: baseline.sha256 must be a non-empty string; got {sha256!r}"
        )

    raw_keys = raw.get("keys", [])
    if not isinstance(raw_keys, list):
        raise errors.SpecInvalid(
            f"conformance declaration: 'keys' must be a list when present; got {raw_keys!r}"
        )
    keys: list[str] = []
    seen: set[str] = set()
    for entry in raw_keys:
        if not isinstance(entry, str) or not entry:
            raise errors.SpecInvalid(
                f"conformance declaration: each key must be a non-empty slug; got {entry!r}"
            )
        if entry in seen:
            raise errors.SpecInvalid(
                f"conformance declaration: duplicate key {entry!r} (each declared key is "
                "counted once)"
            )
        seen.add(entry)
        keys.append(entry)

    min_window_n = raw.get("min_window_n")
    if not isinstance(min_window_n, int) or isinstance(min_window_n, bool) or min_window_n < 1:
        raise errors.SpecInvalid(
            f"conformance declaration: 'min_window_n' must be a positive integer (the "
            f"caller-declared live-window evidence floor); got {min_window_n!r}"
        )

    review_horizon = raw.get("review_horizon")
    if review_horizon is not None and (not isinstance(review_horizon, str) or not review_horizon):
        raise errors.SpecInvalid(
            f"conformance declaration: 'review_horizon', when present, must be a non-empty "
            f"ISO string; got {review_horizon!r}"
        )

    return ConformanceDeclaration(
        baseline=BaselineRef(path=path, sha256=sha256),
        keys=tuple(keys),
        min_window_n=min_window_n,
        review_horizon=review_horizon,
    )


# --- baseline rows + the order-statistics envelope --------------------------


def parse_baseline_rows(rows: Any) -> tuple[dict[str, Any], ...]:
    """Validate the baseline-row loading SHAPE: a list of ``{key: scalar}`` rows.

    Each row is a mapping of opaque metric keys to opaque scalar values (a
    backtest's per-period metrics; a calibration run's readings). Core validates
    only that the container is a list of mappings — values are opaque (the
    envelope reads them as order statistics, never for meaning). Raises
    :class:`errors.SpecInvalid` on a non-list or a non-mapping row.
    """
    if not isinstance(rows, list):
        raise errors.SpecInvalid(
            f"conformance baseline: rows must be a list of {{key: scalar}} mappings; "
            f"got {type(rows).__name__}"
        )
    parsed: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise errors.SpecInvalid(
                f"conformance baseline: row[{i}] must be a mapping; got {row!r}"
            )
        parsed.append(dict(row))
    return tuple(parsed)


@dataclass(frozen=True)
class Envelope:
    """A per-key order-statistics envelope (C-compare's registered/live side).

    ``lo`` / ``hi`` are the observed ``[min, max]``; ``rel_spread`` is the
    derived relative spread (order statistics ONLY — no fitted distribution, no
    mean±kσ); ``n`` is the evidence count. Range-phrased, never sigma-phrased.
    """

    lo: float
    hi: float
    rel_spread: float
    n: int


def _order_statistics_envelope(values: Sequence[float]) -> Envelope:
    """The per-key order-statistics envelope (baseline AND live window).

    Routes through the ONE shared order-statistics leg,
    :func:`state.determinism.order_statistics_envelope` (fingerprint T1 / the
    plan's T1a): the fingerprint reduction and :func:`judge_window` share ONE
    envelope definition (enforcement row) — never a second min/max/spread
    implementation. This thin alias only wraps the shared ``(lo, hi, rel_spread)``
    leg with the conformance :class:`Envelope`'s evidence count ``n``. ``values``
    must be non-empty and pre-filtered to comparable finite numbers (the caller
    handles incomparability upstream).
    """
    lo, hi, rel_spread = determinism.order_statistics_envelope(values)
    return Envelope(lo=lo, hi=hi, rel_spread=rel_spread, n=len(values))


# --- the comparator: judge_window (C-compare) -------------------------------


@dataclass(frozen=True)
class KeyVerdict:
    """The per-key verdict (C-compare): a ``tier_reason`` + both sides' evidence.

    ``within`` is the range-containment result (``None`` when the key was not
    range-compared — thin/novel/incomparable/insufficient). ``baseline`` /
    ``window`` are the two order-statistics envelopes (``None`` when a side had
    no comparable evidence). ``label_sets`` are the DISTINCT label sets observed
    in the window — disclosed evidence, never interpreted. Everything
    range-phrased; NO σ, NO fitted parameter.
    """

    key: str
    tier_reason: str
    within: bool | None
    baseline: Envelope | None
    window: Envelope | None
    baseline_n: int
    window_n: int
    label_sets: tuple[tuple[tuple[str, Any], ...], ...] = ()


@dataclass(frozen=True)
class ConformanceReport:
    """The comparator's result (C-compare): the fold + per-key verdicts.

    ``tier`` is the overall fold; ``keys`` are the per-key verdicts (one per
    declared key, in declaration order); ``window_n`` / ``min_window_n`` are the
    window's evidence and the caller floor; ``as_of`` is the ``now`` the caller
    threaded (the evidence-memory ``as_of`` posture — disclosed, never a default
    window bound). ``keys_from_baseline`` records whether the judged key set was
    disclosed from the baseline (empty declaration) rather than caller-declared.
    """

    tier: str
    keys: tuple[KeyVerdict, ...]
    window_n: int
    min_window_n: int
    as_of: str
    keys_from_baseline: bool


def _is_finite_number(value: Any) -> bool:
    """True iff *value* is a finite real number (int/float, not bool, not NaN/inf).

    The incomparability test (C-compare): a non-number, a NaN, or a type change
    is INCOMPARABLE — routed to the human, never coerced.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _label_key(labels: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """A hashable, order-stable identity for a receipt's opaque label set."""
    return tuple(sorted(labels.items()))


def _judge_key(
    key: str,
    baseline_rows: Sequence[Mapping[str, Any]],
    window: Sequence[Mapping[str, Any]],
    *,
    window_sufficient: bool,
    min_window_n: int,
) -> KeyVerdict:
    """Judge ONE declared key (C-compare arithmetic) → a :class:`KeyVerdict`.

    Precedence (all mechanical properties of the records, never a closeness
    judgment): insufficient window (raw receipt count) → key/window presence
    novelty → insufficient PER-KEY comparable evidence → incomparable values →
    thin baseline → label novelty → range containment. The range comparison runs
    ONLY when the window is sufficient, both sides are present, the key's own
    comparable evidence meets ``min_window_n``, every value is a comparable finite
    number, and the baseline is well-evidenced.

    The per-key floor (``window_n >= min_window_n`` on the key's comparable
    values, not the raw receipt count) is what keeps a key carried by a single
    receipt in a heterogeneous window from auto-verdicting from n=1 — the pinned
    'a thin window never auto-verdicts in either direction' contract.
    """
    baseline_values = [row[key] for row in baseline_rows if key in row]
    window_values = [r["payload"][key] for r in window if key in _payload(r)]
    label_sets = tuple(dict.fromkeys(_label_key(_labels(r)) for r in window))
    baseline_n = len(baseline_values)
    window_n = len(window_values)

    baseline_env: Envelope | None = None
    window_env: Envelope | None = None

    def verdict(reason: str, within: bool | None) -> KeyVerdict:
        return KeyVerdict(
            key=key,
            tier_reason=reason,
            within=within,
            baseline=baseline_env,
            window=window_env,
            baseline_n=baseline_n,
            window_n=window_n,
            label_sets=label_sets,
        )

    if not window_sufficient:
        # Insufficient window routes to needs_verdict in BOTH directions — a
        # verdict is never fabricated from evidence disclosed as insufficient.
        return verdict(INSUFFICIENT_WINDOW, None)

    if not baseline_values or not window_values:
        # A live key the baseline never carried, or a baseline key the window
        # never carried — key novelty, disclosed.
        return verdict(KEY_NOVELTY, None)

    if window_n < min_window_n:
        # The raw receipt count cleared the floor, but THIS key is carried by
        # fewer than min_window_n receipts (a heterogeneous window) — the key's
        # own comparable evidence is thin, so it never auto-verdicts. Disclosed
        # as INSUFFICIENT_WINDOW next to window_n, not folded to a verdict.
        return verdict(INSUFFICIENT_WINDOW, None)

    if not all(_is_finite_number(v) for v in baseline_values) or not all(
        _is_finite_number(v) for v in window_values
    ):
        # NaN / non-number / type change — incomparable, routed to the human.
        return verdict(INCOMPARABLE, None)

    baseline_env = _order_statistics_envelope(baseline_values)
    window_env = _order_statistics_envelope(window_values)

    if baseline_n < WELL_EVIDENCED_MIN_N:
        # A thin baseline never auto-verdicts (inside OR outside).
        return verdict(THIN_BASELINE, None)

    if len(label_sets) > 1:
        # A heterogeneous window mixes disclosed regimes/venues — label novelty.
        return verdict(LABEL_NOVELTY, None)

    within = baseline_env.lo <= window_env.lo and window_env.hi <= baseline_env.hi
    return verdict(WITHIN_ENVELOPE if within else OUTSIDE_ENVELOPE, within)


def judge_window(
    baseline_rows: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    declaration: ConformanceDeclaration,
    *,
    now: str,
) -> ConformanceReport:
    """Judge a live window against the SEALED baseline — the ONE comparator.

    Pure, no I/O (C-compare). *baseline_rows* are the sealed baseline's rows
    (never grown — live receipts NEVER enter them, so no admission path exists);
    *receipts* are the already-selected live window (via
    :func:`hpc_agent.state.conformance_store.select_window`);
    *declaration* carries the judged key set and the ``min_window_n`` floor;
    *now* is the caller's ``as_of`` timestamp (threaded for deterministic
    disclosure, never a fabricated window bound).

    Does ONLY comparison arithmetic: per-key range containment, ``window_n >=
    min_window_n``, and the ``baseline_n >= 3`` well-evidenced bar. Every verdict
    is dual-labeled with both sides' order statistics + ns (range-phrased, no
    σ). Ships NO control RULES — no sequence logic over the verdict stream, so N
    consecutive inside points is exactly N conforming reads. The fold: any
    sufficiently-evidenced key exiting a well-evidenced envelope →
    :data:`NONCONFORMING`; else any thin/novel/incomparable/insufficient key →
    :data:`NEEDS_VERDICT`; else :data:`CONFORMING`.
    """
    window_n = len(receipts)
    window_sufficient = window_n >= declaration.min_window_n

    # Declared keys, or — empty declaration — every key present in the baseline
    # (disclosed, order-stable across the rows).
    keys_from_baseline = not declaration.keys
    if keys_from_baseline:
        judged_keys: list[str] = list(dict.fromkeys(k for row in baseline_rows for k in row))
    else:
        judged_keys = list(declaration.keys)

    verdicts = tuple(
        _judge_key(
            key,
            baseline_rows,
            receipts,
            window_sufficient=window_sufficient,
            min_window_n=declaration.min_window_n,
        )
        for key in judged_keys
    )

    if any(v.tier_reason == OUTSIDE_ENVELOPE for v in verdicts):
        tier = NONCONFORMING
    elif any(v.tier_reason in _NEEDS_VERDICT_REASONS for v in verdicts):
        tier = NEEDS_VERDICT
    else:
        tier = CONFORMING

    return ConformanceReport(
        tier=tier,
        keys=verdicts,
        window_n=window_n,
        min_window_n=declaration.min_window_n,
        as_of=now,
        keys_from_baseline=keys_from_baseline,
    )


def _payload(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    """The receipt's ``payload`` mapping, or ``{}`` (tolerant read)."""
    payload = receipt.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _labels(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    """The receipt's ``labels`` mapping, or ``{}`` (tolerant read)."""
    labels = receipt.get("labels")
    return labels if isinstance(labels, Mapping) else {}
