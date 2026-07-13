"""T5 tests — ``ops/evidence_brief_op.py``: the ``evidence-brief`` point query.

Toy WIDGET vocabulary only (never harxhar/quant — the domain-packs toy-fixture
rule). Exercises the verb end-to-end over crafted journals/ledgers/sidecars:
the happy point query (tags; lineage; both, union deduped); ``as_of`` threading;
the content cache (miss→hit, honesty, ``HPC_NO_EVIDENCE_CACHE`` disabled,
deleted-cache byte-identity); fleet mode with a skipped namespace; the
non-creating pin; an unresolvable citation DISCLOSED (never raised); and the T4
render seam (stubbed).

Expected-red until the orchestrator regen (registry / schema-roundtrip tests) —
those are NOT chased here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent._wire.queries.evidence import EvidenceBriefResult, EvidenceBriefSpec
from hpc_agent.ops import evidence_brief_op
from hpc_agent.ops.evidence_brief_op import evidence_brief

_FROZEN = "2026-07-08T06:12:00+00:00"


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``computed_at`` so recompute is byte-reproducible for a fixed clock."""
    monkeypatch.setattr(evidence_brief_op, "utcnow_iso", lambda: _FROZEN)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the journal home (cache + fleet discovery) to a scratch dir."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("HPC_NO_EVIDENCE_CACHE", raising=False)
    return tmp_path / "home"


# --- toy-store writers -------------------------------------------------------


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _write_conclusion(
    exp: Path,
    cid: str,
    *,
    ts: str,
    tags: list[str],
    citations: list[dict],
    finding: str = "widget showed no drift",
    concludes: list[dict] | None = None,
) -> None:
    resolved: dict = {
        "conclusion_id": cid,
        "tags": tags,
        "citations": citations,
        "finding": finding,
    }
    if concludes is not None:
        resolved["concludes"] = concludes
    rec = {
        "ts": ts,
        "scope_kind": "conclusion",
        "scope_id": cid,
        "block": "conclusion",
        "response": "y",
        "resolved": resolved,
    }
    _append_jsonl(exp / ".hpc" / "conclusions" / f"{cid}.decisions.jsonl", rec)


def _write_sidecar(
    exp: Path, run_id: str, *, cmd_sha: str, submitted_at: str, scopes: list[str] | None = None
) -> None:
    p = exp / ".hpc" / "runs" / f"{run_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    sidecar: dict = {"run_id": run_id, "cmd_sha": cmd_sha, "submitted_at": submitted_at}
    if scopes is not None:
        sidecar["scopes"] = scopes
    p.write_text(json.dumps(sidecar), encoding="utf-8")


def _cite(kind: str, ref: str, sha: str) -> dict:
    return {"kind": kind, "ref": ref, "sha": sha}


def _seed_verified(exp: Path) -> None:
    """A widget-x conclusion citing a run whose sidecar makes the citation verify."""
    _write_sidecar(
        exp,
        "widget-run-1",
        cmd_sha="cmdsha-verify",
        submitted_at="2025-11-01T00:00:00+00:00",
        scopes=["widget-x"],
    )
    _write_conclusion(
        exp,
        "widget-concl",
        ts="2025-11-01T09:00:00+00:00",
        tags=["widget-x"],
        citations=[_cite("run", "widget-run-1", "cmdsha-verify")],
        concludes=[{"scope_kind": "run", "scope_id": "widget-run-1"}],
    )


# --- happy point queries -----------------------------------------------------


