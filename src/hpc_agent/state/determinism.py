"""The determinism-fingerprint kernel — pure, like the attestation kernel.

Design origin: ``docs/design/determinism-fingerprint.md`` (T1, Wave A). This
module is the PURE substrate the fingerprint machinery shares: the sample
record model + validation (projecting to ``state/attestation.py::validate``
records), the canonical content-sha over two compared metrics payloads, the
STATIC structural classifier, the all-samples envelope reduction (order
statistics only), the CURRENT-identity filter, the tiered verdict classifier,
and the ``evidence_meets`` registration predicate. Like
``state.attestation`` it reaches no SSH, no ``_wire``, no filesystem — the
store layer (T3) owns the ledger I/O and the admission JOIN; this module is
pure over ``(samples, admitted_flags)``-shaped input.

**The boundary this module keeps.** Core measures, classifies by STRUCTURE,
and compares — it never names a metric, never privileges one, and never
INVENTS a tolerance. Every number in an envelope is an OBSERVATION; a float
with no measured evidence and no caller override compares EXACTLY (the
no-invented-tolerance rule). The envelope is the OBSERVED RANGE (per-key
min/max + the derived relative spread) — order statistics ONLY, never a fitted
distribution at any n.

**The n=2 honesty — why the envelope must stay LABELED.** The double canary's
n=2 at submit is a labeled PRIOR, not a truth. Three recorded n=2 failure
modes (carried verbatim from the design's decision center 2 — they are WHY the
classifier's well-evidenced bar exists and why a thin envelope only ever routes
to the human, never to a wrong auto-verdict in either direction):

1. **Rare-event nondeterminism** — a race or rare branch that fires once in
   many runs looks ``exact`` at n=2 and is not.
2. **Canary-scale != main-scale regimes** — BLAS/GPU libraries select
   algorithms by problem size; a 1-task canary's spread can differ in kind
   from the main array's. Samples record a ``scale`` label; an envelope with
   only canary-scale evidence is THIN for a main-scale verdict.
3. **Same-node correlated samples** — the double canary's two executions may
   land on the same node/SKU; the n=2 prior records ``same_submission: true``
   so the classifier treats it as one environment observed twice, not two.

Precedence per key (settled 2026-07-07): caller ``tolerance`` override (labeled
``caller_override``, disclosed) > well-evidenced envelope > thin envelope >
exact.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state import attestation

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "SAMPLE_SCHEMA_VERSION",
    "SUBJECT_KIND",
    "SOURCES",
    "SCALES",
    "SAMPLE_VERDICTS",
    "STATIC_CLASSES",
    "EXACT",
    "STOCHASTIC",
    "AUTO_CLEARED",
    "NEEDS_VERDICT",
    "MISMATCH",
    "INCOMPARABLE",
    "WELL_EVIDENCED_MIN_N",
    "IDENTITY_FIELDS",
    "DATA_IDENTITY_FIELD",
    "PerKeyDiff",
    "Sample",
    "Evidence",
    "KeyEnvelope",
    "Envelope",
    "KeyVerdict",
    "Classification",
    "FilterResult",
    "canonical_sha",
    "compute_content_sha",
    "static_class",
    "flatten_metrics",
    "diff_metrics",
    "build_sample_record",
    "validate_sample",
    "filter_current_identity",
    "order_statistics_envelope",
    "reduce_envelope",
    "classify",
    "evidence_meets",
]

# --- vocabulary (opaque strings, never metric names) -------------------------

#: Sample record schema version (append-only ledger; bump on shape change).
SAMPLE_SCHEMA_VERSION = 1

#: The attestation ``subject_kind`` every fingerprint sample carries.
SUBJECT_KIND = "determinism-fingerprint"

#: How a sample was produced. ``double-canary`` is the n=2 submit prior;
#: ``verify-reproduction`` is a later accumulating comparison.
SOURCES = frozenset({"double-canary", "verify-reproduction"})

#: Mechanically-assigned scale label (never judged): a canary is 1-task,
#: a verify-reproduction is main-scale (a partial reproduction is main-scale
#: with ``partial: true`` — partiality and scale are separate axes).
SCALES = frozenset({"canary", "main"})

#: The comparison verdict AT APPEND (judgment always precedes append).
SAMPLE_VERDICTS = frozenset({"auto_cleared", "needs_verdict", "mismatch"})

#: The static structural classes a flattened metric leaf takes. Only ``float``
#: is tolerance-class-eligible; the rest ALWAYS compare exactly.
STATIC_CLASSES = frozenset({"float", "int", "str", "bool", "shape"})

#: Per-key envelope classes.
EXACT = "exact"
STOCHASTIC = "stochastic"

#: Overall tier verdicts (``stage_reached``). ``INCOMPARABLE`` is folded by the
#: consumer (missing artifacts); the classifier here emits the other three.
AUTO_CLEARED = "auto_cleared"
NEEDS_VERDICT = "needs_verdict"
MISMATCH = "mismatch"
INCOMPARABLE = "incomparable"

#: A stochastic envelope is WELL-EVIDENCED only at n>=3 (plus scale + cluster
#: coverage, checked per comparison). This is an evidence-weight bar, NOT a
#: tolerance — it never widens or narrows an observed range.
WELL_EVIDENCED_MIN_N = 3

#: The code-identity fields a sample must match to be CURRENT for a comparison
#: (lifted verbatim, the ``_IDENTITY_FIELDS`` discipline). A drift on any reads
#: prior samples STALE (excluded from the envelope, retained as history).
IDENTITY_FIELDS: tuple[str, ...] = ("cmd_sha", "tasks_py_sha", "executor")

#: The optional data-identity leg (Amendment 1). Present-and-differing samples
#: are excluded as DATA DRIFT and disclosed; an absent field is "data identity
#: unknown" — disclosed, never fabricated, never blocking.
DATA_IDENTITY_FIELD = "data_sha"

# tier_reason vocabulary (D-verdict-wire), for reference:
#   "exact" | "within_evidenced_envelope" | "within_thin_envelope"
#   | "outside_thin_envelope" | "outside_evidenced_envelope"
#   | "caller_override" | None

_ALLOWED_DEMAND_KEYS = frozenset({"min_n", "min_n_full", "scales", "clusters"})


# --- canonical sha (the harness-contract form) -------------------------------


def canonical_sha(obj: Any) -> str:
    """SHA-256 (lowercase hex) over the harness-contract canonical JSON form.

    The one canonicalization every content sha in the system uses
    (``docs/internals/harness-contract.md`` §"The sha canonicalization"):
    ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)``,
    UTF-8 encoded, SHA-256 lowercase hex. ``state`` may not import ``ops``, so
    this reproduces that form byte-for-byte rather than importing a helper — the
    conformance suite pins the two agree.
    """
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_content_sha(payload_a: Any, payload_b: Any) -> str:
    """Canonical content-sha over the TWO compared on-disk metrics payloads.

    The ordered pair ``[a, b]`` (the ``run_ids`` order) is canonicalized once so
    the sample's ``content_sha`` is a stable fingerprint of exactly what was
    compared — the value T3/T4 recompute at ``attestation.bind`` time, so a
    spread cannot be asserted into existence over artifacts that never existed.
    """
    return canonical_sha([payload_a, payload_b])


# --- the static classifier + flattening (EXTRACTED from ops, byte-faithful) --


def _is_number(value: Any) -> bool:
    """True for a real numeric value — ``bool`` excluded (compares by equality).

    ``bool`` is an ``int`` subclass; excluding it keeps ``True``/``False`` on
    the equality-only path (the ``verify_reproduction._is_number`` convention,
    extracted here so ``state`` never imports ``ops``).
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_nan(value: Any) -> bool:
    """True only for a float NaN (never raises for ints / large values)."""
    return isinstance(value, float) and math.isnan(value)


