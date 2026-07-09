"""Tests for ``conformance-status`` (``ops/conformance/status_op.py``, T5).

The read-only comparator seat of live conformance, driven END-TO-END over a REAL
ledger + a crafted registration journal on disk: register-with-declaration →
record a live stream → ``conformance-status`` reports the derived tier. Covers
every fold path (conforming / needs_verdict / nonconforming), the disclose-never-
refuse baseline sha drift, the opt-in refusals (absent registration, missing
declaration), the two window-selection modes, and the write-probe (the query
creates and mutates nothing).

TOY VOCABULARY ONLY (the plan's C6 fixture rule): a fake instrument ``sensor-7``,
a sealed CALIBRATION-readings baseline, a live readings stream. Never trading
words.

Seam note: the ``conformance`` block on a registration's ``resolved`` is the
registration T6 amendment (not landed in this worktree); these tests craft the
registration journal directly on disk with the block in its DOCUMENTED C-declare
shape, exercising the op's read-time ``# T6 seam`` path verbatim.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import hpc_agent.state.conformance as conformance
import hpc_agent.state.conformance_store as conformance_store
from hpc_agent import errors
from hpc_agent.ops.conformance.status_op import conformance_status
from hpc_agent.state.decision_journal import decisions_path

_REG_ID = "sensor-7-cal"
_DOSSIER_SHA = "a" * 64


def _write_registration(
    experiment_dir: Path,
    *,
    registration_id: str = _REG_ID,
    conformance_block: dict[str, Any] | None,
) -> None:
    """Craft a one-record registration journal on disk (the T6 seam shape)."""
    resolved: dict[str, Any] = {
        "registration_id": registration_id,
        "run_id": "cal-run",
        "dossier_sha": _DOSSIER_SHA,
    }
    if conformance_block is not None:
        resolved["conformance"] = conformance_block
    record = {"block": "registration", "ts": "2026-03-02T00:00:00Z", "resolved": resolved}
    path = decisions_path(experiment_dir, "registration", registration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _write_baseline(
    experiment_dir: Path, rows: list[dict[str, Any]], *, rel: str = "cal.json"
) -> str:
    """Write the sealed baseline artifact; return its RAW-bytes sha256."""
    artifact = experiment_dir / rel
    data = json.dumps(rows).encode("utf-8")
    artifact.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _record_observation(
    experiment_dir: Path, *, reading: float, observed_at: str, labels: dict[str, Any] | None = None
) -> None:
    """Append one REAL live observation receipt to the registration's ledger."""
    rec = conformance.build_observation_record(
        registration_id=_REG_ID,
        dossier_sha=_DOSSIER_SHA,
        status_at_record="current",
        payload={"reading": reading},
        observed_at=observed_at,
        labels=labels or {"site": "lab-a"},
        emitter="sensor-7",
        ts=observed_at,
    )
    conformance_store.append_observation(experiment_dir, record=rec)


def _setup_conforming(
    experiment_dir: Path, *, min_window_n: int = 3, baseline_sha: str | None = None
) -> None:
    baseline_rows = [{"reading": v} for v in (0.94, 0.95, 0.96, 0.97, 0.955)]
    sha = _write_baseline(experiment_dir, baseline_rows)
    _write_registration(
        experiment_dir,
        conformance_block={
            "baseline": {"path": "cal.json", "sha256": baseline_sha or sha},
            "keys": ["reading"],
            "min_window_n": min_window_n,
        },
    )


# ── the fold paths ────────────────────────────────────────────────────────────


def test_conforming_window_inside_envelope(tmp_path: Path) -> None:
    _setup_conforming(tmp_path)
    for i, v in enumerate((0.95, 0.96, 0.955)):
        _record_observation(tmp_path, reading=v, observed_at=f"2026-05-0{i + 1}T00:00:00Z")

    result = conformance_status(
        experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=3)
    )
    assert result.overall == "conforming"
    assert result.keys[0].tier_reason == "within_envelope"
    assert result.keys[0].window_n == 3
    assert result.baseline.n == 5
    assert "CONFORMING" in result.render


def test_nonconforming_window_exits_envelope_is_a_finding(tmp_path: Path) -> None:
    _setup_conforming(tmp_path)
    for i, v in enumerate((0.95, 0.99, 0.96)):  # 0.99 > baseline hi 0.97
        _record_observation(tmp_path, reading=v, observed_at=f"2026-05-0{i + 1}T00:00:00Z")

    # A nonconforming window must NOT mutate the registration journal (a FINDING,
    # never an actuation). Snapshot the journal bytes across the read.
    reg_path = decisions_path(tmp_path, "registration", _REG_ID)
    before = reg_path.read_bytes()

    result = conformance_status(
        experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=3)
    )
    assert result.overall == "nonconforming"
    assert result.keys[0].tier_reason == "outside_envelope"
    assert reg_path.read_bytes() == before  # status byte-unchanged


