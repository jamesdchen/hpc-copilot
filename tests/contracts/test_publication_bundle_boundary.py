"""Boundary + behavioural contract for ``export-bundle`` (the publication bundle).

``export-bundle`` COMPOSES the sealed dossier evidence + the derived recipe (the
ONE dossier gather), the signed provenance manifest, a cite-check audit of the
manuscript, and the in-toto/DSSE attestations into one ``.zip`` under a top-level
self-attesting ``VERIFY`` manifest (``docs/design/publication-bundle.md``). It is
a SIBLING of ``export-dossier`` (the ``export-attestations`` precedent), never an
extension: the dossier's run-scoped contract stays untouched.

The pins, one per failure the design foresaw:

* **BUNDLE-MEMBER vocabulary (R-B3)** — the closed member set is equality-pinned
  to an inline authoritative copy AND is DISJOINT from ``DOSSIER_SOURCES`` (the
  cite-check report is a BUNDLE member, never a dossier store noun — so no
  dossier-boundary blast radius, no ``export-attestations`` pair-edit).
* **entry shape** — every sealed member entry is ``{member, path, sha256, bytes}``.
* **no parse** — the sealer copies member bytes verbatim; it never ``json.load``s
  a sealed member's content (the dossier Q1 posture, extended).
* **no LLM on the verdict/render path (R-B4)** — ``publication_bundle`` (the
  verdict) and ``bundle_render`` (the human render) import nothing LLM-adjacent;
  the render reaches no ``_wire`` and takes no free-prose parameter.
* **no domain vocabulary on the wire** — neither wire model exposes a
  domain-semantics field name.
* **decorator posture** — ``mutate``, one ``file_write``, no SSH, NOT MCP-curated.
* **disclosure inheritance (verify a guard can fire, both ways)** — an opted-out
  data run classifies the data link DISCLOSED, a declared one MECHANICAL; the
  verdict never says "reproducible"; an uncitable number rides the disclosure
  ledger, never a failure.
* **offline verify + tamper** — a real bundle recomputes offline (stdlib only)
  and a tampered member / signed manifest fails.

House style: mirrors ``test_dossier_boundary.py`` / ``test_attestation_export_boundary.py``
(AST + a closed authoritative set kept inline so drift surfaces).
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import inspect
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPS_MODULE = "hpc_agent.ops.publication_bundle"
_OPS_FILE = _REPO_ROOT / "src/hpc_agent/ops/publication_bundle.py"
_RENDER_FILE = _REPO_ROOT / "src/hpc_agent/ops/bundle_render.py"

# --- authoritative closed sets (kept inline; drift surfaces here) -----------

# The closed BUNDLE-MEMBER vocabulary (R-B3). Every value types a member of the
# COMPOSITION — never a dossier store noun. Mirrors
# ``publication_bundle.BUNDLE_MEMBERS``; the equality test below fails on drift.
_EXPECTED_MEMBERS = frozenset(
    {
        "dossier-evidence",
        "provenance-manifest",
        "cite-check-report",
        "attestations",
        "verify",
    }
)

# Every sealed member entry is typed by its BUNDLE MEMBER; these four keys
# describe it by provenance. A fifth, meaning-bearing key is the boundary leak.
_ENTRY_KEYS = frozenset({"member", "path", "sha256", "bytes"})

# Domain-semantics vocabulary core must never NAME (field names only).
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
        "accuracy",
        "loss",
    }
)

_LLM_IMPORT_MARKERS = ("anthropic", "openai", "llm", "prompt", "claude_", "generat")


# --- helpers ----------------------------------------------------------------


def _load_ops() -> Any:
    try:
        return importlib.import_module(_OPS_MODULE)
    except ImportError as exc:  # pragma: no cover - only before the verb lands
        pytest.fail(
            f"cannot import {_OPS_MODULE} (the export-bundle composer): {exc}. "
            "This contract pins the verb; it must export BUNDLE_MEMBERS + export_bundle."
        )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.Module) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def _schema_property_names(schema: dict[str, Any]) -> set[str]:
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


# --- (a) BUNDLE-MEMBER vocabulary pin (R-B3) --------------------------------


def test_bundle_member_vocabulary_is_closed_and_disjoint_from_dossier() -> None:
    """``BUNDLE_MEMBERS`` equals the inline set AND is disjoint from ``DOSSIER_SOURCES``.

    The load-bearing R-B3 property: the cite-check report (and the signed
    manifest, the attestations, the verify render) are PUBLICATION members, never
    dossier store nouns — so adding one fires NO dossier boundary pin and forces
    NO ``export-attestations`` pair-edit. The derived recipe travels sealed inside
    ``dossier-evidence`` (pointed at from the manifest), so the two vocabularies
    stay disjoint.
    """
    ops = _load_ops()
    from hpc_agent.ops.export_dossier import DOSSIER_SOURCES

    members = getattr(ops, "BUNDLE_MEMBERS", None)
    assert members is not None, f"{_OPS_MODULE} must export BUNDLE_MEMBERS."
    assert frozenset(members) == _EXPECTED_MEMBERS, (
        "BUNDLE_MEMBERS drifted from the inline authoritative set. "
        f"expected {sorted(_EXPECTED_MEMBERS)}, found {sorted(members)}. Adding a "
        "member is a reviewed vocabulary change."
    )
    overlap = frozenset(members) & frozenset(DOSSIER_SOURCES)
    assert not overlap, (
        f"BUNDLE_MEMBERS overlaps DOSSIER_SOURCES on {sorted(overlap)} — the bundle "
        "vocabulary must be DISJOINT from the dossier store nouns (R-B3): a "
        "publication member is never a run store."
    )
    # No forbidden domain word may masquerade as a member.
    assert not (frozenset(members) & _FORBIDDEN_FIELD_NAMES)


# --- (b) entry-shape pin ----------------------------------------------------


def test_member_entries_are_provenance_records() -> None:
    """Every sealed member entry has EXACTLY ``{member, path, sha256, bytes}``.

    A fifth, meaning-bearing key ("role", "treatment") is the boundary leak.
    Pinned by AST over every dict literal carrying ``sha256`` in the module.
    """
    key_sets: list[frozenset[str]] = []
    for node in ast.walk(_tree(_OPS_FILE)):
        if isinstance(node, ast.Dict):
            keys = frozenset(
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
            if "sha256" in keys:
                key_sets.append(keys)
    assert key_sets, "found no member-entry construction (no dict carrying 'sha256')."
    for keys in key_sets:
        assert keys == _ENTRY_KEYS, (
            f"a member entry's key set drifted: expected {sorted(_ENTRY_KEYS)}, "
            f"found {sorted(keys)}. A member is typed by which part of the "
            "composition it is (member/path/sha256/bytes), never by what it means."
        )


# --- (c) no-parse pin -------------------------------------------------------


def test_sealer_never_parses_member_content() -> None:
    """The sealer copies member bytes; it never ``json.load``s content it seals.

    The added members (cite-check report, signed manifest, attestations, verify
    render) are FRAMEWORK-derived records — serialized once (``json.dumps``,
    untouched) and sealed as opaque bytes. There is NO ``json.load`` /
    ``json.loads`` anywhere in the module (the dossier no-parse posture, extended).
    """
    parse_calls = [
        node.lineno
        for node in ast.walk(_tree(_OPS_FILE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"load", "loads"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
    ]
    assert not parse_calls, (
        f"publication_bundle.py calls json.load/json.loads at line(s) {parse_calls} — "
        "the bundle seals members as OPAQUE BYTES and must never parse a sealed "
        "member's content. json.dumps to serialize a framework record is fine."
    )


# --- (d) no-LLM-on-the-verdict/render-path pin (R-B4) -----------------------


def test_verdict_and_render_paths_import_nothing_llm() -> None:
    """The verdict op + the render module import nothing LLM-adjacent.

    R-B4: the VERIFY verdict is CODE-emitted (a fixed template filled by the
    classification), never LLM-composed. The render is deterministic string
    formatting. Neither path may reach for prose generation.
    """
    for path in (_OPS_FILE, _RENDER_FILE):
        for mod in _imported_modules(_tree(path)):
            low = mod.lower()
            assert not any(marker in low for marker in _LLM_IMPORT_MARKERS), (
                f"{path.name} imports {mod!r} — the verdict/render path must not reach "
                "for LLM/prose generation; the verdict is a code template."
            )


def test_render_path_is_wire_free_and_takes_no_prose() -> None:
    """``bundle_render`` reaches no ``_wire`` and ``render_verify`` takes no prose param."""
    for mod in _imported_modules(_tree(_RENDER_FILE)):
        assert not mod.lower().startswith("hpc_agent._wire"), (
            f"bundle_render.py imports {mod!r} from _wire — the render path is "
            "wire-free (the ops op owns the Pydantic boundary)."
        )
    from hpc_agent.ops.bundle_render import render_verify

    params = set(inspect.signature(render_verify).parameters)
    forbidden = {"prose", "summary", "narrative", "text", "note", "commentary"}
    assert not (params & forbidden), "render_verify exposes a free-prose parameter."


# --- (e) wire forbidden-vocabulary pin --------------------------------------


def test_wire_models_expose_no_domain_vocabulary() -> None:
    """Neither wire model has a field NAME drawn from domain semantics."""
    from hpc_agent._wire.actions.publication_bundle import (
        ExportBundleResult,
        ExportBundleSpec,
    )

    for model in (ExportBundleSpec, ExportBundleResult):
        names = _schema_property_names(model.model_json_schema())
        leaked = names & _FORBIDDEN_FIELD_NAMES
        assert not leaked, (
            f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}."
        )


# --- (f) decorator posture pin ----------------------------------------------


def test_decorator_is_mutate_one_write_no_ssh_not_curated() -> None:
    """``export-bundle`` is ``mutate``, one ``file_write``, no SSH, NOT MCP-curated."""
    from hpc_agent.ops.publication_bundle import export_bundle

    meta = export_bundle._primitive_meta  # type: ignore[attr-defined]
    assert meta.verb == "mutate"
    assert meta.agent_facing is True
    kinds = [se.kind for se in meta.side_effects]
    assert kinds == ["file_write"], f"expected exactly one file_write, got {kinds}"
    assert meta.cli is not None and meta.cli.requires_ssh is False, "export-bundle uses no SSH"

    from hpc_agent._kernel.extension.mcp_server import _CURATED_EXTRA_VERBS

    assert "export-bundle" not in _CURATED_EXTRA_VERBS, (
        "export-bundle is a HUMAN-run publish step (the export-dossier posture) — "
        "it must stay OUT of the curated MCP catalog."
    )


# --- (g) disclosure inheritance / verify-a-guard-fires-both-ways -------------


def test_data_link_classification_fires_both_ways() -> None:
    """A declared-data run classifies data MECHANICAL; an opted-out one DISCLOSED.

    The honest-inheritance guard, exercised BOTH ways at the unit level: an
    undeclared ``data_sha`` must NEVER be laundered into MECHANICAL.
    """
    from hpc_agent.ops.publication_bundle import (
        LINK_DISCLOSED,
        LINK_MECHANICAL,
        _classify_links,
    )

    declared: dict[str, Any] = {"runs": [{"data_sha": "abc"}, {"data_sha": "def"}], "gaps": []}
    opted_out: dict[str, Any] = {"runs": [{"data_sha": "abc"}, {"data_sha": None}], "gaps": []}

    def _data(recipe: dict[str, Any]) -> str:
        links = _classify_links(recipe, None, None, manuscript_present=False)
        return str(next(link["status"] for link in links if link["link"] == "data"))

    assert _data(declared) == LINK_MECHANICAL
    assert _data(opted_out) == LINK_DISCLOSED


def test_transcription_link_fires_three_ways() -> None:
    """transcription: ABSENT (no manuscript) / MECHANICAL (clean) / DISCLOSED (uncitable)."""
    from hpc_agent.ops.publication_bundle import (
        LINK_ABSENT,
        LINK_DISCLOSED,
        LINK_MECHANICAL,
        _classify_links,
    )

    recipe: dict[str, Any] = {"runs": [], "gaps": []}

    def _trans(cite: dict[str, Any] | None, present: bool) -> str:
        links = _classify_links(recipe, cite, None, manuscript_present=present)
        return str(next(link["status"] for link in links if link["link"] == "transcription"))

    assert _trans(None, False) == LINK_ABSENT
    assert _trans({"clean": True, "findings": [{"kind": "matched"}]}, True) == LINK_MECHANICAL
    assert _trans({"clean": False, "findings": [{"kind": "uncitable"}]}, True) == LINK_DISCLOSED


def test_verdict_is_code_emitted_and_never_says_reproducible() -> None:
    """The CODE-emitted verdict never stamps a bare "reproducible" — both ways."""
    from hpc_agent.ops.publication_bundle import (
        LINK_DISCLOSED,
        LINK_MECHANICAL,
        _bundle_verdict,
    )

    all_mechanical = [{"link": name, "status": LINK_MECHANICAL} for name in ("code", "data")]
    with_disclosed = [
        {"link": "code", "status": LINK_MECHANICAL},
        {"link": "data", "status": LINK_DISCLOSED},
    ]
    for links in (all_mechanical, with_disclosed):
        verdict = _bundle_verdict(links)
        assert not re.search(r"\breproducible\b", verdict, re.IGNORECASE), (
            "the verdict must never assert the bundle IS 'reproducible' — it is a "
            f"proof-of-mechanical + ledger-of-disclosed, never a certificate: {verdict!r}"
        )
        # the classification is carried in the code template.
        assert "MECHANICAL" in verdict


# --- (h) behavioural: seed a toy run, export, verify offline, tamper --------


def _seed_run(
    experiment: Path,
    run_id: str,
    *,
    campaign_id: str | None = None,
    data_sha: str | None = None,
) -> None:
    """Seed a run through the REAL writers (the attestation-boundary seeding)."""
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

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
        campaign_id=campaign_id,
        data_sha=data_sha,
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


def _seed_table(experiment: Path, run_id: str, value: str) -> None:
    """Seal a metrics_aggregate.json so cite-check has a value pool."""
    agg = experiment / "_aggregated" / run_id / "metrics_aggregate.json"
    agg.parent.mkdir(parents=True, exist_ok=True)
    agg.write_text(
        json.dumps(
            {
                "aggregated_metrics": {run_id: {"score": value}},
                "provenance": {"source": "local_reduce", "contributing_run_ids": [run_id]},
            }
        ),
        encoding="utf-8",
    )


def test_bundle_seals_verifies_offline_and_detects_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real bundle recomputes offline (stdlib only); a tampered member fails.

    The whole zero-dependency offline-verify story (Layer 1): unzip, sha each
    member, recompute ``bundle_sha256`` over the path-sorted entries — with no
    hpc-agent. Then the signed provenance manifest verifies, and tampering a
    member breaks both its recorded sha AND (for the signed manifest) the
    signature.
    """
    from hpc_agent._wire.actions.publication_bundle import ExportBundleSpec
    from hpc_agent.ops.provenance_manifest import (
        manifest_signature,
        verify_provenance_manifest,
    )
    from hpc_agent.ops.publication_bundle import export_bundle
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    run_id = "20260101-000001-aaaaaaa"
    _seed_run(experiment, run_id, campaign_id="camp-1")
    _seed_table(experiment, run_id, "0.9421")

    manuscript = "We report a score of 0.9421 and a spurious 0.7777 value."
    result = export_bundle(
        experiment_dir=experiment,
        spec=ExportBundleSpec(run_id=run_id, manuscript_text=manuscript),
    )

    bundle = Path(result.bundle_path)
    assert bundle.is_file()

    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        assert "VERIFY.json" in names and "VERIFY.md" in names
        assert "cite-check-report.json" in names, "manuscript supplied → report member present"
        assert "provenance-manifest.json" in names and "attestations.jsonl" in names

        verify = json.loads(zf.read("VERIFY.json"))
        assert verify["bundle_schema_version"] == 1
        assert verify["verdict_meta"]["claims_reproducible"] is False

        # Layer 1 — every member sha + the top-level seal recompute (stdlib only).
        for entry in verify["entries"]:
            data = zf.read(entry["path"])
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
            assert len(data) == entry["bytes"]
        assert manifest_signature(verify["entries"]) == verify["bundle_sha256"]
        assert verify["bundle_sha256"] == result.bundle_sha256

        # the signed provenance manifest member verifies.
        prov = json.loads(zf.read("provenance-manifest.json"))
        assert verify_provenance_manifest(prov)

        # data link DISCLOSED (the seed run declared no data_sha), never MECHANICAL.
        data_link = next(link for link in verify["links"] if link["link"] == "data")
        assert data_link["status"] == "DISCLOSED"

        # Layer 2 — the DSSE attestations subject digests round-trip to the dossier
        # entries (carried under the dossier/ prefix in the bundle).
        entry_by_dossier_path = {e["path"].removeprefix("dossier/"): e for e in verify["entries"]}
        att = [ln for ln in zf.read("attestations.jsonl").decode().splitlines() if ln]
        assert att
        for line in att:
            env = json.loads(line)
            assert env["signatures"] == []
            stmt = json.loads(base64.b64decode(env["payload"]))
            name = stmt["subject"][0]["name"]
            assert stmt["subject"][0]["digest"]["sha256"] == entry_by_dossier_path[name]["sha256"]

    # tamper: flip the signed manifest member's bytes in a rebuilt zip.
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "provenance-manifest.json":
                obj = json.loads(data)
                obj["runs"] = [*obj.get("runs", []), {"run_id": "INJECTED"}]
                data = json.dumps(obj, sort_keys=True, indent=2).encode("utf-8")
            zout.writestr(item, data)

    with zipfile.ZipFile(tampered) as zf:
        verify = json.loads(zf.read("VERIFY.json"))
        prov = json.loads(zf.read("provenance-manifest.json"))
        assert not verify_provenance_manifest(prov), "a tampered signed manifest must fail"
        entry = next(e for e in verify["entries"] if e["path"] == "provenance-manifest.json")
        got = hashlib.sha256(zf.read("provenance-manifest.json")).hexdigest()
        assert got != entry["sha256"], "a tampered member's sha must differ from the seal"


