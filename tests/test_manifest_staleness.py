"""Tests for content-addressed manifest filenames and staleness handling.

Covers:

* :func:`hpc_mapreduce.job.manifest.manifest_filename_for_sha` — canonical
  filename derivation from ``cmd_sha``.
* :func:`aggregate_cmd_sha` — deterministic run-level hash derived from a
  manifest's per-task ``cmd_sha`` values.
* :func:`write_manifest` — writes the content-addressed file and keeps the
  ``manifest.json`` alias in sync.
* :func:`find_existing_manifests` / :func:`find_manifest_by_cmd_sha` — prior
  run detection.
* :func:`prune_old_manifests` — retention cap behaviour.
* :func:`build_manifest_with_resume` — dispatch between fresh and resume
  paths.
"""

from __future__ import annotations

import json
import os

import pytest

from hpc_mapreduce.job import manifest as manifest_mod
from hpc_mapreduce.job.grid import build_task_manifest
from hpc_mapreduce.job.manifest import (
    MANIFEST_ALIAS,
    MAX_MANIFESTS,
    aggregate_cmd_sha,
    build_manifest_with_resume,
    find_existing_manifests,
    find_manifest_by_cmd_sha,
    manifest_filename_for_sha,
    prune_old_manifests,
    write_manifest,
)


def _small_manifest(seed: str = "a") -> dict:
    """Build a tiny manifest whose contents vary by *seed*."""
    return build_task_manifest(
        f"python train.py --tag {seed}",
        {"lr": [0.01, 0.1]},
        "results/{run_id}",
    )


class TestManifestFilenameForSha:
    def test_basic_filename(self):
        sha = "abcdef0123456789" * 4  # 64 hex chars
        assert manifest_filename_for_sha(sha) == "manifest.abcdef01.json"

    def test_filename_takes_first_8_chars(self):
        sha = "0123456789abcdef"
        assert manifest_filename_for_sha(sha) == "manifest.01234567.json"

    def test_empty_cmd_sha_raises(self):
        with pytest.raises(ValueError):
            manifest_filename_for_sha("")

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            manifest_filename_for_sha("abc")

    def test_non_hex_prefix_raises(self):
        with pytest.raises(ValueError):
            manifest_filename_for_sha("xyzxyzxyz")


class TestAggregateCmdSha:
    def test_deterministic(self):
        m = _small_manifest("a")
        assert aggregate_cmd_sha(m) == aggregate_cmd_sha(m)

    def test_differs_for_different_cmds(self):
        a = _small_manifest("a")
        b = _small_manifest("b")
        assert aggregate_cmd_sha(a) != aggregate_cmd_sha(b)

    def test_order_invariant_on_task_id(self):
        """Dict iteration order shouldn't affect the aggregate — we sort by tid."""
        m = _small_manifest("a")
        reordered = {
            "tasks": dict(reversed(list(m["tasks"].items()))),
        }
        assert aggregate_cmd_sha(m) == aggregate_cmd_sha(reordered)

    def test_requires_cmd_sha_on_every_task(self):
        manifest = {"tasks": {"0": {"cmd": "x"}}}
        with pytest.raises(ValueError, match="cmd_sha"):
            aggregate_cmd_sha(manifest)


class TestWriteManifest:
    def test_writes_content_addressed_file(self, tmp_path):
        m = _small_manifest()
        sha = aggregate_cmd_sha(m)
        target = write_manifest(tmp_path, m, cmd_sha=sha)
        assert target.exists()
        assert target.name == manifest_filename_for_sha(sha)
        loaded = json.loads(target.read_text())
        assert loaded["tasks"] == m["tasks"]

    def test_alias_points_at_latest(self, tmp_path):
        m = _small_manifest()
        target = write_manifest(tmp_path, m)
        alias = tmp_path / MANIFEST_ALIAS
        assert alias.exists()
        # Reading through the alias must yield the same contents.
        assert json.loads(alias.read_text()) == json.loads(target.read_text())

    def test_alias_updates_on_subsequent_write(self, tmp_path):
        first = _small_manifest("a")
        write_manifest(tmp_path, first)
        second = _small_manifest("b")
        second_target = write_manifest(tmp_path, second)
        alias = tmp_path / MANIFEST_ALIAS
        assert json.loads(alias.read_text()) == json.loads(second_target.read_text())

    def test_computes_sha_when_omitted(self, tmp_path):
        m = _small_manifest()
        target = write_manifest(tmp_path, m)
        assert target.name == manifest_filename_for_sha(aggregate_cmd_sha(m))


class TestFindExistingManifests:
    def test_empty_dir_returns_empty(self, tmp_path):
        assert find_existing_manifests(tmp_path) == []

    def test_ignores_alias(self, tmp_path):
        m = _small_manifest()
        write_manifest(tmp_path, m)
        hits = find_existing_manifests(tmp_path)
        names = {p.name for p in hits}
        assert MANIFEST_ALIAS not in names

    def test_newest_first_ordering(self, tmp_path):
        first = _small_manifest("a")
        second = _small_manifest("b")
        third = _small_manifest("c")
        paths = [
            write_manifest(tmp_path, first),
            write_manifest(tmp_path, second),
            write_manifest(tmp_path, third),
        ]
        # Force distinct mtimes so the ordering is unambiguous on fast FS.
        for offset, p in enumerate(paths):
            t = 1_700_000_000 + offset
            os.utime(p, (t, t))
        hits = find_existing_manifests(tmp_path)
        assert [h.name for h in hits] == [paths[2].name, paths[1].name, paths[0].name]