def static_class(value: Any) -> str:
    """Classify ONE flattened metric leaf by structure/type.

    ``bool`` is checked before ``int`` (it is an ``int`` subclass). Anything
    that is not a scalar leaf — a list, dict, ``None`` — is ``"shape"`` (a
    structural value that ALWAYS compares exactly, never envelope-eligible).
    Only ``"float"`` is tolerance-class-eligible.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "shape"


def flatten_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a nested metrics mapping to scalar leaves, joining keys with ``.``.

    EXTRACTED byte-faithfully from
    ``ops/verify_reproduction.py::_flatten_metrics`` (T5 re-points that module's
    own copy at this one — ``state`` never imports ``ops``, so the temporary
    duplication is plan-sanctioned, drift-log item 6). Recurses into dict values
    only; non-dict leaves (numbers, strings, lists) are preserved RAW so the
    comparator/classifier sees non-numeric values and applies its rules rather
    than dropping them.
    """
    flat: dict[str, Any] = {}
    for key, value in metrics.items():
        skey = str(key)
        if isinstance(value, dict):
            for sub_key, sub_val in flatten_metrics(value).items():
                flat[f"{skey}.{sub_key}"] = sub_val
        else:
            flat[skey] = value
    return flat


# --- the per-key diff (raw observation, no verdict) --------------------------


