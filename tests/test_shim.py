"""Tests for the content-addressed shim cache."""

from __future__ import annotations

from hpc_mapreduce.map.shim import (
    SHIM_STAMP_PREFIX,
    load_cached_shim,
    save_shim,
    shim_cache_key,
    stamp_shim,
)


def _fixtures(tmp_path):
    executor = tmp_path / "executor.py"
    executor.write_text("def main():\n    return 100\n")
    template = tmp_path / "template.py"
    template.write_text("# shim template\nTOTAL = ...\n")
    return executor, template


def test_shim_cache_key_is_content_stable(tmp_path):
    executor, template = _fixtures(tmp_path)

    key1 = shim_cache_key(executor, template)
    # Rewriting identical content must not change the key
    executor.write_text(executor.read_text())
    key2 = shim_cache_key(executor, template)

    assert key1 == key2


def test_shim_cache_key_changes_with_executor(tmp_path):
    executor, template = _fixtures(tmp_path)
    key1 = shim_cache_key(executor, template)

    executor.write_text("def main():\n    return 200\n")
    key2 = shim_cache_key(executor, template)

    assert key1 != key2


def test_shim_cache_key_changes_with_template(tmp_path):
    executor, template = _fixtures(tmp_path)
    key1 = shim_cache_key(executor, template)

    template.write_text("# different template\n")
    key2 = shim_cache_key(executor, template)

    assert key1 != key2


def test_load_cached_shim_miss_returns_none(tmp_path):
    cache_dir = tmp_path / "cache"
    assert load_cached_shim(cache_dir, "deadbeef") is None


def test_save_then_load_roundtrip(tmp_path):
    executor, template = _fixtures(tmp_path)
    cache_dir = tmp_path / "cache"
    key = shim_cache_key(executor, template)

    save_shim(
        cache_dir,
        key,
        "print('shim')\n",
        executor_path=executor,
        template_path=template,
    )
    cached = load_cached_shim(cache_dir, key)

    assert cached is not None
    assert cached.is_file()
    contents = cached.read_text()
    assert contents.startswith(f"{SHIM_STAMP_PREFIX}{key}\n")
    assert "print('shim')" in contents
    # Meta sidecar is written
    meta = cached.with_suffix(".meta.json")
    assert meta.is_file()


def test_save_shim_is_idempotent(tmp_path):
    executor, template = _fixtures(tmp_path)
    cache_dir = tmp_path / "cache"
    key = shim_cache_key(executor, template)

    save_shim(cache_dir, key, "print('v1')\n")
    first = load_cached_shim(cache_dir, key).read_text()
    save_shim(cache_dir, key, "print('v1')\n")
    second = load_cached_shim(cache_dir, key).read_text()

    assert first == second


def test_save_shim_materializes_to_target(tmp_path):
    executor, template = _fixtures(tmp_path)
    cache_dir = tmp_path / "cache"
    materialize = tmp_path / "src" / "hpc_chunking_shim.py"
    key = shim_cache_key(executor, template)

    save_shim(
        cache_dir,
        key,
        "print('shim')\n",
        materialize_at=materialize,
    )

    assert materialize.is_file()
    assert materialize.read_text() == load_cached_shim(cache_dir, key).read_text()


def test_stamp_shim_idempotent():
    key = "abc123"
    source = "print('x')\n"
    stamped_once = stamp_shim(source, key)
    stamped_twice = stamp_shim(stamped_once, key)
    assert stamped_once == stamped_twice
    assert stamped_once.startswith(f"{SHIM_STAMP_PREFIX}{key}\n")


def test_stamp_shim_replaces_old_stamp():
    source = "# hpc-shim-key: oldkey\nprint('x')\n"
    new = stamp_shim(source, "newkey")
    assert new.startswith(f"{SHIM_STAMP_PREFIX}newkey\n")
    assert "oldkey" not in new.split("\n")[0]
    assert "print('x')" in new


def test_user_edited_shim_is_detectable_via_stamp(tmp_path):
    """If the on-disk shim has no matching stamp, the command prose says don't overwrite.
    This test confirms the stamp check works as the detection mechanism."""
    executor, template = _fixtures(tmp_path)
    cache_dir = tmp_path / "cache"
    key = shim_cache_key(executor, template)

    save_shim(cache_dir, key, "print('auto')\n")
    cached = load_cached_shim(cache_dir, key)

    user_edited = tmp_path / "user_shim.py"
    user_edited.write_text("# edited by the user\nprint('manual')\n")
    stamp_for_key = f"{SHIM_STAMP_PREFIX}{key}"

    # Cached file starts with the stamp for this key
    assert cached.read_text().startswith(stamp_for_key)
    # User-edited file does not
    assert not user_edited.read_text().startswith(stamp_for_key)


def test_date_window_template_has_distinct_cache_key(tmp_path):
    """shim_cache_key must distinguish the two checked-in templates."""
    from pathlib import Path

    from hpc_mapreduce import shim_cache_key

    repo_root = Path(__file__).parent.parent
    starters = repo_root / "hpc_mapreduce" / "templates" / "starters"
    executor = starters / "executor_template.py"
    chunking = starters / "chunking_shim.py"
    date_window = starters / "date_window_shim.py"

    key_chunking = shim_cache_key(executor, chunking)
    key_date_window = shim_cache_key(executor, date_window)
    assert key_chunking != key_date_window
