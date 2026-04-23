"""Tests for hpc_mapreduce.map.metrics_io.write_metrics."""

from __future__ import annotations

import json
import os

import pytest

from hpc_mapreduce.map.metrics_io import write_metrics


class TestWriteMetricsDestination:
    def test_uses_result_dir_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RESULT_DIR", str(tmp_path))
        path = write_metrics({"mse": 0.1, "n_samples": 10})
        assert path == str(tmp_path / "metrics.json")
        assert json.loads((tmp_path / "metrics.json").read_text()) == {
            "mse": 0.1,
            "n_samples": 10,
        }

    def test_explicit_result_dir_overrides_env(self, tmp_path, monkeypatch):
        env_dir = tmp_path / "env"
        arg_dir = tmp_path / "arg"
        env_dir.mkdir()
        arg_dir.mkdir()
        monkeypatch.setenv("RESULT_DIR", str(env_dir))

        write_metrics({"v": 1}, result_dir=str(arg_dir))

        assert (arg_dir / "metrics.json").exists()
        assert not (env_dir / "metrics.json").exists()

    def test_no_dir_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RESULT_DIR", raising=False)
        with pytest.raises(RuntimeError, match="RESULT_DIR"):
            write_metrics({"x": 1})

    def test_creates_missing_result_dir(self, tmp_path):
        target = tmp_path / "nested" / "rdir"
        assert not target.exists()
        write_metrics({"x": 1}, result_dir=str(target))
        assert (target / "metrics.json").exists()


class TestWriteMetricsAtomic:
    """On serialisation failure, the final metrics.json must not exist and
    no stray .metrics.*.json tempfile may be left behind."""

    def test_no_partial_file_on_serialisation_error(self, tmp_path, monkeypatch):
        # Non-serialisable payload: a set is not JSON.
        with pytest.raises(TypeError):
            write_metrics({"bad": {1, 2, 3}}, result_dir=str(tmp_path))

        entries = list(tmp_path.iterdir())
        assert entries == [], f"Expected clean dir, found: {entries}"

    def test_overwrites_existing_metrics_json_atomically(self, tmp_path):
        target = tmp_path / "metrics.json"
        target.write_text(json.dumps({"old": True}))

        write_metrics({"new": True}, result_dir=str(tmp_path))

        assert json.loads(target.read_text()) == {"new": True}
        # No stray temp files.
        strays = [p for p in tmp_path.iterdir() if p.name.startswith(".metrics.")]
        assert strays == []


class TestExecutorTemplateEmitsMetrics:
    """End-to-end: the shipped scaffold writes metrics.json when RESULT_DIR is set."""

    def test_template_emits_metrics_json(self, tmp_path, monkeypatch):
        # The template reads a data path, builds features, fits+predicts, and
        # writes CSV + metrics.json.  The default load_data() returns [], so
        # the run is a no-op aside from the file writes -- which is exactly
        # what we want to test.
        import runpy

        rdir = tmp_path / "rdir"
        rdir.mkdir()
        out_csv = tmp_path / "out.csv"

        monkeypatch.setenv("RESULT_DIR", str(rdir))
        monkeypatch.setattr(
            "sys.argv",
            ["executor_template.py", "--output-file", str(out_csv)],
        )

        # Locate the template relative to this test file.
        template_path = (
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            + "/templates/executor_template.py"
        )
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(template_path, run_name="__main__")
        assert exc.value.code == 0

        metrics_path = rdir / "metrics.json"
        assert metrics_path.exists()
        data = json.loads(metrics_path.read_text())
        assert "metric" in data
        assert "n_samples" in data
