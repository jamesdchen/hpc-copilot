"""Boundary contract for ``export-dossier``: entries typed by STORE, never by
meaning; the wire carries no experiment vocabulary; the bundler never parses
the content it copies.

``export-dossier`` bundles a run's core-owned record trail into one
integrity-sealed archive so a repo-side renderer can build an evidence package
FROM it. The whole feature lives or dies on one line of the boundary test in
``docs/internals/engineering-principles.md`` (Q1, "substrate, not semantics"):
core knows *which store* a bundled entry came from — a sidecar, a decision
record, a harvested aggregate — and NOTHING about what that entry means. The
moment a field name or a store name says "holdout" / "control" / "metric", the
bundler has crossed from IDENTITY+COUNTING over opaque content into naming the
caller's semantics — the exact leak the four-question test forbids.

Three cheap pins hold that line, one per failure the design foresaw:

* **entry shape** — every manifest entry is a store-provenance record with
  EXACTLY ``{source, path, sha256, bytes}``. A fifth key is where a
  meaning-bearing field ("role", "kind", "treatment") would sneak in.
* **forbidden vocabulary** — neither wire model exposes a field NAME drawn from
  the domain-semantics set, and the closed source-store vocabulary equals the
  agreed store-noun set (no ad-hoc store name may appear).
* **no parse** — the bundler copies bytes; it never ``json.load``s the content
  it seals (the aggregated store especially is opaque bytes), so it can never
  grow an interpretation of what it carries.

House style: this mirrors ``test_lint_sidecar_field_reads.py`` (AST + a closed
authoritative set kept inline so drift surfaces) and
``test_monitor_arm_cron_lifecycle_guidance.py`` (a distinctive-marker pin).
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPS_MODULE = "hpc_agent.ops.export_dossier"
_OPS_FILE = _REPO_ROOT / "src/hpc_agent/ops/export_dossier.py"

# --- authoritative closed sets (kept inline; drift surfaces here) -----------

# The store-provenance shape every manifest entry carries. An entry is typed by
# the SOURCE STORE it came from; these four keys describe it by provenance
# (which store, where, integrity, size) — never by what it means. A fifth key
# is the boundary leak this pins against.
_ENTRY_KEYS = frozenset({"source", "path", "sha256", "bytes"})

# The closed set of source-store names the bundler may draw from. Every value is
# a STORE NOUN (a concrete on-disk store), never a caller-owned role. Mirrors
# ``export_dossier.DOSSIER_SOURCES`` — the equality test below fails on drift.
_EXPECTED_SOURCES = frozenset(
    {
        "sidecar",
        "decision-journal",
        "briefs",
        "block-terminal",
        "journal-record",
        "scope-journal",
        "look-ledger",
        "aggregated",
        "audited-source",
        "notebook-journal",
        "renders",
        "determinism-fingerprint",
        "pack-manifest",
        "pack-journal",
    }
)

# Domain-semantics vocabulary core must never name. Field NAMES only — prose and
# filenames (e.g. a ``metrics_aggregate.json`` mentioned in a description) are
# fine, since the bundler copies such a file as opaque bytes without knowing it.
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "control",
        "controls",
        "unit",
        "units",
        "metric",
        "metrics",
        "holdout",
        "treatment",
        "baseline",
        "significance",
        "placebo",
        "anchor",
    }
)


# --- helpers ----------------------------------------------------------------


def _load_ops() -> Any:
    """Import B2's ops module, or fail with a precise, actionable message."""
    try:
        return importlib.import_module(_OPS_MODULE)
    except ImportError as exc:  # pragma: no cover - only before B2 lands
        pytest.fail(
            f"cannot import {_OPS_MODULE} (the export-dossier bundler): {exc}. "
            "This contract pins B2's module; it must exist and export "
            "DOSSIER_SOURCES + build {source,path,sha256,bytes} manifest entries."
        )


def _ops_tree() -> ast.Module:
    return ast.parse(_OPS_FILE.read_text(encoding="utf-8"), filename=str(_OPS_FILE))


def _schema_property_names(schema: dict[str, Any]) -> set[str]:
    """Every property NAME anywhere in a JSON schema, recursively.

    Walks the whole schema object (top-level ``properties`` plus every nested
    model under ``$defs``/``items``/etc.); collects the keys of any dict found
    under a ``properties`` key. Names only — descriptions and titles are not
    walked, so domain words in prose never trip the forbidden-vocabulary test.
    """
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(k for k in props if isinstance(k, str))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return names


def _entry_key_sets_in_module() -> list[frozenset[str]]:
    """Key sets of every manifest-entry construction in the bundler.

    A manifest entry is distinctively the dict carrying ``sha256``. We collect
    both literal ``{"sha256": ...}`` dicts and ``dict(sha256=...)`` calls so the
    pin does not couple to B2's builder-function name or construction style.
    """
    out: list[frozenset[str]] = []
    for node in ast.walk(_ops_tree()):
        # {"source": ..., "path": ..., "sha256": ..., "bytes": ...}
        if isinstance(node, ast.Dict):
            keys = frozenset(
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
            if "sha256" in keys:
                out.append(keys)
        # dict(source=..., path=..., sha256=..., bytes=...)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
        ):
            keys = frozenset(kw.arg for kw in node.keywords if kw.arg is not None)
            if "sha256" in keys:
                out.append(keys)
    return out


