"""Tests for the ``campaign`` CLI subcommand (status, list).

End-to-end via subprocess to pin the JSON envelope shape that external
agent harnesses and other consumers will depend on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

from hpc_agent.state.runs import run_sidecar_path, write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


def _run_cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent", *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_envelope(stdout: str) -> dict:
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line; got {len(lines)}"
    return json.loads(lines[0])


def _common_required_kwargs(run_id: str, task_count: int = 1) -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=task_count,
        tasks_py_sha="1" * 64,
    )


def _write_metrics(result_dir: Path, payload: dict) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "metrics.json").write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# campaign list
# ---------------------------------------------------------------------------


def test_campaign_list_empty(tmp_path: Path) -> None:
    rc, out, _ = _run_cli("campaign", "list", "--experiment-dir", str(tmp_path))
    assert rc == 0
    env = _parse_envelope(out)
    assert env["ok"] is True
    assert env["data"]["campaigns"] == []


def test_campaign_list_groups_by_campaign_id(tmp_path: Path) -> None:
    write_run_sidecar(tmp_path, **_common_required_kwargs("r1"), campaign_id="A")
    write_run_sidecar(tmp_path, **_common_required_kwargs("r2"), campaign_id="B")
    write_run_sidecar(tmp_path, **_common_required_kwargs("r3"), campaign_id="A")
    # Pin distinct mtimes — ``campaign list`` only counts per-campaign
    # totals here, but the sidecar sort is mtime-driven; explicit utimes
    # make the test independent of FS mtime resolution.
    t0 = 1_700_000_000.0
    for i, run_id in enumerate(("r1", "r2", "r3")):
        os.utime(run_sidecar_path(tmp_path, run_id), (t0 + i, t0 + i))

    rc, out, _ = _run_cli("campaign", "list", "--experiment-dir", str(tmp_path))
    assert rc == 0
    env = _parse_envelope(out)
    counts = {c["campaign_id"]: c["iterations"] for c in env["data"]["campaigns"]}
    assert counts == {"A": 2, "B": 1}


def test_campaign_list_skips_untagged_runs(tmp_path: Path) -> None:
    """Open-loop sidecars (no campaign_id) must not appear in the list."""
    write_run_sidecar(tmp_path, **_common_required_kwargs("r1"))  # no campaign_id
    write_run_sidecar(tmp_path, **_common_required_kwargs("r2"), campaign_id="A")

    rc, out, _ = _run_cli("campaign", "list", "--experiment-dir", str(tmp_path))
    env = _parse_envelope(out)
    assert env["data"]["campaigns"] == [{"campaign_id": "A", "iterations": 1}]


# ---------------------------------------------------------------------------
# campaign status
# ---------------------------------------------------------------------------


def test_campaign_status_unknown_campaign_returns_empty(tmp_path: Path) -> None:
    rc, out, _ = _run_cli(
        "campaign", "status", "--experiment-dir", str(tmp_path), "--campaign-id", "ghost"
    )
    assert rc == 0
    env = _parse_envelope(out)
    data = env["data"]
    assert data["campaign_id"] == "ghost"
    assert data["iterations"] == 0
    assert data["history"] == []
    assert data["run_ids"] == []


def test_campaign_status_reports_per_iteration_history(tmp_path: Path) -> None:
    """Each matching sidecar contributes one history dict; oldest-first."""
    write_run_sidecar(tmp_path, **_common_required_kwargs("r1"), campaign_id="A")
    _write_metrics(tmp_path / "results" / "r1" / "task_0", {"loss": 0.5, "n_samples": 1})
    write_run_sidecar(tmp_path, **_common_required_kwargs("r2"), campaign_id="A")
    _write_metrics(tmp_path / "results" / "r2" / "task_0", {"loss": 0.1, "n_samples": 1})
    # Pin distinct mtimes — ``r1``/``r2`` are not ISO-sortable, so the
    # oldest-first contract is enforced via mtime, not via run_id stem.
    t0 = 1_700_000_000.0
    for i, run_id in enumerate(("r1", "r2")):
        os.utime(run_sidecar_path(tmp_path, run_id), (t0 + i, t0 + i))

    rc, out, _ = _run_cli(
        "campaign", "status", "--experiment-dir", str(tmp_path), "--campaign-id", "A"
    )
    assert rc == 0
    env = _parse_envelope(out)
    data = env["data"]
    assert data["iterations"] == 2
    assert data["run_ids"] == ["r1", "r2"]
    assert [h["loss"] for h in data["history"]] == [0.5, 0.1]


def test_campaign_status_envelope_validates_against_schema(tmp_path: Path) -> None:
    """Pin the public contract — `data` block matches campaign.output.json."""
    from importlib.resources import files

    schema = json.loads((files("hpc_agent.schemas") / "campaign.output.json").read_text())

    write_run_sidecar(tmp_path, **_common_required_kwargs("r1"), campaign_id="A")
    rc, out, _ = _run_cli(
        "campaign", "status", "--experiment-dir", str(tmp_path), "--campaign-id", "A"
    )
    assert rc == 0
    env = _parse_envelope(out)
    from hpc_agent._kernel.contract.schema import validate as _validate

    _validate(env["data"], schema)


def test_campaign_list_envelope_validates_against_schema(tmp_path: Path) -> None:
    from importlib.resources import files

    schema = json.loads((files("hpc_agent.schemas") / "campaign.output.json").read_text())

    write_run_sidecar(tmp_path, **_common_required_kwargs("r1"), campaign_id="A")
    rc, out, _ = _run_cli("campaign", "list", "--experiment-dir", str(tmp_path))
    assert rc == 0
    env = _parse_envelope(out)
    from hpc_agent._kernel.contract.schema import validate as _validate

    _validate(env["data"], schema)
