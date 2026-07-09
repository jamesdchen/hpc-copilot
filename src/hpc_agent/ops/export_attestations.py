"""Export-attestations — project a run's SEALED dossier evidence into in-toto form.

The PORTABILITY layer over :mod:`hpc_agent.ops.export_dossier`'s SEALING
layer. ``export-dossier`` gathers a run's concrete on-disk source stores into
one integrity-sealed ``.zip`` with a ``{source, path, sha256, bytes}``
manifest. This SIBLING verb takes that SAME sealed evidence — via the ONE
gather ``export_dossier`` already defines
(:func:`~hpc_agent.ops.export_dossier.compute_dossier_signature`) — and emits
one in-toto **Statement** per sealed store entry, each wrapped in an (unsigned,
v1) **DSSE envelope**, as a JSONL stream ecosystem tooling can verify WITHOUT
hpc-agent.

Why a sibling and not an extension of ``export-dossier`` (``docs/design/
conformance-kit.md`` D-K4): the dossier boundary test pins the manifest entry
shape to exactly ``{source, path, sha256, bytes}`` by AST — a Statement-shaped
entry would break it — pins ``DOSSIER_SOURCES`` closed by equality, and bans
``json.load(s)`` in that module. The dossier stays the sealing layer; this is
portability layered ON it, sharing the ONE gather so the stores are never
walked twice.

Boundary posture (``docs/internals/engineering-principles.md`` Q1, extended):

* **The export never parses record contents.** Subject digests are copied
  VERBATIM from the dossier signature's entries (never recomputed here — this
  module reaches for no ``hashlib`` and re-reads no disk); the predicate embeds
  each store's RAW BYTES verbatim (UTF-8 text, or base64 for non-UTF-8). There
  is NO ``json.load`` / ``json.loads`` anywhere in this module (the dossier
  no-parse posture extends; ``json.dumps`` — used only to serialize the
  Statement into the DSSE payload — is untouched, as in ``export_dossier``).
* **The predicateType vocabulary is CLOSED and equality-pinned to
  ``DOSSIER_SOURCES``.** See :data:`PREDICATE_TYPES` below.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.export_attestations import (
    ExportAttestationsResult,
    ExportAttestationsSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.export_dossier import compute_dossier_signature

__all__ = [
    "DSSE_PAYLOAD_TYPE",
    "IN_TOTO_STATEMENT_TYPE",
    "PREDICATE_TYPE_SCHEME",
    "PREDICATE_TYPES",
    "export_attestations",
]

# The in-toto Statement type URI (in-toto attestation spec v1) and the DSSE
# envelope's payloadType for an in-toto payload. Pinned by the boundary test.
IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"

# The predicateType URI scheme — one URI per DOSSIER_SOURCES store noun,
# ``<scheme>/<store-noun>/v1``.
PREDICATE_TYPE_SCHEME = "https://hpc-agent.dev/attestation"

# The store-noun → predicateType map. Its KEY SET is equality-pinned to the
# live :data:`~hpc_agent.ops.export_dossier.DOSSIER_SOURCES` by
# ``tests/contracts/test_attestation_export_boundary.py`` (both directions), so
# a new store noun landing in ``DOSSIER_SOURCES`` FAILS that boundary test until
# its URI row is added here DELIBERATELY — the dossier vocabulary and this map
# can never silently diverge.
#
# ---------------------------------------------------------------------------
# LIVE-CONFORMANCE PAIR-EDIT (docs/design/conformance-kit.md; read before you
# touch this map): the live-conformance branch lands ONE new noun in
# ``DOSSIER_SOURCES``. When it does, this map's equality pin fires, and the fix
# is a DELIBERATE ONE-LINE PAIR-EDIT that must move together:
#   1. add the ``<new-noun>: f"{PREDICATE_TYPE_SCHEME}/<new-noun>/v1"`` row HERE, and
#   2. add the matching row to ``_EXPECTED_PREDICATE_TYPES`` in the boundary test.
# Both rows are the same fact stated twice (the house-style inline authoritative
# copy, mirroring ``test_dossier_boundary.py``'s ``_EXPECTED_SOURCES``); editing
# only one leaves the pin red on purpose.
# ---------------------------------------------------------------------------
PREDICATE_TYPES: dict[str, str] = {
    "sidecar": f"{PREDICATE_TYPE_SCHEME}/sidecar/v1",
    "decision-journal": f"{PREDICATE_TYPE_SCHEME}/decision-journal/v1",
    "briefs": f"{PREDICATE_TYPE_SCHEME}/briefs/v1",
    "block-terminal": f"{PREDICATE_TYPE_SCHEME}/block-terminal/v1",
    "journal-record": f"{PREDICATE_TYPE_SCHEME}/journal-record/v1",
    "scope-journal": f"{PREDICATE_TYPE_SCHEME}/scope-journal/v1",
    "look-ledger": f"{PREDICATE_TYPE_SCHEME}/look-ledger/v1",
    "aggregated": f"{PREDICATE_TYPE_SCHEME}/aggregated/v1",
    "audited-source": f"{PREDICATE_TYPE_SCHEME}/audited-source/v1",
    "notebook-journal": f"{PREDICATE_TYPE_SCHEME}/notebook-journal/v1",
    "renders": f"{PREDICATE_TYPE_SCHEME}/renders/v1",
    "determinism-fingerprint": f"{PREDICATE_TYPE_SCHEME}/determinism-fingerprint/v1",
    "pack-manifest": f"{PREDICATE_TYPE_SCHEME}/pack-manifest/v1",
    "pack-journal": f"{PREDICATE_TYPE_SCHEME}/pack-journal/v1",
}


def _content_type(archive_path: str) -> str:
    """Return the predicate ``contentType`` for an archive path, by suffix.

    A hint for the reader, drawn ONLY from the archive path's extension — never
    from the bytes' meaning: ``.jsonl`` → ``application/x.jsonl``, ``.json`` →
    ``application/json``, ``.py`` → ``text/x-python``, anything else →
    ``application/octet-stream``. Binary (non-UTF-8) content overrides this to
    octet-stream in :func:`_predicate`.
    """
    if archive_path.endswith(".jsonl"):
        return "application/x.jsonl"
    if archive_path.endswith(".json"):
        return "application/json"
    if archive_path.endswith(".py"):
        return "text/x-python"
    return "application/octet-stream"


def _predicate(archive_path: str, data: bytes) -> dict[str, str]:
    """Build the Statement predicate embedding *data* VERBATIM.

    UTF-8-decodable bytes ride as text under the suffix-derived contentType;
    non-UTF-8 bytes ride as base64 under ``application/octet-stream``. The
    bytes are NEVER parsed — the predicate embeds them exactly as sealed.
    """
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "contentType": "application/octet-stream",
            "content": base64.b64encode(data).decode("ascii"),
        }
    return {"contentType": _content_type(archive_path), "content": content}


def _statement(entry: dict[str, Any], data: bytes) -> dict[str, Any]:
    """Project one sealed dossier entry into an in-toto Statement.

    ``subject`` names the archive path and carries the entry's sha256 copied
    VERBATIM from the dossier manifest (never recomputed); ``predicateType`` is
    the store noun's URI from :data:`PREDICATE_TYPES`; ``predicate`` embeds the
    raw bytes. ``entry['source']`` is always a :data:`DOSSIER_SOURCES` noun (the
    dossier gather guarantees it), so the map lookup cannot miss.
    """
    return {
        "_type": IN_TOTO_STATEMENT_TYPE,
        "subject": [
            {"name": entry["path"], "digest": {"sha256": entry["sha256"]}},
        ],
        "predicateType": PREDICATE_TYPES[entry["source"]],
        "predicate": _predicate(entry["path"], data),
    }


def _dsse_envelope(statement: dict[str, Any]) -> dict[str, Any]:
    """Wrap a Statement in an UNSIGNED, DSSE-ready envelope (v1).

    ``payloadType`` marks an in-toto payload; ``payload`` is the base64 of the
    canonical (sorted-keys) Statement JSON; ``signatures`` is EMPTY — signing is
    a future concern, and the envelope shape means adding a signature later
    changes nothing upstream (``docs/design/conformance-kit.md`` D-K4).
    """
    payload = json.dumps(statement, sort_keys=True).encode("utf-8")
    return {
        "payloadType": DSSE_PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [],
    }


@primitive(
    name="export-attestations",
    verb="mutate",
    side_effects=[
        SideEffect(
            "file_write",
            "<output_path> (default <experiment>/_dossier/<run_id>.attestations.jsonl)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Project a run's sealed dossier evidence into portable in-toto "
            "Statements (one per sealed store entry), each wrapped in an "
            "unsigned DSSE envelope, as a JSONL stream ecosystem tooling can "
            "verify without hpc-agent. Delegates the gather to export-dossier "
            "(the stores are never walked twice); subject digests are copied "
            "verbatim and record bytes ride verbatim in the predicate — the "
            "export never parses what it attests. --include-lineage widens it "
            "to the whole supersession chain. Pure local reads + one local "
            "write, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ExportAttestationsSpec,
        schema_ref=SchemaRef(input="export_attestations"),
    ),
    agent_facing=True,
)
def export_attestations(
    *, experiment_dir: Path, spec: ExportAttestationsSpec
) -> ExportAttestationsResult:
    """Emit the run's sealed evidence as in-toto Statements in DSSE envelopes.

    Delegates the gather to
    :func:`~hpc_agent.ops.export_dossier.compute_dossier_signature` (the ONE
    gather definition — the stores are never re-walked here), then projects each
    sealed ``{source, path, sha256, bytes}`` entry into one in-toto Statement
    (subject digest copied VERBATIM; predicate embedding the raw bytes from the
    signature's ``write_map``) wrapped in an unsigned DSSE envelope, written one
    per line as JSONL.

    Raises :class:`errors.SpecInvalid` when the run has NEITHER a sidecar NOR a
    journal record (the same missing-run guard as ``export-dossier`` — the guard
    lives in the shared gather seam). Absent individual stores are carried
    through as ``gaps`` and produce no Statement; never fatal. An existing bundle
    at the target is overwritten (idempotent replay). Default output:
    ``<experiment>/_dossier/<run_id>.attestations.jsonl`` (parents created).
    """
    experiment_dir = Path(experiment_dir)
    run_id = spec.run_id

    # The ONE gather — never a second walk of the stores. The missing-run guard,
    # the lineage resolution, the per-store sealing and the bundle signature all
    # live in this seam; this verb only PROJECTS its result into in-toto form.
    sig = compute_dossier_signature(experiment_dir, run_id, include_lineage=spec.include_lineage)

    lines: list[str] = []
    for entry in sig.entries:
        # entry['path'] keys the signature's write_map (every sealed entry has
        # its bytes there); the bytes ride verbatim into the predicate.
        data = sig.write_map[entry["path"]]
        envelope = _dsse_envelope(_statement(entry, data))
        lines.append(json.dumps(envelope, sort_keys=True))

    if spec.output_path:
        output_path = Path(spec.output_path)
    else:
        output_path = experiment_dir / "_dossier" / f"{run_id}.attestations.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(f"{line}\n" for line in lines),
        encoding="utf-8",
    )

    return ExportAttestationsResult(
        output_path=str(output_path),
        run_ids=list(sig.run_ids),
        statement_count=len(lines),
        bundle_sha256=sig.bundle_sha256,
        gaps=sig.gaps,
    )
