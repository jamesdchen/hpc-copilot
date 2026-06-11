"""Per-campaign provenance manifest — one signable {code, data, env} record.

``cmd_sha`` (params) and ``tasks_py_sha`` (code) already live on every run
sidecar; #222 adds ``data_sha`` (input-dataset identity) and ``env_hash``
(resolved modules/conda/runtime). This module pairs each
``run_id``/``trial_token`` of a campaign with its full provenance fingerprint
in ONE diffable, signable artifact: given any result, reconstruct exactly what
produced it (code sha, data sha, resolved params, env hash, cluster).

Client-side only. It reads the sidecars the submit path already writes —
no cluster footprint, no dispatcher dependency, DVC optional (the data_sha
the sidecar carries was computed client-side at submit time by
:func:`hpc_agent.state.run_sha.compute_data_sha`).

The manifest is *derived* state: it is recomputed from the sidecars on
demand, so it is always consistent with the runs on disk rather than a
second source of truth that can drift.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.provenance_manifest import ProvenanceManifestInput
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.execution.mapreduce.reduce.history import find_sidecars_by_campaign

__all__ = [
    "build_provenance_manifest",
    "manifest_signature",
    "provenance_manifest",
    "write_provenance_manifest",
]

# Manifest schema version. Bump when the emitted shape changes in a way a
# consumer (a signer, a diff tool) would need to branch on.
PROVENANCE_MANIFEST_SCHEMA_VERSION: int = 1

# Per-run provenance fields lifted verbatim off each sidecar. Kept as an
# explicit allowlist (not ``**sidecar``) so the manifest is a stable,
# reviewable projection — a new sidecar field does not silently leak into
# the signable artifact until it is added here on purpose.
_RUN_PROVENANCE_FIELDS: tuple[str, ...] = (
    "cmd_sha",  # parameter identity (#207)
    "tasks_py_sha",  # code identity
    "data_sha",  # data identity (#222)
    "env_hash",  # resolved-environment identity (#222)
    "cluster",  # cluster key from clusters.yaml
    "profile",  # submission-shape label
    "submitted_at",  # ISO-8601 submit time
    "trial_tokens",  # opaque per-task reconciliation tokens (closed-loop)
)


def build_provenance_manifest(experiment_dir: Path, campaign_id: str) -> dict[str, Any]:
    """Return the provenance manifest for *campaign_id* as a JSON-ready dict.

    Walks every sidecar tagged with *campaign_id* (oldest-first, via
    :func:`hpc_agent.execution.mapreduce.reduce.history.find_sidecars_by_campaign`)
    and projects each to the :data:`_RUN_PROVENANCE_FIELDS` allowlist. The
    result is a single record:

    .. code-block:: json

        {
          "manifest_schema_version": 1,
          "campaign_id": "...",
          "run_count": 2,
          "runs": [
            {"run_id": "...", "cmd_sha": "...", "tasks_py_sha": "...",
             "data_sha": "...", "env_hash": "...", "cluster": "...",
             "profile": "...", "submitted_at": "...", "trial_tokens": [...]},
            ...
          ]
        }

    Each run carries its ``run_id`` (the sidecar's identity) plus the
    provenance allowlist; a field the sidecar never recorded is emitted as
    ``null`` so the manifest shape is uniform regardless of when the sidecar
    was written (v1 sidecars predate ``data_sha``/``env_hash``). The
    ``trial_tokens`` list pairs each task's opaque reconciliation token with
    the run — so a closed-loop result can be traced back to the exact
    {code, data, env, params} it was produced under.

    An *experiment_dir* with no matching sidecars returns a well-formed
    manifest with ``run_count == 0`` and an empty ``runs`` list — the absence
    of runs is itself a provenance fact worth recording, not an error.
    """
    sidecars = find_sidecars_by_campaign(Path(experiment_dir), campaign_id)
    runs: list[dict[str, Any]] = []
    for sidecar in sidecars:
        record: dict[str, Any] = {"run_id": sidecar.get("run_id")}
        for field in _RUN_PROVENANCE_FIELDS:
            record[field] = sidecar.get(field)
        runs.append(record)
    return {
        "manifest_schema_version": PROVENANCE_MANIFEST_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "run_count": len(runs),
        "runs": runs,
    }


def manifest_signature(manifest: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 over *manifest* — the signable digest.

    Canonicalizes the manifest to sorted-keys, separator-tight JSON before
    hashing so two manifests with the same content but different key order /
    whitespace produce the SAME signature. This is the value an operator
    signs (or commits) to attest "these results were produced by exactly
    these {code, data, env, params}"; re-deriving the manifest later and
    re-hashing detects any drift. Returns a 64-char hex string.
    """
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def write_provenance_manifest(
    experiment_dir: Path,
    campaign_id: str,
) -> tuple[Path, dict[str, Any]]:
    """Build and atomically write the campaign provenance manifest.

    Writes ``<experiment>/.hpc/provenance/<campaign_id>.json`` and returns
    ``(path, written_object)`` — the written object is handed back so a
    caller (the ``provenance-manifest`` primitive) can summarize what was
    written without re-reading the file it just wrote. The written object
    is the :func:`build_provenance_manifest`
    record plus a top-level ``signature`` (its :func:`manifest_signature`)
    so the file is self-attesting — a reader can recompute the signature
    over ``{everything except signature}`` and confirm it matches.

    The ``campaign_id`` is sanitized for the filename (``/`` → ``_``) the
    same way :meth:`RepoLayout.runtime_prior` sanitizes profile names, so a
    path-like campaign tag can't escape the provenance directory.
    """
    manifest = build_provenance_manifest(Path(experiment_dir), campaign_id)
    # Sign the manifest body, then attach the signature. The signature
    # deliberately covers only the body (not itself), so a reader strips
    # ``signature`` and re-hashes to verify.
    manifest_with_sig = dict(manifest)
    manifest_with_sig["signature"] = manifest_signature(manifest)

    from hpc_agent._kernel.contract.layout import RepoLayout
    from hpc_agent.infra.io import atomic_write_json

    safe_campaign = campaign_id.replace("/", "_")
    target = RepoLayout(experiment_dir).hpc / "provenance" / f"{safe_campaign}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, manifest_with_sig)
    return target, manifest_with_sig


@primitive(
    name="provenance-manifest",
    verb="mutate",
    side_effects=[SideEffect("file_write", "<experiment>/.hpc/provenance/<campaign_id>.json")],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="campaign_id",
    cli=CliShape(
        help=(
            "Build and write the per-campaign provenance manifest at "
            "<experiment>/.hpc/provenance/<campaign_id>.json — one signable "
            "record pairing every run_id/trial_token of the campaign with "
            "its full {code, data, env, params, cluster} fingerprint, "
            "recomputed from the run sidecars on demand."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ProvenanceManifestInput,
        schema_ref=SchemaRef(input="provenance_manifest"),
    ),
    agent_facing=True,
)
def provenance_manifest(*, experiment_dir: Path, spec: ProvenanceManifestInput) -> dict[str, Any]:
    """Write the campaign provenance manifest and return its summary.

    The agent-facing surface for :func:`write_provenance_manifest` (#312
    Gap 2): without it the manifest builder was a library function no
    orchestrator could reach. Returns ``{"path", "campaign_id",
    "run_count", "signature"}`` — the signature is the manifest's
    self-attesting digest, so a caller can record it (commit message,
    paper appendix) without re-reading the file.

    Idempotent by construction: the manifest is derived state, recomputed
    from the sidecars on every call, so replaying the verb after more
    submits simply refreshes the file to match the runs on disk.
    """
    target, written = write_provenance_manifest(Path(experiment_dir), spec.campaign_id)
    return {
        "path": str(target),
        "campaign_id": spec.campaign_id,
        "run_count": written.get("run_count", 0),
        "signature": written.get("signature", ""),
    }