@dataclass(frozen=True)
class PerKeyDiff:
    """One key's raw observed diff between two payloads — the D-store ``per_key``.

    Carries the OBSERVATION only (``a``, ``b``, ``abs_diff``, ``rel_diff``,
    ``static_class``) — never a verdict; the classifier assigns tiers. ``comparable``
    is a derived convenience (one-sided / NaN / type-changed → False) that the
    classifier reads; it is NOT part of the serialized D-store shape.
    """

    key: str
    a: Any
    b: Any
    abs_diff: float | None
    rel_diff: float | None
    static_class: str
    comparable: bool = True

    def to_dict(self) -> dict[str, Any]:
        """The D-store ``per_key`` entry (no ``comparable`` leg — it is derived)."""
        return {
            "key": self.key,
            "a": self.a,
            "b": self.b,
            "abs_diff": self.abs_diff,
            "rel_diff": self.rel_diff,
            "static_class": self.static_class,
        }

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> PerKeyDiff:
        """Rebuild from a stored ``per_key`` entry, re-deriving ``comparable``."""
        a = record.get("a")
        b = record.get("b")
        comparable = a is not None and b is not None and not (_is_nan(a) or _is_nan(b))
        sc = record.get("static_class")
        if not isinstance(sc, str) or sc not in STATIC_CLASSES:
            raise errors.SpecInvalid(
                f"determinism: per_key.static_class must be one of "
                f"{sorted(STATIC_CLASSES)}; got {sc!r}"
            )
        key = record.get("key")
        if not isinstance(key, str) or not key:
            raise errors.SpecInvalid(
                f"determinism: per_key.key must be a non-empty string; got {key!r}"
            )
        return cls(
            key=key,
            a=a,
            b=b,
            abs_diff=record.get("abs_diff"),
            rel_diff=record.get("rel_diff"),
            static_class=sc,
            comparable=comparable,
        )


def diff_metrics(payload_a: Mapping[str, Any], payload_b: Mapping[str, Any]) -> list[PerKeyDiff]:
    """Pure per-key diff over two RAW metrics payloads → one ``PerKeyDiff`` per key.

    Mirrors ``verify_reproduction._compare_metrics``'s comparability rules (so a
    determinism sample and a reproduction receipt agree on what is comparable),
    but records the raw observation only — never a verdict:

    * key on ONE side only → ``comparable=False`` (a key-set change).
    * numeric vs numeric → ``abs_diff``/``rel_diff`` computed; NaN either side →
      ``comparable=False`` (never a raw ``!=`` surprise).
    * non-numeric → equality path, no numeric diff; a TYPE change between the two
      sides → ``comparable=False``.
    """
    flat_a = flatten_metrics(payload_a)
    flat_b = flatten_metrics(payload_b)
    out: list[PerKeyDiff] = []
    for key in sorted(set(flat_a) | set(flat_b)):
        a_present, b_present = key in flat_a, key in flat_b
        if not (a_present and b_present):
            present = flat_a[key] if a_present else flat_b[key]
            out.append(
                PerKeyDiff(
                    key,
                    flat_a.get(key),
                    flat_b.get(key),
                    None,
                    None,
                    static_class(present),
                    comparable=False,
                )
            )
            continue
        a = flat_a[key]
        b = flat_b[key]
        if _is_number(a) and _is_number(b):
            if _is_nan(a) or _is_nan(b):
                out.append(PerKeyDiff(key, a, b, None, None, "float", comparable=False))
                continue
            af, bf = float(a), float(b)
            abs_diff = abs(af - bf)
            denom = max(abs(af), abs(bf))
            rel_diff = abs_diff / denom if denom else 0.0
            sc = "float" if (isinstance(a, float) or isinstance(b, float)) else "int"
            out.append(PerKeyDiff(key, a, b, abs_diff, rel_diff, sc, comparable=True))
        else:
            sc_a, sc_b = static_class(a), static_class(b)
            out.append(PerKeyDiff(key, a, b, None, None, sc_a, comparable=(sc_a == sc_b)))
    return out


# --- the sample record model + validation ------------------------------------


@dataclass(frozen=True)
class Sample:
    """A validated fingerprint sample — the D-store record projected to an object.

    A valid ``state/attestation.py::validate`` record (``attestor="code"``,
    ``subject_kind="determinism-fingerprint"``, ``subject_id=<cmd_sha>``,
    ``content_sha``) that ALSO carries the fingerprint evidence legs. The store
    layer (T3) binds it through ``attestation.bind`` and joins admission on
    ``content_sha`` — this object holds no I/O.
    """

    content_sha: str
    subject_id: str
    identity: Mapping[str, Any]
    source: str
    run_ids: tuple[str, ...]
    cluster: str
    scale: str
    verdict: str
    same_submission: bool
    partial: bool
    task_indices: tuple[int, ...] | None
    per_key: tuple[PerKeyDiff, ...]
    schema_version: int = SAMPLE_SCHEMA_VERSION


