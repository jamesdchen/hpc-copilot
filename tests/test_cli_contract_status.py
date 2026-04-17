"""Contract test for `python -m hpc_mapreduce.reduce.status`.

Asserts stdout JSON has exactly the pinned 4 top-level keys
(summary, tasks, rollup, errors) with the right types, so the LLM
orchestrator's parser can rely on that shape on every poll.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _build_minimal_manifest(tmp_path: Path) -> Path:
    """Three tasks over two grid points; only task 0 has a completed result."""
    r0 = tmp_path / "task_0"
    r1 = tmp_path / "task_1"
    r2 = tmp_path / "task_2"
    for r in (r0, r1, r2):
        r.mkdir()
    # Mark task 0 complete.
    (r0 / "done.json").write_text("{}")

    manifest = {
        "total_tasks": 3,
        "grid_size": 2,
        "grid_keys": ["model"],
        "tasks": {
            "0": {
                "cmd": "echo 0",
                "result_dir": str(r0),
                "params": {"model": "ridge"},
                "cmd_sha": "deadbeef00",
            },
            "1": {
                "cmd": "echo 1",
                "result_dir": str(r1),
                "params": {"model": "ridge"},
                "cmd_sha": "deadbeef01",
            },
            "2": {
                "cmd": "echo 2",
                "result_dir": str(r2),
                "params": {"model": "xgb"},
                # No cmd_sha on task 2 -> should serialize as null.
            },
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _run_status(manifest_path: Path) -> tuple[int, str, str]:
    """Invoke the status CLI in a subprocess; return (returncode, stdout, stderr)."""
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hpc_mapreduce.reduce.status",
            "--manifest",
            str(manifest_path),
            "--scheduler",
            "slurm",
            "--file-glob",
            "*.json",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestStatusCliContract:
    def test_stdout_is_valid_json_with_pinned_top_level_keys(self, tmp_path):
        manifest_path = _build_minimal_manifest(tmp_path)
        rc, out, err = _run_status(manifest_path)
        assert rc == 0, f"stderr={err}"

        doc = json.loads(out)
        assert isinstance(doc, dict)

        # Four pinned keys must all be present.
        for k in ("summary", "tasks", "rollup", "errors"):
            assert k in doc, f"missing pinned key: {k}"

    def test_top_level_types_match_contract(self, tmp_path):
        manifest_path = _build_minimal_manifest(tmp_path)
        rc, out, _ = _run_status(manifest_path)
        assert rc == 0
        doc = json.loads(out)

        # summary: 5 int keys
        summary = doc["summary"]
        assert isinstance(summary, dict)
        for k in ("complete", "running", "pending", "failed", "unknown"):
            assert k in summary, f"summary missing key: {k}"
            assert isinstance(summary[k], int), f"summary[{k}] not int"

        # tasks: dict
        assert isinstance(doc["tasks"], dict)

        # rollup: dict
        assert isinstance(doc["rollup"], dict)

        # errors: list of {code, detail} dicts (possibly empty)
        assert isinstance(doc["errors"], list)
        for e in doc["errors"]:
            assert isinstance(e, dict)
            assert "code" in e and isinstance(e["code"], str)
            assert "detail" in e and isinstance(e["detail"], str)

    def test_cmd_sha_passed_through_when_present(self, tmp_path):
        manifest_path = _build_minimal_manifest(tmp_path)
        rc, out, _ = _run_status(manifest_path)
        assert rc == 0
        doc = json.loads(out)

        # Task 1 (1-based) corresponds to manifest index "0" which had cmd_sha.
        assert doc["tasks"]["1"].get("cmd_sha") == "deadbeef00"
        # Task 3 (manifest index "2") had no cmd_sha -> null in JSON.
        assert doc["tasks"]["3"].get("cmd_sha") is None

    def test_missing_manifest_still_emits_pinned_shape(self, tmp_path):
        bogus = tmp_path / "does_not_exist.json"
        rc, out, _ = _run_status(bogus)
        # Non-zero exit, but stdout should still be the 4-key JSON doc
        # so the orchestrator can parse it unconditionally.
        assert rc != 0
        doc = json.loads(out)
        for k in ("summary", "tasks", "rollup", "errors"):
            assert k in doc
        assert doc["errors"], "expected at least one error for missing manifest"