def test_insufficient_window_routes_needs_verdict(tmp_path: Path) -> None:
    _setup_conforming(tmp_path, min_window_n=5)
    for i, v in enumerate((0.95, 0.96)):  # only 2 < min_window_n 5
        _record_observation(tmp_path, reading=v, observed_at=f"2026-05-0{i + 1}T00:00:00Z")

    result = conformance_status(
        experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=2)
    )
    assert result.overall == "needs_verdict"
    assert result.keys[0].tier_reason == "insufficient_window"


def test_thin_baseline_routes_needs_verdict(tmp_path: Path) -> None:
    thin = [{"reading": v} for v in (0.95, 0.96)]  # n=2 < well-evidenced bar 3
    sha = _write_baseline(tmp_path, thin)
    _write_registration(
        tmp_path,
        conformance_block={
            "baseline": {"path": "cal.json", "sha256": sha},
            "keys": ["reading"],
            "min_window_n": 3,
        },
    )
    for i, v in enumerate((0.95, 0.96, 0.955)):
        _record_observation(tmp_path, reading=v, observed_at=f"2026-05-0{i + 1}T00:00:00Z")

    result = conformance_status(
        experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=3)
    )
    assert result.overall == "needs_verdict"
    assert result.keys[0].tier_reason == "thin_baseline"


# ── baseline sha drift: DISCLOSE, never refuse ────────────────────────────────


def test_baseline_sha_drift_is_disclosed_not_refused(tmp_path: Path) -> None:
    _setup_conforming(tmp_path, baseline_sha="deadbeef" * 8)  # wrong declared sha
    for i, v in enumerate((0.95, 0.96, 0.955)):
        _record_observation(tmp_path, reading=v, observed_at=f"2026-05-0{i + 1}T00:00:00Z")

    # No raise; the drift is disclosed in the brief and the on-disk rows still read.
    result = conformance_status(
        experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=3)
    )
    assert "does not match the sealed" in result.render
    assert result.baseline.n == 5  # the on-disk rows were still read


# ── opt-in refusals ───────────────────────────────────────────────────────────


def test_absent_registration_refused(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="no registration named"):
        conformance_status(
            experiment_dir=tmp_path, spec=_spec(registration_id="ghost", last_n=3)
        )


def test_missing_declaration_refused(tmp_path: Path) -> None:
    _write_baseline(tmp_path, [{"reading": 0.95}])
    _write_registration(tmp_path, conformance_block=None)  # opted OUT
    with pytest.raises(errors.SpecInvalid, match="no 'conformance' declaration"):
        conformance_status(
            experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=3)
        )


# ── window selection modes ────────────────────────────────────────────────────


def test_last_n_and_since_select_different_windows(tmp_path: Path) -> None:
    _setup_conforming(tmp_path)
    for i, v in enumerate((0.95, 0.96, 0.955, 0.94, 0.97)):
        _record_observation(tmp_path, reading=v, observed_at=f"2026-05-0{i + 1}T00:00:00Z")

    last2 = conformance_status(
        experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=2)
    )
    assert last2.window.n == 2

    since = conformance_status(
        experiment_dir=tmp_path,
        spec=_spec(registration_id=_REG_ID, since="2026-05-03T00:00:00Z"),
    )
    assert since.window.n == 3  # 05-03, 05-04, 05-05
    assert since.window.since == "2026-05-03T00:00:00Z"
    assert since.window.until == "2026-05-05T00:00:00Z"


# ── the write-probe: the query creates and mutates nothing ────────────────────


def test_write_probe_query_mutates_nothing(tmp_path: Path) -> None:
    _setup_conforming(tmp_path)
    for i, v in enumerate((0.95, 0.96, 0.955)):
        _record_observation(tmp_path, reading=v, observed_at=f"2026-05-0{i + 1}T00:00:00Z")

    before = _tree_snapshot(tmp_path)
    conformance_status(experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=3))
    conformance_status(experiment_dir=tmp_path, spec=_spec(registration_id=_REG_ID, last_n=3))
    after = _tree_snapshot(tmp_path)
    assert before == after


# ── helpers ───────────────────────────────────────────────────────────────────


def _spec(**kw: Any) -> Any:
    from hpc_agent._wire.queries.conformance_status import ConformanceStatusSpec

    return ConformanceStatusSpec(**kw)


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    """Path -> bytes for every file under *root* (the write-probe snapshot)."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
