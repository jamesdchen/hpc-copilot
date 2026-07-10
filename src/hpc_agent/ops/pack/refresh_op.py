"""``pack-refresh`` — re-seal stale pack manifests + rebind, journaled (auto-remedy).

The mutate half of the 2026-07-10 auto-remedy ruling ("the pack gate MAY
auto-remedy; latency is to be OBLITERATED", ``docs/design/domain-packs.md`` drift
log). Given an experiment dir it: (1) detects which BOUND packs' manifests are
STALE against on-disk bytes — the MINIMAL set (a stale rv manifest never forces a
quant rebuild); (2) re-seals each stale manifest GENERICALLY from its declarative
``sweep.json`` recipe (:mod:`hpc_agent.state.pack_sweep` — pure hashing, DP2 holds:
core never executes a pack build/check script); (3) re-binds each via the existing
``pack-bind`` path (:func:`hpc_agent.ops.pack.bind_op.pack_bind`), journaling old→new
shas — the drift event IS the archive record, which is why auto-remedy is sound;
(4) REPORTS which caller-authored receipt slots remain to re-earn and each one's
caller-side check command — **core never runs the check itself** (DP2).

:func:`refresh_opted_in_packs` is the reusable core the ``pack-refresh`` verb AND
the submit gate (:mod:`hpc_agent.ops.pack_gate`, auto-remedy) both call. It is
best-effort: a pack with no ``sweep.json`` recipe, or a broken recipe/dangling
manifest, is recorded with a note and skipped — the gate's own assert then raises
loud on any genuine broken setup, and the query reports it as data.

Lives inside the ``pack`` subject, reaching only ``state.*`` + the same-subject
``ops.pack.bind_op`` — the subject-imports lint is satisfied by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.pack_bind import PackBindSpec
from hpc_agent._wire.actions.pack_refresh import (
    PackRefreshEntry,
    PackRefreshResult,
    PackRefreshSpec,
    PackSlotToReearn,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.pack.bind_op import pack_bind
from hpc_agent.state import pack_sweep
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.pack_receipts import (
    CURRENT_PASSED,
    PACK_SUBJECT_KIND,
    current_bind,
    slot_status,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["pack_refresh", "refresh_opted_in_packs", "RefreshedPack", "read_packs_optin"]

_PRIMITIVE = "pack-refresh"

#: The reduced slot-status word → the wire/report status literal. A re-bind moves
#: the manifest sha so a covered receipt reduces STALE by construction.
_STATUS_WORD = {"current+passed": "current", "current+failed": "failed"}


@dataclass(frozen=True)
class RefreshedPack:
    """One pack's refresh outcome (the core's return; the verb maps it to wire)."""

    pack: str
    recipe_found: bool
    stale: bool
    rebound: bool
    old_manifest_sha: str | None
    new_manifest_sha: str | None
    added_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    note: str | None = None


def read_packs_optin(experiment_dir: Path) -> list[dict[str, Any]]:
    """The interview.json ``packs`` opt-in list, or ``[]`` when not opted in.

    Mirrors :func:`hpc_agent.ops.pack_gate._read_packs_optin` exactly (the D7
    probe: a missing/corrupt/non-object interview.json, or an absent ``packs``
    key, reads as not-opted-in → ``[]``). A PRESENT-but-malformed block (not a
    list) is a loud :class:`errors.SpecInvalid` — an opted-in-but-broken setup.
    """
    import json

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
                "{pack, manifest, receipt_bindings} objects; an opted-in repo with a "
                "malformed block is broken, not a silent pass"
            )
        return [e for e in block if isinstance(e, dict)]
    return []


def _unique_pack_entries(
    optin: list[dict[str, Any]], *, only_pack: str | None
) -> list[tuple[str, str]]:
    """The (pack_name, manifest_rel) pairs to refresh, de-duplicated, in opt-in order.

    Skips entries missing a string ``pack``/``manifest`` (a broken opt-in entry the
    gate/query surface loudly elsewhere — refresh is best-effort and never crashes
    on one). ``only_pack`` limits to a single pack.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for entry in optin:
        name = entry.get("pack")
        manifest_rel = entry.get("manifest")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(manifest_rel, str) or not manifest_rel:
            continue
        if only_pack is not None and name != only_pack:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append((name, manifest_rel))
    return out


