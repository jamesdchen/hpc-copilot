"""The determinism-fingerprint ledger — the store layer (T3).

Design origin: ``docs/design/determinism-fingerprint.md`` (D-store, D-consume,
D-double-canary). This module is the **store** half of the fingerprint feature:
the pure envelope math and the tiered classifier live in the kernel
(``state/determinism.py``, T1 — pure over ``(samples, admitted_flags)``); this
module owns the on-disk ledger and everything that touches I/O:

* **Path derivation** — one append-only JSONL ledger per experiment IDENTITY,
  keyed on ``cmd_sha`` at ``<experiment>/_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl``.
  A ledger's subject is the experiment identity, not one run, so samples from
  the original's double canary and from every later reproduction accumulate to
  the SAME file — it cannot key on ``run_id`` (D-store).
* **Append-through-bind** — every sample is a CODE ATTESTATION: it validates to
  a ``state/attestation.py::validate`` record and its ``content_sha`` is
  recompute-locked (``state/attestation.py::bind``) against the two COMPARED
  on-disk payloads, so a spread cannot be asserted into existence (D5 lock 2).
  The single line is then written through the ONE shared JSONL-append helper
  ``infra/io.py::append_jsonl_line`` — never a second flock+fsync.
* **Tolerant read** — malformed lines are skipped and COUNTED (the
  ``decision_journal.read_decisions`` idiom), so one torn line never strands the
  rest of the scientific record; the skip count is disclosed.
* **CURRENT-identity filter** — the kernel's staleness POSTURE: a sample whose
  identity fields differ from the pair under comparison reads STALE (retained in
  the ledger as history, excluded from the envelope). The data-identity leg is
  included defensively (Amendment 1): different-data is data-drift (STALE), and
  an absent manifest on either side is UNKNOWN — disclosed, never fabricated.
* **The admission JOIN** (D-consume, the self-laundering close) — an envelope
  admits a sample iff its comparison received a PASSING verdict. A
  ``double-canary`` sample carries ``verdict="auto_cleared"`` and is admitted by
  construction; a ``needs_verdict``/``mismatch`` sample is admitted ONLY when the
  reproduction run's decision journal carries a ``reproduction-verdict`` record
  whose ``resolved`` names the sample's ``content_sha`` TOKEN-EXACT with
  ``accept: true``. The join key is ``content_sha`` precisely because it is
  bind-locked — an acceptance cannot name evidence that was never on disk. The
  join lives HERE, never in the pure kernel: T3 computes the ``admitted_flags``
  the kernel reduces over, so T1 stays I/O-free. Inadmissible samples remain in
  the ledger as disclosed findings, surfaced as ``excluded_unadmitted``.

The n=2 double-canary prior is a LABELED prior, never a truth. The recorded n=2
failure modes (carried verbatim from D-store §2, and WHY every envelope stays
labeled with its evidence): (1) rare-event nondeterminism looks ``exact`` at
n=2 and is not; (2) canary-scale ≠ main-scale — BLAS/GPU libraries pick
algorithms by problem size, so canary-scale evidence is thin for a main-scale
verdict; (3) same-node correlated samples — the double canary's two executions
may land on one node/SKU (``same_submission: true`` records it).

Parallel-work seam (T1 not yet in tree): the sample-record shape is the D-store
dict verbatim (below), NOT a T1 import. The canonical content-sha over the two
payloads (:func:`content_sha_over_payloads`) is implemented here so the append's
bind-recompute is self-contained; T1's kernel owns the same canonicalization
(``docs/design/determinism-fingerprint.md`` T1 — "the canonical content-sha over
two metrics payloads") and a later commit should re-point one at the other so
there is ONE definition. The pure envelope reduction / tiered classifier /
``evidence_meets`` consume :class:`LedgerEvidence` (``samples`` +
``admitted_flags``, aligned).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state import attestation
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "SUBJECT_KIND",
    "SOURCES",
    "SCALES",
    "VERDICTS",
    "REPRODUCTION_VERDICT_BLOCK",
    "LedgerEvidence",
    "fingerprints_dir",
    "fingerprint_path",
    "pulls_dir",
    "content_sha_over_payloads",
    "append_sample",
    "read_samples",
    "partition_current_identity",
    "compute_admitted_flags",
    "load_evidence",
]

#: Sample record schema version (append-only ledger; bump on shape change —
#: the ``RECEIPT_SCHEMA_VERSION`` convention). Readers tolerate unknown extra
#: keys (forward-compat), so an additive field does NOT need a bump.
SCHEMA_VERSION = 1

#: The opaque attestation ``subject_kind`` every fingerprint sample rides — the
#: subject is the experiment IDENTITY (``subject_id == cmd_sha``), not a run.
SUBJECT_KIND = "determinism-fingerprint"

#: The two sample sources. ``double-canary`` = the submit-time n=2 prior (scale
#: ``canary``); ``verify-reproduction`` = a later reproduction comparison (scale
#: ``main``, possibly ``partial``). Scale is assigned mechanically, never judged.
SOURCES = frozenset({"double-canary", "verify-reproduction"})

#: The two mechanically-assigned scale labels (D-store): a canary is
#: ``canary``-scale, a reproduction is ``main``-scale (partiality is a SEPARATE
#: axis carried by ``partial``).
SCALES = frozenset({"canary", "main"})

#: The verdict AT APPEND (judgment always precedes append, D-consume clause 1).
VERDICTS = frozenset({"auto_cleared", "needs_verdict", "mismatch"})

#: The decision-journal block a human's needs_verdict resolution rides — the
#: EXISTING run scope, no new verdict verb (the no-unlock-verb doctrine). The
#: admission join reads these records off the reproduction run's journal.
REPRODUCTION_VERDICT_BLOCK = "reproduction-verdict"

#: The identity fields that define an experiment's CODE identity — a change in
#: any of them reads prior samples STALE (the kernel's staleness posture). Lifted
#: from the sample's ``identity`` dict VERBATIM, never re-derived (the
#: ``_IDENTITY_FIELDS`` discipline of ``ops/verify_reproduction.py``).
_CODE_IDENTITY_FIELDS: tuple[str, ...] = ("cmd_sha", "tasks_py_sha", "executor")

#: The data-identity leg (Amendment 1). Compared DEFENSIVELY: both sides present
#: and different → data drift (STALE); either side absent → unknown, disclosed
#: (never fabricated as nondeterminism evidence).
_DATA_IDENTITY_FIELD = "data_sha"


# ── canonical content sha ────────────────────────────────────────────────────


def _canonical_json(obj: Any) -> str:
    """The harness sha canonicalization (``docs/internals/harness-contract.md``).

    Sorted keys, compact separators, unicode kept as-is — deterministic and
    platform-stable. The ONE serialization every ``content_sha`` here is taken
    over.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha_over_payloads(payload_a: Any, payload_b: Any) -> str:
    """SHA-256 (lowercase hex) over the two COMPARED payloads, canonical form.

    The pair is hashed in ``run_ids`` order (``[a, b]``) so the sha is
    reproducible: it is the sha the sample's ``content_sha`` must equal, and the
    thing :func:`append_sample`'s bind-recompute re-derives from the on-disk
    artifacts. Payloads are the PARSED JSON (not raw bytes), so cosmetic
    formatting differences don't move the sha but semantic content does.

    T1's pure kernel owns the same canonicalization; keep the two definitions
    identical (one should re-point at the other once T1 lands — the
    one-definition rule).
    """
    return hashlib.sha256(_canonical_json([payload_a, payload_b]).encode("utf-8")).hexdigest()


