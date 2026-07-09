"""Boundary contract for ``export-attestations``: the in-toto/DSSE PORTABILITY
layer over the dossier SEALING layer — a SIBLING of ``export-dossier``, not an
extension of it (``docs/design/conformance-kit.md`` D-K4).

Four pins hold the sibling's boundary, one per failure the design foresaw:

* **predicateType map equality** — ``PREDICATE_TYPES`` is a CLOSED vocabulary
  whose key set equals the live ``DOSSIER_SOURCES`` (both directions), and whose
  rows equal an inline authoritative copy (house style, mirroring
  ``test_dossier_boundary.py``'s ``_EXPECTED_SOURCES``). A new store noun in
  ``DOSSIER_SOURCES`` fails this until its URI row is added deliberately.
* **no parse** — the export copies bytes; it never ``json.load``s the content it
  attests, and it never reaches for ``hashlib`` (subject digests are copied
  VERBATIM from the dossier signature, never recomputed).
* **delegate / one gather** — the module consumes
  ``compute_dossier_signature`` and re-walks NO store (no ``read_bytes``), so
  the stores are gathered exactly once.
* **Statement/DSSE shape** — a real export over a seeded run round-trips to
  DSSE envelopes wrapping in-toto Statements of exactly the pinned shape, with
  subject digests copied verbatim from the dossier entries.

House style: mirrors ``test_dossier_boundary.py`` (AST + a closed authoritative
set kept inline so drift surfaces).

LIVE-CONFORMANCE PAIR-EDIT NOTE: ``_EXPECTED_PREDICATE_TYPES`` below is one half
of the deliberate pair-edit the live-conformance branch must make when it lands
its new ``DOSSIER_SOURCES`` noun — add the row HERE and the matching row in
``ops/export_attestations.py::PREDICATE_TYPES`` together (see the comment beside
that map). The equality pins in this file are what make the omission of either
row fail loudly.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPS_MODULE = "hpc_agent.ops.export_attestations"
_OPS_FILE = _REPO_ROOT / "src/hpc_agent/ops/export_attestations.py"

_PREDICATE_TYPE_SCHEME = "https://hpc-agent.dev/attestation"

# --- authoritative inline copy (drift surfaces here) ------------------------

# The store-noun → predicateType map, kept inline so a drift from the ops map
# (or from the live DOSSIER_SOURCES) surfaces as an explicit reviewed change.
# This is the second half of the live-conformance pair-edit (see module docstring).
_EXPECTED_PREDICATE_TYPES = {
    "sidecar": f"{_PREDICATE_TYPE_SCHEME}/sidecar/v1",
    "decision-journal": f"{_PREDICATE_TYPE_SCHEME}/decision-journal/v1",
    "briefs": f"{_PREDICATE_TYPE_SCHEME}/briefs/v1",
    "block-terminal": f"{_PREDICATE_TYPE_SCHEME}/block-terminal/v1",
    "journal-record": f"{_PREDICATE_TYPE_SCHEME}/journal-record/v1",
    "scope-journal": f"{_PREDICATE_TYPE_SCHEME}/scope-journal/v1",
    "look-ledger": f"{_PREDICATE_TYPE_SCHEME}/look-ledger/v1",
    "aggregated": f"{_PREDICATE_TYPE_SCHEME}/aggregated/v1",
    "audited-source": f"{_PREDICATE_TYPE_SCHEME}/audited-source/v1",
    "notebook-journal": f"{_PREDICATE_TYPE_SCHEME}/notebook-journal/v1",
    "renders": f"{_PREDICATE_TYPE_SCHEME}/renders/v1",
    "determinism-fingerprint": f"{_PREDICATE_TYPE_SCHEME}/determinism-fingerprint/v1",
    "pack-manifest": f"{_PREDICATE_TYPE_SCHEME}/pack-manifest/v1",
    "pack-journal": f"{_PREDICATE_TYPE_SCHEME}/pack-journal/v1",
}

# The DSSE envelope's fixed key set and the in-toto Statement's fixed key set —
# the portability shape a stock verifier reads.
_ENVELOPE_KEYS = frozenset({"payloadType", "payload", "signatures"})
_STATEMENT_KEYS = frozenset({"_type", "subject", "predicateType", "predicate"})
_PREDICATE_KEYS = frozenset({"contentType", "content"})


# --- helpers ----------------------------------------------------------------


def _load_ops() -> Any:
    """Import K3's ops module, or fail with a precise, actionable message."""
    try:
        return importlib.import_module(_OPS_MODULE)
    except ImportError as exc:  # pragma: no cover - only before K3 lands
        pytest.fail(
            f"cannot import {_OPS_MODULE} (the export-attestations projector): {exc}. "
            "This contract pins K3's module; it must exist and export PREDICATE_TYPES."
        )


def _ops_tree() -> ast.Module:
    return ast.parse(_OPS_FILE.read_text(encoding="utf-8"), filename=str(_OPS_FILE))


