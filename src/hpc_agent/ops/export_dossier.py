"""Export-dossier — bundle a run's core-owned record trail into one sealed archive.

A ``mutate`` primitive with a single local write and NO SSH. Given a
``run_id`` (optionally its whole supersession lineage) it gathers every
concrete on-disk store the run left behind — the sidecar, the run decision
journal, the emitted briefs, the detached-block terminals, the journal
record, the scope journals + look ledgers for each scope tag the run carries,
the harvested aggregate artifacts, and the determinism-fingerprint ledger —
and copies each store's bytes verbatim into one ``.zip`` with an integrity
manifest, so a repo-side renderer can build an evidence package FROM the bundle
without the control plane ever knowing what any entry MEANS.

Boundary posture (see ``docs/internals/engineering-principles.md`` Q1,
"substrate, not semantics"): an entry is typed by the SOURCE STORE it came from
— one of the closed :data:`DOSSIER_SOURCES` store nouns — and by nothing else.
The manifest describes the bundle by IDENTITY + COUNTING (which store, where,
its sha256, its byte size); it never names a caller-owned role. In particular
the aggregated store is copied as RAW BYTES: this module never ``json``-parses
anything under ``_aggregated/`` (a deliberately-invalid-JSON aggregate must
round-trip byte-identical), so it can never grow an interpretation of the
numbers it seals. The structured fields it DOES read (the identity projection,
the scope tags) come back through :func:`state.runs.read_run_sidecar` /
:func:`state.journal.load_run` — the parse lives in those modules, never here.

Disclosure-at-graduation, deliberately stale-on-append (the determinism-
fingerprint T8 leg, ``docs/design/determinism-fingerprint.md`` design center 4
"anti-gaming by disclosure" + drift-log item 5): the run's ``cmd_sha``-addressed
fingerprint LEDGER (``state/fingerprint_store.py::fingerprint_path``) is sealed
as RAW BYTES — the measured determinism envelope is DERIVED from that ledger and
lives in the code-rendered briefs, so this module seals the FILE, never a
rendered envelope, and never ``json``-parses the JSONL (the no-parse boundary
holds unchanged). A disclosed consequence follows from the ledger being append-
only: every appended sample moves the sealed bytes, so a registration's dossier
leg reads STALE after new evidence accrues — re-export + re-register is the
remedy (the registration-kernel R7 posture, deliberate — a measurement that
grew is a new dossier, not a silent mutation of the old one).

This file lives at the ``ops/`` *role root* (sibling to the subjects, like
``provenance_manifest.py`` and ``trace.py``) because it reads across subjects —
``state`` sidecars, the decision/brief/terminal journals, the scope substrate,
the journal records, and the experiment-local ``_aggregated`` tree. The
subject-imports lint short-circuits for role-root files, so the cross-subject
reads here are allowed by construction.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from hpc_agent import errors
from hpc_agent._build_info import full_version
from hpc_agent._kernel.contract.layout import JournalLayout, RepoLayout
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.export_dossier import ExportDossierResult, ExportDossierSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.provenance_manifest import manifest_signature
from hpc_agent.state import scopes as _scopes
from hpc_agent.state.decision_briefs import briefs_path
from hpc_agent.state.decision_journal import decisions_path
from hpc_agent.state.fingerprint_store import fingerprint_path
from hpc_agent.state.journal import load_run
from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

__all__ = [
    "DOSSIER_SCHEMA_VERSION",
    "DOSSIER_SOURCES",
    "DossierSignature",
    "compute_dossier_signature",
    "export_dossier",
]

# Bump when the emitted manifest shape changes in a way a consumer (the
# repo-side renderer, an integrity checker) would need to branch on. Mirrored
# on the manifest's ``dossier_schema_version``.
DOSSIER_SCHEMA_VERSION: int = 1

# The closed set of source-store names a bundled entry may be typed by. Every
# value is a concrete on-disk STORE NOUN — never a caller-owned role. The wire
# deliberately does not carry this vocabulary (it stays an ops-layer contract);
# ``tests/contracts/test_dossier_boundary.py`` pins the set by equality so a new
# store name is a reviewed change, and no domain-semantics word may masquerade
# as a store here.
DOSSIER_SOURCES: frozenset[str] = frozenset(
    {
        "sidecar",  # <exp>/.hpc/runs/<run_id>.json
        "decision-journal",  # <exp>/.hpc/runs/<run_id>.decisions.jsonl
        "briefs",  # <exp>/.hpc/runs/<run_id>.briefs.jsonl
        "block-terminal",  # <exp>/.hpc/runs/<run_id>.<block>.terminal.json
        "journal-record",  # ~/.claude/hpc/<repo_hash>/runs/<run_id>.json
        "scope-journal",  # <exp>/.hpc/scopes/<tag>.decisions.jsonl
        "look-ledger",  # <exp>/.hpc/scopes/<tag>.looks.jsonl
        "aggregated",  # <exp>/_aggregated/<run_id>/** and <exp>/_aggregated/<run_id>.json
        "audited-source",  # the audited source .py + its template .py (notebook-audit T14)
        "notebook-journal",  # <exp>/.hpc/notebooks/<audit_id>.decisions.jsonl (attestation journal)
        "renders",  # <exp>/.hpc/renders/<audit_id>/** — the trusted-display render files
        "determinism-fingerprint",  # <exp>/_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl
        "pack-manifest",  # the bound domain-pack manifest file (raw bytes; T10)
        "pack-journal",  # <exp>/.hpc/packs/<pack>.decisions.jsonl (bind/receipt journal; T10)
    }
)

# Per-run identity fields lifted off the sidecar into the manifest's ``runs``
# projection — an EXPLICIT allowlist (like provenance_manifest's
# ``_RUN_PROVENANCE_FIELDS``), never ``**sidecar``, so a new sidecar field does
# not silently leak into the sealed manifest until added here on purpose.
# ``supersedes`` (journal-record only) and ``reproduces`` (sidecar, if present)
# are projected separately below.
_DOSSIER_RUN_FIELDS: tuple[str, ...] = (
    "cmd_sha",  # parameter identity
    "node_sha",  # DAG-node identity (parented runs)
    "cluster",  # cluster key
    "hpc_agent_version",  # writer's package version
    "scopes",  # opaque caller-owned evidence-scope tags
)


def _sha256_hex(data: bytes) -> str:
    """Return the 64-char hex SHA-256 of *data* — one entry's integrity fingerprint."""
    return hashlib.sha256(data).hexdigest()