# ── path derivation ──────────────────────────────────────────────────────────


def fingerprints_dir(experiment_dir: Path) -> Path:
    """``<experiment>/_aggregated/_fingerprints/`` — the ledger home.

    Beside the metrics it describes (D-store): the fingerprint is a durable
    scientific record that must survive a decision-journal wipe, so it lives with
    the experiment's ``_aggregated/`` results, not under the wipeable journal.
    """
    return experiment_dir / "_aggregated" / "_fingerprints"


def fingerprint_path(experiment_dir: Path, cmd_sha: str) -> Path:
    """The append-only ledger for one experiment identity, keyed on ``cmd_sha``.

    ``<experiment>/_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl`` — one ledger
    per identity; the full identity fields live INSIDE every record. Refuses an
    empty ``cmd_sha`` (a ledger cannot key on nothing).
    """
    if not cmd_sha:
        raise errors.SpecInvalid("fingerprint_path: cmd_sha must be a non-empty string")
    return fingerprints_dir(experiment_dir) / f"{cmd_sha[:16]}.jsonl"


def pulls_dir(experiment_dir: Path, run_id: str) -> Path:
    """``<experiment>/_aggregated/_fingerprints/_pulls/<run_id>/`` — pulled canaries.

    Where the double canary's locally-FETCHED task-0 ``metrics.json`` payloads
    land so the sample's ``bind`` recompute has on-disk artifacts to re-hash
    (D-double-canary). The PULL itself is T4's job; this store only names the
    directory so T4 and T3 agree on one location. Refuses an empty / path-unsafe
    ``run_id``.
    """
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise errors.SpecInvalid(f"pulls_dir: run_id must be filesystem-safe; got {run_id!r}")
    return fingerprints_dir(experiment_dir) / "_pulls" / run_id