# --- (a) predicateType map equality pin -------------------------------------


def test_predicate_type_map_equals_inline_authoritative_copy() -> None:
    """``PREDICATE_TYPES`` equals the inline authoritative copy — exactly.

    Values pinned inline (house style, like ``_EXPECTED_SOURCES`` in
    ``test_dossier_boundary.py``): the URI scheme, the per-noun rows, and the
    ``/v1`` suffix cannot drift silently. This is the boundary-test half of the
    live-conformance pair-edit.
    """
    ops = _load_ops()
    predicate_types = getattr(ops, "PREDICATE_TYPES", None)
    assert predicate_types is not None, (
        f"{_OPS_MODULE} must export PREDICATE_TYPES (the store-noun → predicateType map)."
    )
    assert predicate_types == _EXPECTED_PREDICATE_TYPES, (
        "PREDICATE_TYPES drifted from the inline authoritative copy. "
        f"expected {sorted(_EXPECTED_PREDICATE_TYPES.items())}, "
        f"found {sorted(predicate_types.items())}. A predicateType is "
        "`<scheme>/<store-noun>/v1`; changing one is a reviewed vocabulary change."
    )


def test_predicate_type_map_key_set_equals_dossier_sources() -> None:
    """``PREDICATE_TYPES`` keys equal the LIVE ``DOSSIER_SOURCES`` — both directions.

    Equality against the live set (imported, not copied) is the normative
    artifact (``docs/design/conformance-kit.md`` D-K4): a new store noun landing
    in ``DOSSIER_SOURCES`` fails HERE until its URI row is added to
    ``PREDICATE_TYPES`` — the deliberate ops-side half of the pair-edit — and a
    predicateType for a noun the dossier no longer seals fails too.
    """
    ops = _load_ops()
    from hpc_agent.ops.export_dossier import DOSSIER_SOURCES

    predicate_nouns = frozenset(ops.PREDICATE_TYPES)
    missing = frozenset(DOSSIER_SOURCES) - predicate_nouns
    extra = predicate_nouns - frozenset(DOSSIER_SOURCES)
    assert not missing, (
        f"DOSSIER_SOURCES nouns with no predicateType row: {sorted(missing)}. "
        "Add each `<noun>: f'{PREDICATE_TYPE_SCHEME}/<noun>/v1'` row to "
        "PREDICATE_TYPES (and the matching row to _EXPECTED_PREDICATE_TYPES here) "
        "— the deliberate pair-edit."
    )
    assert not extra, (
        f"PREDICATE_TYPES names nouns absent from DOSSIER_SOURCES: {sorted(extra)}. "
        "The map is closed to the dossier's store vocabulary; remove the stale rows."
    )


def test_predicate_types_follow_the_one_scheme() -> None:
    """Every predicateType is ``<scheme>/<noun>/v1`` — one derivation, no ad-hoc URIs."""
    ops = _load_ops()
    for noun, uri in ops.PREDICATE_TYPES.items():
        assert uri == f"{ops.PREDICATE_TYPE_SCHEME}/{noun}/v1", (
            f"predicateType for {noun!r} is {uri!r}, not the derived "
            f"`{ops.PREDICATE_TYPE_SCHEME}/{noun}/v1` — the URI scheme is one "
            "derivation, never a hand-tweaked per-noun URI."
        )


# --- (b) no-parse pin -------------------------------------------------------


def test_export_never_parses_content_and_never_recomputes_digests() -> None:
    """The projector never ``json.load``s content and never reaches for ``hashlib``.

    Subject digests are copied VERBATIM from the dossier signature's entries;
    record bytes ride verbatim into the predicate. So the module has NO
    ``json.load`` / ``json.loads`` (the dossier no-parse posture, extended) and
    NO ``hashlib`` use (digests are never recomputed here). ``json.dumps`` — used
    only to serialize the Statement into the DSSE payload — is untouched.
    """
    tree = _ops_tree()

    parse_calls: list[int] = []
    hashlib_refs: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"load", "loads"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json"
        ):
            parse_calls.append(node.lineno)
        # any `hashlib` reference — import or attribute access
        if isinstance(node, ast.Name) and node.id == "hashlib":
            hashlib_refs.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "read_bytes":
            hashlib_refs.append(-1)  # sentinel handled by the delegate pin below

    assert not parse_calls, (
        "export_attestations.py calls json.load/json.loads at line(s) "
        f"{parse_calls} — the export embeds record bytes VERBATIM in the "
        "predicate and must never parse the content it attests (the dossier "
        "no-parse boundary, extended). json.dumps to build the Statement is fine."
    )
    hashlib_lines = [n for n in hashlib_refs if n != -1]
    assert not hashlib_lines, (
        "export_attestations.py references hashlib at line(s) "
        f"{hashlib_lines} — subject digests are copied VERBATIM from the dossier "
        "signature's entries, never recomputed here."
    )


