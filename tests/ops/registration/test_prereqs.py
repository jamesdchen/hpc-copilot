"""T4 — the per-kind prerequisite-chain checkers (``ops/registration/prereqs.py``).

Toy-domain fixtures ONLY (the widget lineage — never harxhar/quant vocabulary,
the boundary-drift rule). Every checker is exercised for current / stale / absent
plus the structural refusals (not-yet-available kinds, unknown ``requires`` keys),
and the composer's pure-dispatch route-throughs are pinned by ``inspect.getsource``
(the ``test_layers_share_one_drift_predicate`` precedent).
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.registration import prereqs
from hpc_agent.ops.verify_reproduction import _receipt_path
from hpc_agent.state import scopes
from hpc_agent.state.audit_source import sha256_normalized
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.registration import ChainEntry
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

# ── toy notebook-audit source + template ─────────────────────────────────────

_AUDIT = "widget-audit"

_TEMPLATE = """\
# %%
# hpc-audit-section: widget-load
pass

# %%
# hpc-audit-section: widget-jam
pass
"""

_SOURCE = """\
# %%
# hpc-audit-section: widget-load
crate = load_crate("widgets.csv")

# %%
# hpc-audit-section: widget-jam
jam = compute_jam(crate)
"""

_SOURCE_EDITED = """\
# %%
# hpc-audit-section: widget-load
crate = load_crate("widgets.csv")

