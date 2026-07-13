"""T1 tests — ``state/evidence.py``: the conclusion record + the ONE collector.

Toy WIDGET vocabulary only (never harxhar/quant — the domain-packs toy-fixture
rule). Crafted journals/ledgers/sidecars exercise: every refusal, the
current/superseded/revoked/absent reduction, ``as_of`` exclusion everywhere,
retro-indexing, the unconcluded join, envelope-quoted-verbatim, deterministic
ordering, the non-creating pin, the ``CITATION_KINDS`` equality pin, and the
route-through assertion.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.state import (  # type: ignore[attr-defined]
    attestation,
    determinism,
    evidence,
    fingerprint_store,
)

# --- tiny toy-store writers (NON-CREATING globs read these back) -------------


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _conclusion_record(cid: str, *, ts: str, block: str = "conclusion", **resolved) -> dict:
    resolved.setdefault("conclusion_id", cid)
    return {
        "ts": ts,
        "scope_kind": "conclusion",
        "scope_id": cid,
        "block": block,
        "response": "y",
        "resolved": resolved if block == "conclusion" else {"conclusion_id": cid, **resolved},
    }


def _write_conclusion(exp: Path, cid: str, record: dict) -> None:
    _append_jsonl(exp / ".hpc" / "conclusions" / f"{cid}.decisions.jsonl", record)


def _write_sidecar(exp: Path, run_id: str, *, cmd_sha: str, submitted_at: str, scopes=None) -> None:
    p = exp / ".hpc" / "runs" / f"{run_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    sidecar: dict = {"run_id": run_id, "cmd_sha": cmd_sha, "submitted_at": submitted_at}
    if scopes is not None:
        sidecar["scopes"] = list(scopes)
    p.write_text(json.dumps(sidecar), encoding="utf-8")


def _cite(kind: str, ref: str, sha: str) -> dict:
    return {"kind": kind, "ref": ref, "sha": sha}


_WIDGET_CITE = [_cite("run", "widget-run-1", "sha-widget-1")]


# --- CITATION_KINDS equality pin ---------------------------------------------


def test_citation_kinds_equality_pin() -> None:
    expected_kinds = frozenset({"dossier", "run", "fingerprint", "attestation"})
    expected_statuses = frozenset({"current", "superseded", "revoked", "absent"})
    assert expected_kinds == evidence.CITATION_KINDS
    assert expected_statuses == evidence.STATUSES


# --- validate_citation refusals ----------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "widget", "ref": "r", "sha": "s"},  # unknown kind
        {"kind": "run", "ref": "", "sha": "s"},  # empty ref
        {"kind": "run", "ref": "r", "sha": ""},  # empty sha
        {"kind": "run", "ref": "r"},  # missing sha
    ],
)
def test_validate_citation_refuses(raw: dict) -> None:
    with pytest.raises(errors.SpecInvalid):
        evidence.validate_citation(raw)


# --- validate_conclusion_resolved refusals + allowances ----------------------


def test_conclusion_resolved_refuses_empty_citations() -> None:
    with pytest.raises(errors.SpecInvalid, match="NON-EMPTY"):
        evidence.validate_conclusion_resolved({"conclusion_id": "widget-c1", "citations": []})


def test_conclusion_resolved_refuses_bad_conclusion_id() -> None:
    with pytest.raises(errors.SpecInvalid, match="conclusion_id"):
        evidence.validate_conclusion_resolved(
            {"conclusion_id": "bad/id", "citations": _WIDGET_CITE}
        )


def test_conclusion_resolved_refuses_bad_tag_slug() -> None:
    with pytest.raises(errors.SpecInvalid):
        evidence.validate_conclusion_resolved(
            {"conclusion_id": "widget-c1", "tags": ["bad tag"], "citations": _WIDGET_CITE}
        )


def test_conclusion_resolved_allows_empty_tags() -> None:
    parsed = evidence.validate_conclusion_resolved(
        {"conclusion_id": "widget-c1", "tags": [], "citations": _WIDGET_CITE}
    )
    assert parsed.tags == ()
    assert parsed.citations[0].kind == "run"
    assert parsed.content_sha  # a citations sha was computed


def test_conclusion_resolved_refuses_bad_concludes() -> None:
    with pytest.raises(errors.SpecInvalid, match="scope_id"):
        evidence.validate_conclusion_resolved(
            {
                "conclusion_id": "widget-c1",
                "concludes": [{"scope_kind": "campaign"}],
                "citations": _WIDGET_CITE,
            }
        )


# --- the canonical citations-sha helper --------------------------------------


def test_citations_content_sha_matches_canonical() -> None:
    cits = [evidence.validate_citation(c) for c in _WIDGET_CITE]
    expected = determinism.canonical_sha([c.to_dict() for c in cits])
    assert evidence.citations_content_sha(cits) == expected
    # order/whitespace-independent: dicts and Citation objects agree
    assert evidence.citations_content_sha(_WIDGET_CITE) == expected


# --- the reduction: current | superseded | revoked | absent ------------------


def test_reduce_conclusion_absent() -> None:
    status = evidence.reduce_conclusion([], conclusion_id="widget-c1")
    assert status.status == evidence.ABSENT
    assert status.winner is None


def test_reduce_conclusion_current_and_superseded() -> None:
    r1 = _conclusion_record(
        "widget-c1", ts="2025-01-01T00:00:00Z", finding="first", citations=_WIDGET_CITE
    )
    r2 = _conclusion_record(
        "widget-c1",
        ts="2025-02-01T00:00:00Z",
        finding="second",
        citations=[_cite("run", "widget-run-2", "sha-widget-2")],
    )
    status = evidence.reduce_conclusion([r1, r2], conclusion_id="widget-c1")
    assert status.status == evidence.CURRENT
    assert status.winner["finding"] == "second"
    assert status.concluded_at == "2025-02-01T00:00:00Z"
    assert len(status.superseded) == 1
    assert status.superseded[0]["finding"] == "first"


def test_reduce_conclusion_revoked_wins() -> None:
    r1 = _conclusion_record(
        "widget-c1", ts="2025-01-01T00:00:00Z", finding="held", citations=_WIDGET_CITE
    )
    revoke = _conclusion_record("widget-c1", ts="2025-03-01T00:00:00Z", block="conclusion-revoke")
    status = evidence.reduce_conclusion([r1, revoke], conclusion_id="widget-c1")
    assert status.status == evidence.REVOKED


def test_reduce_conclusion_routes_through_kernel() -> None:
    # The winner verdict routes through state/attestation.py::reduce (the "one
    # kernel" enforcement row) — never a re-inlined newest-first / sha-compare.
    src = inspect.getsource(evidence.reduce_conclusion)
    assert "attestation.reduce" in src


# --- resolve_citation dispatch -----------------------------------------------


def test_resolve_dossier_without_resolver_raises() -> None:
    cit = evidence.Citation("dossier", "dossier/path", "bundle-sha")
    with pytest.raises(errors.SpecInvalid, match="injected"):
        evidence.resolve_citation(Path("."), cit)


def test_resolve_dossier_with_resolver(tmp_path: Path) -> None:
    cit = evidence.Citation("dossier", "dossier/path", "bundle-sha")
    res = evidence.resolve_citation(tmp_path, cit, dossier_resolver=lambda ref: "bundle-sha")
    assert res.resolved and res.matches
    res2 = evidence.resolve_citation(tmp_path, cit, dossier_resolver=lambda ref: "other-sha")
    assert res2.resolved and not res2.matches


def test_resolve_run_citation(tmp_path: Path) -> None:
    _write_sidecar(
        tmp_path, "widget-run-1", cmd_sha="cmd-widget", submitted_at="2025-01-01T00:00:00Z"
    )
    ok = evidence.resolve_citation(tmp_path, evidence.Citation("run", "widget-run-1", "cmd-widget"))
    assert ok.resolved and ok.matches
    miss = evidence.resolve_citation(tmp_path, evidence.Citation("run", "widget-run-1", "wrong"))
    assert miss.resolved and not miss.matches
    absent = evidence.resolve_citation(tmp_path, evidence.Citation("run", "nope", "x"))
    assert not absent.resolved


def test_resolve_attestation_citation(tmp_path: Path) -> None:
    # A named journal whose newest record carries content_sha == cited sha.
    p = tmp_path / ".hpc" / "runs" / "widget-run-1.decisions.jsonl"
    _append_jsonl(p, {"ts": "t1", "block": "receipt", "resolved": {"content_sha": "att-sha"}})
    cit = evidence.Citation("attestation", "run:widget-run-1", "att-sha")
    res = evidence.resolve_citation(tmp_path, cit)
    assert res.resolved and res.matches
    bad = evidence.resolve_citation(
        tmp_path, evidence.Citation("attestation", "run:widget-run-1", "nope")
    )
    assert bad.resolved and not bad.matches


# --- collect_evidence: the non-creating pin ----------------------------------


def test_collect_is_non_creating(tmp_path: Path) -> None:
    result = evidence.collect_evidence(tmp_path, tags=["widget"])
    assert result.conclusions == ()
    assert result.activity == ()
    # Not one directory was created under the namespace.
    assert not (tmp_path / ".hpc").exists()
    assert not (tmp_path / "_aggregated").exists()


def test_collect_refuses_bad_query_tag(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        evidence.collect_evidence(tmp_path, tags=["bad tag"])


# --- collect_evidence: conclusion surfaced by tag + as_of exclusion ----------


def test_collect_conclusion_by_tag_and_as_of(tmp_path: Path) -> None:
    rec = _conclusion_record(
        "widget-c1",
        ts="2025-06-01T00:00:00Z",
        tags=["widget"],
        finding="no widget alpha",
        citations=_WIDGET_CITE,
    )
    _write_conclusion(tmp_path, "widget-c1", rec)

    got = evidence.collect_evidence(tmp_path, tags=["widget"])
    assert len(got.conclusions) == 1
    assert got.conclusions[0].conclusion_id == "widget-c1"
    assert got.conclusions[0].finding == "no widget alpha"

    # as_of BEFORE the record → excluded everywhere.
    older = evidence.collect_evidence(tmp_path, tags=["widget"], as_of="2025-01-01T00:00:00Z")
    assert older.conclusions == ()

    # A different tag does not surface it.
    other = evidence.collect_evidence(tmp_path, tags=["gadget"])
    assert other.conclusions == ()


def test_collect_as_of_picks_older_winner(tmp_path: Path) -> None:
    _write_conclusion(
        tmp_path,
        "widget-c1",
        _conclusion_record(
            "widget-c1",
            ts="2025-01-01T00:00:00Z",
            tags=["widget"],
            finding="v1",
            citations=_WIDGET_CITE,
        ),
    )
    _write_conclusion(
        tmp_path,
        "widget-c1",
        _conclusion_record(
            "widget-c1",
            ts="2025-09-01T00:00:00Z",
            tags=["widget"],
            finding="v2",
            citations=_WIDGET_CITE,
        ),
    )
    # as_of between the two → the newer record is invisible, v1 wins.
    got = evidence.collect_evidence(tmp_path, tags=["widget"], as_of="2025-06-01T00:00:00Z")
    assert len(got.conclusions) == 1
    assert got.conclusions[0].finding == "v1"
    assert got.conclusions[0].superseded_count == 0


# --- retro-indexing: a conclusion's tags surface untagged lineage work -------


def test_collect_retro_indexes_untagged_run(tmp_path: Path) -> None:
    # An UNtagged run + a conclusion that carries tag 'widget' and concludes it.
    _write_sidecar(
        tmp_path, "widget-run-9", cmd_sha="cmd-9", submitted_at="2025-05-01T00:00:00Z", scopes=[]
    )
    _write_conclusion(
        tmp_path,
        "widget-c2",
        _conclusion_record(
            "widget-c2",
            ts="2025-06-01T00:00:00Z",
            tags=["widget"],
            concludes=[{"scope_kind": "run", "scope_id": "widget-run-9"}],
            finding="concluded on the untagged run",
            citations=_WIDGET_CITE,
        ),
    )
    got = evidence.collect_evidence(tmp_path, tags=["widget"])
    run_items = [a for a in got.activity if a.kind == "run"]
    assert [r.subject_id for r in run_items] == ["widget-run-9"]
    assert any(m.startswith("retro:") for m in run_items[0].matched_by)


# --- block-terminal co-tenant must not clobber the sidecar (#20) -------------


def test_collect_skips_block_terminal_cotenant(tmp_path: Path) -> None:
    from hpc_agent.state import block_terminal

    # A real sidecar declaring a scope tag + submitted_at ...
    _write_sidecar(
        tmp_path,
        "widget-run-t",
        cmd_sha="cmd-t",
        submitted_at="2025-05-01T00:00:00Z",
        scopes=["widget"],
    )
    # ... plus its detached-flow block-terminal co-tenant(s), written by the
    # real writer so the test pins the actual on-disk name/shape. The terminal
    # record carries a top-level run_id and its path sorts AFTER '<id>.json'.
    for block in ("submit-s2", "submit-s3"):
        block_terminal.record_terminal(
            tmp_path,
            run_id="widget-run-t",
            block=block,
            cmd_sha="cmd-t",
            result_dump={"run_id": "widget-run-t", "block": block},
        )

    # Tag-selected: the run still surfaces with its scope tag preserved.
    got = evidence.collect_evidence(tmp_path, tags=["widget"])
    run_items = [a for a in got.activity if a.kind == "run"]
    assert [r.subject_id for r in run_items] == ["widget-run-t"]
    detail = run_items[0].detail
    assert detail is not None and detail["tags"] == ["widget"]

    # Unkeyed walk: submitted_at (ts) survives — not clobbered to None.
    unkeyed = evidence.collect_evidence(tmp_path)
    run_items = [a for a in unkeyed.activity if a.kind == "run"]
    assert [r.subject_id for r in run_items] == ["widget-run-t"]
    assert run_items[0].ts == "2025-05-01T00:00:00Z"

    # as_of before submission excludes it (submitted_at was preserved, not lost).
    older = evidence.collect_evidence(tmp_path, as_of="2025-01-01T00:00:00Z")
    assert [a for a in older.activity if a.kind == "run"] == []


# --- _decision_journal_path routes every SCOPE_KIND (#21) --------------------


def test_decision_journal_path_matches_canonical_for_all_scope_kinds(tmp_path: Path) -> None:
    from hpc_agent.state import decision_journal

    for kind in decision_journal.SCOPE_KINDS:
        sid = "camp-1" if kind == "campaign" else f"{kind}-id"
        assert evidence._decision_journal_path(
            tmp_path, kind, sid
        ) == decision_journal.decisions_path(tmp_path, kind, sid), kind


@pytest.mark.parametrize("kind", ["registration", "pack", "challenge"])
def test_resolve_attestation_resolves_advertised_scope_kinds(tmp_path: Path, kind: str) -> None:
    # A genuine attestation record under each advertised scope kind's real
    # journal path must resolve — the campaign fallthrough made these UNRESOLVABLE.
    from hpc_agent.state import decision_journal

    subject = f"{kind}-x"
    sha = "c" * 64
    journal = decision_journal.decisions_path(tmp_path, kind, subject)
    _append_jsonl(
        journal,
        {
            "ts": "2025-05-01T00:00:00Z",
            "scope_kind": kind,
            "scope_id": subject,
            "block": kind,
            "response": "y",
            "resolved": {"content_sha": sha},
        },
    )
    res = evidence.resolve_citation(
        tmp_path, evidence.Citation(kind="attestation", ref=f"{kind}:{subject}", sha=sha)
    )
    assert res.resolved is True


# --- the unconcluded join ----------------------------------------------------


def test_collect_unconcluded_join(tmp_path: Path) -> None:
    # A terminal campaign with NO conclusion → appears in unconcluded.
    _append_jsonl(
        tmp_path / ".hpc" / "campaigns" / "widget-camp-1" / "decisions.jsonl",
        {"ts": "2025-04-01T00:00:00Z", "block": "complete", "resolved": {}},
    )
    got = evidence.collect_evidence(tmp_path)
    assert [u.subject_id for u in got.unconcluded] == ["widget-camp-1"]

    # Now a current conclusion NAMES it → it drops off the unconcluded list.
    _write_conclusion(
        tmp_path,
        "widget-c3",
        _conclusion_record(
            "widget-c3",
            ts="2025-05-01T00:00:00Z",
            concludes=[{"scope_kind": "campaign", "scope_id": "widget-camp-1"}],
            citations=_WIDGET_CITE,
        ),
    )
    got2 = evidence.collect_evidence(tmp_path)
    assert got2.unconcluded == ()


# --- envelope QUOTED VERBATIM from determinism reduction ---------------------


def _widget_ledger(exp: Path, cmd_sha: str) -> list[determinism.Sample]:
    payload_a = {"widget_error": 1.00}
    payload_b = {"widget_error": 1.02}
    identity = {"cmd_sha": cmd_sha, "tasks_py_sha": "tp", "executor": "widget.py"}
    diffs = determinism.diff_metrics(payload_a, payload_b)
    record = determinism.build_sample_record(
        ts="2025-05-02T00:00:00Z",
        content_sha=determinism.compute_content_sha(payload_a, payload_b),
        identity=identity,
        source="double-canary",
        run_ids=["widget-run-5", "widget-run-5b"],
        cluster="hoffman2",
        scale="canary",
        verdict="auto_cleared",
        per_key=diffs,
    )
    _append_jsonl(fingerprint_store.fingerprint_path(exp, cmd_sha), record)
    return [determinism.validate_sample(record)]


def test_collect_envelope_quoted_verbatim(tmp_path: Path) -> None:
    cmd_sha = "cmd-widget-env"
    _write_sidecar(
        tmp_path,
        "widget-run-5",
        cmd_sha=cmd_sha,
        submitted_at="2025-05-02T00:00:00Z",
        scopes=["widget"],
    )
    samples = _widget_ledger(tmp_path, cmd_sha)
    identity = dict(samples[-1].identity)
    expected = determinism.reduce_envelope(samples, [True], identity=identity)

    got = evidence.collect_evidence(tmp_path, tags=["widget"])
    assert len(got.envelopes) == len(expected.per_key)
    env_key = got.envelopes[0]
    exp_key = expected.per_key[env_key.key]
    # Byte-identical quoting of the ledger's own reduction — never a paraphrase.
    assert env_key.cls == exp_key.cls
    assert env_key.lo == exp_key.lo
    assert env_key.hi == exp_key.hi
    assert env_key.rel_spread == exp_key.rel_spread
    assert env_key.n == exp_key.evidence.n
    assert env_key.scales == exp_key.evidence.scales
    assert env_key.clusters == exp_key.evidence.clusters


# --- deterministic ordering: shuffle append order → identical projection -----


def test_collect_ordering_is_append_order_independent(tmp_path: Path) -> None:
    def build(exp: Path, order: list[str]) -> None:
        for run_id in order:
            _write_sidecar(
                exp,
                run_id,
                cmd_sha=f"cmd-{run_id}",
                submitted_at="2025-05-01T00:00:00Z",
                scopes=["widget"],
            )

    a = tmp_path / "a"
    b = tmp_path / "b"
    build(a, ["widget-run-1", "widget-run-2", "widget-run-3"])
    build(b, ["widget-run-3", "widget-run-1", "widget-run-2"])

    ra = evidence.collect_evidence(a, tags=["widget"])
    rb = evidence.collect_evidence(b, tags=["widget"])
    assert [x.subject_id for x in ra.activity] == [x.subject_id for x in rb.activity]


# --- dossier citation at READ discloses (never refuses) ----------------------


def test_collect_dossier_citation_discloses_without_resolver(tmp_path: Path) -> None:
    _write_conclusion(
        tmp_path,
        "widget-c4",
        _conclusion_record(
            "widget-c4",
            ts="2025-06-01T00:00:00Z",
            tags=["widget"],
            citations=[_cite("dossier", "dossier/widget", "bundle-sha")],
        ),
    )
    # No dossier_resolver injected → the READ path DISCLOSES, never raises.
    got = evidence.collect_evidence(tmp_path, tags=["widget"])
    assert len(got.citations_status) == 1
    st = got.citations_status[0]
    assert st.kind == "dossier"
    assert not st.resolved and not st.matches


def test_collect_dossier_citation_verified_with_resolver(tmp_path: Path) -> None:
    _write_conclusion(
        tmp_path,
        "widget-c5",
        _conclusion_record(
            "widget-c5",
            ts="2025-06-01T00:00:00Z",
            tags=["widget"],
            citations=[_cite("dossier", "dossier/widget", "bundle-sha")],
        ),
    )
    got = evidence.collect_evidence(
        tmp_path, tags=["widget"], dossier_resolver=lambda ref: "bundle-sha"
    )
    assert got.citations_status[0].matches


def test_reduce_attestation_kernel_is_the_one_kernel() -> None:
    # sanity: our reduction's CURRENT constant aligns with the kernel's verdict
    assert attestation.CURRENT == "current"


# --- C-disclose: the conclusion contested seam (route-through the ONE collector) ---


def test_conclusion_contested_route_through_pin() -> None:
    """C-disclose enforcement row: the seat calls ``standing_challenges``, no re-glob."""
    import inspect

    assert "standing_challenges(" in inspect.getsource(evidence._conclusion_contested)


def test_conclusion_uncontested_field_is_none(tmp_path: Path) -> None:
    """Byte-parity: a namespace with no challenge store leaves ``contested`` None."""
    _write_conclusion(
        tmp_path,
        "widget-c1",
        _conclusion_record(
            "widget-c1",
            ts="2026-07-08T00:00:00Z",
            tags=["widget"],
            finding="f",
            citations=_WIDGET_CITE,
        ),
    )
    got = evidence.collect_evidence(tmp_path, tags=["widget"])
    assert len(got.conclusions) == 1
    assert got.conclusions[0].contested is None
    assert not (tmp_path / ".hpc" / "challenges").exists()  # non-creating


def test_conclusion_contested_populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the conclusion's content_sha is challenged, the flag rides the point (open)."""
    from hpc_agent.state import challenges as _challenges

    _write_conclusion(
        tmp_path,
        "widget-c1",
        _conclusion_record(
            "widget-c1",
            ts="2026-07-08T00:00:00Z",
            tags=["widget"],
            finding="f",
            citations=_WIDGET_CITE,
        ),
    )

    def _fake(experiment_dir, *, content_sha=None, **kw):  # noqa: ANN001, ANN202
        block = _challenges.Contested(
            open=1,
            upheld=0,
            dismissed=0,
            withdrawn=0,
            superseded=0,
            challenge_ids=("widget-dissent-a",),
        )
        return _challenges.StandingChallenges(
            experiment_dir=str(experiment_dir), statuses=(), contested=block, skipped=()
        )

    monkeypatch.setattr(_challenges, "standing_challenges", _fake)
    got = evidence.collect_evidence(tmp_path, tags=["widget"])
    assert got.conclusions[0].contested is not None
    assert got.conclusions[0].contested.open == 1
    assert got.conclusions[0].contested.challenge_ids == ("widget-dissent-a",)
