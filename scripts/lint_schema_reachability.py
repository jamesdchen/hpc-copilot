#!/usr/bin/env python3
"""Every ``schemas/*.json`` file is reachable from ``schema_for`` or allowlisted.

The catalog / ``describe`` / ``capabilities`` surface and the runtime
``validate_output`` gate both resolve a primitive's schema file through
``operations.schema_candidate_ladder`` (the ladder ``schema_for`` walks). A
schema file that NO registered primitive resolves to — and that isn't a
documented cross-cutting / persisted-file shape — is dead wire surface, or a
file that silently lost its owner to a rename (``schema_for`` degraded to
``None`` and the primitive quietly stopped being ``describe``-able / validated).

The existing orphan lints in ``tests/contracts/test_schema_roundtrip.py`` assert
reachability via ``_CLI_VERBS`` (does a verb exist) and via the catalog
``output_schema`` field. This lint closes the remaining gap: it asserts every
one of the ``schemas/*.json`` stems — inputs, outputs, AND the non-``.input`` /
non-``.output`` persisted-file schemas — is reachable specifically from
``schema_for`` over the LIVE registry, or is named in :data:`ALLOWLIST` with a
one-line reason. So the resolution ladder can't silently degrade to ``None`` on
a rename without either this lint or the allowlist noticing.

Reachability is DERIVED, never hardcoded: a file is reachable iff
``operations_catalog()`` (which applies ``schema_for`` to every registered
primitive) reports it as some primitive's ``input_schema`` / ``output_schema``.
The fix for a violation is to wire the owning primitive (or its
``SchemaRef(...)`` override), delete the stranded file, or — for a genuinely
cross-cutting / persisted-file / composed-only shape — add an :data:`ALLOWLIST`
entry with its reason. Never edit the reachability computation to hide a file.

The allowlist is kept honest bidirectionally: an entry naming a file that no
longer exists (dangling) or a file that has SINCE become ``schema_for``-reachable
(redundant) also fails, so the allowlist stays a minimal, accurate inventory.

Usage::

    python scripts/lint_schema_reachability.py
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "hpc_agent" / "schemas"


# Schema files that no primitive's ``schema_for`` resolves — each with a
# one-line reason. Three families:
#
#   * Persisted-file / config schemas — validate an on-disk artifact through
#     the Python API, never a primitive's declared I/O.
#   * Envelope + envelope sub-block shapes — cross-cutting wire contracts
#     attached to any/every envelope, backing no single primitive.
#   * Composed-only / cross-cutting primitive shapes — consumed through code
#     paths other than a verb's declared input/output.
#
# A file that is neither ``schema_for``-reachable nor here fails the lint.
ALLOWLIST: dict[str, str] = {
    # --- persisted-file / config schemas ---
    "axes.json": (
        "config schema for <experiment>/.hpc/axes.yaml (state/axes); validated "
        "via the Python API, not a fireable primitive's I/O"
    ),
    "campaign_manifest.json": (
        "persisted <campaign_dir>/manifest.json schema; validated via the Python "
        "API, not a primitive's I/O"
    ),
    "stages.input.json": (
        "multi-stage DAG list schema validated by state/stages.py; consumed via "
        "the Python API, not a fireable verb (no 'stages' primitive)"
    ),
    "plugin_manifest.json": (
        "hpc_agent.plugins entry manifest schema; validated by the plugin loader / "
        "lint_plugin_manifests, not a primitive's I/O"
    ),
    # --- envelope + envelope sub-block schemas ---
    "envelope.json": (
        "the JSON envelope contract itself; wraps every primitive's output, backs "
        "none as its declared schema"
    ),
    "escalation.json": (
        "optional needs-a-decision block attached to Success/Error envelopes "
        "(#231), not a primitive's declared I/O"
    ),
    "failure_features.json": (
        "structured diagnostic block attached to ok=false envelopes (#230), not a "
        "primitive's declared I/O"
    ),
    # --- composed-only / cross-cutting primitive shapes ---
    "evidence_demand.input.json": (
        "EvidenceDemandSpec (_wire/queries/determinism.py) consumed by the "
        "registration prerequisite checker's `requires` leg via "
        "state/determinism.evidence_meets; never fireable as a verb"
    ),
    "inspect_cluster.output.json": (
        "cluster-snapshot contract validated by code via "
        "_output_schema_for('inspect-cluster'), not a catalog verb's output"
    ),
    "worker.output.json": (
        "sub-agent worker report floor consumed at the invoke / structured-decode "
        "boundary, not emitted by a catalog verb"
    ),
    "worker.strict.output.json": (
        "derived strict variant of worker.output.json for the Codex "
        "--output-schema worker (build_schemas DERIVED_REGISTRY), not a verb output"
    ),
}


def check(
    all_schema_files: set[str],
    reachable: set[str],
    allowlist: Mapping[str, str],
) -> list[str]:
    """Return one error string per reachability violation. Empty ⇒ clean.

    Pure: takes the set of every ``schemas/*.json`` filename, the set
    ``schema_for`` resolves for some registered primitive, and the allowlist.

    Three violation classes:

    * **unreachable & unlisted** — a file no primitive resolves to and that
      carries no allowlist reason (the core guard);
    * **dangling allowlist entry** — an allowlisted file that no longer exists;
    * **redundant allowlist entry** — an allowlisted file that IS now
      ``schema_for``-reachable, so its entry should be dropped.

    The last two keep the allowlist a minimal, accurate inventory rather than an
    ever-growing suppression list.
    """
    errors: list[str] = []

    for fname in sorted(all_schema_files - reachable):
        if fname not in allowlist:
            errors.append(
                f"{fname}: no registered primitive resolves this schema via "
                "schema_for, and it is not on the ALLOWLIST. Either wire the "
                "owning primitive (or its SchemaRef(...) override), delete the "
                "stranded file, or add an ALLOWLIST entry with its reason."
            )

    for fname in sorted(allowlist):
        if fname not in all_schema_files:
            errors.append(
                f"{fname}: ALLOWLIST names a schema file that does not exist under "
                "src/hpc_agent/schemas/ — the file was renamed or removed; drop or "
                "fix the ALLOWLIST entry."
            )
        elif fname in reachable:
            errors.append(
                f"{fname}: ALLOWLIST entry is redundant — the file is now reachable "
                "from schema_for (a primitive owns it). Drop the ALLOWLIST entry so "
                "the allowlist stays minimal."
            )

    return errors


def _all_schema_files() -> set[str]:
    """Every ``schemas/*.json`` filename in the source tree."""
    return {p.name for p in SCHEMAS_DIR.glob("*.json")}


def _reachable_schema_files() -> set[str]:
    """Files ``schema_for`` resolves for some registered primitive.

    Derived from the live catalog: ``operations_catalog()`` applies
    ``schema_for`` to every ``@primitive`` and reports the resolved
    ``input_schema`` / ``output_schema`` filenames. Imported lazily so the pure
    :func:`check` stays unit-testable without importing the whole package.
    """
    import hpc_agent
    from hpc_agent._kernel.registry.operations import operations_catalog

    hpc_agent.register_primitives()
    reached: set[str] = set()
    for row in operations_catalog():
        for key in ("input_schema", "output_schema"):
            fname = row.get(key)
            if fname:
                reached.add(fname)
    return reached


def main() -> int:
    errors = check(_all_schema_files(), _reachable_schema_files(), ALLOWLIST)
    if errors:
        print("ERROR: schema files are not reachable from schema_for:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    print("all schema files are reachable from schema_for or documented on the ALLOWLIST")
    return 0


if __name__ == "__main__":
    sys.exit(main())