def _safe_sidecar(experiment_dir: Path, run_id: str) -> dict[str, Any]:
    """Return a run's parsed sidecar dict, or ``{}`` when none exists.

    The parse happens inside :func:`state.runs.read_run_sidecar` — this module
    itself never parses the bytes it seals (the no-parse boundary pin). A run
    with a journal record but no sidecar (or vice versa) yields ``{}`` rather
    than raising, so a missing store is data, not an error.
    """
    try:
        return read_run_sidecar(experiment_dir, run_id)
    except FileNotFoundError:
        return {}


def _project_run_identity(
    run_id: str, sidecar: dict[str, Any], record: RunRecord | None
) -> dict[str, Any]:
    """Project one run to the manifest identity allowlist (never ``**sidecar``).

    Merges the sidecar-owned provenance fields (:data:`_DOSSIER_RUN_FIELDS`)
    with the journal-record-owned ``supersedes`` link; ``reproduces`` is emitted
    only when the sidecar recorded one (the "reproduces-if-present" projection).
    A field the sidecar never recorded is emitted as ``null`` so the shape is
    uniform across sidecar vintages.
    """
    projection: dict[str, Any] = {"run_id": run_id}
    for field in _DOSSIER_RUN_FIELDS:
        projection[field] = sidecar.get(field)
    # cluster can also live on the journal record for a sidecar-less run.
    if projection.get("cluster") is None and record is not None:
        projection["cluster"] = record.cluster or None
    projection["supersedes"] = (record.supersedes or None) if record is not None else None
    reproduces = sidecar.get("reproduces")
    if reproduces is not None:
        projection["reproduces"] = reproduces
    # audit_id — the opaque audit slug that graduated this run (notebook-audit
    # T14). Projected as run IDENTITY (which audit sealed it) only when the
    # sidecar echoed an audited_source block; the reproduces-if-present
    # precedent — emitted only when audited, never null-padded. The slug is
    # identity; the section-level semantics inside the audit stay opaque.
    audited = sidecar.get("audited_source")
    if isinstance(audited, dict) and audited.get("audit_id") is not None:
        projection["audit_id"] = audited.get("audit_id")
    return projection


