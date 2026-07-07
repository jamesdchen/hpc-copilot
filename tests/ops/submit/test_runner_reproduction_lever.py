"""Reproduction-receipt lever at the submit-time LAYER-2 (cmd_sha) dedup gate.

``submit_and_record``'s cross-machine ``find_run_by_cmd_sha`` fallback dedups
on PARAMETER identity, so a deliberate reproduction of identical params (a
DISTINCT run_id, same cmd_sha) would silently recover the ORIGINAL run instead
of submitting. Threading ``reproduction_of`` down to the layer-2 scan pierces
that — the original is skipped so the reproduction actually runs — while an
UNRELATED same-params prior still dedups.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent._wire.actions.submit import SubmitSpec as _WireSubmitSpec
from hpc_agent.ops.submit.runner import submit_and_record

if TYPE_CHECKING:
    from pathlib import Path

_CMD_SHA = "f" * 64


def _write_sidecar(experiment_dir: Path, run_id: str, **fields) -> Path:
    target = experiment_dir / ".hpc" / "runs" / f"{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sidecar_schema_version": 2,
        "run_id": run_id,
        "cmd_sha": fields.pop("cmd_sha", _CMD_SHA),
        "hpc_agent_version": "0.2.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python3 src/run.py",
        "result_dir_template": "results/{task_id}",
        "task_count": fields.pop("task_count", 4),
        "tasks_py_sha": "1" * 64,
        "job_ids": ["12345"],
    }
    payload.update(fields)
    target.write_text(json.dumps(payload))
    return target


def _spec(run_id: str) -> _WireSubmitSpec:
    return _WireSubmitSpec(
        profile="gpu-a100",
        cluster="discovery",
        ssh_target="me@cluster",
        remote_path="/scratch/exp",
        job_name="ml",
        run_id=run_id,
        job_ids=["99999"],
        total_tasks=4,
    )


def test_layer2_reproduction_does_not_dedup_against_its_original(tmp_path: Path) -> None:
    """A reproduction submit (distinct run_id, same cmd_sha) naming the original
    via ``reproduction_of`` is NOT deduped — it runs fresh with its own run_id."""
    _write_sidecar(tmp_path, "orig-aaaaaaaa")

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec("repro-bbbbbbbb"),
        cmd_sha=_CMD_SHA,
        reproduction_of="orig-aaaaaaaa",
    )

    assert deduped is False
    assert record.run_id == "repro-bbbbbbbb"
    assert record.job_ids == ["99999"]


def test_layer2_reproduction_skips_a_prior_reproduction_too(tmp_path: Path) -> None:
    """A SECOND reproduction of the same original does not dedup against the
    FIRST one either — the prior repro's sidecar records ``reproduces`` == the
    original, which the lever also skips."""
    _write_sidecar(tmp_path, "orig-aaaaaaaa")
    _write_sidecar(tmp_path, "repro1-bbbbbbbb", reproduces="orig-aaaaaaaa")

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec("repro2-cccccccc"),
        cmd_sha=_CMD_SHA,
        reproduction_of="orig-aaaaaaaa",
    )

    assert deduped is False
    assert record.run_id == "repro2-cccccccc"


def test_layer2_still_dedups_an_unrelated_identical_submit(tmp_path: Path) -> None:
    """Without the lever, an identical-params resubmit still dedups against the
    prior (the historical cross-machine recovery contract is untouched)."""
    _write_sidecar(tmp_path, "orig-aaaaaaaa")

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec("resubmit-bbbbbbbb"),
        cmd_sha=_CMD_SHA,
        # reproduction_of unset
    )

    assert deduped is True
    assert record.run_id == "orig-aaaaaaaa"
    assert record.job_ids == ["12345"]


def test_layer2_reproduction_still_dedups_against_an_unrelated_prior(tmp_path: Path) -> None:
    """The lever is surgical: naming an original does NOT disable dedup against
    an UNRELATED same-params prior (different run_id, untagged) — that is still
    the same experiment and a valid recovery target."""
    _write_sidecar(tmp_path, "unrelated-dddddddd")

    record, deduped = submit_and_record(
        tmp_path,
        spec=_spec("repro-bbbbbbbb"),
        cmd_sha=_CMD_SHA,
        reproduction_of="orig-aaaaaaaa",  # names an original that is NOT present
    )

    assert deduped is True
    assert record.run_id == "unrelated-dddddddd"