# ── append (validate → bind → one line) ──────────────────────────────────────


def _validate_sample_shape(record: Mapping[str, Any]) -> None:
    """Validate the D-store sample shape BEYOND the attestation record shape.

    :func:`attestation.bind` already enforces the attestation invariants
    (``attestor``/``subject_kind``/``subject_id``/``content_sha`` non-empty
    strings, ``attestor`` a known literal). This adds the fingerprint-specific
    invariants: schema version, the closed vocabularies (source / scale /
    verdict), the ``run_ids`` pair, the identity block, and the no-silent-caps
    partiality rule (a ``partial`` sample MUST carry its ``task_indices``).

    Raises :class:`errors.SpecInvalid` naming the offending field.
    """
    if record.get("schema_version") != SCHEMA_VERSION:
        raise errors.SpecInvalid(
            f"fingerprint sample: schema_version must be {SCHEMA_VERSION}; "
            f"got {record.get('schema_version')!r}"
        )
    if record.get("subject_kind") != SUBJECT_KIND:
        raise errors.SpecInvalid(
            f"fingerprint sample: subject_kind must be {SUBJECT_KIND!r}; "
            f"got {record.get('subject_kind')!r}"
        )
    source = record.get("source")
    if source not in SOURCES:
        raise errors.SpecInvalid(
            f"fingerprint sample: source must be one of {sorted(SOURCES)}; got {source!r}"
        )
    scale = record.get("scale")
    if scale not in SCALES:
        raise errors.SpecInvalid(
            f"fingerprint sample: scale must be one of {sorted(SCALES)}; got {scale!r}"
        )
    verdict = record.get("verdict")
    if verdict not in VERDICTS:
        raise errors.SpecInvalid(
            f"fingerprint sample: verdict must be one of {sorted(VERDICTS)}; got {verdict!r}"
        )
    run_ids = record.get("run_ids")
    if not isinstance(run_ids, (list, tuple)) or len(run_ids) != 2:
        raise errors.SpecInvalid(
            f"fingerprint sample: run_ids must be a 2-element list [original, repro]; "
            f"got {run_ids!r}"
        )
    if not all(isinstance(r, str) and r for r in run_ids):
        raise errors.SpecInvalid(
            f"fingerprint sample: run_ids members must be non-empty strings; got {run_ids!r}"
        )
    identity = record.get("identity")
    if not isinstance(identity, Mapping):
        raise errors.SpecInvalid(
            f"fingerprint sample: identity must be a mapping of the code-identity fields; "
            f"got {identity!r}"
        )
    # The ledger keys on cmd_sha; subject_id and identity.cmd_sha must agree so a
    # sample cannot land in a ledger it does not describe.
    if record.get("subject_id") != identity.get("cmd_sha"):
        raise errors.SpecInvalid(
            "fingerprint sample: subject_id must equal identity.cmd_sha "
            f"(subject_id={record.get('subject_id')!r}, "
            f"identity.cmd_sha={identity.get('cmd_sha')!r})"
        )
    # No-silent-caps on partiality: a partial sample discloses WHAT it compared.
    if record.get("partial") is True:
        task_indices = record.get("task_indices")
        if not isinstance(task_indices, (list, tuple)) or not task_indices:
            raise errors.SpecInvalid(
                "fingerprint sample: a partial sample must carry a non-empty task_indices "
                f"(no-silent-caps); got {task_indices!r}"
            )


