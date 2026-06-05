"""Tests for the ``prepare-followup-specs`` primitive (#278).

Pins the pre-staging of the two followup specs written at submit time so
``/monitor-hpc`` and ``/aggregate-hpc`` can skip the operator interview:

* both files are written into the experiment dir;
* each carries ``run_id`` + the ``cmd_sha`` staleness gate;
* the operator-choice fields are left as ``null`` sentinels
  (``wait_terminal`` in monitor; ``stage`` + ``allow_partial`` in
  aggregate) — pre-staging never silently picks them;
* the returned paths point at the written files;
* an idempotent re-run (keyed on ``run_id``) overwrites cleanly with
  equivalent content.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent.ops.prepare_followup_specs import prepare_followup_specs

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260605-120000-deadbee"
_CMD_SHA = "a" * 64


def _load(path: str) -> dict:
    """Read+parse a written spec file by its (string) path."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_writes_both_spec_files(tmp_path: Path) -> None:
    out = prepare_followup_specs(
        experiment_dir=str(tmp_path),
        run_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
        profile="train",
    )

    monitor_path = tmp_path / "monitor_spec.json"
    aggregate_path = tmp_path / "aggregate_spec.json"
    assert monitor_path.is_file()
    assert aggregate_path.is_file()

    # Returned paths point at the two written files.
    assert out["monitor_spec_path"] == str(monitor_path)
    assert out["aggregate_spec_path"] == str(aggregate_path)
    assert out["run_id"] == _RUN_ID
    assert out["cmd_sha"] == _CMD_SHA


def test_monitor_spec_contents(tmp_path: Path) -> None:
    prepare_followup_specs(
        experiment_dir=str(tmp_path),
        run_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
    )
    monitor = _load(str(tmp_path / "monitor_spec.json"))

    assert monitor["run_id"] == _RUN_ID
    assert monitor["cmd_sha"] == _CMD_SHA
    # Operator-choice field is the null sentinel — left undecided at submit.
    assert "wait_terminal" in monitor
    assert monitor["wait_terminal"] is None
    assert monitor["prepared_by"] == "prepare-followup-specs"
    assert isinstance(monitor["prepared_at"], str) and monitor["prepared_at"]


def test_aggregate_spec_contents(tmp_path: Path) -> None:
    prepare_followup_specs(
        experiment_dir=str(tmp_path),
        run_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
        profile="train",
    )
    aggregate = _load(str(tmp_path / "aggregate_spec.json"))

    assert aggregate["run_id"] == _RUN_ID
    assert aggregate["cmd_sha"] == _CMD_SHA
    assert aggregate["profile"] == "train"
    # Both operator-choice fields are null sentinels.
    assert "stage" in aggregate
    assert aggregate["stage"] is None
    assert "allow_partial" in aggregate
    assert aggregate["allow_partial"] is None
    assert aggregate["prepared_by"] == "prepare-followup-specs"
    assert isinstance(aggregate["prepared_at"], str) and aggregate["prepared_at"]


def test_cmd_sha_and_profile_default_to_null(tmp_path: Path) -> None:
    # Omitting cmd_sha / profile leaves them as explicit nulls in the specs
    # and in the returned echo.
    out = prepare_followup_specs(experiment_dir=str(tmp_path), run_id=_RUN_ID)
    assert out["cmd_sha"] is None

    monitor = _load(str(tmp_path / "monitor_spec.json"))
    aggregate = _load(str(tmp_path / "aggregate_spec.json"))
    assert monitor["cmd_sha"] is None
    assert aggregate["cmd_sha"] is None
    assert aggregate["profile"] is None


def test_idempotent_rerun_overwrites_cleanly(tmp_path: Path) -> None:
    # Keyed on run_id: a second call for the same run overwrites both files
    # without error and yields equivalent content (modulo prepared_at).
    first = prepare_followup_specs(
        experiment_dir=str(tmp_path),
        run_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
        profile="train",
    )
    monitor_first = _load(str(tmp_path / "monitor_spec.json"))
    aggregate_first = _load(str(tmp_path / "aggregate_spec.json"))

    second = prepare_followup_specs(
        experiment_dir=str(tmp_path),
        run_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
        profile="train",
    )
    monitor_second = _load(str(tmp_path / "monitor_spec.json"))
    aggregate_second = _load(str(tmp_path / "aggregate_spec.json"))

    assert second == first
    # Content is equivalent on the load-bearing fields (timestamp may refresh).
    for key in ("run_id", "cmd_sha", "wait_terminal", "prepared_by"):
        assert monitor_second[key] == monitor_first[key]
    for key in ("run_id", "cmd_sha", "profile", "stage", "allow_partial", "prepared_by"):
        assert aggregate_second[key] == aggregate_first[key]