# %%
# hpc-audit-section: widget-jam
jam = compute_jam(crate, tighten=True)
"""


def _section_sha(source: str, slug: str) -> str:
    from hpc_agent.state.audit_source import parse_percent_source

    parsed = parse_percent_source(source)
    return next(s.section_sha for s in parsed.sections if s.slug == slug)


def _setup_notebook_audit(tmp_path: Path, *, source: str, sign_at: str, sign: bool = True) -> None:
    """Lay down interview.json + source/template .py and sign both sections.

    *sign_at* is the source text whose section shas are signed (pass a DIFFERENT
    text than *source* to simulate a signed-then-edited section).
    """
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": _AUDIT,
                }
            }
        ),
        encoding="utf-8",
    )
    if sign:
        for slug in ("widget-load", "widget-jam"):
            append_decision(
                tmp_path,
                scope_kind="notebook",
                scope_id=_AUDIT,
                block="notebook-sign-off",
                response="y",
                resolved={
                    "audit_id": _AUDIT,
                    "section": slug,
                    "section_sha": _section_sha(sign_at, slug),
                    "view_sha": "widget-view",
                },
            )


def _nb_entry(content_sha: str) -> ChainEntry:
    return ChainEntry(
        slot="audit-slot", kind="notebook-audit", subject_id=_AUDIT, content_sha=content_sha
    )


def test_notebook_audit_current(tmp_path: Path) -> None:
    _setup_notebook_audit(tmp_path, source=_SOURCE, sign_at=_SOURCE)
    module_sha = sha256_normalized(_SOURCE)
    [v] = prereqs.check_chain(tmp_path, [_nb_entry(module_sha)])
    assert v.status == "current"
    assert v.recorded_sha == module_sha == v.recomputed_sha
    assert v.slot == "audit-slot" and v.kind == "notebook-audit"


def test_notebook_audit_stale_on_sha_drift(tmp_path: Path) -> None:
    _setup_notebook_audit(tmp_path, source=_SOURCE, sign_at=_SOURCE)
    [v] = prereqs.check_chain(tmp_path, [_nb_entry("deadbeef" * 8)])
    assert v.status == "stale"
    assert v.recorded_sha == "deadbeef" * 8
    assert v.recomputed_sha == sha256_normalized(_SOURCE)  # the pair is carried


def test_notebook_audit_stale_on_unsigned_section(tmp_path: Path) -> None:
    # Signed at the OLD source, but the on-disk source moved → sections read stale.
    _setup_notebook_audit(tmp_path, source=_SOURCE_EDITED, sign_at=_SOURCE)
    [v] = prereqs.check_chain(tmp_path, [_nb_entry(sha256_normalized(_SOURCE_EDITED))])
    assert v.status == "stale"
    assert "widget-jam" in v.evidence_note


def test_notebook_audit_absent_when_not_opted_in(tmp_path: Path) -> None:
    [v] = prereqs.check_chain(tmp_path, [_nb_entry("abc123")])
    assert v.status == "absent"
    assert v.recomputed_sha is None


# ── reproduction ─────────────────────────────────────────────────────────────

_REPRO = "widget-repro-run"
_ORIG = "widget-orig-run"


_CMD_SHA = "widget-cmd"


def _write_receipt(
    tmp_path: Path,
    *,
    tasks_py_sha: str,
    original_run_id: str = _ORIG,
    cmd_sha: str = _CMD_SHA,
) -> dict:
    receipt = {
        "ts": "2026-01-01T00:00:00Z",
        "overall": "match",
        "original": {"run_id": original_run_id, "cmd_sha": cmd_sha, "tasks_py_sha": "orig-code"},
        "repro": {"run_id": _REPRO, "cmd_sha": cmd_sha, "tasks_py_sha": tasks_py_sha},
    }
    path = _receipt_path(tmp_path, _REPRO)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    return receipt


def _write_fingerprint_sample(
    tmp_path: Path,
    *,
    cmd_sha: str = _CMD_SHA,
    tasks_py_sha: str = "widget-code",
    executor: str = "run.py",
    scale: str = "main",
    cluster: str = "widget-cluster",
    key: str = "widget.jam",
    a: float = 1.0,
    b: float = 1.0,
) -> None:
    """Append ONE admitted (``auto_cleared``, admitted-by-construction) sample.

    Written straight to the ledger path (bypassing ``append_sample``'s bind, which
    would need on-disk canary artifacts) — the floor checker only tolerant-reads
    and identity-filters, so a valid sample dict suffices. Identity matches the
    receipt's ``repro`` block + the sidecar's executor.
    """
    from hpc_agent.infra.io import append_jsonl_line
    from hpc_agent.state import determinism
    from hpc_agent.state.fingerprint_store import fingerprint_path

    diff = determinism.PerKeyDiff(
        key=key,
        a=a,
        b=b,
        abs_diff=abs(a - b),
        rel_diff=(abs(a - b) / max(abs(a), abs(b)) if max(abs(a), abs(b)) else 0.0),
        static_class="float",
    )
    record = determinism.build_sample_record(
        ts="2026-01-01T00:00:00Z",
        content_sha="deadbeef" * 8,
        identity={"cmd_sha": cmd_sha, "tasks_py_sha": tasks_py_sha, "executor": executor},
        source="verify-reproduction",
        run_ids=[f"{cmd_sha}-orig", f"{cmd_sha}-repro"],
        cluster=cluster,
        scale=scale,
        verdict="auto_cleared",
        per_key=[diff],
    )
    append_jsonl_line(fingerprint_path(tmp_path, cmd_sha), record)


def _write_repro_sidecar(tmp_path: Path, *, tasks_py_sha: str) -> None:
    write_run_sidecar(
        tmp_path,
        run_id=_REPRO,
        cmd_sha="widget-cmd",
        hpc_agent_version="0.0.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="run.py",
        result_dir_template="results/{i}",
        task_count=1,
        tasks_py_sha=tasks_py_sha,
    )


def _repro_entry(content_sha: str, requires: dict | None = None) -> ChainEntry:
    return ChainEntry(
        slot="repro-slot",
        kind="reproduction",
        subject_id=_REPRO,
        content_sha=content_sha,
        requires=requires or {},
    )


def test_reproduction_current(tmp_path: Path) -> None:
    receipt = _write_receipt(tmp_path, tasks_py_sha="widget-code")
    _write_repro_sidecar(tmp_path, tasks_py_sha="widget-code")
    sha = prereqs.canonical_sha(receipt)
    [v] = prereqs.check_chain(tmp_path, [_repro_entry(sha)], dossier_run_ids={_ORIG})
    assert v.status == "current"
    assert v.recomputed_sha == sha


def test_reproduction_stale_on_code_drift(tmp_path: Path) -> None:
    receipt = _write_receipt(tmp_path, tasks_py_sha="widget-code")
    _write_repro_sidecar(tmp_path, tasks_py_sha="widget-code-v2")  # tree moved
    sha = prereqs.canonical_sha(receipt)
    [v] = prereqs.check_chain(tmp_path, [_repro_entry(sha)], dossier_run_ids={_ORIG})
    assert v.status == "stale"
    assert "code drifted" in v.evidence_note


def test_reproduction_dossier_cross_link_refusal(tmp_path: Path) -> None:
    receipt = _write_receipt(tmp_path, tasks_py_sha="widget-code")
    _write_repro_sidecar(tmp_path, tasks_py_sha="widget-code")
    sha = prereqs.canonical_sha(receipt)
    # The dossier names a DIFFERENT run — the receipt's original is not in it.
    [v] = prereqs.check_chain(tmp_path, [_repro_entry(sha)], dossier_run_ids={"some-other-run"})
    assert v.status == "stale"
    assert "not in the dossier" in v.evidence_note


def test_reproduction_absent_without_receipt(tmp_path: Path) -> None:
    [v] = prereqs.check_chain(tmp_path, [_repro_entry("abc")])
    assert v.status == "absent"
    assert v.recomputed_sha is None


def test_reproduction_requires_floor_met(tmp_path: Path) -> None:
    # The R4 seam WIRED: a base-current receipt + a ledger meeting the floor → current.
    receipt = _write_receipt(tmp_path, tasks_py_sha="widget-code")
    _write_repro_sidecar(tmp_path, tasks_py_sha="widget-code")
    for _ in range(3):
        _write_fingerprint_sample(tmp_path, scale="main", cluster="widget-cluster")
    sha = prereqs.canonical_sha(receipt)
    demand = {"min_n": 3, "scales": ["main"], "clusters": ["widget-cluster"]}
    [v] = prereqs.check_chain(
        tmp_path, [_repro_entry(sha, requires=demand)], dossier_run_ids={_ORIG}
    )
    assert v.status == "current"
    assert "fingerprint floor" in v.evidence_note and "met" in v.evidence_note


def test_reproduction_requires_floor_unmet_n(tmp_path: Path) -> None:
    # Base current, but only 1 admitted sample against a min_n of 3 → stale, demand named.
    receipt = _write_receipt(tmp_path, tasks_py_sha="widget-code")
    _write_repro_sidecar(tmp_path, tasks_py_sha="widget-code")
    _write_fingerprint_sample(tmp_path, scale="main")
    sha = prereqs.canonical_sha(receipt)
    [v] = prereqs.check_chain(
        tmp_path, [_repro_entry(sha, requires={"min_n": 3})], dossier_run_ids={_ORIG}
    )
    assert v.status == "stale"
    assert "fingerprint evidence floor unmet" in v.evidence_note
    assert "min_n" in v.evidence_note


def test_reproduction_requires_floor_unmet_scale(tmp_path: Path) -> None:
    # Enough samples, but all canary-scale against a main-scale demand → stale.
    receipt = _write_receipt(tmp_path, tasks_py_sha="widget-code")
    _write_repro_sidecar(tmp_path, tasks_py_sha="widget-code")
    for _ in range(3):
        _write_fingerprint_sample(tmp_path, scale="canary")
    sha = prereqs.canonical_sha(receipt)
    [v] = prereqs.check_chain(
        tmp_path,
        [_repro_entry(sha, requires={"min_n": 3, "scales": ["main"]})],
        dossier_run_ids={_ORIG},
    )
    assert v.status == "stale"
    assert "fingerprint evidence floor unmet" in v.evidence_note
    assert "scales" in v.evidence_note


def test_reproduction_requires_floor_missing_ledger_is_shortfall(tmp_path: Path) -> None:
    # No ledger at all → an ordinary n=0 shortfall, NOT a fabricated pass / crash.
    receipt = _write_receipt(tmp_path, tasks_py_sha="widget-code")
    _write_repro_sidecar(tmp_path, tasks_py_sha="widget-code")
    sha = prereqs.canonical_sha(receipt)
    [v] = prereqs.check_chain(
        tmp_path, [_repro_entry(sha, requires={"min_n": 1})], dossier_run_ids={_ORIG}
    )
    assert v.status == "stale"
    assert "fingerprint evidence floor unmet" in v.evidence_note


def test_reproduction_unknown_requires_key_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="unknown 'requires' key"):
        prereqs.check_chain(tmp_path, [_repro_entry("abc", requires={"bogus_key": 1})])


# ── scope-budget ─────────────────────────────────────────────────────────────

_SCOPE = "widget-holdout"


def _budget_entry(content_sha: str, max_looks: int) -> ChainEntry:
    return ChainEntry(
        slot="budget-slot",
        kind="scope-budget",
        subject_id=_SCOPE,
        content_sha=content_sha,
        requires={"max_looks": max_looks},
    )


def _budget_sha(tmp_path: Path) -> str:
    counts = scopes.count_prior_looks(tmp_path, _SCOPE)
    locked = scopes.is_scope_locked(tmp_path, _SCOPE)
    sha: str = prereqs.canonical_sha(
        {
            "prior_looks": counts["prior_looks"],
            "distinct_lineages": counts["distinct_lineages"],
            "locked": locked,
        }
    )
    return sha


def test_scope_budget_under_budget_current(tmp_path: Path) -> None:
    scopes.record_look(
        tmp_path,
        _SCOPE,
        run_id="widget-run-1",
        cmd_sha="c1",
        lineage_root="widget-run-1",
        reducer_block="reduce",
    )
    [v] = prereqs.check_chain(tmp_path, [_budget_entry(_budget_sha(tmp_path), max_looks=3)])
    assert v.status == "current"


def test_scope_budget_over_budget_stale(tmp_path: Path) -> None:
    for i in range(3):
        scopes.record_look(
            tmp_path,
            _SCOPE,
            run_id=f"widget-run-{i}",
            cmd_sha=f"c{i}",
            lineage_root=f"widget-run-{i}",
            reducer_block="reduce",
        )
    [v] = prereqs.check_chain(tmp_path, [_budget_entry(_budget_sha(tmp_path), max_looks=1)])
    assert v.status == "stale"
    assert "exceed budget" in v.evidence_note


def test_scope_budget_locked_stale(tmp_path: Path) -> None:
    scopes.record_lock(tmp_path, _SCOPE, reason="widget freeze")
    [v] = prereqs.check_chain(tmp_path, [_budget_entry(_budget_sha(tmp_path), max_looks=5)])
    assert v.status == "stale"
    assert "locked" in v.evidence_note


def test_scope_budget_missing_max_looks_is_spec_invalid(tmp_path: Path) -> None:
    bad = ChainEntry(
        slot="budget-slot", kind="scope-budget", subject_id=_SCOPE, content_sha="x", requires={}
    )
    with pytest.raises(errors.SpecInvalid, match="max_looks"):
        prereqs.check_chain(tmp_path, [bad])


# ── pack-receipt ─────────────────────────────────────────────────────────────

_PACK = "widget-pack"
_PACK_SLOT = "widget-slot"


def _pack_journal(tmp_path: Path, pack: str = _PACK) -> Path:
    from hpc_agent._kernel.contract.layout import RepoLayout

    path = RepoLayout(tmp_path).hpc / "packs" / f"{pack}.decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _seed_pack_receipt(
    tmp_path: Path,
    *,
    pack: str = _PACK,
    slot: str = _PACK_SLOT,
    passed: bool = True,
) -> str:
    """Journal a current pack-bind + a receipt for *slot*; return the composite sha.

    Uses the module's OWN one-definition :func:`receipt_content_sha` so the fixture
    computes the exact form :func:`slot_status` recomputes on read.
    """
    import hashlib

    from hpc_agent.state.pack_receipts import (
        PACK_BIND_BLOCK,
        PACK_RECEIPT_BLOCK,
        receipt_content_sha,
    )

    manifest_sha = hashlib.sha256(f"{pack}-manifest".encode()).hexdigest()
    rel = "data/widget.csv"
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"widget,rows\n1,2\n")
    on_disk = {rel: hashlib.sha256(p.read_bytes()).hexdigest()}
    content_sha = receipt_content_sha(manifest_sha, on_disk)
    path = _pack_journal(tmp_path, pack)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "block": PACK_BIND_BLOCK,
                    "resolved": {
                        "pack": pack,
                        "version": "1.0.0",
                        "manifest_sha": manifest_sha,
                        "files": [{"path": "vocab.json", "sha256": manifest_sha}],
                        "seams": ["reader_calls"],
                    },
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "block": PACK_RECEIPT_BLOCK,
                    "resolved": {
                        "pack": pack,
                        "version": "1.0.0",
                        "manifest_sha": manifest_sha,
                        "slot": slot,
                        "checked": [rel],
                        "passed": passed,
                        "content_sha": content_sha,
                        "attestor": "code",
                    },
                }
            )
            + "\n"
        )
    return content_sha


def test_pack_receipt_current_passed(tmp_path: Path) -> None:
    content_sha = _seed_pack_receipt(tmp_path, passed=True)
    entry = ChainEntry(
        slot="pack-slot",
        kind="pack-receipt",
        subject_id=f"{_PACK}:{_PACK_SLOT}",
        content_sha=content_sha,
    )
    (verdict,) = prereqs.check_chain(tmp_path, [entry])
    assert verdict.status == prereqs.CURRENT
    assert verdict.recomputed_sha == content_sha


def test_pack_receipt_failed_check_is_stale(tmp_path: Path) -> None:
    content_sha = _seed_pack_receipt(tmp_path, passed=False)
    entry = ChainEntry(
        slot="pack-slot",
        kind="pack-receipt",
        subject_id=f"{_PACK}:{_PACK_SLOT}",
        content_sha=content_sha,
    )
    (verdict,) = prereqs.check_chain(tmp_path, [entry])
    assert verdict.status == prereqs.STALE
    assert "failed" in verdict.evidence_note


def test_pack_receipt_moved_sha_is_stale(tmp_path: Path) -> None:
    _seed_pack_receipt(tmp_path, passed=True)
    entry = ChainEntry(
        slot="pack-slot",
        kind="pack-receipt",
        subject_id=f"{_PACK}:{_PACK_SLOT}",
        content_sha="a-sha-the-registrant-reviewed-that-no-longer-holds",
    )
    (verdict,) = prereqs.check_chain(tmp_path, [entry])
    assert verdict.status == prereqs.STALE
    assert "moved" in verdict.evidence_note


def test_pack_receipt_missing_is_absent(tmp_path: Path) -> None:
    entry = ChainEntry(
        slot="pack-slot", kind="pack-receipt", subject_id=f"{_PACK}:{_PACK_SLOT}", content_sha="x"
    )
    (verdict,) = prereqs.check_chain(tmp_path, [entry])
    assert verdict.status == prereqs.ABSENT
    assert verdict.recomputed_sha is None


def test_pack_receipt_malformed_address_is_loud(tmp_path: Path) -> None:
    entry = ChainEntry(
        slot="pack-slot", kind="pack-receipt", subject_id="no-colon-here", content_sha="x"
    )
    with pytest.raises(errors.SpecInvalid, match="<pack>:<slot>"):
        prereqs.check_chain(tmp_path, [entry])


# ── attestation (generic escape hatch) ───────────────────────────────────────


def _attest(tmp_path: Path, *, scope_kind: str, scope_id: str, content_sha: str) -> None:
    append_decision(
        tmp_path,
        scope_kind=scope_kind,
        scope_id=scope_id,
        block="widget-blessing",
        response="y",
        resolved={"attestor": "human", "content_sha": content_sha},
    )


def _attest_entry(content_sha: str, subject_id: str = "scope:widget-lock") -> ChainEntry:
    return ChainEntry(
        slot="attest-slot", kind="attestation", subject_id=subject_id, content_sha=content_sha
    )


def test_attestation_current_echoes_block_and_attestor(tmp_path: Path) -> None:
    _attest(tmp_path, scope_kind="scope", scope_id="widget-lock", content_sha="sha-1")
    [v] = prereqs.check_chain(tmp_path, [_attest_entry("sha-1")])
    assert v.status == "current"
    assert v.recomputed_sha == "sha-1"
    assert "widget-blessing" in v.evidence_note and "human" in v.evidence_note


def test_attestation_stale_when_newer_sha(tmp_path: Path) -> None:
    _attest(tmp_path, scope_kind="scope", scope_id="widget-lock", content_sha="sha-2")
    [v] = prereqs.check_chain(tmp_path, [_attest_entry("sha-1")])
    assert v.status == "stale"
    assert v.recomputed_sha == "sha-2"


def test_attestation_absent_on_empty_journal(tmp_path: Path) -> None:
    [v] = prereqs.check_chain(tmp_path, [_attest_entry("sha-1")])
    assert v.status == "absent"
    assert v.recomputed_sha is None


def test_attestation_bad_address_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="scope_kind.*scope_id"):
        prereqs.check_chain(tmp_path, [_attest_entry("sha-1", subject_id="no-colon-here")])


# ── unknown kind ─────────────────────────────────────────────────────────────


def test_unknown_kind_is_spec_invalid(tmp_path: Path) -> None:
    bad = ChainEntry(slot="x", kind="not-a-kind", subject_id="s", content_sha="c")
    with pytest.raises(errors.SpecInvalid, match="not a checkable"):
        prereqs.check_chain(tmp_path, [bad])


# ── route-through pins (the composer never re-inlines currency logic) ─────────


def test_notebook_audit_checker_routes_through_audit_module_and_gate() -> None:
    src = inspect.getsource(prereqs._check_notebook_audit)
    assert "audit_module(" in src
    assert "_linked_source_drift(" in src
    assert "sha256_normalized(" in src


def test_reproduction_checker_routes_through_the_one_drift_predicate() -> None:
    src = inspect.getsource(prereqs._check_reproduction)
    assert "detect_code_drift(" in src
    assert "_receipt_path(" in inspect.getsource(prereqs._newest_receipt)


def test_scope_budget_checker_routes_through_scopes() -> None:
    src = inspect.getsource(prereqs._check_scope_budget)
    assert "count_prior_looks(" in src
    assert "is_scope_locked(" in src


def test_attestation_checker_routes_through_the_kernel_reduce() -> None:
    src = inspect.getsource(prereqs._check_attestation)
    assert "attestation.reduce(" in src


def test_check_chain_is_pure_dispatch() -> None:
    """The composer never re-implements any member's currency logic — it dispatches."""
    src = inspect.getsource(prereqs.check_chain)
    for forbidden in (
        "sha256_normalized",
        "detect_code_drift",
        "count_prior_looks",
        "is_scope_locked",
        "attestation.reduce",
        "_receipt_path",
    ):
        assert forbidden not in src, f"check_chain must not re-inline {forbidden}"