def append_sample(
    experiment_dir: Path,
    *,
    record: dict[str, Any],
    artifact_a: Path,
    artifact_b: Path,
) -> dict[str, Any]:
    """Validate, bind-lock, and append ONE fingerprint sample to its ledger.

    *record* is the D-store sample dict (schema below). *artifact_a* /
    *artifact_b* are the on-disk paths of the two COMPARED payloads, in
    ``run_ids`` order — the double canary's two pulled task-0 ``metrics.json``,
    or a reproduction's two artifact-ladder payloads. The bind-recompute RE-READS
    them from disk (not a precomputed value), so a sample cannot claim a
    ``content_sha`` for artifacts that are not on disk saying what it claims.

    Order of operations (never re-inlined): validate the D-store shape →
    ``attestation.bind`` recompute-and-compare the ``content_sha`` against the
    two on-disk payloads → ONE line via ``infra/io.py::append_jsonl_line`` (the
    shared flock+fsync helper; never a second definition). *ts* is stamped
    (current UTC ISO-8601) when the record omits it.

    Returns the record written. Raises :class:`errors.SpecInvalid` on a bad shape
    or a ``content_sha`` that does not match the recomputed payload sha.
    """
    record = dict(record)
    record.setdefault("schema_version", SCHEMA_VERSION)
    record.setdefault("attestor", "code")
    record.setdefault("subject_kind", SUBJECT_KIND)
    record.setdefault("ts", utcnow_iso())

    _validate_sample_shape(record)

    def _recompute() -> str:
        payload_a = json.loads(artifact_a.read_text(encoding="utf-8"))
        payload_b = json.loads(artifact_b.read_text(encoding="utf-8"))
        return content_sha_over_payloads(payload_a, payload_b)

    # The un-fakeable lock: routes through the ONE attestation kernel. Also runs
    # attestation.validate (attestor / subject_kind / subject_id / content_sha).
    attestation.bind(record, recompute=_recompute)

    cmd_sha = record["identity"]["cmd_sha"]
    append_jsonl_line(fingerprint_path(experiment_dir, cmd_sha), record)
    return record


# ── tolerant read ────────────────────────────────────────────────────────────


def read_samples(experiment_dir: Path, cmd_sha: str) -> tuple[list[dict[str, Any]], int]:
    """Read every sample for an identity's ledger → ``(samples, skipped)``.

    Tolerant-read (the ``decision_journal.read_decisions`` idiom): blank and
    individually-corrupt lines are SKIPPED and COUNTED rather than failing the
    whole read — one torn line must never strand the rest of a scientific record.
    Returns ``([], 0)`` when the ledger does not exist yet. ``skipped`` is the
    disclosed count of malformed lines.
    """
    path = fingerprint_path(experiment_dir, cmd_sha)
    samples: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return samples, 0
    except (OSError, UnicodeDecodeError):
        return samples, 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(obj, dict):
            samples.append(obj)
        else:
            skipped += 1
    return samples, skipped


# ── CURRENT-identity filter ──────────────────────────────────────────────────


