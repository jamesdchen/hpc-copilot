"""The data-identity dimension — Phase-3 amendment (``docs/design/data-manifest.md``
"The fingerprint amendment", ruled 0b).

Covers the three seams the amendment adds on top of the fingerprint machinery:

1. **Submit echoes data identity** — ``state/data_manifest.py::data_identity`` +
   the sidecar's new ``data_manifest_sha`` field (present/absent + byte-identity).
2. **Samples comparable only within the same data identity** — verify-reproduction
   excludes a cross-data prior as data drift (disclosed ``excluded_data_drift``),
   discloses an unknown prior, mints a sample carrying the data leg, and NAMES the
   moved dimension in the reason; the wire ``SampleIdentity`` gains an additive
   ``data_sha`` (v1 records parse).
3. **``reproduce-run``'s drift guard grows to three dimensions** — data is a NAMED
   DISCLOSURE (match / drifted / unknown), never a refusal.

Toy fixtures only (widget metrics, ``data/f.txt`` text bytes) — never a parquet,
never a domain vocabulary (the domain-packs toy-fixture rule).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hpc_agent._wire.queries.determinism import DeterminismSampleRecord, SampleIdentity
from hpc_agent._wire.queries.verify_reproduction import (
    ReproductionReceipt,
    VerifyReproductionSpec,
)
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.reproduce_run import _data_drift_disclosure
from hpc_agent.ops.verify_reproduction import verify_reproduction
from hpc_agent.state.data_manifest import data_identity, mint_manifest
from hpc_agent.state.determinism import PerKeyDiff, build_sample_record
from hpc_agent.state.fingerprint_store import fingerprint_path
from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path, write_run_sidecar

ORIG = "orig-run"
REPRO = "repro-run"
CMD_SHA = "a" * 64
TASKS_PY_SHA = "b" * 64
EXECUTOR = "python train.py"
DATA_OLD = "0" * 64
DATA_NEW = "1" * 64


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write_sidecar(exp: Path, run_id: str, *, reproduces: str | None = None, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "cmd_sha": CMD_SHA,
        "hpc_agent_version": "0.11.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": EXECUTOR,
        "result_dir_template": "results/{task_id}",
        "task_count": 1,
        "tasks_py_sha": TASKS_PY_SHA,
        "cluster": "widgetcluster",
    }
    kwargs.update(over)
    write_run_sidecar(exp, reproduces=reproduces, **kwargs)


def _write_aggregate(exp: Path, run_id: str, metrics: dict[str, Any]) -> None:
    agg = exp / "_aggregated" / run_id
    agg.mkdir(parents=True, exist_ok=True)
    (agg / "metrics_aggregate.json").write_text(
        json.dumps({"run_id": run_id, "aggregated_metrics": {"gp": metrics}}), encoding="utf-8"
    )


def _plant_sample(exp: Path, *, widget: tuple[float, float], data_sha: str | None) -> None:
    """Append one prior fingerprint sample carrying an optional data-identity leg."""
    identity: dict[str, Any] = {
        "cmd_sha": CMD_SHA,
        "tasks_py_sha": TASKS_PY_SHA,
        "executor": EXECUTOR,
    }
    if data_sha is not None:
        identity["data_sha"] = data_sha
    a, b = widget
    denom = max(abs(a), abs(b))
    rel = abs(a - b) / denom if denom else 0.0
    per_key = [PerKeyDiff("gp.widget", a, b, abs(a - b), rel, "float")]
    record = build_sample_record(
        ts="2026-01-01T00:00:00Z",
        content_sha="d" * 64,
        identity=identity,
        source="verify-reproduction",
        run_ids=["o-prior", "r-prior"],
        cluster="widgetcluster",
        scale="main",
        verdict="auto_cleared",
        per_key=per_key,
    )
    append_jsonl_line(fingerprint_path(exp, CMD_SHA), record)


def _run(exp: Path):
    spec = VerifyReproductionSpec(original_run_id=ORIG, repro_run_id=REPRO)
    return verify_reproduction(exp, spec=spec)


def _setup_manifest(exp: Path, content: str) -> str:
    """Declare input roots + mint a manifest over ``data/f.txt`` → its data_identity."""
    (exp / ".hpc").mkdir(parents=True, exist_ok=True)
    (exp / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": ["data"]}}), encoding="utf-8"
    )
    (exp / "data").mkdir(parents=True, exist_ok=True)
    (exp / "data" / "f.txt").write_text(content, encoding="utf-8")
    mint_manifest(exp, ["data"])
    ident = data_identity(exp)
    assert ident is not None
    return ident


# --------------------------------------------------------------------------- #
# leg 0 — data_manifest.data_identity (the pinned single-sha shape)
# --------------------------------------------------------------------------- #
def test_data_identity_none_without_roots(tmp_path: Path) -> None:
    # A manifest can't even be minted with no declared roots; no interview.json.
    assert data_identity(tmp_path) is None


def test_data_identity_none_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": ["data"]}}), encoding="utf-8"
    )
    # Roots declared but no manifest minted → still None (never fabricated).
    assert data_identity(tmp_path) is None


def test_data_identity_present_and_moves_with_bytes(tmp_path: Path) -> None:
    first = _setup_manifest(tmp_path, "alpha")
    assert isinstance(first, str) and len(first) == 64
    # A quiet rebuild (same filename, different bytes) → a re-mint moves the sha.
    (tmp_path / "data" / "f.txt").write_text("beta", encoding="utf-8")
    mint_manifest(tmp_path, ["data"])
    second = data_identity(tmp_path)
    assert second is not None and second != first


# --------------------------------------------------------------------------- #
# leg 1 — the sidecar echo (present/absent + byte-identity)
# --------------------------------------------------------------------------- #
def test_sidecar_omits_data_manifest_sha_when_absent(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG)  # no data_manifest_sha passed
    raw = json.loads(run_sidecar_path(tmp_path, ORIG).read_text(encoding="utf-8"))
    assert "data_manifest_sha" not in raw  # only-write-non-None → byte-identical to pre-amendment
    # read_run_sidecar backfills the key to None so consumers can read it uniformly.
    assert read_run_sidecar(tmp_path, ORIG)["data_manifest_sha"] is None


def test_sidecar_echoes_data_manifest_sha_when_present(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, ORIG, data_manifest_sha=DATA_NEW)
    raw = json.loads(run_sidecar_path(tmp_path, ORIG).read_text(encoding="utf-8"))
    assert raw["data_manifest_sha"] == DATA_NEW


def test_backfill_fills_null_data_manifest_sha(tmp_path: Path) -> None:
    from hpc_agent.state.runs import backfill_run_sidecar_provenance

    _write_sidecar(tmp_path, ORIG)  # no manifest sha
    backfill_run_sidecar_provenance(
        tmp_path, ORIG, data_sha=None, env_hash=None, data_manifest_sha=DATA_NEW
    )
    assert read_run_sidecar(tmp_path, ORIG)["data_manifest_sha"] == DATA_NEW
    # An explicit value is never overwritten by a later backfill.
    backfill_run_sidecar_provenance(
        tmp_path, ORIG, data_sha=None, env_hash=None, data_manifest_sha=DATA_OLD
    )
    assert read_run_sidecar(tmp_path, ORIG)["data_manifest_sha"] == DATA_NEW


# --------------------------------------------------------------------------- #
# leg 2a — the wire SampleIdentity is additive (v1 records parse)
# --------------------------------------------------------------------------- #
def test_sample_identity_additive() -> None:
    v1 = SampleIdentity(cmd_sha=CMD_SHA, tasks_py_sha=TASKS_PY_SHA, executor=EXECUTOR)
    assert v1.data_sha is None  # a pre-amendment record parses, data leg null
    v2 = SampleIdentity(
        cmd_sha=CMD_SHA, tasks_py_sha=TASKS_PY_SHA, executor=EXECUTOR, data_sha=DATA_NEW
    )
    assert v2.data_sha == DATA_NEW


def test_sample_record_v1_identity_parses() -> None:
    record = build_sample_record(
        ts="2026-01-01T00:00:00Z",
        content_sha="e" * 64,
        identity={"cmd_sha": CMD_SHA, "tasks_py_sha": TASKS_PY_SHA, "executor": EXECUTOR},
        source="double-canary",
        run_ids=["c1", "c2"],
        cluster="widgetcluster",
        scale="canary",
        verdict="auto_cleared",
        per_key=[],
        same_submission=True,
    )
    parsed = DeterminismSampleRecord.model_validate(record)
    assert parsed.identity.data_sha is None  # additive default


# --------------------------------------------------------------------------- #
# leg 2b — samples comparable only within the same data identity
# --------------------------------------------------------------------------- #
def test_cross_data_prior_excluded_and_disclosed(tmp_path: Path) -> None:
    # A prior under OLD data, a repro under NEW data → the prior is EXCLUDED as
    # data drift (not admitted as nondeterminism evidence), and disclosed.
    _plant_sample(tmp_path, widget=(3.13, 3.16), data_sha=DATA_OLD)
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG, data_manifest_sha=DATA_NEW)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.20})
    res = _run(tmp_path)
    assert res.receipt["schema_version"] == 2  # data_moved forced v2
    disc = res.receipt["data_identity"]
    assert disc["current"] == DATA_NEW
    assert disc["excluded_data_drift"] == 1
    assert disc["data_identity_unknown"] == 0
    assert "data dimension" in res.reason  # the verdict NAMES the moved dimension
    # The excluded prior left no envelope → no stochastic envelope applied on the key.
    entry = {e["key"]: e for e in res.receipt["per_key"]}["gp.widget"]
    assert entry.get("envelope_applied") is None
    ReproductionReceipt.model_validate(res.receipt)  # the v2 shape parses


def test_same_data_prior_is_admitted(tmp_path: Path) -> None:
    # Control: the SAME data identity → the prior is NOT excluded, it forms the
    # envelope. Proves the exclusion above is the data leg, not a filter bug.
    _plant_sample(tmp_path, widget=(3.13, 3.16), data_sha=DATA_NEW)
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG, data_manifest_sha=DATA_NEW)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.155})
    res = _run(tmp_path)
    disc = res.receipt["data_identity"]
    assert disc["excluded_data_drift"] == 0
    entry = {e["key"]: e for e in res.receipt["per_key"]}["gp.widget"]
    assert entry["envelope_applied"] is not None  # the same-data prior formed the envelope


def test_unknown_data_identity_prior_disclosed(tmp_path: Path) -> None:
    # A prior with NO recorded manifest + a current KNOWN identity → the prior is
    # KEPT but disclosed data_identity_unknown (never blocking, never fabricated).
    _plant_sample(tmp_path, widget=(3.13, 3.16), data_sha=None)
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG, data_manifest_sha=DATA_NEW)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.155})
    res = _run(tmp_path)
    disc = res.receipt["data_identity"]
    assert disc["data_identity_unknown"] == 1
    assert disc["excluded_data_drift"] == 0
    assert "data identity unknown" in res.reason


def test_no_manifest_verify_stays_byte_identical(tmp_path: Path) -> None:
    # No data manifest on the repro (current unknown) + a v1 prior → the data leg
    # says nothing: no data_identity disclosure key, no data-dimension reason.
    _plant_sample(tmp_path, widget=(3.13, 3.16), data_sha=None)
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG)  # no data_manifest_sha
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.155})
    res = _run(tmp_path)
    assert res.receipt.get("data_identity") is None
    assert "data dimension" not in res.reason


def test_minted_sample_carries_data_leg(tmp_path: Path) -> None:
    # The comparison this verify appends carries the data-identity leg so a FUTURE
    # comparison can filter on it.
    _write_sidecar(tmp_path, ORIG)
    _write_sidecar(tmp_path, REPRO, reproduces=ORIG, data_manifest_sha=DATA_NEW)
    _write_aggregate(tmp_path, ORIG, {"widget": 3.14})
    _write_aggregate(tmp_path, REPRO, {"widget": 3.14})
    res = _run(tmp_path)
    assert res.appended_sample is not None
    # The wire echo validated (SampleIdentity accepted the additive data_sha leg).
    assert res.appended_sample.identity.data_sha == DATA_NEW


# --------------------------------------------------------------------------- #
# leg 3 — reproduce-run's third drift dimension (named disclosure, not refusal)
# --------------------------------------------------------------------------- #
def test_reproduce_data_drift_disclosure_match(tmp_path: Path) -> None:
    ident = _setup_manifest(tmp_path, "alpha")
    disc = _data_drift_disclosure(tmp_path, {"data_manifest_sha": ident})
    assert disc["status"] == "match"
    assert disc["recorded"] == disc["current"] == ident


def test_reproduce_data_drift_disclosure_drifted(tmp_path: Path) -> None:
    current = _setup_manifest(tmp_path, "alpha")
    disc = _data_drift_disclosure(tmp_path, {"data_manifest_sha": DATA_OLD})
    assert disc["status"] == "drifted"
    assert disc["recorded"] == DATA_OLD
    assert disc["current"] == current


def test_reproduce_data_drift_disclosure_unknown(tmp_path: Path) -> None:
    # No manifest at current time → unknown, disclosed, never a refusal.
    disc = _data_drift_disclosure(tmp_path, {"data_manifest_sha": DATA_OLD})
    assert disc["status"] == "unknown"
    disc2 = _data_drift_disclosure(tmp_path, {})  # nothing recorded either
    assert disc2["status"] == "unknown"
