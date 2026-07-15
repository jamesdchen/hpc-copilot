"""Run-13 finding 4 (the check-time-surfacing class of finding 28): S1's resolved
brief — the human boundary the greenlight crosses BEFORE submit-s2 detaches and
pushes the whole tree — carries a CODE-computed deploy-payload disclosure (file
count, MB, top-3 contributing root dirs). A pathological payload (run 12's 1.18 GB
of analysis outputs re-shipped as "code") must be legible at the greenlight, not
after the hour-long transfer.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import hpc_agent.ops.submit_blocks as blocks
from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsResult
from hpc_agent._wire.workflows.submit_blocks import SubmitS1Spec


def _write(root: Path, rel: str, text: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_deploy_payload_brief_counts_code_drops_the_mirrors(tmp_path: Path) -> None:
    """The pure disclosure seam: counts the shipped tree, drops the stack-minted
    pull mirrors, and names the top root dirs — all in code."""
    _write(tmp_path, "src/train.py", "print('hi')")
    _write(tmp_path, "tasks.py")
    _write(tmp_path, "_per_task_results/task-0/metrics.json", "{}" * 100)
    _write(tmp_path, "_aggregated/run-x/metrics_aggregate.json", "{}")

    block = blocks._deploy_payload_brief(tmp_path, {"rsync_excludes": None})

    assert block is not None
    assert block["file_count"] == 2  # src/train.py + tasks.py; mirrors excluded
    root_names = {r["name"] for r in block["top_roots"]}
    assert "_per_task_results" not in root_names
    assert "_aggregated" not in root_names
    assert "src" in root_names
    assert block["warn"] is False


def test_deploy_payload_brief_fails_open_on_garbage_spec(tmp_path: Path) -> None:
    """A non-dict / missing submit_spec must not raise — the disclosure is never
    load-bearing. A None spec still summarizes the tree under the default excludes."""
    assert blocks._deploy_payload_brief(tmp_path, None) is not None
    assert blocks._deploy_payload_brief(tmp_path, {"rsync_excludes": "not-a-list"}) is not None


def test_s1_resolved_brief_carries_deploy_payload(tmp_path: Path) -> None:
    """Wiring: submit_s1's CLEAN-RESOLVE brief (next_block -> submit-s2) carries the
    deploy_payload block. resolve_submit_inputs is patched so the test stays
    cluster-free and focuses on the brief digestion."""
    _write(tmp_path, "src/train.py", "print('hi')")
    _write(tmp_path, "tasks.py")

    # A clean walk (cluster supplied → no ambiguity) so S1 reaches the resolve leg.
    walk = WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": "carc",
            "configured_clusters": ["carc", "hoffman2"],
            "goal": "sweep ridge",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )
    # resolve is non-None so S1 takes the resolve branch; its value is irrelevant
    # because resolve_submit_inputs is patched. model_construct skips the heavy
    # BuildSubmitSpecInput / WriteRunSidecarInput validation.
    spec = SubmitS1Spec.model_construct(walk=walk, run_preflight=False, resolve=object())

    fake_rr = ResolveSubmitInputsResult(
        stage_reached="resolved",
        needs_decision=True,
        reason="plan resolved; stage & canary.",
        run_id="ridge-abcd1234",
        cmd_sha="0" * 64,
        submit_spec={"rsync_excludes": None},
        sidecar_path=str(tmp_path / ".hpc" / "runs" / "ridge-abcd1234.json"),
    )
    with mock.patch.object(blocks, "resolve_submit_inputs", return_value=fake_rr):
        result = blocks.submit_s1(tmp_path, spec=spec)

    assert result.stage_reached == "resolved"
    payload = result.brief["deploy_payload"]
    assert payload["file_count"] == 2
    assert {r["name"] for r in payload["top_roots"]} >= {"src"}
    assert "total_mb" in payload and "warn" in payload