class TestFindManifestByCmdSha:
    def test_match_by_short_prefix(self, tmp_path):
        m = _small_manifest()
        sha = aggregate_cmd_sha(m)
        write_manifest(tmp_path, m, cmd_sha=sha)
        hit = find_manifest_by_cmd_sha(tmp_path, sha)
        assert hit is not None
        assert hit.name == manifest_filename_for_sha(sha)

    def test_no_match_returns_none(self, tmp_path):
        m = _small_manifest()
        write_manifest(tmp_path, m)
        assert find_manifest_by_cmd_sha(tmp_path, "ff" * 16) is None

    def test_bad_sha_returns_none(self, tmp_path):
        assert find_manifest_by_cmd_sha(tmp_path, "") is None


class TestPruneOldManifests:
    def test_keeps_exactly_k_manifests(self, tmp_path):
        # Write MAX_MANIFESTS + 3 manifests with different shas.
        paths = []
        for i in range(MAX_MANIFESTS + 3):
            m = _small_manifest(str(i))
            p = write_manifest(tmp_path, m)
            # Ensure strictly increasing mtimes on fast filesystems.
            t = 1_700_000_000 + i
            os.utime(p, (t, t))
            paths.append(p)
        # write_manifest already prunes — so only MAX_MANIFESTS should remain.
        remaining = find_existing_manifests(tmp_path)
        assert len(remaining) == MAX_MANIFESTS
        # The oldest 3 should be gone.
        for p in paths[:3]:
            assert not p.exists()
        # The newest MAX_MANIFESTS should still be present.
        for p in paths[3:]:
            assert p.exists()

    def test_explicit_keep_argument(self, tmp_path):
        for i in range(5):
            m = _small_manifest(str(i))
            p = write_manifest(tmp_path, m)
            t = 1_700_000_000 + i
            os.utime(p, (t, t))
        deleted = prune_old_manifests(tmp_path, keep=2)
        assert len(deleted) == 3
        assert len(find_existing_manifests(tmp_path)) == 2

    def test_noop_when_under_cap(self, tmp_path):
        m = _small_manifest()
        write_manifest(tmp_path, m)
        deleted = prune_old_manifests(tmp_path, keep=MAX_MANIFESTS)
        assert deleted == []

    def test_negative_keep_raises(self, tmp_path):
        with pytest.raises(ValueError):
            prune_old_manifests(tmp_path, keep=-1)


class TestBuildManifestWithResume:
    def test_none_resume_returns_manifest_unchanged(self):
        m = _small_manifest()
        assert build_manifest_with_resume(m, resume_from=None) is m

    def test_resume_requires_failed_ids(self, tmp_path):
        m = _small_manifest()
        path = write_manifest(tmp_path, m)
        with pytest.raises(ValueError, match="failed_task_ids"):
            build_manifest_with_resume({}, resume_from=path, failed_task_ids=[])

    def test_resume_path_must_exist(self, tmp_path):
        missing = tmp_path / "nope.json"
        with pytest.raises(FileNotFoundError):
            build_manifest_with_resume({}, resume_from=missing, failed_task_ids=[1])

    def test_resume_produces_resubmit_plan(self, tmp_path):
        """Resume path delegates to resubmit_plan on the prior manifest."""
        from hpc_mapreduce.job.resubmit import ResubmitPlan

        # build_task_manifest uses 0-based task IDs; resubmit_plan uses
        # arbitrary string-keyed IDs present in the manifest. Build a
        # manifest whose tasks are keyed with 1-based string IDs so we
        # can exercise the resume path with "failed" IDs 1 and 2.
        prior = {
            "schema_version": 2,
            "total_tasks": 3,
            "tasks": {
                "1": {"cmd": "a", "result_dir": "r/1", "cmd_sha": "aa" * 8},
                "2": {"cmd": "b", "result_dir": "r/2", "cmd_sha": "bb" * 8},
                "3": {"cmd": "c", "result_dir": "r/3", "cmd_sha": "cc" * 8},
            },
        }
        path = tmp_path / "manifest.deadbeef.json"
        path.write_text(json.dumps(prior))
        plan = build_manifest_with_resume(
            manifest={},
            resume_from=path,
            failed_task_ids=[1, 2],
        )
        assert isinstance(plan, ResubmitPlan)
        assert plan.total_tasks == 2


class TestMaxManifestsConstant:
    def test_default_is_ten(self):
        # Intentional: a simple, visible default that matches docs/contract.
        assert MAX_MANIFESTS == 10

    def test_monkeypatchable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(manifest_mod, "MAX_MANIFESTS", 2)
        for i in range(5):
            m = _small_manifest(str(i))
            p = write_manifest(tmp_path, m)
            t = 1_700_000_000 + i
            os.utime(p, (t, t))
        remaining = find_existing_manifests(tmp_path)
        assert len(remaining) == 2