def refresh_opted_in_packs(
    experiment_dir: Path,
    optin: list[dict[str, Any]],
    *,
    only_pack: str | None = None,
) -> list[RefreshedPack]:
    """Re-seal + rebind every stale opted-in pack manifest. Best-effort, per-pack.

    For each opted-in pack: locate its ``sweep.json`` recipe beside the manifest;
    if absent, record a note and leave the manifest untouched (core cannot
    generically re-seal without the declarative recipe). Otherwise re-seal ONLY if
    semantically stale (:func:`hpc_agent.state.pack_sweep.reseal_manifest`) and, on
    a write, rebind via the existing ``pack-bind`` path (journaling old→new). A
    broken recipe / vanished file / rebind refusal is caught per-pack and recorded
    as a note — the caller (gate assert / query) surfaces genuine breakage loudly.
    """
    results: list[RefreshedPack] = []
    for name, manifest_rel in _unique_pack_entries(optin, only_pack=only_pack):
        manifest_path = experiment_dir / manifest_rel
        recipe_path = pack_sweep.recipe_path_for(manifest_path)
        if not recipe_path.is_file():
            results.append(
                RefreshedPack(
                    pack=name,
                    recipe_found=False,
                    stale=False,
                    rebound=False,
                    old_manifest_sha=None,
                    new_manifest_sha=None,
                    note=(
                        f"no {pack_sweep.RECIPE_FILENAME} recipe beside "
                        f"{manifest_rel!r} — core cannot generically re-seal this "
                        "manifest; re-run the pack's own build script"
                    ),
                )
            )
            continue
        try:
            outcome = pack_sweep.reseal_manifest(manifest_path, recipe_path)
        except errors.SpecInvalid as exc:
            results.append(
                RefreshedPack(
                    pack=name,
                    recipe_found=True,
                    stale=False,
                    rebound=False,
                    old_manifest_sha=None,
                    new_manifest_sha=None,
                    note=f"could not re-seal: {exc}",
                )
            )
            continue

        rebound = False
        note: str | None = None
        if outcome.wrote:
            try:
                pack_bind(
                    experiment_dir=experiment_dir,
                    spec=PackBindSpec(manifest=manifest_rel, pack=name),
                )
                rebound = True
            except errors.SpecInvalid as exc:
                note = f"re-sealed but rebind refused: {exc}"

        results.append(
            RefreshedPack(
                pack=name,
                recipe_found=True,
                stale=outcome.stale,
                rebound=rebound,
                old_manifest_sha=outcome.old_manifest_sha,
                new_manifest_sha=outcome.new_manifest_sha,
                added_files=outcome.added_files,
                removed_files=outcome.removed_files,
                changed_files=outcome.changed_files,
                note=note,
            )
        )
    return results


