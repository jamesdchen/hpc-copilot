"""Domain-pack receipt gate — refuse a submit whose required pack receipts drifted.

The domain-pack substrate's gate (``docs/design/domain-packs.md``, "Receipt naming
+ the gate contract"): ONE definition, two synchronous seats. This module is that
ONE definition; :func:`assert_pack_receipts_current` is called at
:mod:`hpc_agent.ops.resolve_submit_inputs` (pre-sidecar, the S1 human boundary)
and :mod:`hpc_agent.ops.submit_flow` (pre-staging, before any rsync/SSH) — the
same defense-in-depth / gate-before-cluster-work shape the notebook graduation
gate (:mod:`hpc_agent.ops.notebook_gate`) wires at its two seats, next to it.

Opt-in + fail-safe, the ``ops/notebook_gate.py`` posture copied exactly (D7):
with NO ``packs`` block on ``interview.json`` the gate RETURNS silently and
byte-identically — zero filesystem probes beyond the single ``interview.json``
read (the seats already read that file). It fires ONLY inside the opted-in
surface.

The T9 refusal split:

* **Uncleared receipts** — an opted-in ``receipt_bindings`` slot whose reduction
  is not CURRENT **and** ``passed=true`` (missing / stale / failed) raises
  :class:`errors.PackReceiptsMissing` (``precondition_failed``) naming every
  failing slot and its status. Drift = unsigned by construction: a re-bind or a
  changed checked file reads STALE through the ONE currency reduction
  (:func:`hpc_agent.state.pack_receipts.slot_status`).
* **Broken setup** — an opted-in pack whose manifest is missing/unreadable/
  sha-drifted, whose on-disk pack files no longer match the bind, or whose
  ``receipt_bindings`` name a pack with no current bind, raises
  :class:`errors.SpecInvalid` naming the path/slot (the ``_read_required_py``
  posture: D7 silence applies ONLY to the absent opt-in block, resolved first). A
  silent pass on a dangling reference IS the uninstall-softens-gates laundering
  channel one layer up.

Pure local reads — no SSH, no ``_wire`` import, no scheduler.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.pack import load_manifest, sha256_file, verify_manifest_integrity
from hpc_agent.state.pack_receipts import current_bind, slot_status

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.ops.pack.refresh_op import CheckRun
    from hpc_agent.state.pack import PackManifest
    from hpc_agent.state.pack_receipts import PackBind

__all__ = ["assert_pack_receipts_current"]


def _read_packs_optin(experiment_dir: Path) -> list[dict[str, Any]]:
    """The interview.json ``packs`` opt-in list, or ``[]`` when not opted in.

    Mirrors :func:`hpc_agent.ops.notebook_gate._read_audited_source` /
    :func:`hpc_agent.state.pack_declarations._read_packs_optin`: the canonical
    campaign-dir root, ``.hpc/interview.json`` accepted defensively. A missing,
    corrupt, or non-object file, or an absent ``packs`` key, all read as "not
    opted in" → ``[]`` → the D7 silent no-op. This is the ONLY filesystem probe on
    the not-opted-in path.

    A PRESENT-but-malformed ``packs`` block (not a list) is an opted-in-but-broken
    setup → loud :class:`errors.SpecInvalid`, never a silent pass.
    """
    for rel in ("interview.json", ".hpc/interview.json"):
        path = experiment_dir / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        if "packs" not in doc:
            return []
        block = doc["packs"]
        if not isinstance(block, list):
            raise errors.SpecInvalid(
                "interview.json 'packs' opt-in block must be a list of "
                "{pack, manifest, receipt_bindings} objects; an opted-in repo with "
                "a malformed block is broken, not a silent pass"
            )
        return [e for e in block if isinstance(e, dict)]
    return []


def _read_pack_journal(experiment_dir: Path, pack_name: str) -> list[dict[str, Any]]:
    """Read the pack's decision journal in append order (newest last).

    T8 (Wave C) landed the dedicated ``"pack"`` decision-journal scope kind + its
    ``.hpc/packs/<name>.decisions.jsonl`` path branch, so this routes through the
    ONE journal reader — ``read_decisions(experiment_dir, "pack", name)`` — rather
    than re-deriving the path (mirrors ``ops/pack/bind_op._read_pack_records`` and
    ``state/pack_declarations._read_pack_journal``, both reconciled the same way).
    A not-yet-created journal → ``[]``; one corrupt line never strands the trail.
    ``pack_name`` is a validated slug here (``manifest.name``), so the reader's
    scope validation never raises.
    """
    return read_decisions(experiment_dir, "pack", pack_name)


def _receipt_bindings(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``receipt_bindings`` list of one opt-in entry (defensively filtered)."""
    raw = entry.get("receipt_bindings")
    return [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []


def _verify_against_bind(
    manifest_path: Path, manifest: PackManifest, *, pack: str, manifest_sha: str
) -> None:
    """Refuse if the on-disk pack drifted from the CURRENT bind's recorded shas.

    Mirrors :func:`hpc_agent.state.pack_declarations._verify_against_bind` (the
    ``_linked_source_drift`` pattern anchored to the bind): the manifest's own
    raw-bytes sha must still equal the bind's ``manifest_sha`` (a re-generated
    manifest is drift), and every listed file's on-disk raw sha must still equal
    its recorded sha (a changed pack file is drift even before any re-bind). Either
    drift is loud — the drift-revocation the whole design earns.
    """
    disk_manifest_sha = sha256_file(manifest_path)
    if disk_manifest_sha != manifest_sha:
        raise errors.SpecInvalid(
            f"pack {pack!r}: manifest on disk ({disk_manifest_sha}) no longer "
            f"matches the current bind ({manifest_sha}). Editing pack standards "
            "without re-binding revokes every clearance signed under the old sha."
        )
    verify_manifest_integrity(manifest_path.parent, manifest)


def _resolve_current_packs(
    experiment_dir: Path, optin: list[dict[str, Any]]
) -> tuple[dict[str, PackBind], dict[str, list[dict[str, Any]]]]:
    """Verify every opted-in pack is CURRENT; return its per-pack bind + records.

    Loud :class:`errors.SpecInvalid` on any dangling/drifted opted-in pack (a
    missing/unreadable/mismatched manifest, no current bind, or an on-disk sha
    drift). The returned indices key by pack name so cross-pack ``receipt_bindings``
    resolve through them.
    """
    binds_by_pack: dict[str, PackBind] = {}
    records_by_pack: dict[str, list[dict[str, Any]]] = {}
    for entry in optin:
        pack_name = entry.get("pack")
        if not isinstance(pack_name, str) or not pack_name:
            raise errors.SpecInvalid(
                "interview.json 'packs' entry is missing a string 'pack' name — a "
                "dangling opt-in reference is broken, not a silent pass"
            )
        if pack_name in binds_by_pack:
            continue  # already verified (a pack opted in twice)
        manifest_rel = entry.get("manifest")
        if not isinstance(manifest_rel, str) or not manifest_rel:
            raise errors.SpecInvalid(
                f"pack {pack_name!r}: 'packs' entry declares no 'manifest' relpath — "
                "a dangling opt-in reference is broken, not a silent pass"
            )
        manifest_path = experiment_dir / manifest_rel
        manifest = load_manifest(manifest_path)  # loud on missing/unreadable/bad JSON
        if manifest.name != pack_name:
            raise errors.SpecInvalid(
                f"pack opt-in names {pack_name!r} but manifest {manifest_rel!r} "
                f"declares {manifest.name!r} — the reference is dangling/mismatched"
            )
        records = _read_pack_journal(experiment_dir, pack_name)
        bind = current_bind(records, pack=pack_name)
        if bind is None:
            raise errors.SpecInvalid(
                f"pack {pack_name!r}: opted in but has no CURRENT bind — a dangling "
                "receipt/pack reference is loud, never a silent pass (bind the pack "
                "via pack-bind before opting in)"
            )
        _verify_against_bind(
            manifest_path, manifest, pack=pack_name, manifest_sha=bind.manifest_sha
        )
        binds_by_pack[pack_name] = bind
        records_by_pack[pack_name] = records
    return binds_by_pack, records_by_pack


def _compute_slot_failures(
    experiment_dir: Path,
    optin: list[dict[str, Any]],
    binds_by_pack: dict[str, PackBind],
    records_by_pack: dict[str, list[dict[str, Any]]],
    checks_by_key: dict[tuple[str, str], str | None],
) -> tuple[list[tuple[str, str]], dict[str, str | None], dict[tuple[str, str], str]]:
    """Reduce every receipt slot to a failure list + its check + the run targets.

    Returns ``(failures, checks_by_slot, run_targets)``:

    * ``failures`` — ``(slot, status-or-reason)`` for every slot NOT current+passed.
    * ``checks_by_slot`` — ``slot -> caller check command (or None)``, for the
      refusal envelope.
    * ``run_targets`` — ``(target_pack, slot) -> check command`` for the FAILING
      slots that declare a check (what the auto-remedy runs; DP2-safe caller-side
      execution).

    A slot bound to a pack that is not opted-in/current is a dangling reference —
    LOUD :class:`errors.SpecInvalid` (a silent pass on a dangling reference is the
    uninstall-softens-gates laundering channel).
    """
    failures: list[tuple[str, str]] = []
    checks_by_slot: dict[str, str | None] = {}
    run_targets: dict[tuple[str, str], str] = {}
    for entry in optin:
        for binding in _receipt_bindings(entry):
            slot = binding.get("slot")
            if not isinstance(slot, str) or not slot:
                continue  # the wire model already refuses this; shape-tolerant here
            target = binding.get("pack")
            target_name = target if isinstance(target, str) and target else entry.get("pack")
            if target_name not in binds_by_pack:
                raise errors.SpecInvalid(
                    f"receipt slot {slot!r} is bound to pack {target_name!r}, which "
                    "is not an opted-in/current pack — a dangling receipt reference "
                    "is loud, never a silent pass"
                )
            status = slot_status(
                records_by_pack[target_name],
                experiment_dir=experiment_dir,
                slot=slot,
                bind=binds_by_pack[target_name],
            )
            if not status.passing:
                failures.append((slot, status.reason or status.status))
                check = checks_by_key.get((str(target_name), slot))
                checks_by_slot[slot] = check
                if check:
                    run_targets[(str(target_name), slot)] = check
    return failures, checks_by_slot, run_targets


def _check_outcome(run: CheckRun) -> str:
    """A one-line human summary of a surviving check run for the refusal envelope."""
    if run.spawn_error is not None:
        return f"check could not run ({run.spawn_error})"
    if run.timed_out:
        return f"check timed out; tail: {run.stderr_tail or run.stdout_tail}".strip()
    if run.exit_code != 0:
        return f"check exited {run.exit_code}; tail: {run.stderr_tail or run.stdout_tail}".strip()
    # Ran clean but the slot is still not current+passed (e.g. the check recorded
    # passed=false, or receipted a different slot than the one still failing).
    return (
        f"check exited 0 but the slot is still not current+passed; tail: {run.stdout_tail}"
    ).strip()


def assert_pack_receipts_current(experiment_dir: Path) -> None:
    """Refuse a submit whose opted-in domain-pack receipts are not signed-current.

    Loads ``interview.json``'s ``packs`` opt-in block. ABSENT (not opted in) →
    RETURN silently, byte-identically (D7 fail-safe — no further filesystem
    probes). PRESENT → verify every opted-in pack is CURRENT (a dangling/drifted
    manifest or an unbound pack is a LOUD :class:`errors.SpecInvalid`), then for
    every caller-authored ``receipt_bindings`` slot require the ONE currency
    reduction (:func:`hpc_agent.state.pack_receipts.slot_status`) to be CURRENT
    **and** ``passed=true``. Any uncleared slot (missing / stale / failed) raises
    :class:`errors.PackReceiptsMissing` NAMING every offending slot and its status.

    Local reads + the auto-remedy's subprocessed caller checks — no SSH, no pack
    code imported. The two submit seats call this ONE definition.
    """
    optin = _read_packs_optin(experiment_dir)
    if not optin:
        return  # D7 fail-safe: not opted in → byte-identical no-op

    # AUTO-REMEDY step 1 (2026-07-10 ruling: "the pack gate MAY auto-remedy; latency
    # is to be OBLITERATED"). Re-seal + rebind any manifest that is merely STALE
    # against on-disk bytes (from its sweep.json recipe, pure hashing — DP2 holds,
    # no pack code runs), journaling old→new shas (the drift event IS the archive
    # record). Best-effort: a pack with no recipe, or a genuine broken setup, is left
    # for the assert below to refuse loudly. A re-seal is a NO-OP when nothing is
    # stale, so a clean gate stays byte-identical.
    from hpc_agent.ops.pack.refresh_op import (
        refresh_opted_in_packs,
        run_slot_checks,
        slot_check_commands,
    )

    refresh_opted_in_packs(experiment_dir, optin)
    checks_by_key = slot_check_commands(optin)

    # Every opted-in pack must be CURRENT (broken setup → SpecInvalid), building the
    # per-pack bind + records index cross-pack slots resolve through; then reduce
    # every receipt slot to current+passed.
    binds_by_pack, records_by_pack = _resolve_current_packs(experiment_dir, optin)
    failures, checks_by_slot, run_targets = _compute_slot_failures(
        experiment_dir, optin, binds_by_pack, records_by_pack, checks_by_key
    )
    if not failures:
        return

    # AUTO-REMEDY step 2 (2026-07-10 evening ruling, CONVERSION 1 — "prose cannot be
    # load-bearing"): the caller-authored check command re-earns a drifted receipt.
    # Instead of RELYING ON SKILL PROSE to run it, the gate EXECUTES it itself — a
    # subprocess in the experiment dir (the executor precedent; DP2 bans importing
    # pack logic, not orchestrating caller-side execution), captured + journaled —
    # then re-evaluates. A passing check appended a fresh receipt at the now-current
    # bind, so the slot clears with ZERO refusals and zero human turns.
    check_runs: dict[tuple[str, str], CheckRun] = {}
    if run_targets:
        check_runs = run_slot_checks(experiment_dir, run_targets)
        # Re-read journals FRESH (a passing check appended a receipt) and re-evaluate.
        binds_by_pack, records_by_pack = _resolve_current_packs(experiment_dir, optin)
        failures, checks_by_slot, _ = _compute_slot_failures(
            experiment_dir, optin, binds_by_pack, records_by_pack, checks_by_key
        )
        if not failures:
            return  # every uncleared slot re-earned by its check — gate clears

    # The refusal SURVIVES — only when a slot declared no check, its check
    # failed/timed out, or it still isn't current+passed after the check ran. Name
    # each surviving slot, its check command, and (when a check ran) its outcome.
    outcomes_by_slot = {
        slot: _check_outcome(run) for (_pack, slot), run in check_runs.items() if not run.ok
    }
    raise errors.PackReceiptsMissing.for_slots(
        failures, checks=checks_by_slot, check_runs=outcomes_by_slot
    )