# ── C-registration: the cross-kind ``uncontested`` demand (T8) ────────────────


def _write_challenge_against(tmp_path: Path, cid: str, content_sha: str) -> None:
    """A well-formed ``challenge`` filing whose target binds *content_sha* (open)."""
    rec = {
        "ts": "2026-07-08T00:00:00Z",
        "block": "challenge",
        "resolved": {
            "challenge_id": cid,
            "target": {
                "kind": "run",
                "subject_kind": "scope",
                "subject_id": "widget-lock",
                "content_sha": content_sha,
                "scope": {"scope_kind": "scope", "scope_id": "widget-lock"},
            },
            "citations": [{"kind": "run", "ref": "widget-run-1", "sha": "d" * 64}],
            "grounds": "the widget lock rests on refuted evidence",
        },
    }
    p = tmp_path / ".hpc" / "challenges" / f"{cid}.decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _uncontested_entry(content_sha: str) -> ChainEntry:
    return ChainEntry(
        slot="attest-slot",
        kind="attestation",
        subject_id="scope:widget-lock",
        content_sha=content_sha,
        requires={"uncontested": True},
    )


def test_uncontested_demand_unmet_downgrades_to_stale(tmp_path: Path) -> None:
    """A contested prerequisite under an ``uncontested`` demand fails, naming the ids."""
    _attest(tmp_path, scope_kind="scope", scope_id="widget-lock", content_sha="sha-1")
    _write_challenge_against(tmp_path, "widget-dissent-x", "sha-1")
    [v] = prereqs.check_chain(tmp_path, [_uncontested_entry("sha-1")])
    assert v.status == "stale"  # DISCLOSED-and-counted, the caller-declared gate
    assert "widget-dissent-x" in v.evidence_note
    assert "uncontested demand UNMET" in v.evidence_note