def test_no_manuscript_disclose_skips_cite_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent a manuscript, the bundle still seals; the report is disclose-skipped."""
    from hpc_agent._wire.actions.publication_bundle import ExportBundleSpec
    from hpc_agent.ops.publication_bundle import export_bundle
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    run_id = "20260101-000002-bbbbbbb"
    _seed_run(experiment, run_id)  # no campaign → provenance also disclose-skipped

    result = export_bundle(experiment_dir=experiment, spec=ExportBundleSpec(run_id=run_id))

    with zipfile.ZipFile(Path(result.bundle_path)) as zf:
        names = set(zf.namelist())
    assert "cite-check-report.json" not in names, "no manuscript → no report member"
    assert "provenance-manifest.json" not in names, "no campaign → no signed manifest member"

    codes = {d.get("code") for d in result.disclosures}
    assert "cite-check-skipped" in codes
    assert "provenance-manifest-skipped" in codes

    links = result.verify_manifest["links"]
    trans = next(link for link in links if link["link"] == "transcription")
    assert trans["status"] == "ABSENT"


def test_both_manuscript_sources_is_a_spec_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Supplying BOTH manuscript sources is a spec error (at-most-one, R-B2)."""
    from hpc_agent import errors
    from hpc_agent._wire.actions.publication_bundle import ExportBundleSpec
    from hpc_agent.ops.publication_bundle import export_bundle
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    run_id = "20260101-000003-ccccccc"
    _seed_run(experiment, run_id)

    with pytest.raises(errors.SpecInvalid):
        export_bundle(
            experiment_dir=experiment,
            spec=ExportBundleSpec(run_id=run_id, manuscript_text="x", manuscript_path="y.tex"),
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
