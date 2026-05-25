"""Tests for campaign manifest + cursor helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import jsonschema
import pytest

from hpc_agent import errors
from hpc_agent.meta.campaign.cursor import (
    advance_cursor,
    cursor_path,
    read_cursor,
)
from hpc_agent.meta.campaign.manifest import (
    MANIFEST_FILENAME,
    manifest_path,
    read_manifest,
    validate_manifest,
    write_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_manifest_path_lives_under_campaign_dir(tmp_path: Path) -> None:
    p = manifest_path(tmp_path, "camp_a")
    assert p == tmp_path / ".hpc" / "campaigns" / "camp_a" / MANIFEST_FILENAME


def test_write_round_trips(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        campaign_id="camp_a",
        goal="lowest val loss in 24h",
        budget={"max_jobs": 50, "max_tasks": None, "max_walltime_sec": 86400},
        stop_criteria={
            "max_iters": 20,
            "metric": "loss",
            "target": 0.1,
            "direction": "minimize",
            "plateau_window": 5,
            "plateau_tolerance": 0.001,
        },
        strategy={"name": "optuna-tpe", "params": {"n_startup_trials": 10}},
    )
    out = read_manifest(tmp_path, "camp_a")
    assert out is not None
    assert out["campaign_id"] == "camp_a"
    assert out["goal"] == "lowest val loss in 24h"
    assert out["budget"]["max_jobs"] == 50
    assert out["stop_criteria"]["target"] == 0.1
    assert out["strategy"]["params"]["n_startup_trials"] == 10
    assert out["manifest_schema_version"] == 1
    assert "created_at" in out


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_manifest(tmp_path, "camp_missing") is None


def test_minimal_manifest_validates(tmp_path: Path) -> None:
    write_manifest(tmp_path, campaign_id="camp_b")
    out = read_manifest(tmp_path, "camp_b")
    assert out is not None
    assert "budget" not in out
    assert "stop_criteria" not in out


def test_strategy_params_round_trip_untouched(tmp_path: Path) -> None:
    weird = {"nested": {"deeply": [1, 2, {"a": "b"}]}, "null_value": None}
    write_manifest(
        tmp_path,
        campaign_id="camp_c",
        strategy={"name": "custom", "params": weird},
    )
    out = read_manifest(tmp_path, "camp_c")
    assert out is not None
    assert out["strategy"]["params"] == weird


def test_invalid_direction_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest(
            {
                "manifest_schema_version": 1,
                "campaign_id": "x",
                "stop_criteria": {"direction": "sideways"},
            }
        )


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_manifest(
            {
                "manifest_schema_version": 1,
                "campaign_id": "x",
                "hyperparam_space": {"lr": [0.1, 0.2]},
            }
        )


# ─── cursor ─────────────────────────────────────────────────────────────────


def test_cursor_path_lives_under_campaign_dir(tmp_path: Path) -> None:
    p = cursor_path(tmp_path, "camp_a")
    assert p == tmp_path / ".hpc" / "campaigns" / "camp_a" / "cursor.json"


def test_read_returns_none_when_absent_cursor(tmp_path: Path) -> None:
    assert read_cursor(tmp_path, "camp_a") is None


def test_advance_initializes_to_one(tmp_path: Path) -> None:
    state = advance_cursor(tmp_path, "camp_a", last_run_id="run_0001")
    assert state["iteration"] == 1
    assert state["last_run_id"] == "run_0001"
    assert state["cursor_schema_version"] == 1


def test_advance_is_monotonic(tmp_path: Path) -> None:
    advance_cursor(tmp_path, "camp_a")
    advance_cursor(tmp_path, "camp_a")
    state = advance_cursor(tmp_path, "camp_a", last_run_id="run_0003")
    assert state["iteration"] == 3
    assert state["last_run_id"] == "run_0003"
    persisted = read_cursor(tmp_path, "camp_a")
    assert persisted == state


def test_advance_isolates_campaigns(tmp_path: Path) -> None:
    advance_cursor(tmp_path, "camp_a")
    advance_cursor(tmp_path, "camp_a")
    advance_cursor(tmp_path, "camp_b")
    assert read_cursor(tmp_path, "camp_a")["iteration"] == 2
    assert read_cursor(tmp_path, "camp_b")["iteration"] == 1


def test_read_rejects_newer_schema_version(tmp_path: Path) -> None:
    # Forward-compat guard: a cursor written by a newer framework must
    # be loud, not silently mis-parsed.
    import json as _json

    from hpc_agent.meta.campaign.cursor import CURSOR_SCHEMA_VERSION

    path = cursor_path(tmp_path, "camp_x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps(
            {
                "cursor_schema_version": CURSOR_SCHEMA_VERSION + 1,
                "iteration": 7,
                "last_run_id": "from_future",
                "updated_at": "2099-01-01T00:00:00+00:00",
            }
        )
    )
    with pytest.raises(errors.JournalCorrupt, match="newer than"):
        read_cursor(tmp_path, "camp_x")


def test_read_accepts_lower_or_equal_schema_version(tmp_path: Path) -> None:
    # Backward-compat: the current schema_version (and any future lower
    # version) round-trips without error. Future bumps land migrations.
    import json as _json

    from hpc_agent.meta.campaign.cursor import CURSOR_SCHEMA_VERSION

    path = cursor_path(tmp_path, "camp_y")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps(
            {
                "cursor_schema_version": CURSOR_SCHEMA_VERSION,
                "iteration": 3,
                "last_run_id": "ok",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    state = read_cursor(tmp_path, "camp_y")
    assert state is not None
    assert state["iteration"] == 3