def test_same_chain_without_demand_passes_byte_identically(tmp_path: Path) -> None:
    """WITHOUT the demand, contest presence never reshapes the verdict (T-NB)."""
    _attest(tmp_path, scope_kind="scope", scope_id="widget-lock", content_sha="sha-1")
    _write_challenge_against(tmp_path, "widget-dissent-x", "sha-1")
    [contested] = prereqs.check_chain(tmp_path, [_attest_entry("sha-1")])
    assert contested.status == "current"  # ten challenges greenlight like none

    # Byte-identical to an UNCHALLENGED namespace under the same (demand-free) entry.
    other = tmp_path / "clean"
    other.mkdir()
    _attest(other, scope_kind="scope", scope_id="widget-lock", content_sha="sha-1")
    [clean] = prereqs.check_chain(other, [_attest_entry("sha-1")])
    assert (contested.status, contested.recomputed_sha, contested.evidence_note) == (
        clean.status,
        clean.recomputed_sha,
        clean.evidence_note,
    )


def test_uncontested_demand_met_passes(tmp_path: Path) -> None:
    """An ``uncontested`` demand with NO standing challenge passes (open-count 0)."""
    _attest(tmp_path, scope_kind="scope", scope_id="widget-lock", content_sha="sha-1")
    [v] = prereqs.check_chain(tmp_path, [_uncontested_entry("sha-1")])
    assert v.status == "current"


def test_uncontested_accepted_on_attestation_but_other_keys_refused(tmp_path: Path) -> None:
    """The R3 amendment: attestation accepts ``uncontested`` alone; other keys refuse."""
    from hpc_agent.state.registration import parse_chain_entry

    ok = parse_chain_entry(
        {
            "slot": "s",
            "kind": "attestation",
            "subject_id": "scope:widget-lock",
            "content_sha": "sha-1",
            "requires": {"uncontested": True},
        }
    )
    assert ok.requires == {"uncontested": True}
    with pytest.raises(errors.SpecInvalid):
        parse_chain_entry(
            {
                "slot": "s",
                "kind": "attestation",
                "subject_id": "scope:widget-lock",
                "content_sha": "sha-1",
                "requires": {"bogus": True},
            }
        )


def test_uncontested_route_through_pin() -> None:
    """C-registration enforcement row: the demand counts via standing_challenges."""
    assert "standing_challenges(" in inspect.getsource(prereqs._uncontested_open_count)
