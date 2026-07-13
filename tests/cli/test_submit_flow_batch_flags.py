"""Batch-shaped specs must honor ``--partial-ok`` / ``--invalidate-on-code-change``.

``cmd_submit_flow`` auto-routes a ``{"specs": [...]}`` spec to
``cmd_submit_flow_batch`` BEFORE its single-spec flag injection runs, and the
batch handler re-loads the spec from disk — pre-fix both flags were silently
discarded for batches (and the standalone ``submit-flow-batch`` verb has no
equivalents). Now the batch handler injects the flags into EVERY entry, flag
winning over the per-entry spec value (the single-spec "Flag wins over spec
when both are set" contract).
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from hpc_agent.cli.submit import cmd_submit_flow
from hpc_agent.ops.submit_flow import SubmitFlowResult

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.workflows.submit_flow_batch import SubmitFlowBatchSpec


def _entry(run_id: str, **over: object) -> dict:
    base: dict = {
        "profile": "p",
        "cluster": "hoffman2",
        "ssh_target": "user@host",
        "remote_path": "/x",
        "job_name": "j",
        "run_id": run_id,
        "total_tasks": 2,
        "backend": "sge",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 run.py"},
    }
    base.update(over)
    return base


def _batch_args(tmp_path: Path, entries: list[dict], **flags: bool) -> argparse.Namespace:
    spec_file = tmp_path / "batch.json"
    spec_file.write_text(json.dumps({"specs": entries}), encoding="utf-8")
    return argparse.Namespace(
        spec=spec_file,  # argparse type=Path — the handler calls .read_text()
        experiment_dir=tmp_path,
        dry_run=False,
        partial_ok=flags.get("partial_ok", False),
        invalidate_on_code_change=flags.get("invalidate_on_code_change", False),
    )


def _run_capturing_batch_spec(args: argparse.Namespace) -> SubmitFlowBatchSpec:
    """Drive ``cmd_submit_flow`` (the auto-dispatch entry) with the batch op
    stubbed; return the validated ``SubmitFlowBatchSpec`` it received."""
    captured: list[SubmitFlowBatchSpec] = []

    def _fake_batch(experiment_dir, *, spec):  # type: ignore[no-untyped-def]
        captured.append(spec)
        return [
            SubmitFlowResult(
                run_id=s.run_id,
                job_ids=["1"],
                total_tasks=s.total_tasks,
                deduped=False,
                canary_done=False,
            )
            for s in spec.specs
        ]

    with patch("hpc_agent.ops.submit_flow.submit_flow_batch", side_effect=_fake_batch):
        rc = cmd_submit_flow(args)
    assert rc == 0
    assert len(captured) == 1
    return captured[0]


def test_batch_spec_partial_ok_flag_reaches_every_entry(tmp_path: Path, capsys) -> None:
    """The audit reproduction: batch spec + --partial-ok → every submitted
    entry carries partial_ok=True (pre-fix: silently discarded)."""
    entries = [_entry("20260101-000000-aaa0001"), _entry("20260101-000000-bbb0001")]
    batch_spec = _run_capturing_batch_spec(_batch_args(tmp_path, entries, partial_ok=True))
    capsys.readouterr()  # drain the envelope
    assert [s.partial_ok for s in batch_spec.specs] == [True, True]


def test_batch_spec_invalidate_flag_reaches_every_entry(tmp_path: Path, capsys) -> None:
    entries = [_entry("20260101-000000-aaa0001"), _entry("20260101-000000-bbb0001")]
    batch_spec = _run_capturing_batch_spec(
        _batch_args(tmp_path, entries, invalidate_on_code_change=True)
    )
    capsys.readouterr()
    assert [s.invalidate_on_code_change for s in batch_spec.specs] == [True, True]


def test_batch_spec_flag_off_leaves_per_entry_values_alone(tmp_path: Path, capsys) -> None:
    """No flag → per-entry spec values pass through untouched (the flag only
    ever forces True, matching the single-spec contract)."""
    entries = [
        _entry("20260101-000000-aaa0001", partial_ok=True),
        _entry("20260101-000000-bbb0001"),
    ]
    batch_spec = _run_capturing_batch_spec(_batch_args(tmp_path, entries))
    capsys.readouterr()
    assert [s.partial_ok for s in batch_spec.specs] == [True, False]
    assert [s.invalidate_on_code_change for s in batch_spec.specs] == [False, False]