def test_tag_query_surfaces_conclusion(home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_verified(exp)
    res = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert isinstance(res, EvidenceBriefResult)
    assert [c.conclusion_id for c in res.conclusions] == ["widget-concl"]
    assert res.conclusions[0].cited_shas == ["cmdsha-v"]  # sha[:8]
    assert res.conclusions[0].status == "current"
    # citation re-resolved at read → verified True (the sidecar cmd_sha matches).
    assert res.citations_status and res.citations_status[0].verified is True
    assert res.computed_at == _FROZEN


def test_lineage_query_surfaces_work(home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_verified(exp)
    res = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(lineage="widget-run-1"))
    # matched by code identity (the run is named in concludes / carries the cmd_sha).
    assert [c.conclusion_id for c in res.conclusions] == ["widget-concl"]


def test_both_keys_union_deduped(home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_verified(exp)
    res = evidence_brief(
        experiment_dir=exp,
        spec=EvidenceBriefSpec(tags=["widget-x"], lineage="widget-run-1"),
    )
    # The conclusion matches via BOTH keys but is disclosed ONCE (union, not doubled).
    assert [c.conclusion_id for c in res.conclusions] == ["widget-concl"]


def test_as_of_excludes_newer(home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(
        exp,
        "widget-old",
        ts="2025-06-01T00:00:00+00:00",
        tags=["widget-x"],
        citations=[_cite("run", "r-old", "sha-old-000000")],
    )
    _write_conclusion(
        exp,
        "widget-new",
        ts="2025-12-01T00:00:00+00:00",
        tags=["widget-x"],
        citations=[_cite("run", "r-new", "sha-new-000000")],
    )
    res = evidence_brief(
        experiment_dir=exp,
        spec=EvidenceBriefSpec(tags=["widget-x"], as_of="2025-09-01T00:00:00+00:00"),
    )
    ids = [c.conclusion_id for c in res.conclusions]
    assert ids == ["widget-old"]
    assert res.as_of == "2025-09-01T00:00:00+00:00"


# --- the content cache -------------------------------------------------------


def test_cache_miss_then_hit(home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_verified(exp)
    r1 = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert r1.cache == "miss"
    r2 = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert r2.cache == "hit"
    # the served payload equals the computed one everywhere but the cache field.
    assert r2.model_dump(exclude={"cache"}) == r1.model_dump(exclude={"cache"})


def test_cache_disabled_is_honest(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_NO_EVIDENCE_CACHE", "1")
    exp = tmp_path / "exp"
    _seed_verified(exp)
    r1 = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    r2 = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert r1.cache == "disabled" and r2.cache == "disabled"
    # never stored: no cache dir under the journal home.
    assert not (home / "evidence_cache").exists()


def test_deleted_cache_byte_identical(home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _seed_verified(exp)
    r1 = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    # Deleting the cache loses nothing — recompute is byte-equal (disposable index).
    import shutil

    shutil.rmtree(home / "evidence_cache")
    r3 = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert r3.cache == "miss"
    assert json.dumps(r3.model_dump(), sort_keys=True) == json.dumps(
        r1.model_dump(), sort_keys=True
    )


# --- fleet mode --------------------------------------------------------------


def test_fleet_with_skipped_namespace(home: Path, tmp_path: Path) -> None:
    exp1 = tmp_path / "exp1"
    exp1.mkdir()
    _seed_verified(exp1)
    # a good namespace pointing at exp1
    ns1 = home / "ns1"
    ns1.mkdir(parents=True)
    (ns1 / "repo.json").write_text(json.dumps({"experiment_dir": str(exp1)}), encoding="utf-8")
    # a torn namespace — must be skipped + counted, never fatal
    torn = home / "torn"
    torn.mkdir(parents=True)
    (torn / "repo.json").write_text("{ this is not json", encoding="utf-8")

    res = evidence_brief(
        experiment_dir=tmp_path / "unused", spec=EvidenceBriefSpec(tags=["widget-x"], fleet=True)
    )
    assert [c.conclusion_id for c in res.conclusions] == ["widget-concl"]
    assert [s.ref for s in res.skipped] == ["torn"]


# --- the non-creating pin ----------------------------------------------------


def test_non_creating_fresh_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyed read over a fresh namespace creates NO directories (cache disabled)."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("HPC_NO_EVIDENCE_CACHE", "1")
    exp = tmp_path / "exp"
    exp.mkdir()
    res = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert res.conclusions == []
    assert list(exp.iterdir()) == []  # no .hpc tree materialized
    assert not (tmp_path / "home").exists()  # journal home untouched


# --- read-side citation disclosure -------------------------------------------


def test_unresolvable_citation_disclosed_not_raised(home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(
        exp,
        "widget-ghost",
        ts="2025-11-01T00:00:00+00:00",
        tags=["widget-x"],
        citations=[_cite("run", "ghost-run", "sha-ghost-0000")],
    )
    res = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert [c.conclusion_id for c in res.conclusions] == ["widget-ghost"]
    # unresolvable → disclosed as verified=False, never a refusal.
    assert res.citations_status and res.citations_status[0].verified is False


def test_dossier_citation_unresolvable_disclosed(home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(
        exp,
        "widget-doss",
        ts="2025-11-01T00:00:00+00:00",
        tags=["widget-x"],
        citations=[_cite("dossier", "some-run", "sha-doss-00000")],
    )
    # No dossier resolvable on a bare namespace → disclosed, the read never raises.
    res = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert res.citations_status[0].kind == "dossier"
    assert res.citations_status[0].verified is False


# --- the T4 render seam ------------------------------------------------------


def test_render_seam_stubbed(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = tmp_path / "exp"
    _seed_verified(exp)
    monkeypatch.setattr(
        evidence_brief_op,
        "_render_brief",
        lambda coll, *, computed_at, as_of: f"WIDGET-RENDER {computed_at}",
    )
    res = evidence_brief(experiment_dir=exp, spec=EvidenceBriefSpec(tags=["widget-x"]))
    assert res.render == f"WIDGET-RENDER {_FROZEN}"