def _receipt_bindings(entry: dict[str, Any]) -> list[dict[str, Any]]:
    raw = entry.get("receipt_bindings")
    return [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []


def slot_check_commands(optin: list[dict[str, Any]]) -> dict[tuple[str, str], str | None]:
    """Map ``(target_pack, slot) -> caller-authored check command`` (or ``None``).

    The receipt/check association is recorded caller-side on each
    ``receipt_bindings`` entry's opaque ``check`` field (DP4: a requirement AND its
    remedy originate with the caller). Core reads it as an opaque string it echoes
    as the remedy — never a command it runs (DP2).
    """
    out: dict[tuple[str, str], str | None] = {}
    for entry in optin:
        enclosing = entry.get("pack")
        for binding in _receipt_bindings(entry):
            slot = binding.get("slot")
            if not isinstance(slot, str) or not slot:
                continue
            target = binding.get("pack")
            target_name = target if isinstance(target, str) and target else enclosing
            if not isinstance(target_name, str) or not target_name:
                continue
            check = binding.get("check")
            out[(target_name, slot)] = check if isinstance(check, str) and check else None
    return out


def _slots_to_reearn(
    experiment_dir: Path,
    optin: list[dict[str, Any]],
    *,
    only_pack: str | None,
) -> list[PackSlotToReearn]:
    """Every caller-authored receipt slot NOT current+passed, with its check command.

    Read fresh AFTER the re-seal/rebind so the post-refresh drift shows: a re-bound
    pack's covered receipts read ``stale``. Each entry carries the opaque
    caller-side check command the driving skill runs to re-earn it.
    """
    checks = slot_check_commands(optin)
    records_cache: dict[str, Sequence[dict[str, Any]]] = {}

    def _records(pack: str) -> Sequence[dict[str, Any]]:
        if pack not in records_cache:
            records_cache[pack] = read_decisions(experiment_dir, PACK_SUBJECT_KIND, pack)
        return records_cache[pack]

    out: list[PackSlotToReearn] = []
    reported: set[tuple[str, str]] = set()
    for entry in optin:
        enclosing = entry.get("pack")
        for binding in _receipt_bindings(entry):
            slot = binding.get("slot")
            if not isinstance(slot, str) or not slot:
                continue
            target = binding.get("pack")
            target_name = target if isinstance(target, str) and target else enclosing
            if not isinstance(target_name, str) or not target_name:
                continue
            if only_pack is not None and target_name != only_pack:
                continue
            if (target_name, slot) in reported:
                continue
            records = _records(target_name)
            bind = current_bind(records, pack=target_name)
            status = slot_status(records, experiment_dir=experiment_dir, slot=slot, bind=bind)
            if status.status == CURRENT_PASSED:
                continue
            reported.add((target_name, slot))
            out.append(
                PackSlotToReearn(
                    slot=slot,
                    pack=target_name,
                    status=_STATUS_WORD.get(status.status, status.status),
                    check=checks.get((target_name, slot)),
                )
            )
    return out


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/<pack>/manifest.json"),
        SideEffect("file_write", "<experiment>/.hpc/packs/<pack>.decisions.jsonl"),
    ],
    error_codes=[errors.SpecInvalid],
    # Append-only rebind + an idempotent re-seal: a second call over unchanged
    # content finds nothing stale and writes/journals nothing (byte-identical
    # no-op); a call after a content edit re-seals + appends a fresh bind.
    idempotent=False,
    cli=CliShape(
        help=(
            "Re-seal every opted-in domain pack whose manifest is STALE against "
            "on-disk bytes (the minimal set — a stale pack never forces another's "
            "rebuild) from its declarative sweep.json recipe (pure hashing; core "
            "never runs a pack build/check script), rebind each via the pack-bind "
            "path (journaling old→new shas — the drift event is the archive "
            "record), and report which caller-authored receipt slots must be "
            "re-earned plus each one's caller-side check command (core never runs "
            "the check). Not opted in → empty and silent. Pure local read + "
            "manifest write + journal append, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=PackRefreshSpec,
        schema_ref=SchemaRef(input="pack_refresh"),
    ),
    agent_facing=True,
)
def pack_refresh(*, experiment_dir: Path, spec: PackRefreshSpec) -> PackRefreshResult:
    """Re-seal + rebind stale opted-in pack manifests; report slots to re-earn.

    Not opted in → empty :class:`PackRefreshResult`, byte-identical and silent.
    Opted in → re-seal each stale manifest from its ``sweep.json`` recipe, rebind
    it (journaling old→new), and report per pack what moved plus every
    caller-authored receipt slot now un-cleared and its check command.
    """
    experiment_dir = Path(experiment_dir)
    optin = read_packs_optin(experiment_dir)
    if not optin:
        return PackRefreshResult()

    refreshed = refresh_opted_in_packs(experiment_dir, optin, only_pack=spec.pack)
    to_reearn = _slots_to_reearn(experiment_dir, optin, only_pack=spec.pack)
    reearn_by_pack: dict[str, list[PackSlotToReearn]] = {}
    for slot in to_reearn:
        reearn_by_pack.setdefault(slot.pack, []).append(slot)

    entries: dict[str, PackRefreshEntry] = {}
    for rp in refreshed:
        entries[rp.pack] = PackRefreshEntry(
            pack=rp.pack,
            recipe_found=rp.recipe_found,
            stale=rp.stale,
            rebound=rp.rebound,
            old_manifest_sha=rp.old_manifest_sha,
            new_manifest_sha=rp.new_manifest_sha,
            added_files=rp.added_files,
            removed_files=rp.removed_files,
            changed_files=rp.changed_files,
            slots_to_reearn=reearn_by_pack.get(rp.pack, []),
            note=rp.note,
        )
    return PackRefreshResult(
        any_rebound=any(rp.rebound for rp in refreshed),
        refreshed=entries,
    )
