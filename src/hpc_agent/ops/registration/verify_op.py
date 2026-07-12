"""``verify-registration`` — the read-only consumer seat of the registration
kernel (``docs/design/registration-kernel.md`` R8, Wave-B T5).

Given a ``registration_id`` (or a ``run_id`` to find the registrations naming
it) this ``query`` primitive RECOMPUTES the prerequisite chain and the live
dossier signature AT READ TIME and REPORTS the reduced status
(``current | stale | revoked | superseded | absent``, R7), the per-leg detail
(R8), and a code-rendered markdown brief whose canonical-JSON sha is the
``view_sha`` a subsequent registration sign-off must carry (R6). It is a
REPORTER: every status — including ``absent`` — returns ok; it NEVER raises on a
stale/revoked/missing subject and it NEVER blocks. The deployment refusal lives
CALLER-SIDE (R8: "core does not own the deploy boundary"), wired against
``status`` in the consuming repo.

**This ``status`` is TIME-INDEPENDENT by design (R6).** This op takes NO ``now``
source and passes none into ``reduce_registration`` — so a lapsed
``review_horizon`` (live-conformance C-horizon, a TIME-based staleness) is NOT
reflected here. That is deliberate: the ``view_sha`` a sign-off binds is the
canonical-JSON sha of the reduced status + legs (R6), and admitting a wall-clock
``now`` would make an unchanged registration's witness drift by the hour. The
DEPLOYMENT gate's horizon leg therefore lives on the TIME-aware attention queue
(``ops/attention_queue.py::collect_registrations`` threads ``now`` into the ONE
``reduce_registration``; ``horizon_lapsed_registration_ids`` is its read helper),
NOT on this op — bug-sweep #48 arm (a), RULING 2 (2026-07-12): the time-aware
queue owns the deployment gate. The caller refuses on a non-``current`` ``status``
here OR a horizon-lapsed item there.

Boundary posture (``docs/internals/engineering-principles.md`` Q1): every value
this op touches is opaque caller data — field slugs, field values, ``subject_id``s,
evidence notes are counted, echoed, diffed by IDENTITY, never read for meaning.
The only vocabularies core owns here are the status set (R7) and
``PREREQUISITE_KINDS`` (T1).

The four recompute legs, all server-computed (never a trusted caller sha):

* **dossier** — the live ``bundle_sha256`` re-gathered through the ONE signature
  seam (:func:`~hpc_agent.ops.export_dossier.compute_dossier_signature`, T3) vs
  the sha the registration bound; ``drifted_stores`` diffs the recorded per-store
  entries against the live ones when the record carries them, else empty with the
  brief noting a sha-only comparison. A missing/moved run → the signature cannot
  be recomputed → the winner reads ``stale`` with the gap named (never a crash).
* **template** — the template file's raw-bytes sha on disk vs the recorded
  ``template_sha`` (R5: a template is bind-as-data, not percent-format Python, so
  ``normalize_source`` does NOT apply). Template drift is a DISCLOSED finding,
  never a silent revoke.
* **prerequisites** — T4's ``check_chain`` verdicts (``ops/registration/prereqs.py``)
  mapped 1:1 into :class:`~hpc_agent._wire.actions.verify_registration.PrerequisiteLeg`.
* **fields** — COUNTING (R5): every declared field slug that carries a non-empty
  value in the winner's ``resolved["fields"]`` is present; the rest are missing.
  Values are opaque and never interpreted.

The brief + ``view_sha`` are a PURE function of the reduced status + legs (R6's
fourth recompute leg depends on that): the T7 gate recomputes the same brief sha
from the same projection and binds it, so a witness you can regenerate is
regenerated rather than trusted.

Parallel-work seams (Wave B is file-disjoint; these are code-against contracts):

* **T4 — ``check_chain``.** Imported LATE inside :func:`_check_chain` so this
  module imports cleanly before ``ops/registration/prereqs.py`` lands (the
  intra-package late-binding idiom this codebase already uses for optional /
  in-flight siblings). Wrapped so a checker that raises (e.g. the reserved
  ``pack-receipt`` not-yet-available refusal) degrades to reported ``absent``
  legs — the reporter never raises.
* **T6 — the ``"registration"`` scope kind.** Records are read through the ONE
  reader (``state/decision_journal.py::read_decisions``) with scope kind
  ``"registration"``; it REFUSES until T6 adds the kind + the
  ``.hpc/registrations/<id>.decisions.jsonl`` path branch. The ``run_id`` lookup
  globs that same path (the ``# T6 seam`` below) — the orchestrator reconciles the
  path constant when T6 lands.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.actions.verify_registration import (
    DossierLeg,
    FieldsBlock,
    PrerequisiteKind,
    PrerequisiteLeg,
    SlotContested,
    TemplateLeg,
    TemplateStatus,
    VerifyRegistrationResult,
    VerifyRegistrationSpec,
)
from hpc_agent._wire.queries.challenge_status import ContestedCounts
from hpc_agent.cli._dispatch import CliShape, SchemaRef

# Reach the top-level ``ops/export_dossier.py`` module through the ``from
# hpc_agent.ops import <module>`` FACADE FORM — the direct
# ``from hpc_agent.ops.export_dossier import ...`` spelling trips the
# subject-import lint from inside the ``registration`` subject
# (``scripts/lint_subject_imports.py``). The module-level alias keeps
# ``verify_op.compute_dossier_signature`` a patchable attribute for tests.
from hpc_agent.ops import export_dossier
from hpc_agent.state.challenges import standing_challenges
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.registration import (
    ABSENT,
    CURRENT,
    REVOKED,
    STALE,
    SUPERSEDED,
    parse_chain_entry,
    parse_template,
    reduce_registration,
)

compute_dossier_signature = export_dossier.compute_dossier_signature  # type: ignore[attr-defined]

if TYPE_CHECKING:
    # ``DossierSignature`` is referenced as ``export_dossier.DossierSignature`` in
    # annotations (string-typed under ``from __future__ import annotations``) so no
    # direct ``ops.export_dossier`` import is needed here — the facade rule again.
    from hpc_agent.state.registration import ChainEntry

__all__ = ["verify_registration", "build_view"]

_JOURNAL_SUFFIX = ".decisions.jsonl"


# ── canonical JSON / hashing (the harness-contract form) ─────────────────────
# One local definition, matching docs/internals/harness-contract.md "The sha
# canonicalization": json.dumps sort_keys + compact separators + ensure_ascii=
# False, UTF-8, sha256 lowercase hex. The same serialization every view_sha in
# the system is taken over (ops/notebook/audit_view.py::_canonical_json is the
# reference); the T7 gate recomputes the brief sha through this identical form.


def _canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, compact separators, unicode kept as-is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha_json(obj: Any) -> str:
    """sha256 hexdigest of :func:`_canonical_json` of *obj* (utf-8)."""
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


# ── the T4 / T6 seams ────────────────────────────────────────────────────────


def _check_chain(experiment_dir: Path, entries: list[ChainEntry]) -> list[Any]:
    """Route to T4's per-kind chain checker (``ops/registration/prereqs.py``).

    Late import so this module loads before its Wave-B sibling lands (the
    intra-package late-binding idiom). Tests stub this seam directly.
    """
    from hpc_agent.ops.registration.prereqs import check_chain

    return list(check_chain(experiment_dir, entries))


def _read_records(experiment_dir: Path, registration_id: str) -> list[dict[str, Any]]:
    """Read a registration id's journal through the ONE reader (the T6 seam).

    ``read_decisions`` with scope kind ``"registration"`` is the one-definition
    reader; it REFUSES until T6 adds the kind. Records come back in append
    (chronological) order — the order :func:`reduce_registration` expects.
    """
    return read_decisions(experiment_dir, "registration", registration_id)


def _all_registration_ids(experiment_dir: Path) -> list[str]:
    """Every registration id with a journal on disk — the run_id-lookup scan.

    The registration journals live at
    ``.hpc/registrations/<registration_id>.decisions.jsonl`` — the directory is
    DERIVED from the ONE path definition (``decisions_path``, T6) rather than
    re-spelled here, so there is never a second path constant to drift.
    """
    from hpc_agent.state.decision_journal import decisions_path

    # decisions_path builds ``<dir>/<id>.decisions.jsonl``; ``.parent`` is the
    # registrations directory. The placeholder id is a throwaway valid slug.
    reg_dir = decisions_path(experiment_dir, "registration", "_").parent
    if not reg_dir.is_dir():
        return []
    ids: list[str] = []
    for p in sorted(reg_dir.glob(f"*{_JOURNAL_SUFFIX}")):
        ids.append(p.name[: -len(_JOURNAL_SUFFIX)])
    return ids


# ── resolution ────────────────────────────────────────────────────────────────


def _resolve_registration_id(experiment_dir: Path, spec: VerifyRegistrationSpec) -> str | None:
    """Resolve the id to verify: the named one, or the newest registration naming a run.

    For a ``run_id`` lookup, scans every registration journal, reduces each, and
    reports the id whose winner's ``resolved["run_id"]`` matches — newest by
    ``registered_at`` when several do. ``None`` when nothing matches (→ absent).
    """
    if spec.registration_id is not None:
        return str(spec.registration_id)

    run_id = str(spec.run_id)
    best_id: str | None = None
    best_ts = ""
    for rid in _all_registration_ids(experiment_dir):
        records = _read_records(experiment_dir, rid)
        status = reduce_registration(records, registration_id=rid, live_dossier_sha=None)
        if status.status == ABSENT:
            continue
        winner = status.winner or {}
        if winner.get("run_id") != run_id:
            continue
        ts = status.registered_at or ""
        if best_id is None or ts > best_ts:
            best_id, best_ts = rid, ts
    return best_id


# ── the per-leg recomputes ─────────────────────────────────────────────────────


def _recompute_dossier(
    experiment_dir: Path, winner: Mapping[str, Any]
) -> tuple[str | None, export_dossier.DossierSignature | None]:
    """Re-gather the winner's dossier signature LIVE, or ``(None, None)`` on any gap.

    The run_id names the run; ``include_lineage`` mirrors what the registration
    recorded (default False). A missing/moved run — or any read failure — yields
    ``None`` (→ the winner reads ``stale``), never a raise: this is a reporter.
    """
    run_id = winner.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None, None
    include_lineage = bool(winner.get("include_lineage", False))
    try:
        sig = compute_dossier_signature(experiment_dir, run_id, include_lineage=include_lineage)
    except Exception:  # noqa: BLE001 — a reporter never raises on a moved/absent subject
        return None, None
    return sig.bundle_sha256, sig


def _drifted_stores(
    winner: Mapping[str, Any], sig: export_dossier.DossierSignature | None
) -> list[str]:
    """Source-store names whose bytes moved since the dossier was sealed (R8).

    Derives from a per-store entry diff ONLY when the record carries the recorded
    ``dossier_entries`` breakdown; otherwise empty (a sha-only comparison — the
    brief discloses that the per-store breakdown was unavailable).
    """
    recorded = winner.get("dossier_entries")
    if not isinstance(recorded, list) or sig is None:
        return []
    rec_sha: dict[str, Any] = {}
    rec_src: dict[str, Any] = {}
    for e in recorded:
        if isinstance(e, Mapping):
            path = e.get("path")
            if isinstance(path, str):
                rec_sha[path] = e.get("sha256")
                rec_src[path] = e.get("source")
    live_sha: dict[str, Any] = {}
    live_src: dict[str, Any] = {}
    for e in sig.entries:
        if isinstance(e, Mapping):
            path = e.get("path")
            if isinstance(path, str):
                live_sha[path] = e.get("sha256")
                live_src[path] = e.get("source")
    drifted: set[str] = set()
    for path in set(rec_sha) | set(live_sha):
        if rec_sha.get(path) != live_sha.get(path):
            src = rec_src.get(path) or live_src.get(path)
            if isinstance(src, str) and src:
                drifted.add(src)
    return sorted(drifted)


def _dossier_leg(
    winner: Mapping[str, Any], live_sha: str | None, sig: export_dossier.DossierSignature | None
) -> DossierLeg:
    """The dossier signature leg: recorded vs live-recomputed + per-store drift."""
    recorded = winner.get("dossier_sha")
    return DossierLeg(
        recorded_sha=recorded if isinstance(recorded, str) else "",
        recomputed_sha=live_sha or "",
        drifted_stores=_drifted_stores(winner, sig),
    )


def _template_leg_and_fields(
    experiment_dir: Path, winner: Mapping[str, Any]
) -> tuple[TemplateLeg, list[str]]:
    """The template-drift leg (R5) + the declared field slugs off the template.

    Reads the on-disk template's RAW BYTES for the drift sha (one read); parses
    it for the declared field slugs. A missing file → ``recomputed_sha=""`` and
    ``stale``; an unparseable file still yields the raw-bytes drift verdict with
    an empty declared set (both disclosed, never a crash).
    """
    recorded = winner.get("template_sha")
    recorded_sha = recorded if isinstance(recorded, str) else ""
    template_rel = winner.get("template")
    recomputed_sha = ""
    declared: list[str] = []
    if isinstance(template_rel, str) and template_rel:
        data: bytes | None
        try:
            data = (Path(experiment_dir) / template_rel).read_bytes()
        except OSError:
            data = None
        if data is not None:
            recomputed_sha = hashlib.sha256(data).hexdigest()
            try:
                tmpl = parse_template(json.loads(data.decode("utf-8")), template_sha=recomputed_sha)
                declared = list(tmpl.fields)
            except (ValueError, UnicodeDecodeError, errors.SpecInvalid):
                declared = []
    status: TemplateStatus = (
        "current" if recorded_sha and recomputed_sha == recorded_sha else "stale"
    )
    return (
        TemplateLeg(status=status, recorded_sha=recorded_sha, recomputed_sha=recomputed_sha),
        declared,
    )


def _prerequisite_legs(experiment_dir: Path, winner: Mapping[str, Any]) -> list[PrerequisiteLeg]:
    """Map T4's ``check_chain`` verdicts 1:1 into :class:`PrerequisiteLeg` (R8).

    A malformed recorded chain, or a checker that raises (the reserved
    ``pack-receipt`` refusal), degrades to reported ``absent`` legs — the reporter
    never raises. The verdicts' shas/notes are echoed verbatim (opaque data).
    """
    raw = winner.get("prerequisites")
    if not isinstance(raw, list) or not raw:
        return []
    entries: list[ChainEntry] = []
    try:
        for e in raw:
            if isinstance(e, Mapping):
                entries.append(parse_chain_entry(e))
    except errors.SpecInvalid:
        entries = []
    if not entries:
        return [_absent_leg(e) for e in raw if isinstance(e, Mapping)]
    try:
        verdicts = _check_chain(experiment_dir, entries)
    except Exception as exc:  # noqa: BLE001 — a reporter never raises
        return [
            PrerequisiteLeg(
                slot=en.slot,
                kind=cast("PrerequisiteKind", en.kind),
                status="absent",
                recorded_sha=en.content_sha,
                recomputed_sha=None,
                evidence_note=f"chain check unavailable: {exc}",
            )
            for en in entries
        ]
    return [
        PrerequisiteLeg(
            slot=v.slot,
            kind=v.kind,
            status=v.status,
            recorded_sha=v.recorded_sha,
            recomputed_sha=v.recomputed_sha,
            evidence_note=v.evidence_note,
        )
        for v in verdicts
    ]


def _absent_leg(raw: Mapping[str, Any]) -> PrerequisiteLeg:
    """An ``absent`` prerequisite leg for a chain entry that would not parse."""
    slot = raw.get("slot")
    kind = raw.get("kind")
    return PrerequisiteLeg(
        slot=slot if isinstance(slot, str) and slot else "?",
        kind=cast("PrerequisiteKind", kind if isinstance(kind, str) else "attestation"),
        status="absent",
        recorded_sha=raw.get("content_sha") if isinstance(raw.get("content_sha"), str) else None,
        recomputed_sha=None,
        evidence_note="chain entry could not be parsed (malformed recorded address)",
    )


def _nonempty(value: Any) -> bool:
    """True when a field value counts as PRESENT — non-None, non-empty (opaque)."""
    if value is None:
        return False
    return not (isinstance(value, (str, list, tuple, dict, set)) and len(value) == 0)


def _fields_report(declared: list[str], winner: Mapping[str, Any]) -> FieldsBlock:
    """Template-field completeness by COUNTING (R5) — slugs opaque, never read."""
    resolved = winner.get("fields")
    resolved = resolved if isinstance(resolved, Mapping) else {}
    present = [s for s in declared if _nonempty(resolved.get(s))]
    missing = [s for s in declared if not _nonempty(resolved.get(s))]
    return FieldsBlock(declared=list(declared), present=present, missing=missing)


# ── the brief (pure render) ────────────────────────────────────────────────────

_STATUS_VERDICT: dict[str, str] = {
    CURRENT: (
        "CURRENT — the sealed dossier, the template, and every prerequisite still "
        "hold at read time."
    ),
    STALE: (
        "STALE — at least one leg drifted since registration. This is NOT a live "
        "clearance; re-registration is the remedy."
    ),
    REVOKED: "REVOKED — this registration was explicitly overturned; it authorizes nothing.",
    SUPERSEDED: "SUPERSEDED — a newer registration under this id is the live record.",
    ABSENT: "ABSENT — no registration record names this subject.",
}


def _sha_disp(value: Any) -> str:
    """Display a sha (or the em-dash placeholder when absent)."""
    return value if isinstance(value, str) and value else "—"


def _render_brief(projection: Mapping[str, Any]) -> str:
    """Render the human-facing markdown brief — a PURE function of *projection*.

    Deterministic and verdict-stating (the ``ops/relay_render.py`` posture): the
    same projection renders byte-identically every time, which is what lets the
    T7 gate recompute the ``view_sha`` as a fourth recompute leg (R6). Reports;
    never advises a deploy decision (that boundary is caller-side, R8).
    """
    status = str(projection.get("status"))
    reg_id = projection.get("registration_id")
    lines: list[str] = []
    heading_id = reg_id if isinstance(reg_id, str) and reg_id else "(none)"
    lines.append(f"# Registration {heading_id} — {status}")
    lines.append("")
    lines.append(_STATUS_VERDICT.get(status, status))
    registered_at = projection.get("registered_at")
    if isinstance(registered_at, str) and registered_at:
        lines.append("")
        lines.append(f"registered_at: {registered_at}")

    if status == ABSENT:
        return "\n".join(lines) + "\n"

    dossier = projection.get("dossier")
    if isinstance(dossier, Mapping):
        recorded = dossier.get("recorded_sha")
        recomputed = dossier.get("recomputed_sha")
        drifted = dossier.get("drifted_stores") or []
        lines.append("")
        lines.append("## Dossier")
        lines.append(f"- recorded:   {_sha_disp(recorded)}")
        lines.append(f"- recomputed: {_sha_disp(recomputed)}")
        if not (isinstance(recomputed, str) and recomputed):
            lines.append("- drift: dossier could not be recomputed (run missing/moved)")
        elif recorded == recomputed:
            lines.append("- drift: none — the live signature matches the sealed dossier")
        elif drifted:
            lines.append(f"- drift: stores moved — {', '.join(str(s) for s in drifted)}")
        else:
            lines.append(
                "- drift: signature differs (sha-only comparison — per-store breakdown unavailable)"
            )

    template = projection.get("template")
    if isinstance(template, Mapping):
        lines.append("")
        lines.append("## Template")
        lines.append(f"- status: {template.get('status')}")
        lines.append(f"- recorded:   {_sha_disp(template.get('recorded_sha'))}")
        lines.append(f"- recomputed: {_sha_disp(template.get('recomputed_sha'))}")
        if template.get("status") == "stale":
            lines.append(
                "- note: template drift is a DISCLOSED finding, not a revoke — a "
                "consumer requiring the new standard re-registers"
            )

    prereqs = projection.get("prerequisites") or []
    lines.append("")
    lines.append(f"## Prerequisites ({len(prereqs)})")
    if not prereqs:
        lines.append("- none declared")
    else:
        for p in prereqs:
            if not isinstance(p, Mapping):
                continue
            note = p.get("evidence_note")
            note_seg = f" — {note}" if isinstance(note, str) and note else ""
            lines.append(f"- {p.get('slot')} [{p.get('kind')}]: {p.get('status')}{note_seg}")

    fields = projection.get("fields")
    if isinstance(fields, Mapping):
        declared = fields.get("declared") or []
        present = fields.get("present") or []
        missing = fields.get("missing") or []
        lines.append("")
        lines.append("## Fields")
        lines.append(
            f"- declared: {len(declared)}; present: {len(present)}; missing: {len(missing)}"
        )
        if missing:
            lines.append(f"- missing: {', '.join(str(s) for s in missing)}")

    return "\n".join(lines) + "\n"


def build_view(
    *,
    status: str,
    registration_id: str | None,
    registered_at: str | None,
    dossier: DossierLeg | None,
    template: TemplateLeg | None,
    prerequisites: list[PrerequisiteLeg],
    fields: FieldsBlock,
) -> tuple[str, str]:
    """Render ``(brief, view_sha)`` from the reduced status + legs — a PURE function.

    The ONE deterministic projection→brief→view_sha definition (R6's fourth
    recompute leg). Both :func:`_finalize` (the verify-registration reporter) and
    the T7 append gate (``ops/decision/journal.py::_assert_registration_authorship``,
    reached through the ``ops/registration_view.py`` facade) call THIS, so a
    witness the gate recomputes over its own append-time legs is byte-identical to
    the one the reporter renders over the same inputs — a witness you can
    regenerate is regenerated, never trusted.
    """
    projection: dict[str, Any] = {
        "status": status,
        "registration_id": registration_id,
        "registered_at": registered_at,
        "dossier": dossier.model_dump() if dossier is not None else None,
        "template": template.model_dump() if template is not None else None,
        "prerequisites": [p.model_dump() for p in prerequisites],
        "fields": fields.model_dump(),
    }
    brief = _render_brief(projection)
    view_sha = _sha_json({**projection, "brief": brief})
    return brief, view_sha


def _finalize(
    *,
    status: str,
    registration_id: str | None,
    registered_at: str | None,
    dossier: DossierLeg | None,
    template: TemplateLeg | None,
    prerequisites: list[PrerequisiteLeg],
    fields: FieldsBlock,
) -> VerifyRegistrationResult:
    """Assemble the result, rendering the brief + ``view_sha`` from ONE projection.

    ``view_sha`` is the canonical-JSON sha of the projection INCLUDING the rendered
    brief — the witness a sign-off must carry (R6). Because the brief is a pure
    function of the structured projection (:func:`build_view`), the T7 gate
    recomputes both from the same reduced status + legs and binds the identical sha.
    """
    brief, view_sha = build_view(
        status=status,
        registration_id=registration_id,
        registered_at=registered_at,
        dossier=dossier,
        template=template,
        prerequisites=prerequisites,
        fields=fields,
    )
    return VerifyRegistrationResult(
        status=status,  # type: ignore[arg-type]
        registration_id=registration_id,
        registered_at=registered_at,
        dossier=dossier,
        template=template,
        prerequisites=prerequisites,
        fields=fields,
        brief=brief,
        view_sha=view_sha,
    )


# ── the C-disclose contested seam (route-through the ONE collector) ────────────
# Every ``contested`` block this seat surfaces routes through
# ``state/challenges.py::standing_challenges`` — the ONE collector (C-disclose
# enforcement row; the attention-queue D5 route-through form). Never a private
# re-glob or re-reduction; the T6 test pins ``standing_challenges`` in this source.
# DISCLOSED, never blocking (C4): ``status`` is whatever R7 computed and the flag
# rides beside it (C-status). Fail-open: any collector failure yields no block.


def _challenge_dossier_resolver(experiment_dir: Path) -> Any:
    """The INJECTED dossier resolver for target re-resolution (state never imports ops).

    A ``dossier`` target's newest signature is recomputed through the ONE seam
    (:func:`compute_dossier_signature`); an unresolvable dossier returns ``None`` →
    the collector DISCLOSES it (read side never raises). Mirrors the
    evidence-brief / conclusion-gate injection idiom.
    """

    def _resolve(ref: str) -> str | None:
        try:
            sig = compute_dossier_signature(experiment_dir, ref)
        except Exception:  # noqa: BLE001 — read side: any failure is "unresolvable here"
            return None
        return sig.bundle_sha256

    return _resolve


def _contested_counts(
    experiment_dir: Path, resolver: Any, **address: Any
) -> ContestedCounts | None:
    """Collect standing challenges for an address → :class:`ContestedCounts` or None.

    Routes through the ONE collector (:func:`standing_challenges`); returns the
    C-status counts + ids, or ``None`` when nothing is contested (the all-zero
    omission — ``StandingChallenges.contested`` is already ``None`` then). FAIL-OPEN:
    any collector error yields ``None`` (a disclosure gap never breaks the report,
    and never blocks — C4).
    """
    try:
        collected = standing_challenges(experiment_dir, dossier_resolver=resolver, **address)
    except Exception:  # noqa: BLE001 — a reporter's disclosure seat never raises
        return None
    block = collected.contested
    if block is None:
        return None
    return ContestedCounts(
        open=block.open,
        upheld=block.upheld,
        dismissed=block.dismissed,
        withdrawn=block.withdrawn,
        superseded=block.superseded,
        challenge_ids=list(block.challenge_ids),
    )


def _attach_contested(
    experiment_dir: Path, result: VerifyRegistrationResult
) -> VerifyRegistrationResult:
    """Attach the C-disclose ``contested`` blocks to *result* — OUTSIDE ``view_sha``.

    The registration's OWN standing challenges (addressed by
    ``{subject_kind='registration', subject_id}``) plus one :class:`SlotContested`
    per prerequisite whose ``recorded_sha`` (content_sha) is contested. Mutates the
    already-``_finalize``d result so the ``view_sha`` (computed over the deterministic
    projection) is NEVER perturbed by a later-filed challenge (R6); the contested
    brief lines are appended AFTER, for the same reason. All-zero blocks are omitted
    (the emitted-only-when-present precedent). Fail-open throughout.
    """
    if result.registration_id is None:
        return result  # absent — no subject to address
    resolver = _challenge_dossier_resolver(experiment_dir)

    own = _contested_counts(
        experiment_dir, resolver, subject_kind="registration", subject_id=result.registration_id
    )
    result.contested = own

    slot_blocks: list[SlotContested] = []
    for leg in result.prerequisites:
        if not leg.recorded_sha:
            continue
        block = _contested_counts(experiment_dir, resolver, content_sha=leg.recorded_sha)
        if block is not None:
            slot_blocks.append(SlotContested(slot=leg.slot, contested=block))
    result.prerequisite_contested = slot_blocks

    # Brief addendum — appended AFTER view_sha so a later challenge never drifts the
    # bound witness (R6). One line per open challenge (C-disclose).
    addendum = _render_contested_lines(own, slot_blocks)
    if addendum:
        result.brief = result.brief + addendum
    return result


def _render_contested_lines(own: ContestedCounts | None, slots: list[SlotContested]) -> str:
    """Render the contested brief addendum (dated, id-cited), or ``""`` when clean.

    NOT part of ``view_sha`` (see :func:`_attach_contested`). Mechanism-nouned:
    counts + ids only, no urgency vocabulary (the D1 no-urgency rule).
    """
    if own is None and not slots:
        return ""
    lines: list[str] = ["", "## Contested"]
    if own is not None:
        ids = ", ".join(own.challenge_ids)
        lines.append(
            f"- registration: {own.open} open · {own.upheld} upheld · "
            f"{own.dismissed} dismissed ({ids}) — DISCLOSED, not blocking"
        )
    for sc in slots:
        c = sc.contested
        ids = ", ".join(c.challenge_ids)
        lines.append(f"- prerequisite {sc.slot}: {c.open} open · {c.upheld} upheld ({ids})")
    return "\n".join(lines) + "\n"


# ── the primitive ──────────────────────────────────────────────────────────────


@primitive(
    name="verify-registration",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Verify a registration and REPORT its status (current | stale | revoked "
            "| superseded | absent) with per-leg detail — the live dossier signature "
            "re-gathered vs the sha it bound, the template raw-bytes drift, every "
            "prerequisite re-checked through its kind's one checker, and field "
            "completeness by counting. Takes exactly one of registration_id / run_id. "
            "A REPORTER: it never blocks and never raises on a stale/revoked/missing "
            "subject — the deploy refusal is wired caller-side against status. The "
            "brief's canonical-JSON sha is the view_sha a sign-off must carry. "
            "Read-only, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=VerifyRegistrationSpec,
        schema_ref=SchemaRef(input="verify_registration"),
    ),
    agent_facing=True,
)
def verify_registration(
    *, experiment_dir: Path, spec: VerifyRegistrationSpec
) -> VerifyRegistrationResult:
    """Verify one registration and REPORT its status + per-leg detail (R8).

    Resolves the ``registration_id`` (directly, or via the ``run_id`` lookup),
    reduces the id's journal to a status (R7), recomputes the four legs at read
    time, and renders the deterministic brief. Every status — including ``absent``
    — returns ok; this op never blocks and never raises on a drifted/missing
    subject (the SourceUnaudited fires/passes posture MINUS the raise).
    """
    experiment_dir = Path(experiment_dir)

    registration_id = _resolve_registration_id(experiment_dir, spec)
    if registration_id is None:
        return _finalize(
            status=ABSENT,
            registration_id=None,
            registered_at=None,
            dossier=None,
            template=None,
            prerequisites=[],
            fields=FieldsBlock(),
        )

    records = _read_records(experiment_dir, registration_id)
    # A first, live-sha-agnostic reduction locates the winner + its run_id.
    peek = reduce_registration(records, registration_id=registration_id, live_dossier_sha=None)

    if peek.status == ABSENT:
        return _finalize(
            status=ABSENT,
            registration_id=None,
            registered_at=None,
            dossier=None,
            template=None,
            prerequisites=[],
            fields=FieldsBlock(),
        )

    winner = peek.winner or {}

    if peek.status == REVOKED:
        # An explicit overturn binds no new sha (R7): report the withdrawal and
        # its timestamp; there is no dossier/template/chain to recompute.
        return _attach_contested(
            experiment_dir,
            _finalize(
                status=REVOKED,
                registration_id=registration_id,
                registered_at=peek.registered_at,
                dossier=None,
                template=None,
                prerequisites=[],
                fields=FieldsBlock(),
            ),
        )

    # The winner is a registration (SUPERSEDED never surfaces as the id's overall
    # status — the reduction describes the id by its winner). Recompute the legs.
    live_sha, sig = _recompute_dossier(experiment_dir, winner)
    reduced = reduce_registration(
        records, registration_id=registration_id, live_dossier_sha=live_sha
    )
    template_leg, declared = _template_leg_and_fields(experiment_dir, winner)
    prerequisite_legs = _prerequisite_legs(experiment_dir, winner)

    # R7 overall status: CURRENT requires the newest record to be a registration
    # whose live dossier signature (via the reduction) AND every prerequisite slot
    # still hold. The reduction covers only the dossier leg; a prerequisite that
    # now reads non-current flips the answer to STALE with the leg named (R7's
    # drift-revocation bullet, "any prerequisite that now reads stale flips the
    # answer to stale"). Template drift is DISCLOSED-only and never flips the
    # overall status (R5's recorded divergence from the pack-receipt posture).
    status = reduced.status
    if status == CURRENT and any(leg.status != "current" for leg in prerequisite_legs):
        status = STALE

    result = _finalize(
        status=status,
        registration_id=registration_id,
        registered_at=reduced.registered_at,
        dossier=_dossier_leg(winner, live_sha, sig),
        template=template_leg,
        prerequisites=prerequisite_legs,
        fields=_fields_report(declared, winner),
    )
    return _attach_contested(experiment_dir, result)
