"""Per-kind prerequisite-chain checkers — the composer ``check_chain`` (T4).

Design origin: ``docs/design/registration-kernel.md`` (R3 kind table, R4 evidence
floors + address chain). A registration NAMES its required prior attestations as
a chain of FULL ADDRESSES (:class:`~hpc_agent.state.registration.ChainEntry`);
this module answers, per entry, *does the named prerequisite read CURRENT at the
sha the registrant asserted?* — one :class:`SlotVerdict` per slot.

The one load-bearing rule (the enforcement-map "one kernel" row,
``docs/internals/engineering-principles.md``): :func:`check_chain` is PURE
DISPATCH. It never re-implements any member's currency logic — each kind routes
through its ONE existing definition:

* ``notebook-audit`` → :func:`~hpc_agent.state.notebook_audit.audit_module` +
  the gate layer's linked-source drift check
  (:func:`~hpc_agent.ops.notebook_gate._linked_source_drift`); the recomputed
  ``content_sha`` is the module sha
  (:func:`~hpc_agent.state.audit_source.sha256_normalized`).
* ``reproduction`` → the newest receipt in
  ``_aggregated/<repro_run_id>/reproduction_receipts.jsonl``
  (:func:`~hpc_agent.ops.verify_reproduction._receipt_path`), fresh iff no code
  drift since (:func:`~hpc_agent.state.code_drift.detect_code_drift`); the
  recomputed ``content_sha`` is the canonical-JSON sha of that newest receipt.
* ``scope-budget`` → :func:`~hpc_agent.state.scopes.count_prior_looks` +
  :func:`~hpc_agent.state.scopes.is_scope_locked`; the recomputed ``content_sha``
  is the canonical-JSON sha of ``{prior_looks, distinct_lineages, locked}``.
* ``pack-receipt`` → a LOUD not-yet-available refusal until domain-packs lands
  (the S6 reserved-seam posture; never a silent pass).
* ``attestation`` → :func:`~hpc_agent.state.attestation.reduce` over a named
  journal addressed by ``subject_id = "<scope_kind>:<scope_id>"``; the satisfying
  record's ``{block, attestor}`` are echoed VERBATIM into the evidence note.

Behavioral contract: a checker returning ``"stale"`` ALWAYS carries the
recorded-vs-recomputed sha pair; ``"absent"`` means the substrate/record does not
exist. :func:`check_chain` raises :class:`~hpc_agent.errors.SpecInvalid` ONLY for
structurally invalid input (an unknown kind, a bad ``requires`` key, ``requires``
on a kind that forbids it, or one of the not-yet-available kinds) — never for a
merely failing slot, which is a verdict, not an exception.

Pure local reads — no SSH, no ``_wire`` import, no scheduler.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state import attestation, code_drift, notebook_audit, scopes
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.determinism import evidence_meets, validate_sample
from hpc_agent.state.fingerprint_store import load_evidence
from hpc_agent.state.registration import (
    KIND_ATTESTATION,
    KIND_NOTEBOOK_AUDIT,
    KIND_PACK_RECEIPT,
    KIND_REPRODUCTION,
    KIND_SCOPE_BUDGET,
    UNCONTESTED_REQUIRES_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from pathlib import Path

    from hpc_agent.state.determinism import Sample
    from hpc_agent.state.registration import ChainEntry

__all__ = ["SlotVerdict", "check_chain"]

# --- per-slot status vocabulary ---------------------------------------------
#: The prerequisite was checked and reads CURRENT at the asserted sha.
CURRENT = "current"
#: The prerequisite exists but has moved — the recorded/recomputed pair differs
#: or its currency condition no longer holds (dated evidence, revoked for free).
STALE = "stale"
#: The substrate/record the entry names does not exist (nothing to compare).
ABSENT = "absent"

#: The determinism-fingerprint design — the substrate a ``reproduction``
#: ``requires`` evidence floor is checked against (R4, now WIRED): the newest
#: receipt's ``repro.cmd_sha`` addresses the ledger via
#: ``state/fingerprint_store.py::load_evidence`` (tolerant read + CURRENT-identity
#: partition + admission JOIN), and ONE ``state/determinism.py::evidence_meets``
#: call counts the ADMITTED current-identity samples against the caller floor.
_FINGERPRINT_DOC = "docs/design/determinism-fingerprint.md"

#: The CLOSED set of ``requires`` KEYS each kind may carry (R3/R4). An unknown key
#: for a kind is a loud :class:`errors.SpecInvalid` — an opted-in requirement core
#: cannot check must never silently pass (the dangling-reference posture). The
#: generic ``attestation`` kind and ``notebook-audit`` accept NONE. ``reproduction``
#: names the fingerprint's exact demand vocabulary (reserved — see
#: :data:`_FINGERPRINT_DOC`); ``scope-budget``'s budget key is PINNED to
#: ``max_looks`` (the plan left it unnamed — see the drift-log entry).
_REQUIRES_KEYS: dict[str, frozenset[str]] = {
    KIND_NOTEBOOK_AUDIT: frozenset(),
    KIND_REPRODUCTION: frozenset({"min_n", "min_n_full", "scales", "clusters"}),
    KIND_SCOPE_BUDGET: frozenset({"max_looks"}),
    KIND_PACK_RECEIPT: frozenset(),
    KIND_ATTESTATION: frozenset(),
}


@dataclass(frozen=True)
class SlotVerdict:
    """One prerequisite slot's currency verdict (R8's per-slot detail shape).

    * ``slot`` — the caller-authored slug the entry filled.
    * ``kind`` — the entry's :data:`~hpc_agent.state.registration.PREREQUISITE_KINDS`
      member.
    * ``status`` — :data:`CURRENT` / :data:`STALE` / :data:`ABSENT`.
    * ``recorded_sha`` — the entry's asserted ``content_sha`` (what the registrant
      reviewed at).
    * ``recomputed_sha`` — the checker's freshly recomputed sha, or ``None`` when
      the substrate is :data:`ABSENT` (nothing to recompute).
    * ``evidence_note`` — a code-rendered one-line disclosure of what filled (or
      failed to fill) the slot; for the ``attestation`` kind it echoes the
      satisfying record's ``{block, attestor}`` VERBATIM.
    """

    slot: str
    kind: str
    status: str
    recorded_sha: str
    recomputed_sha: str | None
    evidence_note: str


def _canonical_sha(obj: Any) -> str:
    """sha256 hexdigest of *obj*'s canonical JSON (harness-contract form).

    ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` per
    ``docs/internals/harness-contract.md`` "The sha canonicalization" — the ONE
    local canonicalization this module uses for the ``reproduction`` /
    ``scope-budget`` recomputed-evidence shas (no ``infra``/``state`` helper of
    this exact form exists to reuse; the ``ops/notebook/audit_view`` /
    ``ops/story_render`` copies are private view-sha helpers, not a shared seam).
    """
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _verdict(
    entry: ChainEntry,
    *,
    status: str,
    recomputed_sha: str | None,
    evidence_note: str,
) -> SlotVerdict:
    """Build a :class:`SlotVerdict` for *entry*, carrying its recorded sha."""
    return SlotVerdict(
        slot=entry.slot,
        kind=entry.kind,
        status=status,
        recorded_sha=entry.content_sha,
        recomputed_sha=recomputed_sha,
        evidence_note=evidence_note,
    )


def _reject_unknown_requires(entry: ChainEntry) -> None:
    """Refuse an unknown ``requires`` KEY for *entry*'s kind (R4 dangling-ref).

    An opted-in requirement core cannot check must never silently pass. The
    allowed set per kind is :data:`_REQUIRES_KEYS`; any key outside it is a loud
    :class:`errors.SpecInvalid`. (The ``attestation`` kind's takes-none rule is
    already pinned by the T1 loader, re-checked here for the composer's own
    inputs.)
    """
    # ``uncontested`` is the ONE cross-kind key (C-registration): every kind accepts
    # it, INCLUDING the otherwise requires-free attestation kind, because it is a
    # mechanism property core checks by counting standing challenges (never a domain
    # word). Every OTHER key stays kind-scoped and loud-refused.
    allowed = _REQUIRES_KEYS.get(entry.kind, frozenset()) | {UNCONTESTED_REQUIRES_KEY}
    unknown = sorted(k for k in entry.requires if k not in allowed)
    if unknown:
        raise errors.SpecInvalid(
            f"registration chain entry {entry.slot!r} (kind {entry.kind!r}): unknown "
            f"'requires' key(s) {unknown} — core cannot check a requirement it does not "
            f"understand, so it refuses rather than silently passing. Allowed for this "
            f"kind: {sorted(allowed)}."
        )


# --- notebook-audit ----------------------------------------------------------


def _check_notebook_audit(
    experiment_dir: Path, entry: ChainEntry, *, dossier_run_ids: Collection[str] | None
) -> SlotVerdict:
    """Currency of a notebook audit (R3): every required section signed/cleared
    AND the recomputed module sha equals the asserted ``content_sha``.

    Routes through :func:`~hpc_agent.state.notebook_audit.audit_module` for the
    per-section verdict and the gate layer's
    :func:`~hpc_agent.ops.notebook_gate._linked_source_drift` for the
    linked-dependency revocation — never a re-inlined sign-off reduction. The
    audited source/template ``.py`` are located via the interview ``audited_source``
    echo (the ONE opt-in read the submit gate uses); a missing echo, a mismatched
    ``audit_id``, or an unreadable source is :data:`ABSENT` (no such audit).
    """
    from hpc_agent.ops import notebook_gate

    echo = notebook_gate.audited_source_echo(experiment_dir)
    if echo is None or echo.get("audit_id") != entry.subject_id:
        return _verdict(
            entry,
            status=ABSENT,
            recomputed_sha=None,
            evidence_note=(
                f"no opted-in notebook audit {entry.subject_id!r} on interview.json's "
                "audited_source block"
            ),
        )
    source_rel = echo.get("source")
    template_rel = echo.get("template")
    try:
        source_text = (experiment_dir / str(source_rel)).read_text(encoding="utf-8")
        template_text = (experiment_dir / str(template_rel)).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _verdict(
            entry,
            status=ABSENT,
            recomputed_sha=None,
            evidence_note=(
                f"audit {entry.subject_id!r}: source {source_rel!r} or template "
                f"{template_rel!r} unreadable"
            ),
        )
    try:
        parsed_source = parse_percent_source(source_text)
        parsed_template = parse_percent_source(template_text)
    except errors.SpecInvalid as exc:
        return _verdict(
            entry,
            status=ABSENT,
            recomputed_sha=None,
            evidence_note=f"audit {entry.subject_id!r}: unparseable source/template ({exc})",
        )

    recomputed = sha256_normalized(source_text)
    audit = notebook_audit.audit_module(
        experiment_dir,
        entry.subject_id,
        source=parsed_source,
        required_slugs=parsed_template.slugs,
    )
    records = read_decisions(experiment_dir, "notebook", entry.subject_id)
    drift_note: str | None = None
    for sect in audit.sections:
        if sect.status not in notebook_audit.PASSING_STATUSES:
            continue
        drift = notebook_gate._linked_source_drift(
            experiment_dir,
            notebook_gate._winning_record(records, sect.slug, sect.signed_section_sha),
        )
        if drift is not None:
            drift_note = f"{sect.slug}: linked-source drift ({drift})"
            break

    sha_ok = recomputed == entry.content_sha
    if audit.passed and drift_note is None and sha_ok:
        return _verdict(
            entry,
            status=CURRENT,
            recomputed_sha=recomputed,
            evidence_note=f"audit {entry.subject_id!r}: all {len(audit.sections)} required "
            "sections signed_current/auto_cleared at the asserted module sha",
        )
    reasons: list[str] = []
    if not audit.passed:
        unsigned = [
            s.slug for s in audit.sections if s.status not in notebook_audit.PASSING_STATUSES
        ]
        reasons.append(f"unsigned/stale sections {unsigned}")
    if drift_note is not None:
        reasons.append(drift_note)
    if not sha_ok:
        reasons.append("module sha moved since the audit was asserted")
    return _verdict(
        entry,
        status=STALE,
        recomputed_sha=recomputed,
        evidence_note=f"audit {entry.subject_id!r} stale: {'; '.join(reasons)}",
    )


# --- reproduction ------------------------------------------------------------


def _newest_receipt(experiment_dir: Path, repro_run_id: str) -> dict[str, Any] | None:
    """The newest well-formed receipt record in the repro run's ledger, or ``None``.

    The ledger lives under the REPRODUCTION run
    (:func:`~hpc_agent.ops.verify_reproduction._receipt_path` →
    ``_aggregated/<repro_run_id>/reproduction_receipts.jsonl``); append order →
    the last valid line is the newest. Malformed lines are skipped (tolerant read).
    """
    # Facade form (``from hpc_agent.ops import <module>``): the direct
    # ``from hpc_agent.ops.verify_reproduction import ...`` spelling trips the
    # subject-import lint from inside the ``registration`` subject.
    from hpc_agent.ops import verify_reproduction

    path = verify_reproduction._receipt_path(experiment_dir, repro_run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    newest: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            newest = obj
    return newest


def _reproduction_evidence_floor(
    experiment_dir: Path,
    *,
    repro_ident: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    demand: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """The R4 fingerprint evidence floor — does the ledger meet the caller *demand*?

    The address chain the plan reserved (registration-kernel R4), now wired: the
    newest receipt's ``repro`` identity block supplies the ``cmd_sha`` the ledger
    keys on (the same key its own reproduction sample was written under); the run's
    sidecar supplies the ``executor`` the code-identity filter needs (the receipt
    identity block carries no ``executor`` field). ONE call each:
    :func:`~hpc_agent.state.fingerprint_store.load_evidence` (tolerant read +
    CURRENT-identity partition + the admission JOIN — so ``state`` stays pure) then
    :func:`~hpc_agent.state.determinism.evidence_meets` over the ADMITTED
    current-identity samples. Core matches identity and counts — it never
    interprets a metric.

    A MISSING ledger (or an empty ``cmd_sha``) is an ordinary shortfall (n=0)
    named in the returned dict — never a fabricated pass. Returns ``evidence_meets``'
    ``(met, shortfall)``.
    """
    cmd_sha = str(repro_ident.get("cmd_sha") or "")
    identity: dict[str, Any] = {
        "cmd_sha": repro_ident.get("cmd_sha"),
        "tasks_py_sha": repro_ident.get("tasks_py_sha"),
        "executor": sidecar.get("executor"),
    }
    samples: list[Sample] = []
    admitted: list[bool] = []
    if cmd_sha:
        try:
            evidence = load_evidence(experiment_dir, cmd_sha=cmd_sha, identity=identity)
        except errors.SpecInvalid:
            evidence = None
        if evidence is not None:
            for record, flag in zip(evidence.samples, evidence.admitted_flags, strict=False):
                try:
                    samples.append(validate_sample(record))
                except errors.SpecInvalid:
                    continue  # a malformed ledger line never counts toward the floor
                admitted.append(bool(flag))
    return evidence_meets(samples, admitted, demand, identity=identity)


def _check_reproduction(
    experiment_dir: Path, entry: ChainEntry, *, dossier_run_ids: Collection[str] | None
) -> SlotVerdict:
    """Currency of a reproduction receipt (R3/R4).

    Current iff: a verdict is recorded on the newest receipt, no code drift since
    (:func:`~hpc_agent.state.code_drift.detect_code_drift` over the receipt's
    recorded repro identity vs the current sidecar), the receipt links into the
    dossier (its ``original.run_id`` appears in *dossier_run_ids* when supplied),
    the recomputed canonical-JSON sha of that newest receipt equals the asserted
    ``content_sha``, AND — when the entry carries a ``requires`` evidence floor —
    the determinism-fingerprint ledger MEETS that floor (:func:`_reproduction_evidence_floor`,
    the R4 address chain, now wired: newest receipt → ``cmd_sha`` → ``load_evidence``
    → one ``evidence_meets`` call). A ledger that falls short (missing, too few
    samples, wrong scale/cluster) reads :data:`STALE` with the demand NAMED — an
    ordinary shortfall, never a fabricated pass. Unknown ``requires`` keys are
    still refused loudly by :func:`_reject_unknown_requires` upstream.
    """
    receipt = _newest_receipt(experiment_dir, entry.subject_id)
    if receipt is None or not receipt.get("overall"):
        return _verdict(
            entry,
            status=ABSENT,
            recomputed_sha=None,
            evidence_note=(
                f"no reproduction receipt with a recorded verdict under repro run "
                f"{entry.subject_id!r}"
            ),
        )

    recomputed = _canonical_sha(receipt)
    overall = receipt.get("overall")
    repro_raw = receipt.get("repro")
    repro_ident: dict[str, Any] = repro_raw if isinstance(repro_raw, dict) else {}
    original_raw = receipt.get("original")
    original_ident: dict[str, Any] = original_raw if isinstance(original_raw, dict) else {}

    # Code drift since the receipt was written: the receipt's recorded repro
    # identity vs the current sidecar (the ONE drift predicate; never re-inlined).
    from hpc_agent.state.runs import read_run_sidecar

    try:
        current_sidecar = read_run_sidecar(experiment_dir, entry.subject_id)
    except FileNotFoundError:
        current_sidecar = {}
    drift = code_drift.detect_code_drift(
        recorded_executor=None,  # the receipt identity carries no executor field
        recorded_tasks_py_sha=repro_ident.get("tasks_py_sha"),
        current_executor=None,
        current_tasks_py_sha=current_sidecar.get("tasks_py_sha"),
    )

    # Dossier cross-link (R3): an unrelated run's receipt cannot fill the slot.
    original_run_id = original_ident.get("run_id")
    cross_linked = dossier_run_ids is None or (
        original_run_id is not None and original_run_id in dossier_run_ids
    )

    sha_ok = recomputed == entry.content_sha

    # The R4 fingerprint evidence floor (the reserved seam, now WIRED). Absent a
    # ``requires`` floor the reproduction currency is exactly as before.
    floor_reason: str | None = None
    if entry.requires:
        floor_met, shortfall = _reproduction_evidence_floor(
            experiment_dir,
            repro_ident=repro_ident,
            sidecar=current_sidecar,
            demand=dict(entry.requires),
        )
        if not floor_met:
            floor_reason = (
                "fingerprint evidence floor unmet: "
                f"{json.dumps(shortfall, sort_keys=True)} (demand "
                f"{json.dumps(dict(entry.requires), sort_keys=True)})"
            )

    if not drift.drifted and cross_linked and sha_ok and floor_reason is None:
        note = (
            f"reproduction verdict {overall!r} for original {original_run_id!r} "
            f"(repro {entry.subject_id!r}); no code drift since"
        )
        if entry.requires:
            note += f"; fingerprint floor {json.dumps(dict(entry.requires), sort_keys=True)} met"
        return _verdict(entry, status=CURRENT, recomputed_sha=recomputed, evidence_note=note)
    reasons = []
    if drift.drifted:
        reasons.append("code drifted since the receipt (tasks_py_sha moved)")
    if not cross_linked:
        reasons.append(
            f"receipt's original {original_run_id!r} is not in the dossier's runs — an "
            "unrelated run's receipt cannot fill this slot"
        )
    if not sha_ok:
        reasons.append("newest-receipt sha moved (a later re-verify appended a newer receipt)")
    if floor_reason is not None:
        reasons.append(floor_reason)
    return _verdict(
        entry,
        status=STALE,
        recomputed_sha=recomputed,
        evidence_note=f"reproduction {entry.subject_id!r} stale: {'; '.join(reasons)}",
    )


# --- scope-budget ------------------------------------------------------------


def _check_scope_budget(
    experiment_dir: Path, entry: ChainEntry, *, dossier_run_ids: Collection[str] | None
) -> SlotVerdict:
    """Currency of a scope budget (R3): look count ``<=`` the caller's budget AND
    the scope is not locked, at the asserted evidence sha.

    The budget key is PINNED to ``requires: {"max_looks": <int>}`` (drift-logged).
    Routes through :func:`~hpc_agent.state.scopes.count_prior_looks` +
    :func:`~hpc_agent.state.scopes.is_scope_locked` — core COMPARES counts against
    a caller number, never picks the number. The recomputed sha is the
    canonical-JSON sha of ``{prior_looks, distinct_lineages, locked}`` (a new look
    moves it — dated evidence).
    """
    max_looks = entry.requires.get("max_looks")
    if not isinstance(max_looks, int) or isinstance(max_looks, bool):
        raise errors.SpecInvalid(
            f"registration chain entry {entry.slot!r} (kind {entry.kind!r}): a scope-budget "
            f"entry must declare its budget as requires: {{'max_looks': <int>}}; got "
            f"{entry.requires.get('max_looks')!r}. Core compares the look count against this "
            "number — it never picks a budget."
        )

    counts = scopes.count_prior_looks(experiment_dir, entry.subject_id)
    locked = scopes.is_scope_locked(experiment_dir, entry.subject_id)
    projection = {
        "prior_looks": counts["prior_looks"],
        "distinct_lineages": counts["distinct_lineages"],
        "locked": locked,
    }
    recomputed = _canonical_sha(projection)
    within_budget = counts["prior_looks"] <= max_looks
    sha_ok = recomputed == entry.content_sha
    if within_budget and not locked and sha_ok:
        return _verdict(
            entry,
            status=CURRENT,
            recomputed_sha=recomputed,
            evidence_note=(
                f"scope {entry.subject_id!r}: {counts['prior_looks']} look(s) <= budget "
                f"{max_looks}, not locked"
            ),
        )
    reasons = []
    if not within_budget:
        reasons.append(f"{counts['prior_looks']} look(s) exceed budget {max_looks}")
    if locked:
        reasons.append("scope is locked")
    if sha_ok and not reasons:
        reasons.append("evidence sha unchanged but condition failed")
    if not sha_ok:
        reasons.append("evidence sha moved (a new look or lock/unlock since)")
    return _verdict(
        entry,
        status=STALE,
        recomputed_sha=recomputed,
        evidence_note=f"scope {entry.subject_id!r} stale: {'; '.join(reasons)}",
    )


# --- pack-receipt (reserved) -------------------------------------------------


def _check_pack_receipt(
    experiment_dir: Path, entry: ChainEntry, *, dossier_run_ids: Collection[str] | None
) -> SlotVerdict:
    """RESERVED (R3): the domain-packs substrate has not landed.

    A LOUD not-yet-available refusal exactly as domain-packs S6 reserved this seam
    — never a silent pass. Lands as a real checker when
    ``state/pack_receipts.py`` ships.
    """
    raise errors.SpecInvalid(
        f"registration chain entry {entry.slot!r} (kind {entry.kind!r}): the pack-receipt "
        "substrate (state/pack_receipts.py, domain-packs) has not landed yet, so this kind "
        "cannot be checked — a reserved seam refused LOUDLY, never a silent pass. See "
        "docs/design/domain-packs.md."
    )


# --- attestation (the generic escape hatch) ----------------------------------


def _project_attestation(record: dict[str, Any], subject_id: str) -> dict[str, Any]:
    """Project a generic journal record to an attestation dict for the kernel.

    The escape-hatch convention (R3): a record satisfying the ``attestation`` kind
    carries ``resolved.attestor`` and ``resolved.content_sha``. Records lacking
    them fail :func:`~hpc_agent.state.attestation.validate` and are skipped by the
    reducer (tolerant read). ``subject_id`` is stamped as the journal-address key
    so :func:`~hpc_agent.state.attestation.reduce` reduces this address's records.
    """
    resolved = record.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    return {
        "attestor": resolved.get("attestor"),
        "subject_kind": "attestation",
        "subject_id": subject_id,
        "content_sha": resolved.get("content_sha"),
    }


def _check_attestation(
    experiment_dir: Path, entry: ChainEntry, *, dossier_run_ids: Collection[str] | None
) -> SlotVerdict:
    """Currency of a generic attestation (R3): the newest attestation in a named
    journal carries the entry's ``content_sha``.

    The journal address rides ``subject_id = "<scope_kind>:<scope_id>"`` (pinned +
    drift-logged — no prior convention existed). Routes the current/stale verdict
    through :func:`~hpc_agent.state.attestation.reduce` (never a re-inlined
    newest-first). The satisfying record's ``{block, attestor}`` are echoed
    VERBATIM into the note, so the brief discloses exactly what filled the slot
    (an ungated journal append is visible in the evidence, never silent).
    """
    scope_kind, sep, scope_id = entry.subject_id.partition(":")
    if not sep or not scope_kind or not scope_id:
        raise errors.SpecInvalid(
            f"registration chain entry {entry.slot!r} (kind {entry.kind!r}): the attestation "
            f"kind addresses its journal via subject_id='<scope_kind>:<scope_id>'; got "
            f"{entry.subject_id!r} (no ':' separator)."
        )

    records = read_decisions(experiment_dir, scope_kind, scope_id)
    projected = [_project_attestation(r, entry.subject_id) for r in records]

    # Find the newest VALID record for the recomputed sha + the {block, attestor}
    # echo — selection only; the drift VERDICT is the kernel's below.
    newest_record: dict[str, Any] | None = None
    newest_sha: str | None = None
    for record in records:
        try:
            att = attestation.validate(_project_attestation(record, entry.subject_id))
        except errors.SpecInvalid:
            continue
        newest_record = record
        newest_sha = att.content_sha

    if newest_record is None:
        return _verdict(
            entry,
            status=ABSENT,
            recomputed_sha=None,
            evidence_note=(
                f"no valid attestation in journal {scope_kind!r}/{scope_id!r} "
                f"(needs resolved.attestor + resolved.content_sha)"
            ),
        )

    verdict = attestation.reduce(
        projected, current_sha=entry.content_sha, subject_id=entry.subject_id
    )
    block = newest_record.get("block")
    attestor = _project_attestation(newest_record, entry.subject_id).get("attestor")
    echo = f"attestation block={block!r} attestor={attestor!r} in {scope_kind!r}/{scope_id!r}"
    if verdict == attestation.CURRENT:
        return _verdict(entry, status=CURRENT, recomputed_sha=newest_sha, evidence_note=echo)
    return _verdict(
        entry,
        status=STALE,
        recomputed_sha=newest_sha,
        evidence_note=f"{echo} carries an older sha than asserted",
    )


# --- the composer ------------------------------------------------------------

_DISPATCH = {
    KIND_NOTEBOOK_AUDIT: _check_notebook_audit,
    KIND_REPRODUCTION: _check_reproduction,
    KIND_SCOPE_BUDGET: _check_scope_budget,
    KIND_PACK_RECEIPT: _check_pack_receipt,
    KIND_ATTESTATION: _check_attestation,
}


def check_chain(
    experiment_dir: Path,
    entries: Sequence[ChainEntry],
    *,
    dossier_run_ids: Collection[str] | None = None,
) -> list[SlotVerdict]:
    """Check each prerequisite-chain entry's currency → one :class:`SlotVerdict` each.

    PURE DISPATCH (the enforcement-map "one kernel" row): each entry routes to its
    kind's ONE existing checker; this composer re-implements no member's currency
    logic. *dossier_run_ids*, when supplied, is the set of run ids the sealed
    dossier names — the ``reproduction`` checker refuses a receipt whose original
    run is not among them (an unrelated run cannot fill the slot).

    Raises :class:`~hpc_agent.errors.SpecInvalid` ONLY for STRUCTURALLY invalid
    input — an unknown kind, an unknown ``requires`` key, a ``requires`` a kind
    forbids, a not-yet-available kind (``pack-receipt``, or ``reproduction`` with a
    ``requires`` floor), or a malformed address. A merely failing prerequisite is
    a :data:`STALE` / :data:`ABSENT` verdict, never an exception (R4: partial
    registration is refused by the GATE reading these verdicts, not by a raise
    here).
    """
    verdicts: list[SlotVerdict] = []
    for entry in entries:
        _reject_unknown_requires(entry)
        checker = _DISPATCH.get(entry.kind)
        if checker is None:
            raise errors.SpecInvalid(
                f"registration chain entry {entry.slot!r}: kind {entry.kind!r} is not a "
                "checkable PREREQUISITE_KINDS member — no per-kind checker is registered."
            )
        verdict = checker(experiment_dir, entry, dossier_run_ids=dossier_run_ids)
        verdicts.append(_apply_uncontested_demand(experiment_dir, entry, verdict))
    return verdicts


def _apply_uncontested_demand(
    experiment_dir: Path, entry: ChainEntry, verdict: SlotVerdict
) -> SlotVerdict:
    """Downgrade *verdict* to :data:`STALE` when a declared ``uncontested`` demand is UNMET.

    C-registration, the ``evidence_meets`` declarative pattern: the caller opts in
    with ``requires: {"uncontested": true}``; core COUNTS standing challenges against
    the entry's ``content_sha`` (the ONE collector
    ``state/challenges.py::standing_challenges``, the D5 route-through — never a
    re-glob) and, when the OPEN count is non-zero, the slot reads :data:`STALE` with
    the challenge ids named. Core never decides on the challenge's MERITS — it counts.

    Only the declared demand gates: absent ``uncontested``, contest presence never
    reshapes the verdict (the never-blocking pin, T-NB). A challenge whose target no
    longer carries the sha reduces to ``superseded`` and drops out of the open count,
    so a remedied prerequisite passes again. Fail-open: any collector failure counts
    ZERO (a disclosure gap never blocks the chain — the codebase-wide read posture).
    """
    if entry.requires.get(UNCONTESTED_REQUIRES_KEY) is not True:
        return verdict
    open_n, ids = _uncontested_open_count(experiment_dir, entry.content_sha)
    if open_n <= 0:
        return verdict
    named = ", ".join(ids) if ids else "(ids unavailable)"
    return _verdict(
        entry,
        status=STALE,
        recomputed_sha=verdict.recomputed_sha,
        evidence_note=(
            f"uncontested demand UNMET: {open_n} open challenge(s) against the recorded "
            f"content_sha ({named}) — DISCLOSED and counted, the caller-declared gate"
        ),
    )


def _uncontested_open_count(experiment_dir: Path, content_sha: str) -> tuple[int, tuple[str, ...]]:
    """The OPEN standing-challenge count + ids against *content_sha* (fail-open).

    Routes through the ONE collector ``state/challenges.py::standing_challenges``
    (the C-disclose / C-registration route-through — never a private re-reduction).
    Returns ``(0, ())`` when uncontested, when nothing matches, or on ANY collector
    failure (fail-open: a challenge-store read gap never bricks a registration chain).
    """
    from hpc_agent.state.challenges import standing_challenges

    try:
        collected = standing_challenges(experiment_dir, content_sha=content_sha)
    except Exception:  # noqa: BLE001 — a disclosure gap never blocks the chain
        return 0, ()
    block = collected.contested
    if block is None or block.open <= 0:
        return 0, ()
    return block.open, tuple(block.challenge_ids)
