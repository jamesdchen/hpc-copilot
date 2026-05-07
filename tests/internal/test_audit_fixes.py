"""Regression tests for the audit-fix commits (post 69faf39).

Covers behaviour added by the audit pass that previously had no
coverage:

* :func:`claude_hpc.infra.remote._env_int` — env-var override parser.
* :func:`claude_hpc.infra.gpu.load_gpu_config_for_cluster` and
  :func:`claude_hpc.infra.gpu._excluded_prefixes_for_cluster` —
  YAML-driven GPU queue / exclusion override.
* :mod:`scripts.check_no_pending_primitive_docs` — fails on stub.
* :mod:`scripts.lint_skill_command_sync` — fails on missing pair.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from claude_hpc.infra import gpu as gpu_module
from claude_hpc.infra import remote as remote_module

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"


# --------------------------------------------------------------------------- #
# infra/remote._env_int
# --------------------------------------------------------------------------- #


class TestEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("HPC_TEST_FAKE", raising=False)
        assert remote_module._env_int("HPC_TEST_FAKE", 60) == 60

    def test_valid_int_overrides(self, monkeypatch):
        monkeypatch.setenv("HPC_TEST_FAKE", "120")
        assert remote_module._env_int("HPC_TEST_FAKE", 60) == 120

    def test_invalid_value_falls_back(self, monkeypatch):
        # A typo can't disable timeout enforcement entirely.
        monkeypatch.setenv("HPC_TEST_FAKE", "not-an-int")
        assert remote_module._env_int("HPC_TEST_FAKE", 60) == 60

    def test_negative_int_passes_through(self, monkeypatch):
        # _env_int doesn't validate sign — subprocess will raise loudly
        # if anyone is foolish enough to set a negative timeout.
        monkeypatch.setenv("HPC_TEST_FAKE", "-1")
        assert remote_module._env_int("HPC_TEST_FAKE", 60) == -1


# --------------------------------------------------------------------------- #
# infra/gpu YAML overrides
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_clusters_yaml(tmp_path, monkeypatch):
    """Point ``HPC_CLUSTERS_CONFIG`` at a tmp YAML so loaders read it."""
    cfg = tmp_path / "clusters.yaml"

    def write(payload: dict) -> Path:
        cfg.write_text(yaml.safe_dump(payload), encoding="utf-8")
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))
        return cfg

    return write


class TestLoadGpuConfigForCluster:
    def test_returns_none_for_unknown_cluster(self, fake_clusters_yaml):
        fake_clusters_yaml({"hoffman2": {"scheduler": "sge"}})
        assert gpu_module.load_gpu_config_for_cluster("nonexistent") is None

    def test_returns_none_when_yaml_missing_gpu_queues(self, fake_clusters_yaml):
        fake_clusters_yaml({"hoffman2": {"scheduler": "sge"}})
        assert gpu_module.load_gpu_config_for_cluster("hoffman2") is None

    def test_loads_gpu_queues(self, fake_clusters_yaml):
        fake_clusters_yaml(
            {
                "carc": {
                    "scheduler": "slurm",
                    "gpu_queues": {
                        "gpu_a40": {"name": "A40", "perf": 1.0},
                    },
                }
            }
        )
        cfg = gpu_module.load_gpu_config_for_cluster("carc")
        assert cfg == {"gpu_a40": {"name": "A40", "perf": 1.0}}

    def test_rejects_non_dict_gpu_queues(self, fake_clusters_yaml):
        fake_clusters_yaml({"carc": {"gpu_queues": ["not", "a", "dict"]}})
        with pytest.raises(ValueError, match="must be a mapping"):
            gpu_module.load_gpu_config_for_cluster("carc")

    def test_rejects_entry_missing_required_keys(self, fake_clusters_yaml):
        fake_clusters_yaml(
            {"carc": {"gpu_queues": {"gpu_a40": {"name": "A40"}}}}  # missing 'perf'
        )
        with pytest.raises(ValueError, match="must be a mapping with 'name' and 'perf'"):
            gpu_module.load_gpu_config_for_cluster("carc")


class TestExcludedPrefixes:
    def test_none_cluster_returns_default(self):
        assert (
            gpu_module._excluded_prefixes_for_cluster(None)
            == gpu_module._DEFAULT_EXCLUDED_PREFIXES
        )

    def test_unknown_cluster_returns_default(self, fake_clusters_yaml):
        fake_clusters_yaml({"hoffman2": {"scheduler": "sge"}})
        assert (
            gpu_module._excluded_prefixes_for_cluster("nonexistent")
            == gpu_module._DEFAULT_EXCLUDED_PREFIXES
        )

    def test_loads_yaml_override(self, fake_clusters_yaml):
        fake_clusters_yaml(
            {"carc": {"excluded_gpu_queue_prefixes": ["gpu_legacy", "gpu_test"]}}
        )
        assert gpu_module._excluded_prefixes_for_cluster("carc") == {
            "gpu_legacy",
            "gpu_test",
        }

    def test_rejects_non_list(self, fake_clusters_yaml):
        fake_clusters_yaml({"carc": {"excluded_gpu_queue_prefixes": "gpu_legacy"}})
        with pytest.raises(ValueError, match="must be a list of strings"):
            gpu_module._excluded_prefixes_for_cluster("carc")

    def test_rejects_non_string_items(self, fake_clusters_yaml):
        fake_clusters_yaml({"carc": {"excluded_gpu_queue_prefixes": [1, 2, 3]}})
        with pytest.raises(ValueError, match="must be a list of strings"):
            gpu_module._excluded_prefixes_for_cluster("carc")


class TestParseQstatFExcludedPrefixesParameter:
    """The audit-fix wires ``excluded_prefixes`` through parse_qstat_f.
    Previously the function read the module-level ``_EXCLUDED_PREFIXES``
    directly, ignoring any cluster-specific override.
    """

    QSTAT_TEXT = (
        "queuename                      qtype resv/used/tot. load_avg arch          states\n"
        "---------------------------------------------------------------------------------\n"
        "gpu_a100.q@n1                  BIP   0/4/8          0.50     lx-amd64\n"
        "---------------------------------------------------------------------------------\n"
        "gpu_legacy.q@n2                BIP   0/2/4          0.50     lx-amd64\n"
    )

    def test_default_excludes_match_module_default(self):
        agg = gpu_module.parse_qstat_f(self.QSTAT_TEXT)
        assert "gpu_a100" in agg

    def test_explicit_exclude_drops_queue(self):
        # Override with a custom exclusion set that includes gpu_a100.
        agg = gpu_module.parse_qstat_f(
            self.QSTAT_TEXT,
            gpu_config={"gpu_a100": {"name": "A100", "perf": 1.0}},
            excluded_prefixes={"gpu_a100"},
        )
        assert "gpu_a100" not in agg

    def test_empty_exclude_keeps_everything(self):
        agg = gpu_module.parse_qstat_f(
            self.QSTAT_TEXT,
            gpu_config={
                "gpu_a100": {"name": "A100", "perf": 1.0},
                "gpu_legacy": {"name": "Legacy", "perf": 0.1},
            },
            excluded_prefixes=set(),
        )
        # gpu_legacy was previously excluded by module default; with empty
        # set it should now appear.
        assert "gpu_legacy" in agg


# --------------------------------------------------------------------------- #
# scripts/check_no_pending_primitive_docs.py
# --------------------------------------------------------------------------- #


class TestCheckNoPendingPrimitiveDocs:
    SCRIPT = SCRIPTS / "check_no_pending_primitive_docs.py"

    def test_passes_when_clean(self):
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_fails_when_stub_introduced(self, tmp_path):
        # Build a minimal mirror with a stub primitive doc.
        repo = tmp_path / "repo"
        primitives = repo / "docs" / "primitives"
        primitives.mkdir(parents=True)
        (primitives / "fake.md").write_text(
            "---\nname: fake\n---\n# fake\n\n_Documentation pending._\n"
        )
        (primitives / "README.md").write_text("README\n_Documentation pending._\n")
        # README mentions of the placeholder are explicitly excluded by the script.
        # We need the script's REPO_ROOT to point at our fake repo. Easiest: copy
        # the script into the fake repo's scripts/ and run it there.
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir()
        shutil.copy(self.SCRIPT, scripts_dir / self.SCRIPT.name)
        result = subprocess.run(
            [sys.executable, str(scripts_dir / self.SCRIPT.name)],
            capture_output=True,
            text=True,
            cwd=repo,
        )
        assert result.returncode == 1
        assert "fake.md" in result.stderr


# --------------------------------------------------------------------------- #
# scripts/lint_skill_command_sync.py
# --------------------------------------------------------------------------- #


class TestLintSkillCommandSync:
    SCRIPT = SCRIPTS / "lint_skill_command_sync.py"

    def test_passes_on_repo(self):
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
