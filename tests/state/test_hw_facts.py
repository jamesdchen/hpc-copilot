"""The hardware-placement leg — U-HW1 (reproducibility program gap #5).

Covers the placement-facts capture the #5 crisis gap needs:

1. **The pure reducer** — ``hw_sha`` is stable + whitespace/order-insensitive +
   refuses empty facts; ``normalize_facts`` drops empties + keeps the vocabulary;
   ``resolve_hw_facts`` degrades to could-not-capture (never a raise); partial
   facts are honest; ``hw_drift_disclosure`` is match / drifted / unknown with a
   named per-fact delta (disclose, never gate).
2. **The sidecar stamp** — ``stamp_run_sidecar_hw_facts`` is strictly additive
   (never overwrites a recorded sha/facts), records a could-not-capture status
   even with a null sha (no-silent-caps), and an OLD sidecar without the field
   reads not-captured (backfilled None).

Toy fixtures only — opaque node/cpu strings, never a real machine inventory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hpc_agent.state.hw_facts import (
    STATUS_CAPTURED,
    STATUS_COULD_NOT_CAPTURE,
    hw_drift_disclosure,
    hw_sha,
    normalize_facts,
    resolve_hw_facts,
)
from hpc_agent.state.runs import (
    read_run_sidecar,
    run_sidecar_path,
    stamp_run_sidecar_hw_facts,
    write_run_sidecar,
)

FACTS_A = {"node": "gpu-a-01", "cpu_model": "Widget Xeon Gold 6248", "partition": "gpu"}
# A move of ONE fact (the node) — a scheduler placed the repro on a different box.
FACTS_B = {"node": "gpu-a-99", "cpu_model": "Widget Xeon Gold 6248", "partition": "gpu"}


# --------------------------------------------------------------------------- #
# 1 — the pure reducer
# --------------------------------------------------------------------------- #
def test_hw_sha_is_stable_and_whitespace_insensitive() -> None:
    a = hw_sha({"node": "gpu-a-01", "cpu_model": "Widget Xeon Gold 6248"})
    # Reordered keys + padded / doubled whitespace normalize to the same sha.
    b = hw_sha({"cpu_model": "Widget   Xeon Gold 6248  ", "node": " gpu-a-01"})
    assert a == b and len(a) == 64
    # A changed node moves the sha.
    assert hw_sha(FACTS_B) != hw_sha(FACTS_A)


def test_hw_sha_refuses_empty_facts() -> None:
    with pytest.raises(ValueError, match="no placement facts"):
        hw_sha({"node": "  ", "cpu_model": ""})


def test_normalize_facts_keeps_vocabulary_drops_empty_and_unknown() -> None:
    norm = normalize_facts(
        {"node": " gpu-a-01 ", "cpu_model": "", "partition": "gpu", "bogus": "x"}
    )
    # Empty cpu_model dropped; unknown key dropped; node stripped.
    assert norm == {"node": "gpu-a-01", "partition": "gpu"}


def test_resolve_partial_facts_is_honest() -> None:
    # Only the node resolved (no cpuinfo on this node) — a partial fact set is
    # still captured, hashed over exactly what was present.
    snap = resolve_hw_facts({"node": "gpu-a-01", "cpu_model": "", "partition": ""})
    assert snap.resolved and snap.status == STATUS_CAPTURED
    assert snap.facts == {"node": "gpu-a-01"}
    assert snap.sha == hw_sha({"node": "gpu-a-01"})


def test_resolve_could_not_capture_when_all_empty() -> None:
    for raw in (None, {}, {"node": "  ", "cpu_model": "", "partition": None}):
        snap = resolve_hw_facts(raw)
        assert not snap.resolved
        assert snap.facts is None and snap.sha is None
        assert snap.status == STATUS_COULD_NOT_CAPTURE
        assert "could not be resolved" in snap.detail


def test_hw_drift_disclosure_match_drifted_unknown_with_delta() -> None:
    sha_a, sha_b = hw_sha(FACTS_A), hw_sha(FACTS_B)
    assert hw_drift_disclosure(sha_a, sha_a)["status"] == "match"
    drift = hw_drift_disclosure(sha_a, sha_b, recorded_facts=FACTS_A, current_facts=FACTS_B)
    assert drift["status"] == "drifted"
    assert drift["recorded"] == sha_a and drift["current"] == sha_b
    # The delta NAMES exactly the moved fact (the node), the attribution surface.
    assert drift["delta"] == ["node"]
    # Either side absent → unknown, disclosed, never a refusal; delta stays empty.
    for pair in ((None, sha_a), (sha_a, None), (None, None)):
        disc = hw_drift_disclosure(*pair)
        assert disc["status"] == "unknown" and disc["delta"] == []


def test_hw_drift_disclosure_delta_empty_without_both_facts() -> None:
    # Drifted shas but no facts supplied → the status still drifts, but the delta
    # cannot be named (never fabricated).
    disc = hw_drift_disclosure("a" * 64, "b" * 64)
    assert disc["status"] == "drifted" and disc["delta"] == []


# --------------------------------------------------------------------------- #
# 2 — the sidecar stamp (additive) + old-record backfill
# --------------------------------------------------------------------------- #
def _write_sidecar(exp: Path, run_id: str, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "cmd_sha": "a" * 64,
        "hpc_agent_version": "0.11.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python train.py",
        "result_dir_template": "results/{task_id}",
        "task_count": 1,
        "tasks_py_sha": "b" * 64,
        "cluster": "widgetcluster",
    }
    kwargs.update(over)
    write_run_sidecar(exp, **kwargs)


def test_old_sidecar_without_field_reads_not_captured(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")
    raw = json.loads(run_sidecar_path(tmp_path, "run-x").read_text(encoding="utf-8"))
    # Byte-identical to a pre-U-HW1 sidecar — the fields are not written.
    assert "hw_sha" not in raw and "hw_facts" not in raw and "hw_status" not in raw
    # read_run_sidecar backfills all to None → "hardware placement not captured".
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["hw_sha"] is None and sc["hw_facts"] is None and sc["hw_status"] is None


def test_stamp_records_facts_sha_and_status(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")
    sha = hw_sha(FACTS_A)
    stamp_run_sidecar_hw_facts(
        tmp_path, "run-x", hw_facts=FACTS_A, hw_sha=sha, hw_status=STATUS_CAPTURED
    )
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["hw_sha"] == sha and sc["hw_facts"] == FACTS_A
    assert sc["hw_status"] == STATUS_CAPTURED


def test_stamp_is_additive_never_overwrites(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")
    first = hw_sha(FACTS_A)
    stamp_run_sidecar_hw_facts(
        tmp_path, "run-x", hw_facts=FACTS_A, hw_sha=first, hw_status=STATUS_CAPTURED
    )
    # A later stamp with DIFFERENT facts must not rewrite recorded provenance.
    second = hw_sha(FACTS_B)
    stamp_run_sidecar_hw_facts(
        tmp_path, "run-x", hw_facts=FACTS_B, hw_sha=second, hw_status=STATUS_CAPTURED
    )
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["hw_sha"] == first and sc["hw_facts"] == FACTS_A


def test_stamp_could_not_capture_records_status_with_null_sha(tmp_path: Path) -> None:
    # No-silent-caps: an unresolvable hardware read records the status even with
    # no sha and no facts.
    _write_sidecar(tmp_path, "run-x")
    stamp_run_sidecar_hw_facts(
        tmp_path, "run-x", hw_facts=None, hw_sha=None, hw_status=STATUS_COULD_NOT_CAPTURE
    )
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["hw_sha"] is None and sc["hw_facts"] is None
    assert sc["hw_status"] == STATUS_COULD_NOT_CAPTURE
