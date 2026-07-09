"""``pack-bind`` — enter pack content into an experiment AS DATA, un-fakeably.

The bind event (``docs/design/domain-packs.md``, "The bind event"). Binding is
the explicit, journalable moment a caller pins a set of DOMAIN STANDARDS — a
pack manifest and every file it lists, by raw-bytes sha — to an experiment. A
``mutate`` verb: given a caller-referenced manifest relpath, it reads the
manifest ON DISK, recomputes every listed file's raw-bytes SHA-256 (refusing on
any drift), and appends a CODE attestation under the pack's decision journal.

**The recompute IS the lock (DP1, D5 lock 2).** No sha is caller-suppliable:
the verb recomputes the manifest sha and every file sha server-side, and binds
through the ONE attestation kernel (:func:`hpc_agent.state.attestation.bind`)
against the FRESH manifest hash — a bind can no more assert a sha into existence
than a human sign-off can. Consequence by construction: pack content changes →
the manifest sha moves → drift-revocation (``attestation.reduce``) fires on
every clearance signed under the old standards, with no state machine.

**Loud on a dangling reference (the D7/opted-in split).** A missing/unreadable
manifest, or any listed file whose on-disk sha no longer matches, is a broken
opted-in setup — a loud :class:`errors.SpecInvalid` naming the path and both
shas, never a silent pass (the ``ops/notebook_gate._read_required_py`` posture).
Silent D7 absence belongs to the interview opt-in read (T8a), not here: reaching
``pack-bind`` at all means the caller intends to bind.

Core never imports, executes, or interprets a manifest-named file: this verb
reads bytes and hashes them (``state/pack.py`` shape-only loaders), nothing more
(DP2/DP3).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.pack_bind import (
    PackBindResult,
    PackBindSpec,
    PackFileEntry,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state import attestation, pack
from hpc_agent.state.decision_journal import append_decision, read_decisions
from hpc_agent.state.pack_receipts import PACK_BIND_BLOCK, PACK_SUBJECT_KIND

if TYPE_CHECKING:
    from hpc_agent.state.pack import PackManifest

__all__ = ["pack_bind"]

_PRIMITIVE = "pack-bind"

#: The bind record's mechanical response — an honest string, NEVER a human-ack
#: token (the ``record_auto_clear`` / ``notebook-render-receipt`` naming
#: discipline). A CODE attestation has no human to ack; "bound" states what the
#: verb mechanically did.
_BOUND_RESPONSE = "bound"


def _resolve_manifest(experiment_dir: Path, relpath: str) -> Path:
    """Resolve the caller-referenced manifest relpath against the experiment dir.

    The ``_AuditedSource.source`` posture: a campaign-dir-relative path core
    reads and hashes — never a blessed directory, never a search path (DP1).
    An already-absolute path is honoured as-is.
    """
    path = Path(relpath)
    if not path.is_absolute():
        path = Path(experiment_dir) / path
    return path


# --- T8 reconciled ----------------------------------------------------------
#
# T8 (Wave C) landed the dedicated ``"pack"`` scope kind + the
# ``.hpc/packs/<name>.decisions.jsonl`` path branch on
# ``state/decision_journal.py``. These two thin wrappers now route through the
# ONE decision-journal writer/reader — no journal I/O is re-implemented here, and
# the path derives from the single ``decisions_path`` definition. ``pack-bind`` is
# a mechanical CODE attestation whose ``block="pack-bind"`` / ``response="bound"``
# clear ``append_decision``'s only validation (non-empty scope/block/response, a
# filesystem-safe slug id); it carries no gate stack a mechanical record could
# fail, so the writer routes through ``append_decision`` exactly like the sibling
# ``pack-record-receipt`` (T5). Record shape is byte-identical to the prior
# hand-rolled writer. ``_read_pack_records`` stays as the state-layer read name the
# bind atom tests import.


def _append_pack_record(
    experiment_dir: Path,
    *,
    pack_name: str,
    block: str,
    response: str,
    resolved: dict[str, Any],
) -> dict[str, Any]:
    # T8: route through the ONE decision-journal writer (scope kind "pack").
    return append_decision(
        experiment_dir,
        scope_kind=PACK_SUBJECT_KIND,  # "pack"
        scope_id=pack_name,
        block=block,
        response=response,
        resolved=resolved,
    )


def _read_pack_records(experiment_dir: Path, pack_name: str) -> list[dict[str, Any]]:
    # T8: route through the ONE decision-journal reader (scope kind "pack"),
    # which tolerates a not-yet-created journal (returns []).
    return read_decisions(experiment_dir, PACK_SUBJECT_KIND, pack_name)


def _bind_resolved(manifest: PackManifest, manifest_sha: str) -> dict[str, Any]:
    """Build the bind record's ``resolved`` block — identity/pointer echo only."""
    return {
        "pack": manifest.name,
        "version": manifest.version,
        "manifest_sha": manifest_sha,
        "files": [{"path": f.path, "sha256": f.sha256} for f in manifest.files],
        "seams": sorted(manifest.seams),
    }


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/packs/<pack>.decisions.jsonl"),
    ],
    error_codes=[errors.SpecInvalid],
    # Append-only: each bind journals a fresh record. A re-bind at a new manifest
    # sha appends a newer record that makes the old bind STALE (the reduction
    # kernel decides currency on read); retries are safe but not byte-idempotent,
    # exactly like append-decision / notebook-record-receipt.
    idempotent=False,
    cli=CliShape(
        help=(
            "Bind a domain pack into an experiment AS DATA: read the "
            "caller-referenced manifest ON DISK, recompute every listed file's "
            "raw-bytes SHA-256 and the manifest sha server-side (no sha is "
            "caller-suppliable), refuse loudly on any drift or a "
            "missing/unreadable manifest, then append a CODE attestation "
            "(block pack-bind, response 'bound') under the pack's decision "
            "journal, bound through the one kernel against the fresh manifest "
            "hash. Editing pack content later moves the sha and revokes every "
            "clearance signed under the old standards — no drift state machine. "
            "Core never imports, executes, or interprets a pack file. Pure local "
            "read + journal append, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=PackBindSpec,
        schema_ref=SchemaRef(input="pack_bind"),
    ),
    agent_facing=True,
)
def pack_bind(*, experiment_dir: Path, spec: PackBindSpec) -> PackBindResult:
    """Bind a pack manifest into *experiment_dir*; journal the CODE attestation.

    Resolves *spec.manifest* against *experiment_dir*, reads + parses it, checks
    the optional *spec.pack* cross-check, recomputes every listed file's
    raw-bytes sha (refusing on any drift), binds the manifest sha through the ONE
    attestation kernel, appends the bind record, and echoes what was bound.

    Raises :class:`errors.SpecInvalid` on a missing/unreadable/invalid manifest,
    a ``pack`` cross-check mismatch, or any file sha drift (all loud — a dangling
    opted-in reference is a broken setup, never a silent pass).
    """
    experiment_dir = Path(experiment_dir)
    manifest_path = _resolve_manifest(experiment_dir, spec.manifest)

    # Read + parse (loud SpecInvalid on missing/unreadable/non-JSON/bad-shape).
    manifest = pack.load_manifest(manifest_path)

    # Optional caller cross-check: the manifest's own name stays authoritative,
    # but a mismatch means the caller is binding the wrong manifest — refuse.
    if spec.pack is not None and spec.pack != manifest.name:
        raise errors.SpecInvalid(
            f"pack-bind cross-check failed: caller expected pack {spec.pack!r} but "
            f"manifest {str(manifest_path)!r} declares name {manifest.name!r}"
        )

    # Recompute EVERY listed file's raw-bytes sha against disk — loud on any
    # missing file or mismatch (the drift-revocation the whole design earns).
    pack.verify_manifest_integrity(manifest_path.parent, manifest)

    # The manifest file's own raw-bytes sha IS the pack identity sha.
    manifest_sha = pack.sha256_file(manifest_path)
    resolved = _bind_resolved(manifest, manifest_sha)

    # Project to a CODE attestation and bind through the ONE kernel with the
    # recompute wired to the fresh manifest hash — a bind can no more assert a
    # sha into existence than a sign-off can (D5 lock 2). This validates the
    # record shape AND recompute-compares content_sha == manifest_sha.
    attestation.bind(
        {
            "attestor": "code",
            "subject_kind": PACK_SUBJECT_KIND,
            "subject_id": manifest.name,
            "content_sha": manifest_sha,
        },
        recompute=manifest_sha,
    )

    _append_pack_record(
        experiment_dir,
        pack_name=manifest.name,
        block=PACK_BIND_BLOCK,
        response=_BOUND_RESPONSE,
        resolved=resolved,
    )

    return PackBindResult(
        pack=manifest.name,
        version=manifest.version,
        manifest_sha=manifest_sha,
        files=[PackFileEntry(path=f.path, sha256=f.sha256) for f in manifest.files],
        seams=sorted(manifest.seams),
    )
