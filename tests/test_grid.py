"""Tests for hpc_mapreduce.job.grid — grid expansion and task manifests."""

from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime, timezone

import pytest

from hpc_mapreduce.job.grid import (
    MANIFEST_SCHEMA_VERSION,
    build_task_manifest,
    expand_grid,
    resolve_git_sha,
    total_tasks,
    validate_grid_keys,
    validate_result_dir_template,
)


def _git_init_with_commit(repo: object) -> None:
    """Initialize *repo* as a git repo with a single empty commit.

    Disables GPG signing to keep tests hermetic (CI environments may have
    commit-signing hooks that reject tests with a bogus signing key).
    """
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "--no-gpg-sign",
            "-q",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
    )


class TestExpandGrid:
    def test_single_dimension(self):
        points = expand_grid({"model": ["a", "b"]})
        assert points == [{"model": "a"}, {"model": "b"}]

    def test_cartesian_product(self):
        points = expand_grid({"x": [1, 2], "y": ["a", "b"]})
        assert len(points) == 4
        assert {"x": "1", "y": "a"} in points
        assert {"x": "2", "y": "b"} in points


class TestBuildTaskManifest:
    def test_grid_only(self):
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01, 0.1]},
            "results/{run_id}",
        )
        assert m["total_tasks"] == 2
        assert "--lr 0.01" in m["tasks"]["0"]["cmd"]
        assert "--lr 0.1" in m["tasks"]["1"]["cmd"]

    def test_result_dir_per_grid_point(self):
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf", "xgb"]},
            "results/{run_id}",
        )
        assert m["tasks"]["0"]["result_dir"] != m["tasks"]["1"]["result_dir"]


def test_build_task_manifest_shell_quotes_grid_values_with_spaces():
    """Grid values containing spaces/shell metachars must round-trip through
    shlex.split — the remote dispatcher runs cmd with shell=True, so naive
    interpolation would word-split values like a date range."""
    m = build_task_manifest(
        "python -m foo",
        {"range": ["2024-01-01 to 2024-12-31"]},
        "results/{run_id}",
    )
    assert shlex.split(m["tasks"]["0"]["cmd"]) == [
        "python",
        "-m",
        "foo",
        "--range",
        "2024-01-01 to 2024-12-31",
    ]


class TestTotalTasks:
    def test_simple(self):
        assert total_tasks({"a": [1, 2], "b": [3, 4, 5]}) == 6


class TestBuildTaskManifestMaxTasks:
    def test_raises_when_grid_exceeds_max_tasks(self):
        # 6 total tasks, ceiling of 5 -> ValueError before any tasks are materialized.
        with pytest.raises(ValueError, match=r"max_tasks=5"):
            build_task_manifest(
                "python train.py",
                {"a": [1, 2, 3], "b": [10, 20]},
                "results/{run_id}",
                max_tasks=5,
            )

    def test_disabled_with_none_allows_large_grid(self):
        # 12 total tasks; with max_tasks=None the check is skipped.
        m = build_task_manifest(
            "python train.py",
            {"a": list(range(4)), "b": list(range(3))},
            "results/{run_id}",
            max_tasks=None,
        )
        assert m["total_tasks"] == 12

    def test_raised_threshold_allows_large_grid(self):
        # Same 12 tasks, explicit higher threshold.
        m = build_task_manifest(
            "python train.py",
            {"a": list(range(4)), "b": list(range(3))},
            "results/{run_id}",
            max_tasks=100,
        )
        assert m["total_tasks"] == 12


class TestBuildTaskManifestSchemaVersion:
    def test_schema_version_embedded(self):
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01, 0.1]},
            "results/{run_id}",
        )
        assert m["schema_version"] == MANIFEST_SCHEMA_VERSION


