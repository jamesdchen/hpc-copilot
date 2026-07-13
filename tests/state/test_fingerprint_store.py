"""T3 — the determinism-fingerprint ledger (``state/fingerprint_store.py``).

Toy-domain vocabulary only (widget metrics), never a real domain's words — the
domain-packs toy-fixture rule (real domain words in fixtures smuggle a
vocabulary into the tree).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.fingerprint_store import (
    REPRODUCTION_VERDICT_BLOCK,
    SUBJECT_KIND,
    LedgerEvidence,
    append_sample,
    compute_admitted_flags,
    content_sha_over_payloads,
    fingerprint_path,
    fingerprints_dir,
    load_evidence,
    partition_current_identity,
    pulls_dir,
    read_samples,
)

CMD_SHA = "widgetcmd0123456789abcdef"  # >16 chars: exercises the [:16] key
IDENTITY = {
    "cmd_sha": CMD_SHA,
    "tasks_py_sha": "tasks-sha-aaa",
    "executor": "widget_executor.py",
    "data_sha": "data-sha-111",
}

# Two toy widget-metrics payloads (byte-identical → exact double canary).
PAYLOAD_A = {"grid0": {"throughput": 100.0, "defects": 3}}
PAYLOAD_B = {"grid0": {"throughput": 100.0, "defects": 3}}


def _write_payloads(tmp_path: Path, a: dict, b: dict) -> tuple[Path, Path]:
    pa = tmp_path / "canary_a_metrics.json"
    pb = tmp_path / "canary_b_metrics.json"
    pa.write_text(json.dumps(a), encoding="utf-8")
    pb.write_text(json.dumps(b), encoding="utf-8")
    return pa, pb


def _sample(
    *,
    content_sha: str,
    source: str = "double-canary",
    scale: str = "canary",
    verdict: str = "auto_cleared",
    run_ids: list[str] | None = None,
    identity: dict | None = None,
    **extra: object,
) -> dict:
    ident = identity if identity is not None else dict(IDENTITY)
    rec: dict = {
        "attestor": "code",
        "subject_kind": SUBJECT_KIND,
        "subject_id": ident["cmd_sha"],
        "content_sha": content_sha,
        "identity": ident,
        "source": source,
        "run_ids": run_ids if run_ids is not None else ["orig-run", "repro-run"],
        "cluster": "widgetlab",
        "scale": scale,
        "verdict": verdict,
        "same_submission": source == "double-canary",
        "partial": False,
        "task_indices": None,
        "per_key": [],
    }
    rec.update(extra)
    return rec


# ── path derivation ──────────────────────────────────────────────────────────


def test_fingerprint_path_keys_on_cmd_sha_prefix(tmp_path: Path) -> None:
    p = fingerprint_path(tmp_path, CMD_SHA)
    assert p == fingerprints_dir(tmp_path) / f"{CMD_SHA[:16]}.jsonl"
    assert p.parent == tmp_path / "_aggregated" / "_fingerprints"


def test_fingerprint_path_refuses_empty_cmd_sha(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        fingerprint_path(tmp_path, "")


def test_pulls_dir_location_and_safety(tmp_path: Path) -> None:
    assert pulls_dir(tmp_path, "repro-run") == (
        tmp_path / "_aggregated" / "_fingerprints" / "_pulls" / "repro-run"
    )
    with pytest.raises(errors.SpecInvalid):
        pulls_dir(tmp_path, "../escape")


# ── append round-trip + bind lock ────────────────────────────────────────────


def test_append_round_trip_binds_to_on_disk_payloads(tmp_path: Path) -> None:
    pa, pb = _write_payloads(tmp_path, PAYLOAD_A, PAYLOAD_B)
    content_sha = content_sha_over_payloads(PAYLOAD_A, PAYLOAD_B)
    rec = _sample(content_sha=content_sha)

    written = append_sample(tmp_path, record=rec, artifact_a=pa, artifact_b=pb)
    assert written["content_sha"] == content_sha
    assert "ts" in written  # auto-stamped

    samples, skipped = read_samples(tmp_path, CMD_SHA)
    assert skipped == 0
    assert len(samples) == 1
    assert samples[0]["content_sha"] == content_sha
    assert samples[0]["verdict"] == "auto_cleared"


def test_append_refuses_doctored_content_sha(tmp_path: Path) -> None:
    """A content_sha that does not match the on-disk payloads → bind refusal."""
    pa, pb = _write_payloads(tmp_path, PAYLOAD_A, PAYLOAD_B)
    rec = _sample(content_sha="0" * 64)  # a sha the payloads do not carry
    with pytest.raises(errors.SpecInvalid):
        append_sample(tmp_path, record=rec, artifact_a=pa, artifact_b=pb)
    # Nothing was appended.
    assert not fingerprint_path(tmp_path, CMD_SHA).exists()


def test_append_refuses_partial_without_task_indices(tmp_path: Path) -> None:
    pa, pb = _write_payloads(tmp_path, PAYLOAD_A, PAYLOAD_B)
    content_sha = content_sha_over_payloads(PAYLOAD_A, PAYLOAD_B)
    rec = _sample(
        content_sha=content_sha,
        source="verify-reproduction",
        scale="main",
        partial=True,
        task_indices=None,  # no-silent-caps violation
    )
    with pytest.raises(errors.SpecInvalid):
        append_sample(tmp_path, record=rec, artifact_a=pa, artifact_b=pb)


def test_append_refuses_bad_verdict(tmp_path: Path) -> None:
    pa, pb = _write_payloads(tmp_path, PAYLOAD_A, PAYLOAD_B)
    content_sha = content_sha_over_payloads(PAYLOAD_A, PAYLOAD_B)
    rec = _sample(content_sha=content_sha, verdict="totally_fine")
    with pytest.raises(errors.SpecInvalid):
        append_sample(tmp_path, record=rec, artifact_a=pa, artifact_b=pb)


# ── tolerant read ────────────────────────────────────────────────────────────


def test_read_skips_and_counts_malformed_line(tmp_path: Path) -> None:
    pa, pb = _write_payloads(tmp_path, PAYLOAD_A, PAYLOAD_B)
    content_sha = content_sha_over_payloads(PAYLOAD_A, PAYLOAD_B)
    append_sample(tmp_path, record=_sample(content_sha=content_sha), artifact_a=pa, artifact_b=pb)

    # Corrupt the record store by hand-appending a torn line + a blank line.
    path = fingerprint_path(tmp_path, CMD_SHA)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write("\n")

    samples, skipped = read_samples(tmp_path, CMD_SHA)
    assert len(samples) == 1  # the good line survives
    assert skipped == 1  # one torn line disclosed; the blank is not counted


def test_read_missing_ledger_is_empty(tmp_path: Path) -> None:
    assert read_samples(tmp_path, CMD_SHA) == ([], 0)


# ── identity partition (current vs stale vs data-drift) ──────────────────────


def test_partition_current_vs_stale_vs_data_drift() -> None:
    current_sample = _sample(content_sha="a" * 64)
    code_drift = _sample(
        content_sha="b" * 64,
        identity={**IDENTITY, "tasks_py_sha": "tasks-sha-CHANGED"},
    )
    data_drift = _sample(
        content_sha="c" * 64,
        identity={**IDENTITY, "data_sha": "data-sha-OTHER"},
    )
    data_unknown_sample = _sample(
        content_sha="d" * 64,
        identity={k: v for k, v in IDENTITY.items() if k != "data_sha"},
    )

    current, stale, data_unknown = partition_current_identity(
        [current_sample, code_drift, data_drift, data_unknown_sample], IDENTITY
    )
    current_shas = {s["content_sha"] for s in current}
    stale_shas = {s["content_sha"] for s in stale}
    assert current_shas == {"a" * 64, "d" * 64}  # current + data-unknown retained
    assert stale_shas == {"b" * 64, "c" * 64}  # code drift + data drift excluded
    assert data_unknown == 1  # the absent-manifest sample disclosed


# ── the admission JOIN ───────────────────────────────────────────────────────


def test_auto_cleared_admitted_by_construction(tmp_path: Path) -> None:
    sample = _sample(content_sha="e" * 64, verdict="auto_cleared")
    flags, excluded = compute_admitted_flags(tmp_path, [sample])
    assert flags == [True]
    assert excluded == 0


def test_needs_verdict_unadmitted_without_record(tmp_path: Path) -> None:
    sample = _sample(
        content_sha="f" * 64,
        source="verify-reproduction",
        scale="main",
        verdict="needs_verdict",
    )
    flags, excluded = compute_admitted_flags(tmp_path, [sample])
    assert flags == [False]
    assert excluded == 1  # recorded-but-inadmissible, disclosed


def test_needs_verdict_admitted_by_token_exact_accept(tmp_path: Path) -> None:
    content_sha = "1234abcd" * 8  # 64 hex
    sample = _sample(
        content_sha=content_sha,
        source="verify-reproduction",
        scale="main",
        verdict="needs_verdict",
        run_ids=["orig-run", "repro-run-42"],
    )
    # A gated human acceptance on the REPRODUCTION run's journal, token-exact.
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="repro-run-42",
        block=REPRODUCTION_VERDICT_BLOCK,
        response="y",
        resolved={"accept": True, "content_sha": content_sha},
    )
    flags, excluded = compute_admitted_flags(tmp_path, [sample])
    assert flags == [True]
    assert excluded == 0


def test_accept_false_not_admitted(tmp_path: Path) -> None:
    content_sha = "abcd1234" * 8
    sample = _sample(
        content_sha=content_sha,
        source="verify-reproduction",
        scale="main",
        verdict="mismatch",
        run_ids=["orig-run", "repro-run-x"],
    )
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="repro-run-x",
        block=REPRODUCTION_VERDICT_BLOCK,
        response="n",
        resolved={"accept": False, "content_sha": content_sha},
    )
    flags, _ = compute_admitted_flags(tmp_path, [sample])
    assert flags == [False]


def test_prefix_only_naming_not_admitted(tmp_path: Path) -> None:
    """Token-exact join: a resolved naming only a PREFIX does not admit."""
    content_sha = "deadbeef" * 8
    sample = _sample(
        content_sha=content_sha,
        source="verify-reproduction",
        scale="main",
        verdict="needs_verdict",
        run_ids=["orig-run", "repro-run-p"],
    )
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="repro-run-p",
        block=REPRODUCTION_VERDICT_BLOCK,
        response="y",
        resolved={"accept": True, "content_sha": content_sha[:16]},  # prefix only
    )
    flags, _ = compute_admitted_flags(tmp_path, [sample])
    assert flags == [False]


def test_mismatch_admitted_by_human_accept(tmp_path: Path) -> None:
    """A mismatch judged an env-sensitivity FINDING is admitted by human accept."""
    content_sha = "cafe0000" * 8
    sample = _sample(
        content_sha=content_sha,
        source="verify-reproduction",
        scale="main",
        verdict="mismatch",
        run_ids=["orig-run", "repro-run-m"],
    )
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="repro-run-m",
        block=REPRODUCTION_VERDICT_BLOCK,
        response="y",
        resolved={"accept": True, "content_sha": content_sha},
    )
    flags, excluded = compute_admitted_flags(tmp_path, [sample])
    assert flags == [True]
    assert excluded == 0


# ── the one admission rule: unadmitted doesn't flip, accepted does ───────────


def test_unadmitted_sample_flag_stays_false_accepted_flips_true(tmp_path: Path) -> None:
    content_sha = "0f0f0f0f" * 8
    sample = _sample(
        content_sha=content_sha,
        source="verify-reproduction",
        scale="main",
        verdict="needs_verdict",
        run_ids=["orig-run", "repro-run-flip"],
    )
    # Before any acceptance: inadmissible.
    flags_before, _ = compute_admitted_flags(tmp_path, [sample])
    assert flags_before == [False]
    # After a token-exact human accept: admitted — the same sample flips.
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="repro-run-flip",
        block=REPRODUCTION_VERDICT_BLOCK,
        response="y",
        resolved={"accept": True, "content_sha": content_sha},
    )
    flags_after, _ = compute_admitted_flags(tmp_path, [sample])
    assert flags_after == [True]


# ── load_evidence: the full T1 seam ──────────────────────────────────────────


def test_load_evidence_produces_aligned_samples_and_flags(tmp_path: Path) -> None:
    # Sample 1: double-canary auto_cleared (admitted by construction).
    pa, pb = _write_payloads(tmp_path, PAYLOAD_A, PAYLOAD_B)
    sha1 = content_sha_over_payloads(PAYLOAD_A, PAYLOAD_B)
    append_sample(tmp_path, record=_sample(content_sha=sha1), artifact_a=pa, artifact_b=pb)

    # Sample 2: verify-reproduction needs_verdict, UNADMITTED (no record).
    other_a = {"grid0": {"throughput": 100.0, "defects": 3}}
    other_b = {"grid0": {"throughput": 100.4, "defects": 3}}  # float jitter
    pc = tmp_path / "repro_a.json"
    pd = tmp_path / "repro_b.json"
    pc.write_text(json.dumps(other_a), encoding="utf-8")
    pd.write_text(json.dumps(other_b), encoding="utf-8")
    sha2 = content_sha_over_payloads(other_a, other_b)
    append_sample(
        tmp_path,
        record=_sample(
            content_sha=sha2,
            source="verify-reproduction",
            scale="main",
            verdict="needs_verdict",
            run_ids=["orig-run", "repro-run-unadmitted"],
        ),
        artifact_a=pc,
        artifact_b=pd,
    )

    # Sample 3: STALE by code identity — retained as history, out of the envelope.
    pe = tmp_path / "stale_a.json"
    pf = tmp_path / "stale_b.json"
    pe.write_text(json.dumps(PAYLOAD_A), encoding="utf-8")
    pf.write_text(json.dumps(PAYLOAD_B), encoding="utf-8")
    sha3 = content_sha_over_payloads(PAYLOAD_A, PAYLOAD_B)
    append_sample(
        tmp_path,
        record=_sample(
            content_sha=sha3,
            identity={**IDENTITY, "tasks_py_sha": "DRIFTED"},
        ),
        artifact_a=pe,
        artifact_b=pf,
    )

    ev = load_evidence(tmp_path, cmd_sha=CMD_SHA, identity=IDENTITY)
    assert isinstance(ev, LedgerEvidence)
    assert len(ev.samples) == 2  # current only; the stale one excluded
    assert len(ev.admitted_flags) == len(ev.samples)  # aligned
    # sample1 admitted (auto_cleared), sample2 unadmitted (no accept record).
    admitted_by_sha = {
        s["content_sha"]: f for s, f in zip(ev.samples, ev.admitted_flags, strict=True)
    }
    assert admitted_by_sha[sha1] is True
    assert admitted_by_sha[sha2] is False
    assert ev.excluded_unadmitted == 1
    assert len(ev.stale) == 1
    assert ev.malformed_skipped == 0


# --- P-S1 one-definition pin (the re-point to determinism.compute_content_sha) --


def test_content_sha_over_payloads_routes_to_kernel_byte_for_byte() -> None:
    """P-S1: ``content_sha_over_payloads`` IS ``determinism.compute_content_sha``.

    The store's append bind-recompute and the pure kernel's reduction now share
    ONE canonicalization; this pins them byte-for-byte so a future edit to either
    surfaces as a failure here rather than a silent fingerprint drift.
    """
    from hpc_agent.state import determinism

    a = {"loss": 0.5, "acc": {"top1": 0.9}}
    b = {"loss": 0.5, "acc": {"top1": 0.91}}
    assert content_sha_over_payloads(a, b) == determinism.compute_content_sha(a, b)
    assert content_sha_over_payloads(a, b) == determinism.canonical_sha([a, b])