def _seal(
    source: str,
    disk_path: Path,
    archive_path: str,
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
) -> None:
    """Read *disk_path* as RAW BYTES, register it for the zip, append its entry.

    The one place a source store's bytes enter the bundle: ``read_bytes`` →
    sha256 → a ``{source, path, sha256, bytes}`` provenance record. Content is
    never decoded or parsed, so any store (the aggregated one especially) round-
    trips byte-identical.
    """
    data = disk_path.read_bytes()
    write_map[archive_path] = data
    entries.append(
        {
            "source": source,
            "path": archive_path,
            "sha256": _sha256_hex(data),
            "bytes": len(data),
        }
    )


def _gather_optional(
    source: str,
    disk_path: Path,
    archive_path: str,
    run_id: str,
    note: str,
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> None:
    """Seal a single expected store, or record a gap when it is absent.

    Absent individual stores are REPORTED (a ``{source, run_id, note}`` gap),
    never fatal — a bundle with gaps is still written.
    """
    if disk_path.is_file():
        _seal(source, disk_path, archive_path, write_map=write_map, entries=entries)
    else:
        gaps.append({"source": source, "run_id": run_id, "note": note})


def _gather_run(
    experiment_dir: Path,
    run_id: str,
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Gather every per-run store for *run_id*; return (identity projection, scope tags).

    Seals the sidecar, the run decision journal, the emitted briefs, the journal
    record, each detached-block terminal, and the harvested aggregate artifacts
    (both the ``_aggregated/<run_id>/`` dir and the ``_aggregated/<run_id>.json``
    file variant). The scope tags carried on the sidecar are returned so the
    caller can union them across a lineage and gather each scope's stores once.
    """
    runs_dir = RepoLayout(experiment_dir).runs

    # sidecar — <exp>/.hpc/runs/<run_id>.json
    _gather_optional(
        "sidecar",
        run_sidecar_path(experiment_dir, run_id),
        f"runs/{run_id}/sidecar.json",
        run_id,
        "no run sidecar on disk",
        write_map=write_map,
        entries=entries,
        gaps=gaps,
    )
    # decision-journal — <exp>/.hpc/runs/<run_id>.decisions.jsonl
    _gather_optional(
        "decision-journal",
        decisions_path(experiment_dir, "run", run_id),
        f"runs/{run_id}/decisions.jsonl",
        run_id,
        "no run decision journal on disk",
        write_map=write_map,
        entries=entries,
        gaps=gaps,
    )
    # briefs — <exp>/.hpc/runs/<run_id>.briefs.jsonl
    _gather_optional(
        "briefs",
        briefs_path(experiment_dir, run_id),
        f"runs/{run_id}/briefs.jsonl",
        run_id,
        "no emitted-brief journal on disk",
        write_map=write_map,
        entries=entries,
        gaps=gaps,
    )
    # journal-record — ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (honors HPC_JOURNAL_DIR)
    _gather_optional(
        "journal-record",
        JournalLayout(experiment_dir).run_record(run_id),
        f"runs/{run_id}/journal.json",
        run_id,
        "no journal record on disk",
        write_map=write_map,
        entries=entries,
        gaps=gaps,
    )
    # block-terminal — every <run_id>.<block>.terminal.json under the runs tree.
    # Inherently multi/optional: zero terminals is normal (no gap), each present
    # one is sealed under its block name.
    prefix = f"{run_id}."
    suffix = ".terminal.json"
    for term_path in sorted(runs_dir.glob(f"{run_id}.*.terminal.json")):
        block = term_path.name.removeprefix(prefix).removesuffix(suffix)
        _seal(
            "block-terminal",
            term_path,
            f"runs/{run_id}/{block}.terminal.json",
            write_map=write_map,
            entries=entries,
        )

    # aggregated — BOTH the dir and the file variant, copied as RAW BYTES.
    _gather_aggregated(experiment_dir, run_id, write_map=write_map, entries=entries, gaps=gaps)

    sidecar = _safe_sidecar(experiment_dir, run_id)
    # audited-source + notebook-journal — sealed only when the sidecar echoed an
    # audited_source block (notebook-audit T14). "The dossier is sealed
    # attestations": the attestation journal + the source .py and template .py
    # the graduation gate hashed.
    _gather_audited_source(
        experiment_dir, run_id, sidecar, write_map=write_map, entries=entries, gaps=gaps
    )
    # determinism-fingerprint — the cmd_sha-addressed ledger, sealed as RAW BYTES
    # (disclosure at graduation; never the derived envelope, never parsed).
    _gather_fingerprint(
        experiment_dir, run_id, sidecar, write_map=write_map, entries=entries, gaps=gaps
    )
    # pack-manifest + pack-journal — sealed only when the sidecar echoed a `packs`
    # block (domain-packs T10). The bound domain standards (manifest + journal),
    # copied as RAW BYTES, never parsed.
    _gather_packs(experiment_dir, run_id, sidecar, write_map=write_map, entries=entries, gaps=gaps)
    record = load_run(experiment_dir, run_id)
    projection = _project_run_identity(run_id, sidecar, record)
    tags = [str(t) for t in (sidecar.get("scopes") or []) if t]
    return projection, tags


def _gather_audited_source(
    experiment_dir: Path,
    run_id: str,
    sidecar: dict[str, Any],
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> None:
    """Seal the run's audited-source trail when the sidecar echoes one (T14).

    The sidecar's opaque ``audited_source`` echo (``{source, template,
    audit_id}``) points at four concrete stores: the source ``.py`` and its
    template ``.py`` — both sealed as RAW BYTES under the ``audited-source`` store
    noun (same store KIND, distinguished by archive path, not by a role field) —
    the notebook attestation journal at
    ``.hpc/notebooks/<audit_id>.decisions.jsonl`` (the ``notebook-journal`` noun),
    and the trusted-display render files at ``.hpc/renders/<audit_id>/`` (the
    ``renders`` noun — what-the-human-saw, F6).
    A run with no echo seals nothing and records no gap (an un-audited run
    legitimately has no audit trail); an echo whose declared file is missing
    records a gap (present-or-gap accounting, never a crash).
    """
    echo = sidecar.get("audited_source")
    if not isinstance(echo, dict):
        return
    source_rel = echo.get("source")
    if isinstance(source_rel, str) and source_rel:
        _gather_optional(
            "audited-source",
            Path(experiment_dir) / source_rel,
            f"runs/{run_id}/audited/source.py",
            run_id,
            f"audited source .py {source_rel!r} not on disk",
            write_map=write_map,
            entries=entries,
            gaps=gaps,
        )
    template_rel = echo.get("template")
    if isinstance(template_rel, str) and template_rel:
        _gather_optional(
            "audited-source",
            Path(experiment_dir) / template_rel,
            f"runs/{run_id}/audited/template.py",
            run_id,
            f"audited template .py {template_rel!r} not on disk",
            write_map=write_map,
            entries=entries,
            gaps=gaps,
        )
    audit_id = echo.get("audit_id")
    if isinstance(audit_id, str) and audit_id:
        _gather_optional(
            "notebook-journal",
            decisions_path(experiment_dir, "notebook", audit_id),
            f"runs/{run_id}/notebook.decisions.jsonl",
            run_id,
            f"no notebook attestation journal on disk for audit_id {audit_id!r}",
            write_map=write_map,
            entries=entries,
            gaps=gaps,
        )
        _gather_renders(
            experiment_dir, run_id, audit_id, write_map=write_map, entries=entries, gaps=gaps
        )


def _gather_renders(
    experiment_dir: Path,
    run_id: str,
    audit_id: str,
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> None:
    """Seal the TRUSTED-DISPLAY render files for *audit_id* — what-the-human-saw.

    Every file under ``<exp>/.hpc/renders/<audit_id>/`` (the content-addressed
    section render files the T8 sign-off gate required to exist on disk) is copied
    as RAW BYTES under the ``renders`` store noun, so the dossier can reproduce
    exactly what the human was shown when they signed — the audit's evidence was
    incomplete without it (adversarial review F6). Present-or-gap accounting: no
    renders dir / no files → one gap, success. Never parsed (the file bytes are
    opaque to the bundler, same as every other store).
    """
    renders_dir = RepoLayout(experiment_dir).hpc / "renders" / audit_id
    found = False
    if renders_dir.is_dir():
        for p in sorted(renders_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(renders_dir).as_posix()
                _seal(
                    "renders",
                    p,
                    f"runs/{run_id}/renders/{rel}",
                    write_map=write_map,
                    entries=entries,
                )
                found = True
    if not found:
        gaps.append(
            {
                "source": "renders",
                "run_id": run_id,
                "note": f"no trusted-display renders on disk for audit_id {audit_id!r}",
            }
        )


def _gather_fingerprint(
    experiment_dir: Path,
    run_id: str,
    sidecar: dict[str, Any],
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> None:
    """Seal the run's determinism-fingerprint LEDGER as RAW BYTES (fingerprint T8).

    The ledger is the ``cmd_sha``-addressed append-only sample file at
    ``<exp>/_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl``
    (:func:`state.fingerprint_store.fingerprint_path`), resolved from the
    ``cmd_sha`` the sidecar already carries — the disclosure-at-graduation surface
    for the measured determinism envelope (``docs/design/determinism-fingerprint.md``
    design center 4, "anti-gaming by disclosure"). It is sealed as OPAQUE BYTES
    like every other store: this module NEVER ``json``-parses the JSONL (the
    derived envelope is rendered in the code-rendered briefs, never here — the
    no-parse boundary), so a deliberately-torn ledger round-trips byte-identical.

    A run whose sidecar carries no ``cmd_sha`` (no experiment identity to resolve a
    ledger from) seals nothing and records no gap; a resolvable identity whose
    ledger is not yet on disk (no fingerprint sample ever minted) records a gap
    (present-or-gap accounting, never a crash). Keyed by the identity path, the
    ledger is sealed at most ONCE across a lineage whose runs share a ``cmd_sha``
    (one file, one entry — never a duplicate).
    """
    cmd_sha = sidecar.get("cmd_sha")
    if not isinstance(cmd_sha, str) or not cmd_sha:
        return
    archive_path = f"fingerprints/{cmd_sha[:16]}.jsonl"
    if archive_path in write_map:
        return  # already sealed for this identity (a lineage sharing a cmd_sha)
    _gather_optional(
        "determinism-fingerprint",
        fingerprint_path(experiment_dir, cmd_sha),
        archive_path,
        run_id,
        f"no determinism-fingerprint ledger on disk for cmd_sha {cmd_sha[:16]!r}",
        write_map=write_map,
        entries=entries,
        gaps=gaps,
    )


def _gather_packs(
    experiment_dir: Path,
    run_id: str,
    sidecar: dict[str, Any],
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> None:
    """Seal each opted-in pack's manifest file + decision journal as RAW BYTES (T10).

    The sidecar's opaque ``packs`` echo (a list of ``{pack, version, sha,
    manifest}``) points at two per-pack stores, both sealed as OPAQUE BYTES and
    NEVER parsed (the no-parse boundary): the bound manifest file at the echoed
    ``manifest`` relpath (the ``pack-manifest`` store noun) and the pack decision
    journal at ``.hpc/packs/<pack>.decisions.jsonl`` (the ``pack-journal`` noun —
    the bind + receipt attestation trail). Both prove WHICH domain standards, at
    WHICH hashes, gated the run.

    A run with no ``packs`` echo seals nothing and records no gap (a pack-free run
    legitimately has none); an echo whose store is missing records a gap
    (present-or-gap accounting, never a crash). The manifest relpath comes from the
    already-parsed sidecar echo, so this module still never parses interview.json.
    """
    echoes = sidecar.get("packs")
    if not isinstance(echoes, list):
        return
    hpc = RepoLayout(experiment_dir).hpc
    for echo in echoes:
        if not isinstance(echo, dict):
            continue
        pack_name = echo.get("pack")
        if not isinstance(pack_name, str) or not pack_name:
            continue
        manifest_rel = echo.get("manifest")
        if isinstance(manifest_rel, str) and manifest_rel:
            _gather_optional(
                "pack-manifest",
                Path(experiment_dir) / manifest_rel,
                f"runs/{run_id}/packs/{pack_name}/manifest.json",
                run_id,
                f"bound pack manifest {manifest_rel!r} not on disk for pack {pack_name!r}",
                write_map=write_map,
                entries=entries,
                gaps=gaps,
            )
        _gather_optional(
            "pack-journal",
            hpc / "packs" / f"{pack_name}.decisions.jsonl",
            f"runs/{run_id}/packs/{pack_name}.decisions.jsonl",
            run_id,
            f"no pack decision journal on disk for pack {pack_name!r}",
            write_map=write_map,
            entries=entries,
            gaps=gaps,
        )


def _gather_aggregated(
    experiment_dir: Path,
    run_id: str,
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> None:
    """Seal the run's harvested aggregate artifacts — opaque bytes, never parsed.

    Copies every file under ``<exp>/_aggregated/<run_id>/`` (recursively,
    preserving the relative subpath) AND the ``<exp>/_aggregated/<run_id>.json``
    file variant when present. NOTHING here is ``json``-parsed — a deliberately
    invalid-JSON aggregate must round-trip byte-identical. Neither present → one
    gap, success.
    """
    agg_root = Path(experiment_dir) / "_aggregated"
    agg_dir = agg_root / run_id
    agg_file = agg_root / f"{run_id}.json"
    found = False
    if agg_dir.is_dir():
        for p in sorted(agg_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(agg_dir).as_posix()
                _seal(
                    "aggregated",
                    p,
                    f"aggregated/{run_id}/{rel}",
                    write_map=write_map,
                    entries=entries,
                )
                found = True
    if agg_file.is_file():
        _seal(
            "aggregated",
            agg_file,
            f"aggregated/{run_id}.json",
            write_map=write_map,
            entries=entries,
        )
        found = True
    if not found:
        gaps.append(
            {
                "source": "aggregated",
                "run_id": run_id,
                "note": "no aggregated artifacts on disk",
            }
        )


def _gather_scope(
    experiment_dir: Path,
    tag: str,
    declared_by: str,
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> None:
    """Seal a scope tag's decision journal + look ledger (or gap each if absent).

    A tag is opaque — sealed by IDENTITY (the slug is a path segment), never by
    any role vocabulary. *declared_by* is the run that carried the tag, recorded
    on a gap so a consumer can trace an absent scope store back to its run.
    """
    _gather_optional(
        "scope-journal",
        decisions_path(experiment_dir, "scope", tag),
        f"scopes/{tag}.decisions.jsonl",
        declared_by,
        f"no scope decision journal on disk for tag {tag!r}",
        write_map=write_map,
        entries=entries,
        gaps=gaps,
    )
    _gather_optional(
        "look-ledger",
        _scopes.looks_path(experiment_dir, tag),
        f"scopes/{tag}.looks.jsonl",
        declared_by,
        f"no look ledger on disk for tag {tag!r}",
        write_map=write_map,
        entries=entries,
        gaps=gaps,
    )


@dataclass(frozen=True)
class DossierSignature:
    """The dry-gathered dossier fingerprint — the ONE signature seam (no zip).

    :func:`compute_dossier_signature` builds this by running the full gather
    pipeline (every per-run + per-scope store read as raw bytes, sha'd,
    path-sorted, :func:`~hpc_agent.ops.provenance_manifest.manifest_signature`
    applied) WITHOUT writing an archive, a ``manifest.json``, or anything else.
    :func:`export_dossier` routes through it — the ``bundle_sha256`` it seals
    into the archive IS this object's, never a parallel computation — and the
    registration recompute lock (``docs/design/registration-kernel.md`` R2)
    re-gathers from the LIVE stores through the same seam, so a store that moved
    since export is caught by a differing signature.

    Fields:

    * ``bundle_sha256`` — ``manifest_signature`` over ``entries`` ONLY
      (``generated_at`` / ``tool_version`` excluded from the pre-image), so two
      dry gathers of unchanged stores fingerprint identically.
    * ``entries`` — the path-sorted ``{source, path, sha256, bytes}`` records
      (the exact pre-image of the signature); the per-store breakdown a drift
      check (R8's ``drifted_stores``) diffs entry-by-entry.
    * ``run_projections`` — the identity-allowlist projection per resolved run.
    * ``gaps`` — the absent-store report (a store expected but not on disk).
    * ``run_ids`` — the resolved run set (the single run, or its whole
      supersession lineage newest→root when ``include_lineage`` was set).
    * ``write_map`` — archive-path → raw bytes, the zip writer's input. A pure
      signature consumer (the registration recompute) ignores it: the bytes are
      already sha'd into ``entries``. It carries no zip concern of its own — it
      is just the sealed bytes, keyed by where they would land.
    """

    bundle_sha256: str
    entries: list[dict[str, Any]]
    run_projections: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    run_ids: list[str]
    write_map: dict[str, bytes]


def compute_dossier_signature(
    experiment_dir: Path,
    run_id: str,
    include_lineage: bool = False,
) -> DossierSignature:
    """Dry-gather a run's (optionally its lineage's) sealed stores → the signature.

    Runs the FULL gather pipeline with NO side effects — no ``.zip``, no
    ``manifest.json``, no write of any kind: resolves the run set, seals every
    per-run + per-scope store into an in-memory ``write_map`` and its
    ``{source, path, sha256, bytes}`` entry, path-sorts the entries, and applies
    :func:`~hpc_agent.ops.provenance_manifest.manifest_signature` over the
    entries list ONLY (``generated_at`` / ``tool_version`` never enter the
    pre-image). This is the ONE place the dossier signature is defined (the
    "dossier sha via the ONE signature seam" enforcement row); both
    :func:`export_dossier` and the registration recompute lock route through it,
    so there is never a second signature definition to drift.

    Raises :class:`errors.SpecInvalid` when the run has NEITHER a sidecar NOR a
    journal record (nothing to bundle) — the same missing-run guard export
    applies. Absent individual stores are reported as ``gaps``, never fatal.
    """
    experiment_dir = Path(experiment_dir)

    # Missing-run guard (the ops/trace.py precedent): no sidecar AND no journal
    # record → there is nothing to bundle. Lives in the seam so the export path
    # AND the registration recompute share one guard.
    has_sidecar = run_sidecar_path(experiment_dir, run_id).is_file()
    if not has_sidecar and load_run(experiment_dir, run_id) is None:
        raise errors.SpecInvalid(
            f"no run sidecar or journal record found for run_id {run_id!r} — nothing to bundle"
        )

    # Resolve the run set: the single run, or its whole supersession lineage
    # (newest→root) when include_lineage is set.
    run_ids = _scopes.lineage_chain(experiment_dir, run_id) if include_lineage else [run_id]

    write_map: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    run_projections: list[dict[str, Any]] = []
    # Union of scope tags across the run set, remembering the first run that
    # declared each so a scope gap can name its origin. Insertion-ordered.
    tag_origin: dict[str, str] = {}

    for rid in run_ids:
        projection, tags = _gather_run(
            experiment_dir, rid, write_map=write_map, entries=entries, gaps=gaps
        )
        run_projections.append(projection)
        for tag in tags:
            tag_origin.setdefault(tag, rid)

    for tag, declared_by in tag_origin.items():
        _gather_scope(
            experiment_dir,
            tag,
            declared_by,
            write_map=write_map,
            entries=entries,
            gaps=gaps,
        )

    # Path-sort the entries (and the write order) so a store hashes identically
    # regardless of gather order.
    entries.sort(key=lambda e: e["path"])
    # bundle_sha256 canonicalizes the path-sorted entries list ONLY — reusing
    # provenance_manifest.manifest_signature verbatim (its json.dumps(...,
    # sort_keys=True) canonicalizes any JSON value, list included), so there is
    # never a second canonicalization here. generated_at / tool_version are
    # excluded from the pre-image for hash stability across identical stores.
    bundle_sha256 = manifest_signature(cast("Any", entries))

    return DossierSignature(
        bundle_sha256=bundle_sha256,
        entries=entries,
        run_projections=run_projections,
        gaps=gaps,
        run_ids=list(run_ids),
        write_map=write_map,
    )


@primitive(
    name="export-dossier",
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<output_path> (default <experiment>/_dossier/<run_id>.zip)"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Bundle a run's core-owned record trail — sidecar, decision journal, "
            "briefs, block terminals, journal record, scope journals + look "
            "ledgers, and harvested aggregates — into one integrity-sealed .zip "
            "with a provenance manifest. --include-lineage widens it to the run's "
            "whole supersession chain. Pure local reads + one local write, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ExportDossierSpec,
        schema_ref=SchemaRef(input="export_dossier"),
    ),
    agent_facing=True,
)
def export_dossier(*, experiment_dir: Path, spec: ExportDossierSpec) -> ExportDossierResult:
    """Assemble the dossier bundle for a run (optionally its lineage) and seal it.

    Gathers each concrete on-disk store the run left behind, copies every store's
    bytes verbatim into a ``.zip`` (entries written in sorted path order), and
    writes a ``manifest.json`` at the archive root pairing every entry with its
    source store, path, sha256, and byte size. ``bundle_sha256`` is the
    canonical signature over the path-sorted entries list ONLY (``generated_at``
    / ``tool_version`` excluded from the pre-image), so two exports of an
    unchanged store produce an identical fingerprint.

    Raises :class:`errors.SpecInvalid` when the requested run has NEITHER a
    sidecar NOR a journal record (nothing to bundle). Absent individual stores
    are reported as ``gaps`` and are never fatal. An existing archive at the
    target is overwritten (idempotent replay). Default output:
    ``<experiment>/_dossier/<run_id>.zip`` (parents created).
    """
    experiment_dir = Path(experiment_dir)
    run_id = spec.run_id

    # The gather + signature is defined ONCE, in the dry seam (the "dossier sha
    # via the ONE signature seam" enforcement row). Export adds only the manifest
    # envelope and the zip write on top of it; the bundle_sha256 sealed here IS
    # the seam's output, never a parallel computation. The missing-run guard and
    # the lineage resolution live in the seam.
    sig = compute_dossier_signature(experiment_dir, run_id, include_lineage=spec.include_lineage)

    manifest: dict[str, Any] = {
        "dossier_schema_version": DOSSIER_SCHEMA_VERSION,
        "generated_at": utcnow_iso(),
        "tool_version": full_version(),
        "runs": sig.run_projections,
        "entries": sig.entries,
        "gaps": sig.gaps,
        "bundle_sha256": sig.bundle_sha256,
    }

    # Resolve the output path (caller's or the derived default) and overwrite any
    # existing archive there — an idempotent replay re-seals cleanly.
    if spec.output_path:
        archive_path = Path(spec.output_path)
    else:
        archive_path = experiment_dir / "_dossier" / f"{run_id}.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(sig.write_map):
            zf.writestr(path, sig.write_map[path])
        # manifest.json is the seal over the entries — NOT itself an entry.
        # json.dumps SERIALIZES provenance (allowed); the boundary ban is on
        # json.load/loads reading opaque content back into structure.
        zf.writestr("manifest.json", json.dumps(manifest, sort_keys=True, indent=2))

    return ExportDossierResult(
        archive_path=str(archive_path),
        run_ids=list(sig.run_ids),
        bundle_sha256=sig.bundle_sha256,
        entry_count=len(sig.entries),
        gaps=sig.gaps,
        manifest=manifest,
    )