def build_sample_record(
    *,
    ts: str,
    content_sha: str,
    identity: Mapping[str, Any],
    source: str,
    run_ids: Sequence[str],
    cluster: str,
    scale: str,
    verdict: str,
    per_key: Sequence[PerKeyDiff],
    same_submission: bool = False,
    partial: bool = False,
    task_indices: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Assemble the D-store sample record dict — the ONE place the shape lives.

    ``subject_id`` is derived from ``identity["cmd_sha"]`` (the experiment
    identity a ledger keys on). Round-trips through :func:`validate_sample`
    before returning, so a malformed record can never leave this function.
    T3/T4 supply ``content_sha`` (via :func:`compute_content_sha` over the
    on-disk payloads) and ``ts``; this module stays clock-free and I/O-free.
    """
    cmd_sha = identity.get("cmd_sha")
    if not isinstance(cmd_sha, str) or not cmd_sha:
        raise errors.SpecInvalid(
            "determinism: identity.cmd_sha must be a non-empty string "
            f"(it is the subject_id); got {cmd_sha!r}"
        )
    record: dict[str, Any] = {
        "ts": ts,
        "schema_version": SAMPLE_SCHEMA_VERSION,
        "attestor": "code",
        "subject_kind": SUBJECT_KIND,
        "subject_id": cmd_sha,
        "content_sha": content_sha,
        "identity": dict(identity),
        "source": source,
        "run_ids": list(run_ids),
        "cluster": cluster,
        "scale": scale,
        "verdict": verdict,
        "same_submission": bool(same_submission),
        "partial": bool(partial),
        "task_indices": (list(task_indices) if task_indices is not None else None),
        "per_key": [d.to_dict() for d in per_key],
    }
    validate_sample(record)  # refuse a malformed record at construction
    return record


def validate_sample(record: Mapping[str, Any]) -> Sample:
    """Validate a sample record dict → :class:`Sample`, or refuse loudly.

    Routes the attestation shape through the ONE kernel
    (:func:`state.attestation.validate` — the enforcement-map "one kernel" row),
    then enforces the fingerprint-specific legs: the ``code``/subject-kind
    literals, the schema version, the ``source``/``scale``/``verdict``
    vocabularies, the identity fields, and the no-silent-caps partiality rule
    (``partial: true`` REQUIRES ``task_indices``). Raises
    :class:`errors.SpecInvalid` naming the offending field.
    """
    if not isinstance(record, Mapping):
        raise errors.SpecInvalid(
            f"determinism: sample record must be a mapping; got {type(record).__name__}"
        )
    # Reuse the attestation kernel for the shared record shape (attestor literal,
    # required non-empty strings). Never re-inline that check here.
    att = attestation.validate(record)
    if att.attestor != "code":
        raise errors.SpecInvalid(
            "determinism: sample.attestor must be 'code' (a fingerprint is a code "
            f"attestation); got {att.attestor!r}"
        )
    if att.subject_kind != SUBJECT_KIND:
        raise errors.SpecInvalid(
            f"determinism: sample.subject_kind must be {SUBJECT_KIND!r}; got {att.subject_kind!r}"
        )

    schema_version = record.get("schema_version")
    if schema_version != SAMPLE_SCHEMA_VERSION:
        raise errors.SpecInvalid(
            f"determinism: sample.schema_version must be {SAMPLE_SCHEMA_VERSION}; "
            f"got {schema_version!r}"
        )

    identity = record.get("identity")
    if not isinstance(identity, Mapping):
        raise errors.SpecInvalid(
            f"determinism: sample.identity must be a mapping; got {type(identity).__name__}"
        )
    for field in IDENTITY_FIELDS:
        value = identity.get(field)
        if not isinstance(value, str) or not value:
            raise errors.SpecInvalid(
                f"determinism: sample.identity.{field} must be a non-empty string; got {value!r}"
            )
    if identity.get("cmd_sha") != att.subject_id:
        raise errors.SpecInvalid(
            f"determinism: sample.subject_id {att.subject_id!r} must equal identity.cmd_sha "
            f"{identity.get('cmd_sha')!r} (the ledger keys on the experiment identity)"
        )

    source = record.get("source")
    if source not in SOURCES:
        raise errors.SpecInvalid(
            f"determinism: sample.source must be one of {sorted(SOURCES)}; got {source!r}"
        )
    scale = record.get("scale")
    if scale not in SCALES:
        raise errors.SpecInvalid(
            f"determinism: sample.scale must be one of {sorted(SCALES)}; got {scale!r}"
        )
    verdict = record.get("verdict")
    if verdict not in SAMPLE_VERDICTS:
        raise errors.SpecInvalid(
            f"determinism: sample.verdict must be one of {sorted(SAMPLE_VERDICTS)}; got {verdict!r}"
        )

    cluster = record.get("cluster")
    if not isinstance(cluster, str) or not cluster:
        raise errors.SpecInvalid(
            f"determinism: sample.cluster must be a non-empty string; got {cluster!r}"
        )

    run_ids = record.get("run_ids")
    if not isinstance(run_ids, Sequence) or isinstance(run_ids, str) or not run_ids:
        raise errors.SpecInvalid(
            f"determinism: sample.run_ids must be a non-empty list of run ids; got {run_ids!r}"
        )
    for rid in run_ids:
        if not isinstance(rid, str) or not rid:
            raise errors.SpecInvalid(
                f"determinism: sample.run_ids entries must be non-empty strings; got {rid!r}"
            )

    same_submission = record.get("same_submission")
    if not isinstance(same_submission, bool):
        raise errors.SpecInvalid(
            f"determinism: sample.same_submission must be a bool; got {same_submission!r}"
        )
    partial = record.get("partial")
    if not isinstance(partial, bool):
        raise errors.SpecInvalid(f"determinism: sample.partial must be a bool; got {partial!r}")

    task_indices = record.get("task_indices")
    if task_indices is not None:
        if not isinstance(task_indices, Sequence) or isinstance(task_indices, str):
            raise errors.SpecInvalid(
                "determinism: sample.task_indices must be null or a list of ints; "
                f"got {task_indices!r}"
            )
        for idx in task_indices:
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise errors.SpecInvalid(
                    f"determinism: sample.task_indices entries must be ints; got {idx!r}"
                )
    # No-silent-caps on partiality: a partial sample MUST name what it compared.
    if partial and task_indices is None:
        raise errors.SpecInvalid(
            "determinism: sample.partial is true but task_indices is null — a partial "
            "sample must record the exact task indices it compared (no-silent-caps)."
        )

    per_key_raw = record.get("per_key")
    if not isinstance(per_key_raw, Sequence) or isinstance(per_key_raw, str):
        raise errors.SpecInvalid(
            f"determinism: sample.per_key must be a list; got {type(per_key_raw).__name__}"
        )
    per_key = tuple(PerKeyDiff.from_dict(e) for e in per_key_raw)

    return Sample(
        content_sha=att.content_sha,
        subject_id=att.subject_id,
        identity=dict(identity),
        source=source,
        run_ids=tuple(run_ids),
        cluster=cluster,
        scale=scale,
        verdict=verdict,
        same_submission=same_submission,
        partial=partial,
        task_indices=(tuple(task_indices) if task_indices is not None else None),
        per_key=per_key,
    )


# --- the CURRENT-identity filter (item 5) ------------------------------------


@dataclass(frozen=True)
class FilterResult:
    """The CURRENT-identity filter's output — kept samples + disclosed exclusions.

    ``excluded_identity_drift`` and ``excluded_data_drift`` samples are DROPPED
    (retained upstream in the ledger as history); ``data_identity_unknown``
    samples are KEPT (absent data field is disclosed, never blocking).
    """

    samples: tuple[Sample, ...]
    admitted: tuple[bool, ...]
    excluded_identity_drift: int
    excluded_data_drift: int
    data_identity_unknown: int


def _identity_matches(sample_identity: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    """True iff every code-identity field equals the pair-under-comparison's."""
    return all(sample_identity.get(f) == identity.get(f) for f in IDENTITY_FIELDS)


def filter_current_identity(
    samples: Sequence[Sample],
    admitted: Sequence[bool],
    *,
    identity: Mapping[str, Any],
    data_identity: str | None = None,
) -> FilterResult:
    """Keep only CURRENT-identity samples; disclose drifted ones by count.

    Code identity (``IDENTITY_FIELDS``) equal to the pair under comparison is
    required — a drift on any field reads the sample STALE (excluded here,
    retained in the ledger as history). When *data_identity* is supplied
    (Amendment 1, Phase-3's sidecar echo): a sample whose ``data_sha`` differs
    is excluded as data drift; a sample with NO ``data_sha`` is kept and counted
    ``data_identity_unknown`` (disclosed, never fabricated, never blocking). When
    *data_identity* is ``None`` the data leg is not applied at all.

    The admission flags ride the same filter (parallel to *samples*): T3 computes
    them (the store-layer JOIN); this filter stays pure. Raises
    :class:`errors.SpecInvalid` if the two sequences differ in length.
    """
    if len(samples) != len(admitted):
        raise errors.SpecInvalid(
            f"determinism: samples ({len(samples)}) and admitted ({len(admitted)}) must be parallel"
        )
    kept: list[Sample] = []
    kept_admitted: list[bool] = []
    id_drift = 0
    data_drift = 0
    data_unknown = 0
    for sample, adm in zip(samples, admitted, strict=False):
        if not _identity_matches(sample.identity, identity):
            id_drift += 1
            continue
        if data_identity is not None:
            sample_data = sample.identity.get(DATA_IDENTITY_FIELD)
            if sample_data is None:
                data_unknown += 1  # kept — never blocking
            elif sample_data != data_identity:
                data_drift += 1
                continue
        kept.append(sample)
        kept_admitted.append(bool(adm))
    return FilterResult(
        samples=tuple(kept),
        admitted=tuple(kept_admitted),
        excluded_identity_drift=id_drift,
        excluded_data_drift=data_drift,
        data_identity_unknown=data_unknown,
    )


# --- the envelope reduction (D-envelope: order statistics only) --------------


@dataclass(frozen=True)
class Evidence:
    """A per-key envelope's evidence label — the honesty leg every consumer reads."""

    n: int
    n_full: int
    n_partial: int
    scales: tuple[str, ...]
    clusters: tuple[str, ...]
    same_submission_only: bool
    excluded_unadmitted: int


@dataclass(frozen=True)
class KeyEnvelope:
    """One key's observed range + class + evidence. DERIVED at read, never stored."""

    key: str
    cls: str  # EXACT | STOCHASTIC
    lo: float | None
    hi: float | None
    rel_spread: float | None
    evidence: Evidence

    def to_envelope_applied(self) -> dict[str, Any]:
        """The D-verdict-wire ``envelope_applied`` dict for a receipt/brief."""
        return {
            "class": self.cls,
            "lo": self.lo,
            "hi": self.hi,
            "rel_spread": self.rel_spread,
            "evidence": {
                "n": self.evidence.n,
                "n_full": self.evidence.n_full,
                "n_partial": self.evidence.n_partial,
                "scales": list(self.evidence.scales),
                "clusters": list(self.evidence.clusters),
                "same_submission_only": self.evidence.same_submission_only,
            },
        }


@dataclass(frozen=True)
class Envelope:
    """The reduced envelope over CURRENT-identity ADMITTED samples, per key.

    ``per_key`` maps a flattened key to its :class:`KeyEnvelope`. The top-level
    exclusion counts DISCLOSE what did not contribute (no-silent-caps).
    """

    per_key: Mapping[str, KeyEnvelope]
    excluded_unadmitted: int
    excluded_identity_drift: int
    excluded_data_drift: int
    data_identity_unknown: int


def order_statistics_envelope(values: Sequence[float]) -> tuple[float, float, float]:
    """The ONE order-statistics leg: ``(lo, hi, rel_spread)`` over non-empty values.

    ``lo = min``, ``hi = max``, ``rel_spread = (hi - lo) / max(|lo|, |hi|)`` (0.0
    when the magnitude scale is 0). Order statistics ONLY — no mean, no stddev, no
    fitted anything (the D-envelope no-invented-tolerance rule). This is the ONE
    envelope definition (enforcement row): the fingerprint reduction
    (:func:`_reduce_key`) and registration conformance's ``judge_window``
    (``state/conformance.py``, the plan's T1a re-point) both route through this —
    never a second min/max/spread implementation. ``values`` must be non-empty;
    the caller filters to comparable finite numbers upstream.
    """
    lo = min(values)
    hi = max(values)
    denom = max(abs(lo), abs(hi))
    rel_spread = (hi - lo) / denom if denom else 0.0
    return float(lo), float(hi), rel_spread


def _reduce_key(key: str, samples: Sequence[Sample], admitted: Sequence[bool]) -> KeyEnvelope:
    """Reduce ONE key to its observed range + evidence over ADMITTED samples only.

    Order statistics ONLY: ``lo=min``, ``hi=max`` over every observed ``a``/``b``
    numeric value; ``rel_spread=(hi-lo)/max(|lo|,|hi|)`` (0 when both are 0).
    No mean, no stddev, no fitted anything. ``exact`` unless a nonzero float
    spread was observed.
    """
    values: list[float] = []
    n = n_full = n_partial = excluded = 0
    scales: set[str] = set()
    clusters: set[str] = set()
    same_submission_only = True
    observed_any = False
    saw_float = False
    for sample, adm in zip(samples, admitted, strict=False):
        entry = next((d for d in sample.per_key if d.key == key), None)
        if entry is None:
            continue
        if not adm:
            excluded += 1
            continue
        observed_any = True
        n += 1
        if sample.partial:
            n_partial += 1
        else:
            n_full += 1
        scales.add(sample.scale)
        clusters.add(sample.cluster)
        if not sample.same_submission:
            same_submission_only = False
        if entry.static_class == "float":
            saw_float = True
        for value in (entry.a, entry.b):
            if _is_number(value) and not _is_nan(value):
                values.append(float(value))
    lo: float | None
    hi: float | None
    rel_spread: float | None
    if values:
        # Route through the ONE order-statistics leg (T1a) — never a re-inlined
        # min/max/spread. Byte-identical to the prior inline reduction.
        lo, hi, rel_spread = order_statistics_envelope(values)
        is_stochastic = saw_float and hi > lo
    else:
        lo = hi = rel_spread = None
        is_stochastic = False
    cls = STOCHASTIC if is_stochastic else EXACT
    evidence = Evidence(
        n=n,
        n_full=n_full,
        n_partial=n_partial,
        scales=tuple(sorted(scales)),
        clusters=tuple(sorted(clusters)),
        same_submission_only=(same_submission_only if observed_any else False),
        excluded_unadmitted=excluded,
    )
    return KeyEnvelope(key=key, cls=cls, lo=lo, hi=hi, rel_spread=rel_spread, evidence=evidence)


def reduce_envelope(
    samples: Sequence[Sample],
    admitted: Sequence[bool],
    *,
    identity: Mapping[str, Any],
    data_identity: str | None = None,
) -> Envelope:
    """Reduce the whole ledger to per-key envelopes — DERIVED fresh at every read.

    Filters to CURRENT-identity samples (item 5), then per-key reduces over the
    ADMITTED ones only (an unadmitted sample never moves an envelope — the
    D-consume admission rule; it is DISCLOSED as ``excluded_unadmitted``). Pure
    over ``(samples, admitted_flags)``-shaped input: T3 computes the flags.
    """
    filtered = filter_current_identity(
        samples, admitted, identity=identity, data_identity=data_identity
    )
    keys: set[str] = set()
    for sample in filtered.samples:
        for entry in sample.per_key:
            keys.add(entry.key)
    per_key = {key: _reduce_key(key, filtered.samples, filtered.admitted) for key in sorted(keys)}
    excluded_unadmitted = sum(1 for adm in filtered.admitted if not adm)
    return Envelope(
        per_key=per_key,
        excluded_unadmitted=excluded_unadmitted,
        excluded_identity_drift=filtered.excluded_identity_drift,
        excluded_data_drift=filtered.excluded_data_drift,
        data_identity_unknown=filtered.data_identity_unknown,
    )


# --- the tiered verdict classifier -------------------------------------------


@dataclass(frozen=True)
class KeyVerdict:
    """One key's tiered verdict — the D-verdict-wire per-key wire shape."""

    key: str
    verdict: str  # match | mismatch | incomparable
    tier_reason: str | None
    envelope_applied: dict[str, Any] | None


@dataclass(frozen=True)
class Classification:
    """The whole comparison's tiered verdict (D-verdict-wire overall fold)."""

    per_key: tuple[KeyVerdict, ...]
    stage_reached: str  # AUTO_CLEARED | NEEDS_VERDICT | MISMATCH
    needs_decision: bool


def _well_evidenced(env: KeyEnvelope, current_scale: str, current_cluster: str) -> bool:
    """Mechanized, never judged: n>=3 AND scale coverage AND cluster coverage.

    A well-evidenced envelope is the ONLY thing that may auto-clear a nonzero
    float deviation or produce a ``mismatch`` — this bar, not a wider synthetic
    range, guards the weak-n case (the n=2 failure modes in the module docstring).
    """
    return (
        env.evidence.n >= WELL_EVIDENCED_MIN_N
        and current_scale in env.evidence.scales
        and current_cluster in env.evidence.clusters
    )


def _inside_range(diff: PerKeyDiff, env: KeyEnvelope) -> bool:
    """True iff BOTH compared values lie within the observed range [lo, hi].

    A value is inside the range or outside it — NO near-boundary proximity
    trigger, NO invented tolerance (D-envelope). ``lo``/``hi`` are observations,
    never fabricated parameters.
    """
    if env.lo is None or env.hi is None:
        return False
    lo_side = diff.a if _is_number(diff.a) else diff.b
    hi_side = diff.b if _is_number(diff.b) else diff.a
    if not (_is_number(lo_side) and _is_number(hi_side)):
        return False
    low = min(float(lo_side), float(hi_side))
    high = max(float(lo_side), float(hi_side))
    return env.lo <= low and high <= env.hi


def _classify_key(
    diff: PerKeyDiff,
    env: KeyEnvelope | None,
    *,
    current_scale: str,
    current_cluster: str,
    tolerance: Callable[[str], tuple[float | None, float | None] | None] | None,
) -> KeyVerdict:
    """Classify ONE key. Precedence: caller override > evidenced > thin > exact."""
    applied = env.to_envelope_applied() if env is not None else None

    if not diff.comparable:
        # One-sided / NaN / type-changed → incomparable → routes to the human.
        return KeyVerdict(diff.key, INCOMPARABLE, None, applied)

    # Caller override WINS but is DISCLOSED (labeled caller_override) — only over a
    # numeric key (a supplied tolerance on a non-numeric is meaningless).
    if tolerance is not None and diff.static_class in ("float", "int"):
        resolved = tolerance(diff.key)
        if resolved is not None:
            abs_tol, rel_tol = resolved
            if abs_tol is not None or rel_tol is not None:
                matched = (
                    abs_tol is not None and diff.abs_diff is not None and diff.abs_diff <= abs_tol
                ) or (
                    rel_tol is not None and diff.rel_diff is not None and diff.rel_diff <= rel_tol
                )
                return KeyVerdict(
                    diff.key, "match" if matched else "mismatch", "caller_override", None
                )

    # Exact-class (non-float) key: ALWAYS exact — an exact-class key that moved is
    # a mismatch (key sets and shapes are always exact).
    if diff.static_class != "float":
        moved = diff.a != diff.b
        return KeyVerdict(diff.key, "mismatch" if moved else "match", "exact", applied)

    # Float key. Identity observed is identity, whatever n → exact match.
    if diff.abs_diff == 0:
        return KeyVerdict(diff.key, "match", "exact", applied)

    # Nonzero float deviation. With NO measured float evidence, no-invented-
    # tolerance holds: it is NOT auto — the thinnest envelope routes to the human.
    if env is None or env.cls != STOCHASTIC:
        return KeyVerdict(diff.key, "mismatch", "outside_thin_envelope", applied)

    inside = _inside_range(diff, env)
    if _well_evidenced(env, current_scale, current_cluster):
        if inside:
            return KeyVerdict(diff.key, "match", "within_evidenced_envelope", applied)
        return KeyVerdict(diff.key, "mismatch", "outside_evidenced_envelope", applied)
    # Thin envelope: EITHER direction routes to the human (never a wrong auto-verdict).
    if inside:
        return KeyVerdict(diff.key, "match", "within_thin_envelope", applied)
    return KeyVerdict(diff.key, "mismatch", "outside_thin_envelope", applied)


def _bucket(kv: KeyVerdict) -> str:
    """Route a per-key verdict to a fold bucket: 'mismatch' | 'verdict' | 'auto'."""
    if kv.verdict == INCOMPARABLE:
        return "verdict"
    if kv.tier_reason == "outside_evidenced_envelope":
        return "mismatch"
    if kv.tier_reason == "exact" and kv.verdict == "mismatch":
        return "mismatch"  # an exact-class key moved
    if kv.tier_reason == "caller_override":
        return "mismatch" if kv.verdict == "mismatch" else "auto"
    if kv.tier_reason in ("within_thin_envelope", "outside_thin_envelope"):
        return "verdict"
    if kv.tier_reason in ("exact", "within_evidenced_envelope"):
        return "auto"
    return "verdict"


def classify(
    per_key_diffs: Sequence[PerKeyDiff],
    envelope: Envelope,
    *,
    current_scale: str,
    current_cluster: str,
    tolerance: Callable[[str], tuple[float | None, float | None] | None] | None = None,
) -> Classification:
    """The tiered verdict classifier — pure over prior evidence + this comparison.

    Judge BEFORE append (D-consume clause 1): *envelope* is reduced from the
    PRIOR samples only; this comparison's own sample never participates in the
    envelope that judges it. Per-key tiers fold to an overall ``stage_reached``:
    any outside-evidenced-envelope or exact-class-moved key → ``mismatch``; else
    any thin / novel / incomparable key → ``needs_verdict``; else
    ``auto_cleared``. An empty comparison folds to ``needs_verdict`` — nothing
    proven is not an auto-clear.

    *tolerance* is the caller override resolver: ``tolerance(key) -> (abs_tol,
    rel_tol) | None`` (labeled ``caller_override`` and disclosed when it decides
    a key). NO near-boundary trigger, NO invented epsilon anywhere.
    """
    key_verdicts = tuple(
        _classify_key(
            diff,
            envelope.per_key.get(diff.key),
            current_scale=current_scale,
            current_cluster=current_cluster,
            tolerance=tolerance,
        )
        for diff in per_key_diffs
    )
    if not key_verdicts:
        return Classification(per_key=(), stage_reached=NEEDS_VERDICT, needs_decision=True)
    buckets = {_bucket(kv) for kv in key_verdicts}
    if "mismatch" in buckets:
        stage = MISMATCH
    elif "verdict" in buckets:
        stage = NEEDS_VERDICT
    else:
        stage = AUTO_CLEARED
    return Classification(
        per_key=key_verdicts, stage_reached=stage, needs_decision=stage != AUTO_CLEARED
    )


# --- the registration predicate ----------------------------------------------


def evidence_meets(
    samples: Sequence[Sample],
    admitted: Sequence[bool],
    demand: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    data_identity: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Does the ADMITTED CURRENT-identity evidence satisfy a registration *demand*?

    The ONE predicate the registration kernel consumes (the one-definition rule)
    so it never re-implements the envelope reduction. Demand vocabulary
    ``{min_n, min_n_full?, scales, clusters}`` (plural): ``min_n`` counts
    n_full + n_partial; the optional ``min_n_full`` demands the full-sample leg
    separately; every named ``scale``/``cluster`` label must be PRESENT in the
    evidence sets (identity over labels, never interpretation). Counts ADMITTED,
    CURRENT-identity samples ONLY — an unadmitted sample can never satisfy a
    demand. Missing evidence is an ordinary shortfall NAMED in the return dict;
    an UNKNOWN demand key is a loud :class:`errors.SpecInvalid`.

    Returns ``(met, shortfall)`` — ``shortfall`` empty iff met.
    """
    unknown = set(demand) - _ALLOWED_DEMAND_KEYS
    if unknown:
        raise errors.SpecInvalid(
            f"determinism.evidence_meets: unknown demand key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_ALLOWED_DEMAND_KEYS)}"
        )
    filtered = filter_current_identity(
        samples, admitted, identity=identity, data_identity=data_identity
    )
    n = n_full = 0
    scales: set[str] = set()
    clusters: set[str] = set()
    for sample, adm in zip(filtered.samples, filtered.admitted, strict=False):
        if not adm:
            continue
        n += 1
        if not sample.partial:
            n_full += 1
        scales.add(sample.scale)
        clusters.add(sample.cluster)

    shortfall: dict[str, Any] = {}
    min_n = demand.get("min_n")
    if min_n is not None and n < min_n:
        shortfall["min_n"] = {"demanded": min_n, "have": n}
    min_n_full = demand.get("min_n_full")
    if min_n_full is not None and n_full < min_n_full:
        shortfall["min_n_full"] = {"demanded": min_n_full, "have": n_full}
    want_scales = demand.get("scales") or []
    missing_scales = [s for s in want_scales if s not in scales]
    if missing_scales:
        shortfall["scales"] = {"missing": missing_scales, "have": sorted(scales)}
    want_clusters = demand.get("clusters") or []
    missing_clusters = [c for c in want_clusters if c not in clusters]
    if missing_clusters:
        shortfall["clusters"] = {"missing": missing_clusters, "have": sorted(clusters)}

    return (not shortfall, shortfall)
