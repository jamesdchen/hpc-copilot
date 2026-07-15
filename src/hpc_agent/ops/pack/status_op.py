"""``pack-status`` — a read-only digest of an experiment's domain-pack state.

A ``query`` primitive (domain-packs T6, ``docs/design/domain-packs.md``): given a
``PackStatusSpec`` (a pack name, or omitted → every opted-in pack), it reports, per
pack:

* the **current bind** — the newest valid ``pack-bind`` in force (or null when the
  pack was never bound), via :func:`hpc_agent.state.pack_receipts.current_bind`;
* the **per-slot receipt status** — each caller-authored ``receipt_bindings`` slot
  reduced to ``current`` / ``failed`` / ``stale`` / ``missing`` via
  :func:`hpc_agent.state.pack_receipts.slot_status` (the ONE currency reduction,
  routed through the attestation kernel);
* the **unfillable-requirement report** — a ``receipt_bindings`` slot whose bound
  pack's manifest ``fills_slots`` does not list it. ADVISORY / identity-only:
  ``fills_slots`` never becomes load-bearing (DP4 — a requirement always
  originates with the caller), so this is an early warning, never a gate;
* the **dangling-reference findings** — an opted-in manifest that is
  missing/unreadable/sha-drifted, or a slot bound to a pack with no current bind.

**A query never RAISES for a dangling reference — it REPORTS it.** The loud
refusals (``SpecInvalid`` on a dangling manifest, ``precondition_failed`` on an
uncleared slot) live in the mutate verbs and the gate (T4/T5/T9). ``pack-status``
is a read: it surfaces the same facts as data so a human/agent can see the broken
setup without a submit being blocked.

**Not opted in at all → empty result, SILENT.** No ``packs`` block on
interview.json returns an empty :class:`PackStatusResult` with zero filesystem
probes beyond the single interview.json read (the D7 posture,
``ops/notebook_gate.py::_read_audited_source`` template). A repo that never opted
in never pays.

Pure read: no SSH, no scheduler, ``idempotent=True``. The status is derived state,
recomputed from interview.json + the pack journals + the pack files on disk on
every call — no cache, no second source of truth.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Literal

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.actions.pack_status import (
    PackBind,
    PackDanglingReference,
    PackLineage,
    PackSlotStatus,
    PackStatusEntry,
    PackStatusResult,
    PackStatusSpec,
    PackUnfillableRequirement,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state import decision_journal
from hpc_agent.state.interview_doc import iter_interview_docs
from hpc_agent.state.pack import (
    PackManifest,
    load_manifest,
    sha256_file,
    verify_manifest_integrity,
)
from hpc_agent.state.pack_receipts import (
    CURRENT_FAILED,
    CURRENT_PASSED,
    MISSING,
    PACK_BIND_BLOCK,
    PACK_SUBJECT_KIND,
    STALE,
    current_bind,
    slot_status,
)
from hpc_agent.state.pack_receipts import (
    PackBind as StatePackBind,
)

__all__ = ["pack_status"]

#: The reduced slot-status word (``state/pack_receipts.py`` vocabulary) → the wire
#: ``PackSlotStatus.status`` literal. Mechanical mapping, never interpreted.
_WIRE_STATUS: dict[str, Literal["current", "stale", "missing", "failed"]] = {
    CURRENT_PASSED: "current",
    CURRENT_FAILED: "failed",
    STALE: "stale",
    MISSING: "missing",
}


def _read_packs_optin(experiment_dir: Path) -> list[dict[str, Any]] | None:
    """The interview.json ``packs`` opt-in block, or ``None`` when not opted in.

    Mirrors ``ops/notebook_gate.py::_read_audited_source``: the canonical
    campaign-dir root, ``.hpc/interview.json`` accepted defensively; a missing,
    corrupt, or non-object file, or an absent/malformed ``packs`` key, all read as
    "not opted in" → ``None`` → the D7 silent empty result. This is the ONLY
    filesystem probe on the not-opted-in path.

    # T8a seam (LANDED): ``_wire/actions/interview.py::InterviewSpec.packs`` is
    # the typed source of this block — ``list[PackOptIn]`` where
    # ``PackOptIn = {pack, manifest, receipt_bindings: [ReceiptBinding{slot, pack}]}``.
    # This raw read is intentionally shape-tolerant (it is the D7 gate probe, not
    # the writer) but agrees with the typed shape exactly: same keys, same nesting.
    """
    for doc in iter_interview_docs(experiment_dir):
        block = doc.get("packs")
        if isinstance(block, list):
            return [e for e in block if isinstance(e, dict)]
    return None


def _receipt_bindings(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``receipt_bindings`` list of one opt-in entry (defensively filtered)."""
    raw = entry.get("receipt_bindings")
    return [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []


def _bound_at(records: list[dict[str, Any]], manifest_sha: str) -> str:
    """The ``ts`` of the newest ``pack-bind`` record at *manifest_sha* (else ``""``).

    Append order → the last matching record is the current bind's; its timestamp
    is an honest metadatum for the digest. Absent (a projected-but-tsless record)
    reads ``""`` — never fabricated.
    """
    bound_at = ""
    for record in records:
        if record.get("block") != PACK_BIND_BLOCK:
            continue
        resolved = record.get("resolved")
        if isinstance(resolved, dict) and resolved.get("manifest_sha") == manifest_sha:
            ts = record.get("ts")
            if isinstance(ts, str):
                bound_at = ts
    return bound_at


def _load_manifest_or_dangling(
    experiment_dir: Path, entry: dict[str, Any]
) -> tuple[PackManifest | None, PackDanglingReference | None]:
    """Load + integrity-check an entry's manifest; a failure REPORTS, never raises.

    A missing/unreadable/non-JSON manifest, or an on-disk sha drift, is a dangling
    reference the query surfaces as data (the mutate verbs raise; a read reports).
    Returns ``(manifest, None)`` on success, ``(None, finding)`` otherwise.
    """
    manifest_rel = entry.get("manifest")
    if not isinstance(manifest_rel, str) or not manifest_rel:
        return None, PackDanglingReference(
            reason="opted-in pack entry declares no manifest relpath", path=None
        )
    manifest_path = experiment_dir / manifest_rel
    try:
        manifest = load_manifest(manifest_path)
        verify_manifest_integrity(manifest_path.parent, manifest)
    except errors.SpecInvalid as exc:
        return None, PackDanglingReference(reason=str(exc), path=manifest_rel)
    return manifest, None


def _resolved_bind(records: list[dict[str, Any]]) -> StatePackBind | None:
    """The current bind over one pack's journal (reads via the ONE reduction)."""
    return current_bind(records)


def _lineage_echo(
    experiment_dir: Path,
    manifest: PackManifest | None,
    *,
    entries: dict[str, dict[str, Any]],
    manifests_by_pack: dict[str, PackManifest | None],
    binds_by_pack: dict[str, StatePackBind | None],
) -> PackLineage | None:
    """The lineage + freshness echo for a PROGRAM pack (DC10), or ``None``.

    ``None`` unless the pack's manifest carries a ``derived_from`` stamp. Freshness
    compares the recorded seam sha against the currently-bound SOURCE pack's seam
    file (identified by ``derived_from.pack`` name among co-bound packs — DC2,
    NEVER by sha equality). A mismatch reports ``behind`` but never severs the edge;
    an unbound/unresolvable source reports ``source-not-bound``.
    """
    if manifest is None or manifest.derived_from is None:
        return None
    df = manifest.derived_from
    freshness: str = "source-not-bound"
    source_bind = binds_by_pack.get(df.pack)
    source_manifest = manifests_by_pack.get(df.pack)
    source_entry = entries.get(df.pack)
    if source_bind is not None and source_manifest is not None and source_entry is not None:
        seam_rel = source_manifest.seams.get(df.seam)
        source_manifest_rel = source_entry.get("manifest")
        if isinstance(seam_rel, str) and seam_rel and isinstance(source_manifest_rel, str):
            seam_path = (experiment_dir / source_manifest_rel).parent / seam_rel
            try:
                freshness = "current" if sha256_file(seam_path) == df.sha else "behind"
            except (OSError, UnicodeDecodeError):
                freshness = "source-not-bound"
    return PackLineage(
        pack=df.pack,
        seam=df.seam,
        version=df.version,
        sha=df.sha,
        freshness=freshness,  # type: ignore[arg-type]
    )


def _status_for_pack(
    experiment_dir: Path,
    pack_name: str,
    entry: dict[str, Any],
    *,
    entries: dict[str, dict[str, Any]],
    records_by_pack: dict[str, list[dict[str, Any]]],
    manifests_by_pack: dict[str, PackManifest | None],
    binds_by_pack: dict[str, StatePackBind | None],
    dangling_by_pack: dict[str, PackDanglingReference | None],
) -> PackStatusEntry:
    """Build the full digest for one reported pack from the precomputed indices."""
    dangling: list[PackDanglingReference] = []
    own_manifest_dangling = dangling_by_pack.get(pack_name)
    if own_manifest_dangling is not None:
        dangling.append(own_manifest_dangling)

    bind_state = binds_by_pack.get(pack_name)
    bind_wire: PackBind | None = None
    audit_template: str | None = None
    if bind_state is not None:
        bind_wire = PackBind(
            pack=bind_state.pack,
            version=bind_state.version or "",
            manifest_sha=bind_state.manifest_sha,
            bound_at=_bound_at(records_by_pack.get(pack_name, []), bind_state.manifest_sha),
        )
        # Run-#12 finding 1: COMPOSE the audit-template default from the bound
        # pack's ``audit_template`` seam (identity/pointer only). Resolve the seam
        # relpath (manifest-dir-relative) to an experiment-dir-relative path — what
        # the caller would pass as ``template`` — so the on-ramp presents a
        # confirm-default rather than an open question. Only when the pack is
        # current-bound AND declares the seam; a manifest missing/drifted → None.
        manifest = manifests_by_pack.get(pack_name)
        manifest_rel = entry.get("manifest")
        if manifest is not None and isinstance(manifest_rel, str) and manifest_rel:
            seam_rel = manifest.seams.get("audit_template")
            if isinstance(seam_rel, str) and seam_rel:
                template_path = (experiment_dir / manifest_rel).parent / seam_rel
                with contextlib.suppress(ValueError):  # cross-drive (Windows) → None
                    audit_template = os.path.relpath(template_path, experiment_dir).replace(
                        os.sep, "/"
                    )

    slots: list[PackSlotStatus] = []
    unfillable: list[PackUnfillableRequirement] = []
    for binding in _receipt_bindings(entry):
        slot = binding.get("slot")
        target = binding.get("pack")
        if not isinstance(slot, str) or not slot:
            continue
        # The slot resolves against the pack the caller bound it to (usually this
        # pack; cross-pack references are resolved via the opt-in index).
        target_name = target if isinstance(target, str) and target else pack_name
        target_records = records_by_pack.get(target_name)
        target_bind = binds_by_pack.get(target_name)
        target_manifest = manifests_by_pack.get(target_name)

        if target_records is None:
            # A slot bound to a pack that is not opted in at all — a dangling
            # reference (reported, not raised). No journal to reduce → missing.
            dangling.append(
                PackDanglingReference(
                    reason=(
                        f"slot {slot!r} is bound to pack {target_name!r}, which is not "
                        "an opted-in pack"
                    ),
                    slot=slot,
                )
            )
            slots.append(PackSlotStatus(slot=slot, status="missing", passed=None, reason="missing"))
            continue

        if target_bind is None:
            # Opted in but never bound (or the bind is stale): a slot cannot be
            # current relative to a pack with no current bind — loud in the gate,
            # reported here.
            dangling.append(
                PackDanglingReference(
                    reason=(
                        f"slot {slot!r} is bound to pack {target_name!r}, which has no current bind"
                    ),
                    slot=slot,
                )
            )

        reduced = slot_status(
            target_records, experiment_dir=experiment_dir, slot=slot, bind=target_bind
        )
        slots.append(
            PackSlotStatus(
                slot=slot,
                status=_WIRE_STATUS[reduced.status],
                passed=reduced.passed,
                reason=reduced.reason,
            )
        )

        # Advisory: the bound pack's manifest does not claim it can fill this slot.
        if target_manifest is not None and slot not in target_manifest.fills_slots:
            unfillable.append(
                PackUnfillableRequirement(
                    slot=slot,
                    pack=target_name,
                    reason=(
                        f"pack {target_name!r} manifest fills_slots does not list "
                        f"{slot!r} (advisory — a requirement originates with the caller)"
                    ),
                )
            )

    return PackStatusEntry(
        bind=bind_wire,
        slots=slots,
        unfillable=unfillable,
        dangling=dangling,
        audit_template=audit_template,
        derived_from=_lineage_echo(
            experiment_dir,
            manifests_by_pack.get(pack_name),
            entries=entries,
            manifests_by_pack=manifests_by_pack,
            binds_by_pack=binds_by_pack,
        ),
    )


@primitive(
    name="pack-status",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Report an experiment's domain-pack state: per opted-in pack, the "
            "current bind (or null), each caller-authored receipt slot's currency "
            "(current / failed / stale / missing), an advisory unfillable-requirement "
            "report (a slot the bound pack's manifest fills_slots omits), and "
            "dangling-reference findings (a missing/sha-drifted manifest, or a slot "
            "bound to an unbound pack). Read-only, no SSH; a query REPORTS a dangling "
            "reference as data, never raising — the loud refusals live in the mutate "
            "verbs and the gate. Not opted in → empty and silent. Recomputed on every "
            "call (no cache)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=PackStatusSpec,
        schema_ref=SchemaRef(input="pack_status"),
    ),
    agent_facing=True,
)
def pack_status(*, experiment_dir: Path, spec: PackStatusSpec) -> PackStatusResult:
    """Digest every opted-in pack (or the one named), read-only.

    Not opted in → empty :class:`PackStatusResult`, byte-identical and silent. Opted
    in → one :class:`PackStatusEntry` per reported pack (keyed by pack name, the
    ``scope-status`` precedent), carrying the current bind, per-slot receipt
    currency, the advisory unfillable report, and dangling-reference findings.

    Never raises for a broken setup: a dangling manifest or an unbound slot is
    REPORTED as a finding, not a refusal (the query/mutate split — the loud
    refusals live in ``pack-bind``/``pack-record-receipt`` and the gate).
    """
    experiment_dir = Path(experiment_dir)
    optin = _read_packs_optin(experiment_dir)
    if not optin:
        return PackStatusResult()

    # Index the opt-in entries by pack name (cross-pack receipt_bindings resolve
    # through this), and precompute each pack's journal, current bind, and manifest
    # ONCE — a pack referenced by several slots is read a single time.
    entries: dict[str, dict[str, Any]] = {}
    for entry in optin:
        name = entry.get("pack")
        if isinstance(name, str) and name and name not in entries:
            entries[name] = entry

    records_by_pack: dict[str, list[dict[str, Any]]] = {}
    manifests_by_pack: dict[str, PackManifest | None] = {}
    binds_by_pack: dict[str, StatePackBind | None] = {}
    dangling_by_pack: dict[str, PackDanglingReference | None] = {}
    for name, entry in entries.items():
        # T8 (Wave C) landed the ``"pack"`` scope kind + the
        # ``.hpc/packs/<name>.decisions.jsonl`` path branch on
        # state/decision_journal.py. Read via the ONE decision-journal reader.
        records_by_pack[name] = decision_journal.read_decisions(
            experiment_dir, PACK_SUBJECT_KIND, name
        )
        manifest, finding = _load_manifest_or_dangling(experiment_dir, entry)
        manifests_by_pack[name] = manifest
        dangling_by_pack[name] = finding
        binds_by_pack[name] = _resolved_bind(records_by_pack[name])

    reported: dict[str, PackStatusEntry] = {}
    for name, entry in entries.items():
        if spec.pack is not None and name != spec.pack:
            continue
        reported[name] = _status_for_pack(
            experiment_dir,
            name,
            entry,
            entries=entries,
            records_by_pack=records_by_pack,
            manifests_by_pack=manifests_by_pack,
            binds_by_pack=binds_by_pack,
            dangling_by_pack=dangling_by_pack,
        )
    return PackStatusResult(packs=reported)