# --- (a) entry-shape pin ----------------------------------------------------


def test_manifest_entries_are_store_provenance_records() -> None:
    """Every manifest entry has EXACTLY ``{source, path, sha256, bytes}``.

    A store-provenance record: source store, path, integrity hash, size. A
    fifth key is where a meaning-bearing field ("role", "treatment", "kind")
    would enter — the boundary leak. Pinned by AST so it holds regardless of
    which internal helper builds the entry.
    """
    key_sets = _entry_key_sets_in_module()
    assert key_sets, (
        "found no manifest-entry construction in export_dossier.py (no dict or "
        "dict(...) carrying a 'sha256' key). Either the entry shape changed or "
        "the bundler builds entries in a form this AST pin cannot see — update "
        "the pin deliberately if the {source,path,sha256,bytes} contract moved."
    )
    for keys in key_sets:
        assert keys == _ENTRY_KEYS, (
            "a manifest entry's key set drifted from the store-provenance shape. "
            f"expected {sorted(_ENTRY_KEYS)}, found {sorted(keys)}. An entry is "
            "typed by its SOURCE STORE (source/path/sha256/bytes), never by what "
            "it means — a fifth, meaning-bearing key is the boundary leak."
        )


# --- (b) forbidden-vocabulary pin -------------------------------------------


def test_wire_models_expose_no_domain_vocabulary() -> None:
    """Neither wire model has a field NAME drawn from domain semantics.

    Walks ``model_json_schema()`` property names recursively (nested models
    included) for both ExportDossierSpec and ExportDossierResult. Names only:
    the ``manifest``/``gaps`` descriptions may mention aggregate filenames in
    prose without tripping this, because the bundler copies such files as opaque
    bytes and never names their meaning.
    """
    from hpc_agent._wire.actions.export_dossier import (
        ExportDossierResult,
        ExportDossierSpec,
    )

    for model in (ExportDossierSpec, ExportDossierResult):
        names = _schema_property_names(model.model_json_schema())
        leaked = names & _FORBIDDEN_FIELD_NAMES
        assert not leaked, (
            f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
            "The dossier wire describes a bundle by PROVENANCE only (which stores, "
            "how many entries, what identities); a field named for a caller-owned "
            "role is the substrate-vs-semantics leak (engineering-principles Q1)."
        )


def test_source_store_vocabulary_is_the_closed_store_noun_set() -> None:
    """``DOSSIER_SOURCES`` equals the agreed store-noun set — exactly.

    Every value is a concrete on-disk STORE, never a caller role. Equality (not
    subset) so a new ad-hoc store name cannot be added without landing here as a
    reviewed change to the closed vocabulary.
    """
    ops = _load_ops()
    sources = getattr(ops, "DOSSIER_SOURCES", None)
    assert sources is not None, (
        f"{_OPS_MODULE} must export DOSSIER_SOURCES (the closed source-store "
        "vocabulary); the wire deliberately does not carry it."
    )
    assert frozenset(sources) == _EXPECTED_SOURCES, (
        "DOSSIER_SOURCES drifted from the closed store-noun set. "
        f"expected {sorted(_EXPECTED_SOURCES)}, found {sorted(sources)}. Every "
        "value must be a store noun; adding one is a reviewed vocabulary change."
    )
    # No forbidden domain word may masquerade as a store name.
    assert not (frozenset(sources) & _FORBIDDEN_FIELD_NAMES), (
        "a DOSSIER_SOURCES value collides with the domain-semantics vocabulary — "
        "a store is named for where content lives, never for what it means."
    )


# --- (c) no-parse pin -------------------------------------------------------


def test_bundler_copies_bytes_and_never_parses_content() -> None:
    """The bundler seals bytes; it never ``json.load``s the content it copies.

    Pin choice (strongest cheap form of the three the task offered): the module
    copies every source as raw bytes — so there is simply NO ``json.load`` /
    ``json.loads`` anywhere in it, and it DOES reach for ``read_bytes``. This is
    stronger and cheaper than AST-tracing which path an ``aggregated`` filename
    flows into: if no parse exists at all, the aggregated store (opaque bytes by
    contract) cannot be parsed either. ``json.dumps`` (used to sign the manifest
    of provenance records via ``manifest_signature``) is untouched — the ban is
    on reading content back into structure, not on serializing provenance.
    """
    tree = _ops_tree()

    parse_calls: list[int] = []
    reads_bytes = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "read_bytes":
            reads_bytes = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"load", "loads"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json"
        ):
            parse_calls.append(node.lineno)

    assert not parse_calls, (
        "export_dossier.py calls json.load/json.loads at line(s) "
        f"{parse_calls} — the bundler copies source stores as OPAQUE BYTES and "
        "must never parse the content it seals (the aggregated store especially "
        "is copied raw, never interpreted). Copy read_bytes(); don't parse."
    )
    assert reads_bytes, (
        "export_dossier.py never calls read_bytes — a byte-copying bundler is "
        "expected to gather source content as raw bytes for hashing and sealing."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
