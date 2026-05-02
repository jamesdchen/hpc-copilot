"""Tests for ``hpc_mapreduce.job.stages`` loader and JSON Schema validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import jsonschema
import pytest

from hpc_mapreduce.job.stages import (
    STAGES_FILENAME,
    load_stages,
    load_stages_module,
    stages_path,
    stages_schema,
    validate_stages,
)

if TYPE_CHECKING:
    from pathlib import Path

REPO_ROOT_FIXTURES = "tests/fixtures"


# ---------------------------------------------------------------------------
# Path / schema basics
# ---------------------------------------------------------------------------


def test_stages_path_points_into_dot_hpc(tmp_path: Path) -> None:
    p = stages_path(tmp_path)
    assert p == tmp_path / ".hpc" / STAGES_FILENAME


def test_stages_schema_is_loadable_dict() -> None:
    schema = stages_schema()
    assert schema["type"] == "array"
    assert "stage" in schema["$defs"]


# ---------------------------------------------------------------------------
# validate_stages — happy path
# ---------------------------------------------------------------------------


def test_validate_minimum_valid_dag() -> None:
    validate_stages(
        [
            {"name": "a", "run": "echo a"},
            {"name": "b", "run": "echo b", "depends_on": "a"},
        ]
    )


def test_validate_full_field_dag() -> None:
    validate_stages(
        [
            {
                "name": "fit",
                "run": "python fit.py",
                "resources": {
                    "cpus": 4,
                    "mem": "16G",
                    "walltime": "01:00:00",
                    "gpus": 1,
                    "gpu_type": "a100",
                },
                "env": {"modules": "python/3.11", "conda_env": "ml"},
                "env_group": "dl",
                "constraints": {"max_array_size": 100},
                "results": {"pattern": "out/*.json"},
                "runtime": "uv",
                "max_retries": 2,
            },
            {
                "name": "eval",
                "run": "python eval.py",
                "depends_on": ["fit"],
            },
        ]
    )


# ---------------------------------------------------------------------------
# validate_stages — schema rejections
# ---------------------------------------------------------------------------


def test_validate_rejects_missing_name() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_stages([{"run": "echo hi"}])


def test_validate_rejects_missing_run() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_stages([{"name": "a"}])


def test_validate_rejects_unknown_field() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_stages([{"name": "a", "run": "echo a", "unknown_field": 1}])


def test_validate_rejects_invalid_name_pattern() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_stages([{"name": "1bad-start", "run": "echo a"}])


def test_validate_rejects_empty_list() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_stages([])


# ---------------------------------------------------------------------------
# validate_stages — DAG-level checks beyond schema
# ---------------------------------------------------------------------------


def test_validate_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate stage names"):
        validate_stages(
            [
                {"name": "a", "run": "echo a"},
                {"name": "a", "run": "echo a2"},
            ]
        )


def test_validate_rejects_unknown_dependency() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        validate_stages(
            [
                {"name": "a", "run": "echo a", "depends_on": "ghost"},
            ]
        )


def test_validate_rejects_unknown_dependency_in_list() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        validate_stages(
            [
                {"name": "a", "run": "echo a"},
                {"name": "b", "run": "echo b", "depends_on": ["a", "ghost"]},
            ]
        )


# ---------------------------------------------------------------------------
# load_stages_module / load_stages
# ---------------------------------------------------------------------------


def _write_stages_py(tmp_path: Path, body: str) -> Path:
    hpc = tmp_path / ".hpc"
    hpc.mkdir()
    p = hpc / "stages.py"
    p.write_text(body)
    return p


def test_load_stages_module_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_stages_module(tmp_path / ".hpc" / "stages.py")


def test_load_stages_module_without_callable_raises(tmp_path: Path) -> None:
    _write_stages_py(tmp_path, "stages = 'not callable'\n")
    with pytest.raises(AttributeError, match="stages\\(\\) callable"):
        load_stages_module(tmp_path / ".hpc" / "stages.py")


def test_load_stages_returns_validated_list(tmp_path: Path) -> None:
    _write_stages_py(
        tmp_path,
        "def stages():\n"
        "    return [\n"
        "        {'name': 'a', 'run': 'echo a'},\n"
        "        {'name': 'b', 'run': 'echo b', 'depends_on': 'a'},\n"
        "    ]\n",
    )
    result = load_stages(tmp_path)
    assert len(result) == 2
    assert result[0]["name"] == "a"
    assert result[1]["depends_on"] == "a"


def test_load_stages_rejects_non_list_return(tmp_path: Path) -> None:
    _write_stages_py(tmp_path, "def stages():\n    return {'a': {'run': 'echo'}}\n")
    with pytest.raises(TypeError, match="must return list"):
        load_stages(tmp_path)


def test_load_stages_propagates_validation_error(tmp_path: Path) -> None:
    _write_stages_py(
        tmp_path,
        "def stages():\n    return [{'name': 'a', 'run': 'echo a', 'depends_on': 'ghost'}]\n",
    )
    with pytest.raises(ValueError, match="unknown stage"):
        load_stages(tmp_path)


# ---------------------------------------------------------------------------
# Reference fixture validates
# ---------------------------------------------------------------------------


def test_reference_fixture_validates() -> None:
    """``tests/fixtures/stages_example.py`` (the canonical example
    replacing ``hpc_multistage.yaml``) must pass validation."""
    from tests.fixtures.stages_example import stages as fixture_stages

    validate_stages(fixture_stages())