class TestResultDirTemplating:
    def test_run_id_placeholder_still_works(self):
        """Back-compat: the original {run_id}-only template must continue to work."""
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf", "xgb"]},
            "results/{run_id}",
        )
        assert m["tasks"]["0"]["result_dir"] == "results/rf"
        assert m["tasks"]["1"]["result_dir"] == "results/xgb"

    def test_date_placeholder_is_utc_today(self):
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf"]},
            "results/{date}/{run_id}",
        )
        expected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert m["tasks"]["0"]["result_dir"] == f"results/{expected_date}/rf"

    def test_git_sha_placeholder_resolves(self, tmp_path, monkeypatch):
        """When git is available in repo_path, {git_sha} resolves to 7 hex chars."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_with_commit(repo)
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf"]},
            "results/{git_sha}/{run_id}",
            repo_path=repo,
        )
        result = m["tasks"]["0"]["result_dir"]
        match = re.match(r"^results/([0-9a-f]{7})/rf$", result)
        assert match, f"unexpected result_dir: {result!r}"

    def test_git_sha_falls_back_to_nogit_outside_repo(self, tmp_path):
        """Non-git directory must yield the literal 'nogit' string."""
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf"]},
            "results/{git_sha}/{run_id}",
            repo_path=tmp_path,
        )
        assert m["tasks"]["0"]["result_dir"] == "results/nogit/rf"

    def test_grid_key_placeholder_varies_per_task(self):
        """A grid-point key in the template must vary per task."""
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf", "xgb"], "dataset": ["a", "b"]},
            "results/{model}/{dataset}",
        )
        seen = {m["tasks"][tid]["result_dir"] for tid in m["tasks"]}
        assert seen == {
            "results/rf/a",
            "results/rf/b",
            "results/xgb/a",
            "results/xgb/b",
        }

    def test_mixed_run_level_and_grid_placeholder(self):
        m = build_task_manifest(
            "python train.py",
            {"model": ["rf", "xgb"]},
            "results/{date}/{model}/{run_id}",
        )
        expected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert m["tasks"]["0"]["result_dir"] == f"results/{expected_date}/rf/rf"
        assert m["tasks"]["1"]["result_dir"] == f"results/{expected_date}/xgb/xgb"

    def test_unknown_placeholder_raises(self):
        """Placeholders not in the grid or run-level set must raise."""
        with pytest.raises(ValueError, match="unknown placeholder"):
            build_task_manifest(
                "python train.py",
                {"model": ["rf"]},
                "results/{unknown_key}/{run_id}",
            )

    def test_unknown_placeholder_error_lists_valid_keys(self):
        """Error message must enumerate the valid keys to help users."""
        with pytest.raises(ValueError) as exc:
            build_task_manifest(
                "python train.py",
                {"model": ["rf"], "lr": [0.1]},
                "results/{notakey}",
            )
        msg = str(exc.value)
        # Run-level names must appear in the valid list.
        assert "run_id" in msg
        assert "date" in msg
        assert "git_sha" in msg
        # Grid keys must appear.
        assert "model" in msg and "lr" in msg
        # The bad name must be called out.
        assert "notakey" in msg


class TestValidateResultDirTemplate:
    def test_noop_on_valid_template(self):
        validate_result_dir_template("r/{run_id}/{model}", {"model": ["a"]})

    def test_no_placeholders_is_ok(self):
        validate_result_dir_template("results", {})

    def test_multiple_missing_keys_collected(self):
        with pytest.raises(ValueError) as exc:
            validate_result_dir_template("{a}/{b}/{run_id}", {"model": ["x"]})
        msg = str(exc.value)
        assert "'a'" in msg and "'b'" in msg


class TestValidateGridKeys:
    def test_valid_keys_no_raise(self):
        validate_grid_keys({"horizon": [1], "model": ["a"]})

    def test_empty_grid(self):
        validate_grid_keys({})

    def test_underscore_keys_ok(self):
        validate_grid_keys({"_foo": [1], "bar_baz": [2]})

    def test_leading_digit_raises(self):
        with pytest.raises(ValueError, match=r"1bad"):
            validate_grid_keys({"1bad": [1]})

    def test_hyphen_raises(self):
        with pytest.raises(ValueError, match=r"foo-bar"):
            validate_grid_keys({"foo-bar": [1]})

    def test_dot_raises(self):
        with pytest.raises(ValueError, match=r"a\.b"):
            validate_grid_keys({"a.b": [1]})

    def test_space_raises(self):
        with pytest.raises(ValueError):
            validate_grid_keys({"k k": [1]})

    def test_multiple_invalid_listed(self):
        with pytest.raises(ValueError) as exc:
            validate_grid_keys({"1x": [1], "y-z": [2]})
        msg = str(exc.value)
        assert "1x" in msg and "y-z" in msg


def test_build_task_manifest_rejects_invalid_grid_key():
    """build_task_manifest must reject malformed grid keys before doing other work."""
    with pytest.raises(ValueError):
        build_task_manifest(
            "python train.py",
            {"1bad": [1]},
            "results/{run_id}",
        )


class TestResolveGitSha:
    def test_nogit_outside_repo(self, tmp_path):
        assert resolve_git_sha(tmp_path) == "nogit"

    def test_short_sha_in_repo(self, tmp_path):
        _git_init_with_commit(tmp_path)
        sha = resolve_git_sha(tmp_path)
        assert re.fullmatch(r"[0-9a-f]{7}", sha), f"unexpected sha: {sha!r}"


class TestBuildTaskManifestRuntime:
    def test_uv_prefixes_every_task_cmd(self):
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01, 0.1]},
            "results/{run_id}",
            runtime="uv",
        )
        for task in m["tasks"].values():
            assert task["cmd"].startswith("uv run "), task["cmd"]
            assert "python train.py" in task["cmd"]

    def test_default_runtime_unchanged_cmds(self):
        """Back-compat: with no runtime, task cmds match the historical shape."""
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01]},
            "results/{run_id}",
        )
        cmd = m["tasks"]["0"]["cmd"]
        assert not cmd.startswith("uv run ")
        assert cmd.startswith("python train.py")

    def test_manifest_top_level_runtime_set_when_uv(self):
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01]},
            "results/{run_id}",
            runtime="uv",
        )
        assert m.get("runtime") == "uv"

    def test_manifest_top_level_runtime_omitted_when_none(self):
        m = build_task_manifest(
            "python train.py",
            {"lr": [0.01]},
            "results/{run_id}",
        )
        assert "runtime" not in m

    def test_unknown_runtime_raises_valueerror(self):
        with pytest.raises(ValueError, match="runtime="):
            build_task_manifest(
                "python train.py",
                {"lr": [0.01]},
                "results/{run_id}",
                runtime="bogus",
            )

    def test_uv_changes_cmd_sha(self):
        """cmd_sha is derived from cmd, so the prefix must propagate."""
        plain = build_task_manifest(
            "python train.py",
            {"lr": [0.01]},
            "results/{run_id}",
        )
        with_uv = build_task_manifest(
            "python train.py",
            {"lr": [0.01]},
            "results/{run_id}",
            runtime="uv",
        )
        assert plain["tasks"]["0"]["cmd_sha"] != with_uv["tasks"]["0"]["cmd_sha"]
