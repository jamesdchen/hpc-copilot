"""MARs-layout integration for executor discovery.

Covers:

* ``discover_executors`` honors MARs's modules-only ``src/`` contract when
  ``meta.json`` is present at the experiment-dir root — modules under
  ``src/`` must NOT be reported as executors.
* The Tier-1 ``probe.py`` at experiment-dir root is still discoverable.
* Default behavior is unchanged when no ``meta.json`` is present.
* ``detect_mars_tier`` infers Tier-1 / Tier-2 from path layout + markers.
* ``read_meta_json`` is tolerant of missing/malformed files.
* The ``hpc-mapreduce discover`` envelope surfaces a ``meta`` block when
  meta.json is present at the experiment-dir root.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

from claude_hpc.orchestrator.discover import (
    detect_mars_tier,
    discover_executors,
    read_meta_json,
)

if TYPE_CHECKING:
    from pathlib import Path

_EXEC_SRC = (
    "import argparse\n"
    "def main():\n"
    "    argparse.ArgumentParser().parse_args()\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)


def _write_executor(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_EXEC_SRC, encoding="utf-8")


def _mars_meta(root: Path) -> None:
    (root / "meta.json").write_text(
        '{"experiment_id": "x", "seed": 42, "purpose": "t"}\n', encoding="utf-8"
    )


# ─── Item 1: MARs layout filter ───────────────────────────────────────────


class TestMarsLayoutFilter:
    def test_meta_present_skips_src(self, tmp_path: Path) -> None:
        _mars_meta(tmp_path)
        _write_executor(tmp_path / "src" / "foo.py")
        assert discover_executors(tmp_path) == []

    def test_meta_present_finds_scripts(self, tmp_path: Path) -> None:
        _mars_meta(tmp_path)
        _write_executor(tmp_path / "scripts" / "run.py")
        infos = discover_executors(tmp_path)
        assert [i.name for i in infos] == ["run"]

    def test_meta_present_finds_probe_at_root(self, tmp_path: Path) -> None:
        _mars_meta(tmp_path)
        _write_executor(tmp_path / "probe.py")
        infos = discover_executors(tmp_path)
        assert [i.name for i in infos] == ["probe"]

    def test_meta_absent_finds_src(self, tmp_path: Path) -> None:
        _write_executor(tmp_path / "src" / "legacy.py")
        infos = discover_executors(tmp_path)
        assert [i.name for i in infos] == ["legacy"]

    def test_explicit_search_dirs_overrides_default(self, tmp_path: Path) -> None:
        _mars_meta(tmp_path)
        _write_executor(tmp_path / "src" / "foo.py")
        infos = discover_executors(tmp_path, search_dirs=("src",))
        assert [i.name for i in infos] == ["foo"]


# ─── Item 2: Tier detection ───────────────────────────────────────────────


class TestDetectMarsTier:
    def test_tier1_probe_with_marker(self, tmp_path: Path) -> None:
        probe = tmp_path / "probes" / "probe-001-foo"
        probe.mkdir(parents=True)
        (probe / "probe.py").write_text("# probe\n", encoding="utf-8")
        assert detect_mars_tier(probe) == 1

    def test_tier2_run_with_scripts(self, tmp_path: Path) -> None:
        run = tmp_path / "runs" / "run-001-bar"
        (run / "scripts").mkdir(parents=True)
        assert detect_mars_tier(run) == 2

    def test_arbitrary_path_returns_none(self, tmp_path: Path) -> None:
        d = tmp_path / "random"
        d.mkdir()
        assert detect_mars_tier(d) is None

    def test_probe_path_without_marker_is_none(self, tmp_path: Path) -> None:
        probe = tmp_path / "probes" / "probe-002-baz"
        probe.mkdir(parents=True)
        # no probe.py
        assert detect_mars_tier(probe) is None


# ─── Item 3: read_meta_json ───────────────────────────────────────────────


class TestReadMetaJson:
    def test_missing_returns_none(self, tmp_path: Path) -> None:
        assert read_meta_json(tmp_path) is None

    def test_valid_returns_dict(self, tmp_path: Path) -> None:
        (tmp_path / "meta.json").write_text(
            '{"experiment_id": "x", "seed": 42}\n', encoding="utf-8"
        )
        out = read_meta_json(tmp_path)
        assert out == {"experiment_id": "x", "seed": 42}

    def test_malformed_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "meta.json").write_text("{not json", encoding="utf-8")
        assert read_meta_json(tmp_path) is None

    def test_non_object_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "meta.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert read_meta_json(tmp_path) is None


# ─── Item 3: discover envelope `meta` block ───────────────────────────────


def _run_discover(experiment_dir: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "claude_hpc", "discover", "--experiment-dir", str(experiment_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert len(lines) == 1
    env = json.loads(lines[0])
    assert env["ok"] is True
    return env["data"]


class TestDiscoverEnvelopeMeta:
    def test_envelope_includes_meta_when_present(self, tmp_path: Path) -> None:
        run = tmp_path / "runs" / "run-001-foo"
        (run / "scripts").mkdir(parents=True)
        (run / "meta.json").write_text(
            json.dumps(
                {
                    "experiment_id": "run-001-foo",
                    "seed": 42,
                    "purpose": "ridge",
                }
            ),
            encoding="utf-8",
        )
        _write_executor(run / "scripts" / "train.py")
        data = _run_discover(run)
        assert data["meta"]["experiment_id"] == "run-001-foo"
        assert data["meta"]["seed"] == 42
        assert data["meta"]["purpose"] == "ridge"
        assert data["meta"]["tier"] == 2

    def test_envelope_meta_tier_for_tier1(self, tmp_path: Path) -> None:
        probe = tmp_path / "probes" / "probe-001-bar"
        probe.mkdir(parents=True)
        (probe / "meta.json").write_text(
            json.dumps({"experiment_id": "probe-001-bar"}), encoding="utf-8"
        )
        _write_executor(probe / "probe.py")
        data = _run_discover(probe)
        assert data["meta"]["tier"] == 1

    def test_envelope_omits_meta_when_absent(self, tmp_path: Path) -> None:
        _write_executor(tmp_path / "executors" / "exec.py")
        data = _run_discover(tmp_path)
        assert "meta" not in data

    def test_envelope_omits_meta_when_malformed(self, tmp_path: Path) -> None:
        _write_executor(tmp_path / "scripts" / "run.py")
        (tmp_path / "meta.json").write_text("{not json", encoding="utf-8")
        data = _run_discover(tmp_path)
        assert "meta" not in data