# --- (c) delegate / one-gather pin ------------------------------------------


def test_export_delegates_the_gather_and_re_walks_no_store() -> None:
    """The module consumes ``compute_dossier_signature`` and re-reads no disk.

    The one gather is defined in ``export_dossier`` — this sibling PROJECTS its
    result. So the module references ``compute_dossier_signature`` and never
    calls ``read_bytes`` (which would be a second walk of the stores). Pinned by
    AST so it holds regardless of internal helper structure.
    """
    tree = _ops_tree()

    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "compute_dossier_signature" in names, (
        "export_attestations.py never references compute_dossier_signature — the "
        "export MUST delegate the gather to export_dossier's one signature seam, "
        "never re-walk the stores itself."
    )

    reads_bytes = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "read_bytes"
    ]
    assert not reads_bytes, (
        "export_attestations.py calls read_bytes at line(s) "
        f"{reads_bytes} — the sealed bytes come from the delegated dossier "
        "signature's write_map; re-reading a store from disk is a SECOND gather "
        "(the one-gather boundary). Consume compute_dossier_signature's result."
    )


# --- (d) Statement / DSSE shape pin (behavioral) ----------------------------


def test_exported_bundle_has_dsse_envelopes_wrapping_in_toto_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real export over a seeded run round-trips to the pinned DSSE/Statement shape.

    Toy fixture: a run with a sidecar + journal record (+ a couple of stores)
    seeded through the real writers, then exported. Every output line is a DSSE
    envelope wrapping an in-toto Statement of exactly the pinned shape, with the
    subject digest copied VERBATIM from the dossier entry and ``signatures``
    empty (unsigned v1).
    """
    import base64
    import json

    from hpc_agent._wire.actions.export_attestations import ExportAttestationsSpec
    from hpc_agent.ops.export_attestations import (
        DSSE_PAYLOAD_TYPE,
        IN_TOTO_STATEMENT_TYPE,
        PREDICATE_TYPES,
        export_attestations,
    )
    from hpc_agent.ops.export_dossier import compute_dossier_signature
    from hpc_agent.state import run_record
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    # Redirect the per-user journal home into the test's tmp dir.
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    run_id = "20260101-000001-aaaaaaa"

    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
    )
    upsert_run(
        experiment,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/remote",
            job_name="p",
            job_ids=["9001"],
            total_tasks=2,
            submitted_at="2026-01-01T00:00:00Z",
            experiment_dir=str(run_id),
        ),
    )
    append_decision(experiment, scope_kind="run", scope_id=run_id, block="s1", response="y")

    result = export_attestations(
        experiment_dir=experiment, spec=ExportAttestationsSpec(run_id=run_id)
    )

    out_path = Path(result.output_path)
    assert out_path.is_file()
    lines = [ln for ln in out_path.read_text(encoding="utf-8").splitlines() if ln]
    assert lines, "export produced no Statements for a seeded run"
    assert len(lines) == result.statement_count

    # The dossier signature the projection tied back to — subject digests must
    # match its entries verbatim.
    sig = compute_dossier_signature(experiment, run_id)
    assert result.bundle_sha256 == sig.bundle_sha256
    entry_by_path = {e["path"]: e for e in sig.entries}
    assert len(lines) == len(sig.entries)

    for line in lines:
        envelope = json.loads(line)
        assert frozenset(envelope) == _ENVELOPE_KEYS, (
            f"DSSE envelope key set drifted: {sorted(envelope)} != {sorted(_ENVELOPE_KEYS)}"
        )
        assert envelope["payloadType"] == DSSE_PAYLOAD_TYPE
        assert envelope["signatures"] == [], "v1 is UNSIGNED — signatures must be empty"

        statement = json.loads(base64.b64decode(envelope["payload"]))
        assert frozenset(statement) == _STATEMENT_KEYS, (
            f"Statement key set drifted: {sorted(statement)} != {sorted(_STATEMENT_KEYS)}"
        )
        assert statement["_type"] == IN_TOTO_STATEMENT_TYPE
        assert statement["predicateType"] in PREDICATE_TYPES.values()

        subject = statement["subject"]
        assert isinstance(subject, list) and len(subject) == 1
        assert frozenset(subject[0]) == {"name", "digest"}
        assert frozenset(subject[0]["digest"]) == {"sha256"}

        # subject digest copied VERBATIM from the dossier entry.
        name = subject[0]["name"]
        assert name in entry_by_path, f"Statement subject {name!r} is not a dossier entry"
        assert subject[0]["digest"]["sha256"] == entry_by_path[name]["sha256"]

        # predicateType matches the entry's source store noun.
        assert statement["predicateType"] == PREDICATE_TYPES[entry_by_path[name]["source"]]

        predicate = statement["predicate"]
        assert frozenset(predicate) == _PREDICATE_KEYS, (
            f"predicate key set drifted: {sorted(predicate)} != {sorted(_PREDICATE_KEYS)}"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