def partition_current_identity(
    samples: Iterable[Mapping[str, Any]],
    identity: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Partition samples into ``(current, stale, data_unknown)`` vs *identity*.

    *identity* is the pair-under-comparison's identity fields. A sample is
    CURRENT iff its CODE identity (``cmd_sha``, ``tasks_py_sha``, ``executor``)
    equals *identity* AND its data identity is not KNOWN-DIFFERENT; otherwise
    STALE (retained here for the caller to keep as ledger history, excluded from
    the envelope — the kernel's staleness posture).

    The data-identity leg is defensive (Amendment 1): both sides carry a
    ``data_sha`` and they DIFFER → data drift (STALE); either side absent →
    UNKNOWN — the sample stays CURRENT (an absent manifest is not evidence of
    drift) but is COUNTED in ``data_unknown`` for disclosure, never fabricated as
    nondeterminism evidence.
    """
    current: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    data_unknown = 0
    for sample in samples:
        sident = sample.get("identity")
        sident = sident if isinstance(sident, Mapping) else {}
        code_matches = all(
            identity.get(field) == sident.get(field) for field in _CODE_IDENTITY_FIELDS
        )
        if not code_matches:
            stale.append(dict(sample))
            continue
        q_data = identity.get(_DATA_IDENTITY_FIELD)
        s_data = sident.get(_DATA_IDENTITY_FIELD)
        if q_data and s_data:
            if q_data != s_data:
                stale.append(dict(sample))  # data drift — a different-data sample
                continue
        else:
            data_unknown += 1  # absent manifest on a side — disclosed, not excluded
        current.append(dict(sample))
    return current, stale, data_unknown


# ── the admission JOIN ───────────────────────────────────────────────────────


def _is_admitted(experiment_dir: Path, sample: Mapping[str, Any]) -> bool:
    """ONE admission rule (D-consume): a sample joins iff it got a PASSING verdict.

    * ``verdict == "auto_cleared"`` → admitted at append, code's passing verdict,
      no join needed (double-canary priors are ``auto_cleared`` by construction —
      without this the n=2 prior could never enter any envelope).
    * ``needs_verdict`` / ``mismatch`` → admitted ONLY when the REPRODUCTION run's
      decision journal (the run scope of the sample's SECOND ``run_ids`` member —
      the run whose receipts hold the comparison) carries a
      ``reproduction-verdict`` record whose ``resolved`` names the sample's
      ``content_sha`` TOKEN-EXACT with ``accept: true``. Token-exact on the
      bind-locked ``content_sha`` is the join key: an acceptance cannot name
      evidence that was never on disk, and a prefix-only naming does NOT admit.

    Nothing is ever admitted silently: an unresolved ``needs_verdict`` sample and
    an un-accepted ``mismatch`` sample both read inadmissible.
    """
    verdict = sample.get("verdict")
    if verdict == "auto_cleared":
        return True
    if verdict not in ("needs_verdict", "mismatch"):
        return False  # unknown verdict never admits

    content_sha = sample.get("content_sha")
    run_ids = sample.get("run_ids")
    if not content_sha or not isinstance(run_ids, (list, tuple)) or len(run_ids) < 2:
        return False
    repro_run_id = run_ids[1]
    if not isinstance(repro_run_id, str) or not repro_run_id:
        return False

    try:
        records = read_decisions(experiment_dir, "run", repro_run_id)
    except errors.SpecInvalid:
        return False
    for rec in records:
        if rec.get("block") != REPRODUCTION_VERDICT_BLOCK:
            continue
        resolved = rec.get("resolved")
        if not isinstance(resolved, Mapping):
            continue
        # accept: true AND the accepted content_sha names THIS sample token-exact.
        if resolved.get("accept") is True and resolved.get("content_sha") == content_sha:
            return True
    return False


def compute_admitted_flags(
    experiment_dir: Path,
    samples: Iterable[Mapping[str, Any]],
) -> tuple[list[bool], int]:
    """Compute the ``admitted_flags`` for *samples* → ``(flags, excluded_unadmitted)``.

    ``flags`` is aligned to *samples* (one bool each); ``excluded_unadmitted`` is
    the count of ``False`` — the disclosed, ledger-resident findings that inform
    the human but never the auto path (the no-silent-caps posture). This is the
    STORE-layer join that keeps T1 pure: the kernel reduces over
    ``(samples, admitted_flags)`` and never reads a journal.
    """
    flags = [_is_admitted(experiment_dir, sample) for sample in samples]
    excluded_unadmitted = sum(1 for f in flags if not f)
    return flags, excluded_unadmitted


# ── the envelope input for T1 ────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerEvidence:
    """The store's output for the pure kernel (T1): ``samples`` + ``admitted_flags``.

    The T1 kernel's envelope reduction, tiered classifier, and ``evidence_meets``
    are pure over ``(samples, admitted_flags)`` — this dataclass carries exactly
    that pair (aligned), plus the disclosure counts the evidence brief needs.

    * ``samples`` — the CURRENT-identity samples (the envelope's candidate set).
    * ``admitted_flags`` — aligned to ``samples``; True iff the sample's
      comparison received a passing verdict (the admission rule). The kernel folds
      the envelope over the ADMITTED samples only.
    * ``excluded_unadmitted`` — CURRENT samples that failed admission (disclosed).
    * ``stale`` — samples excluded by identity drift, retained as history.
    * ``data_unknown`` — CURRENT samples with an absent data manifest on a side.
    * ``malformed_skipped`` — corrupt ledger lines skipped by the tolerant read.
    """

    samples: list[dict[str, Any]]
    admitted_flags: list[bool]
    excluded_unadmitted: int
    stale: list[dict[str, Any]]
    data_unknown: int
    malformed_skipped: int


def load_evidence(
    experiment_dir: Path,
    *,
    cmd_sha: str,
    identity: Mapping[str, Any],
) -> LedgerEvidence:
    """Read the ledger, filter to CURRENT identity, and join admission → evidence.

    The full store-side pipeline the kernel consumes: tolerant-read the ledger
    for *cmd_sha* → partition to CURRENT-identity samples vs *identity* → compute
    the admission flags over the current set. Returns a :class:`LedgerEvidence`
    whose ``(samples, admitted_flags)`` is exactly the pair T1 reduces (D-consume
    clause 1 — judge before append is the caller's ordering; this read never
    includes a not-yet-appended comparison).
    """
    all_samples, malformed_skipped = read_samples(experiment_dir, cmd_sha)
    current, stale, data_unknown = partition_current_identity(all_samples, identity)
    admitted_flags, excluded_unadmitted = compute_admitted_flags(experiment_dir, current)
    return LedgerEvidence(
        samples=current,
        admitted_flags=admitted_flags,
        excluded_unadmitted=excluded_unadmitted,
        stale=stale,
        data_unknown=data_unknown,
        malformed_skipped=malformed_skipped,
    )
