"""Tests for hpc.dispatch atomic output pattern."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc import dispatch
from hpc.status import check_results


class TestDispatchAtomicOutput:
    def _write_manifest(self, path: Path, tasks: dict) -> str:
        """Write a manifest JSON and return its path as a string."""
        manifest = {"tasks": tasks}
        manifest_path = str(path / "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)
        return manifest_path

    def test_successful_task_promotes_output(self, tmp_path, monkeypatch):
        result_dir = str(tmp_path / "results")
        manifest_path = self._write_manifest(tmp_path, {
            "1": {
                "cmd": 'echo hello > "$RESULT_DIR/results_chunk_1.csv"',
                "result_dir": result_dir,
            },
        })

        monkeypatch.setenv("CHUNK_ID", "1")
        monkeypatch.setenv("HPC_MANIFEST", manifest_path)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0

        # Output file should be promoted to the final result dir
        assert (Path(result_dir) / "results_chunk_1.csv").exists()
        assert (Path(result_dir) / "results_chunk_1.csv").read_text().strip() == "hello"

        # WIP directory should be cleaned up
        assert not (Path(result_dir) / "_wip_1").exists()

    def test_failed_task_preserves_wip(self, tmp_path, monkeypatch):
        result_dir = str(tmp_path / "results")
        manifest_path = self._write_manifest(tmp_path, {
            "0": {
                "cmd": 'echo partial > "$RESULT_DIR/out.csv" && exit 1',
                "result_dir": result_dir,
            },
        })

        monkeypatch.setenv("CHUNK_ID", "0")
        monkeypatch.setenv("HPC_MANIFEST", manifest_path)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 1

        # WIP directory should still exist with the partial file
        wip_dir = Path(result_dir) / "_wip_0"
        assert wip_dir.exists()
        assert (wip_dir / "out.csv").exists()
        assert (wip_dir / "out.csv").read_text().strip() == "partial"

        # Final result dir should NOT have the output file
        assert not (Path(result_dir) / "out.csv").exists()

    def test_multiple_files_promoted(self, tmp_path, monkeypatch):
        result_dir = str(tmp_path / "results")
        cmd = (
            'echo "a,b" > "$RESULT_DIR/results_chunk_1.csv" && '
            'echo "x,y" > "$RESULT_DIR/results_chunk_2.csv"'
        )
        manifest_path = self._write_manifest(tmp_path, {
            "5": {
                "cmd": cmd,
                "result_dir": result_dir,
            },
        })

        monkeypatch.setenv("CHUNK_ID", "5")
        monkeypatch.setenv("HPC_MANIFEST", manifest_path)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.main()

        assert exc_info.value.code == 0

        # Both files should be in the final result dir
        assert (Path(result_dir) / "results_chunk_1.csv").exists()
        assert (Path(result_dir) / "results_chunk_2.csv").exists()
        assert (Path(result_dir) / "results_chunk_1.csv").read_text().strip() == "a,b"
        assert (Path(result_dir) / "results_chunk_2.csv").read_text().strip() == "x,y"

        # WIP directory should be cleaned up
        assert not (Path(result_dir) / "_wip_5").exists()


class TestCheckResultsIgnoresWip:
    def test_check_results_ignores_wip(self, tmp_path):
        result_dir = tmp_path / "results"
        result_dir.mkdir()

        # Write a valid CSV in the result dir (header + 1 data row)
        valid_csv = result_dir / "results_chunk_1.csv"
        valid_csv.write_text("col_a,col_b\n1,2\n")

        # Create a _wip_0 subdir with another CSV that should be ignored
        wip_dir = result_dir / "_wip_0"
        wip_dir.mkdir()
        wip_csv = wip_dir / "results_chunk_2.csv"
        wip_csv.write_text("col_a,col_b\n3,4\n")

        results = check_results(result_dir, total_chunks=2)

        # Only chunk 1 should be found; chunk 2 in _wip_ should be skipped
        assert 1 in results
        assert 2 not in results
        assert len(results) == 1
