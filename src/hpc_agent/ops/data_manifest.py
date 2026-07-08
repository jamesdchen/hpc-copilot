"""``data-manifest`` — mint an identity record for the experiment's input data.

The verb (``docs/design/data-manifest.md`` "The verb"): a ``mutate`` primitive
that hashes every file under the DECLARED input roots into
``.hpc/data_manifest.json`` (``{relpath: {sha256, size, built_by?}}`` + a
manifest-doc sha), journals the mint (the tier-0 "who changed the data, when"
timeline), and refreshes the ``(size, mtime)`` fast-path cache. Re-minting after a
legitimate data change IS the journaled "this is the new known-good" act.

Roots default to the experiment's ONE existing input declaration
(:func:`hpc_agent.state.data_manifest.declared_input_roots`); a hardcoded
``data/`` default is REFUSED — no roots in the spec AND no declaration is a LOUD
:class:`errors.SpecInvalid` naming the declaration path (core never guesses which
directories are data).

:func:`render_manifest_disclosure` is the greenlight-brief disclosure (consumer
#2): a deterministic, code-rendered, VERDICT-FREE projection of the drift counts +
identities. It NEVER raises and NEVER gates — the never-blocking pin asserts the
disclosure path contains no ``raise`` / gate branch. The LLM's only role is
POINTING the human at the code render, relay-verbatim; it never re-derives drift.

Agnosticism: no ``pyarrow`` / ``pandas`` import; opaque bytes only.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.data_manifest import DataManifestResult, DataManifestSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state import data_manifest as dm

__all__ = ["data_manifest", "render_manifest_disclosure"]

_PRIMITIVE = "data-manifest"


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/data_manifest.json"),
    ],
    error_codes=[errors.SpecInvalid],
    # Not idempotent by identity_key: a re-mint after a data change is a NEW
    # journaled act (the point of the mint history), not a replayed no-op.
    idempotent=False,
    cli=CliShape(
        help=(
            "Mint an identity record (sha256 + size + opaque built_by per file) "
            "for the experiment's DECLARED input data into .hpc/data_manifest.json, "
            "journal the mint, and refresh the (size, mtime) fast-path cache. "
            "Converts silent data changes from invisible to attributed — the "
            "quiet-corruption class (same filename, rebuilt bytes) that no "
            "robustness layer catches because nothing throws. Roots default to "
            "interview.json's audited_source.input_roots (the ONE input "
            "declaration); no roots + no declaration REFUSES (core never guesses "
            "a data/ dir). Re-minting is the journaled 'new known-good' act. Pure "
            "local read + hash + write, no SSH."
        ),
        spec_arg=True,
        spec_required=False,
        experiment_dir_arg=True,
        spec_model=DataManifestSpec,
        schema_ref=SchemaRef(input="data_manifest"),
    ),
    agent_facing=True,
)
def data_manifest(
    *, experiment_dir: Path, spec: DataManifestSpec | None = None
) -> DataManifestResult:
    """Mint the data manifest over the resolved roots (spec ``roots`` or the declaration).

    Resolves ``roots`` from ``spec.roots`` when supplied, else the experiment's
    ``audited_source.input_roots`` declaration. Neither present → LOUD
    :class:`errors.SpecInvalid` naming the declaration path (a hardcoded ``data/``
    default is refused by design). Then mints the manifest, journals the act with
    the manifest-doc sha, and returns the counts + identities.
    """
    experiment_dir = Path(experiment_dir)
    spec = spec or DataManifestSpec()

    roots = spec.roots or dm.declared_input_roots(experiment_dir)
    if not roots:
        raise errors.SpecInvalid(
            f"{_PRIMITIVE}: no roots supplied and no input declaration found. "
            "Either pass spec.roots (opaque relpath list), or declare the "
            "experiment's inputs in interview.json's audited_source.input_roots. "
            "A hardcoded data/ default is refused by design — core never guesses "
            "which directories are data."
        )

    manifest = dm.mint_manifest(experiment_dir, roots, output_path=spec.output_path)
    return DataManifestResult(
        manifest_path=str(dm.manifest_path(experiment_dir, output_path=spec.output_path)),
        roots=list(manifest["roots"]),
        manifest_doc_sha=manifest["manifest_doc_sha"],
        file_count=len(manifest["files"]),
        files=manifest["files"],
    )


def render_manifest_disclosure(experiment_dir: Path) -> dict[str, object] | None:
    """The greenlight-brief data-drift disclosure — VERDICT-FREE, code-rendered.

    Returns a deterministic projection of the drift report (consumer #2): counts +
    identities + a relay-verbatim ``line``, or the standing "no manifest" line when
    an experiment declares inputs but has never minted, or ``None`` when there is
    nothing to disclose (no manifest AND no declared roots — the brief stays
    byte-identical for a repo not using the manifest).

    NEVER raises, NEVER gates: the disclosure is pure observation (the accept-with-
    disclosure rule). Core states counts and identities only — it never says
    "updated / appended / corrupted"; the human concludes at the brief.
    """
    report = dm.compute_drift(experiment_dir)
    if report.unmanifested:
        if dm.declared_input_roots(experiment_dir) is None:
            return None  # nothing declared, nothing minted → nothing to say
        return {
            "status": "no-manifest",
            "line": (
                "no data manifest (runs invisible to data-drift attribution) — "
                "mint with `hpc-agent data-manifest`"
            ),
        }
    c = report.counts
    return {
        "status": "manifest",
        "line": (
            f"data: {c['matched']} match, {c['drifted']} drifted, "
            f"{c['new']} new, {c['missing']} missing"
        ),
        "counts": c,
        "drifted": list(report.drifted),
        "new": list(report.new),
        "missing": list(report.missing),
    }
