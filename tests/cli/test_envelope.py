"""Subset of the CLI smoke tests, split out from the previously
~1380-LOC ``test_agent_cli.py`` for navigability.

Shared subprocess + envelope helpers live in :mod:`._helpers`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hpc import agent_cli as cli

from ._helpers import SUBMIT_SPEC, parse_envelope as _parse_envelope, run_cli as _run_cli

"""Smoke tests for the hpc-mapreduce CLI.

The CLI is a public surface MARs depends on — these tests pin the JSON
envelope shape, exit codes, and the error-classification path. They do NOT
exercise actual SSH/cluster operations; the atomic-ops tests in
test_runner.py cover that.
"""


import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hpc import agent_cli as cli






# ─── envelope shape ────────────────────────────────────────────────────────


def test_help_lists_every_subcommand() -> None:
    """--help output must surface every subcommand the test suite exercises."""
    rc, out, _ = _run_cli("--help")
    assert rc == 0
    for cmd in (
        "capabilities",
        "preflight",
        "discover",
        "clusters",
        "list-in-flight",
        "status",
        "submit",
        "aggregate",
        "resubmit",
        "reconcile",
        "build-executor",
        "inspect-cluster",
        "runtime-prior",
        "plan-submit",
    ):
        assert cmd in out, f"--help missing subcommand {cmd!r}"


def test_version_flag() -> None:
    rc, out, _ = _run_cli("--version")
    assert rc == 0
    assert "hpc-mapreduce" in out


def test_capabilities_envelope_shape() -> None:
    """Capabilities is the introspection contract; pin its data shape."""
    rc, out, _ = _run_cli("capabilities")
    assert rc == 0
    env = _parse_envelope(out)
    assert env["ok"] is True
    assert env["idempotent"] is True
    data = env["data"]
    assert isinstance(data["version"], str)
    assert isinstance(data["subcommands"], list)
    assert "submit" in data["subcommands"]
    assert "status" in data["subcommands"]
    assert "preflight" in data["subcommands"]
    # A7: derived from argparse, so newly-added cmd_walltime_drift /
    # cmd_house_edge subcommands appear automatically.
    assert "walltime-drift" in data["subcommands"]
    assert "house-edge" in data["subcommands"]
    assert data["supported_schedulers"] == ["sge", "slurm"]
    assert isinstance(data["ssh_multiplexing"], bool)


def test_capabilities_exposes_mars_skill_paths_and_required_env() -> None:
    """Programmatic introspection for MARs: skill paths + required env."""
    rc, out, _ = _run_cli("capabilities")
    assert rc == 0
    data = _parse_envelope(out)["data"]

    skill_paths = data["mars_skill_paths"]
    assert isinstance(skill_paths, dict)
    # Source-tree installs ship five skills; wheel-only installs may ship
    # zero. Either is acceptable, but every value must point to a real file.
    for name, path in skill_paths.items():
        assert name.startswith("hpc-"), name
        assert Path(path).is_file(), f"{name} path does not exist: {path}"
        assert path.endswith(f"skills/{name}/SKILL.md")

    assert data["required_env"] == [
        "SSH_AUTH_SOCK",
        "HPC_JOURNAL_DIR",
        "HPC_CLUSTERS_CONFIG",
    ]


def test_capabilities_exposes_cluster_yaml_keys() -> None:
    """B-M4: capabilities surfaces a declarative manifest of every
    recognised per-cluster yaml key so a campus user learning the
    schema by inspection sees the new survival fields (nfs_data_dir,
    cold_start_mem_buffer, max_walltime_sec, ...) without reading
    claude_hpc/infra/clusters.py source."""
    rc, out, _ = _run_cli("capabilities")
    assert rc == 0
    data = _parse_envelope(out)["data"]

    keys = data.get("cluster_yaml_keys")
    assert isinstance(keys, list) and keys, "cluster_yaml_keys must be a non-empty list"
    # Each entry has the documented shape.
    for entry in keys:
        assert isinstance(entry, dict), entry
        for required in ("key", "type", "required", "description"):
            assert required in entry, f"{entry!r} missing {required}"
        assert isinstance(entry["required"], bool)

    # The new survival fields must be discoverable here.
    names = {entry["key"] for entry in keys}
    for expected in ("nfs_data_dir", "cold_start_mem_buffer", "max_walltime_sec"):
        assert expected in names, f"cluster_yaml_keys missing {expected!r}; got {sorted(names)}"


def test_clusters_list_returns_known_clusters() -> None:
    """clusters list must return the names defined in the active clusters.yaml."""
    rc, out, _ = _run_cli("clusters", "list")
    assert rc == 0
    env = _parse_envelope(out)
    assert env["ok"] is True
    names = [c["name"] for c in env["data"]["clusters"]]
    assert names, "no clusters defined; check hpc_mapreduce/config/clusters.yaml"


# ─── error envelope shape and exit codes ───────────────────────────────────


def test_unknown_cluster_returns_user_error() -> None:
    rc, out, _ = _run_cli("clusters", "describe", "definitely-not-a-real-cluster")
    assert rc == 1, "user errors must exit 1"
    env = _parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "cluster_unknown"
    assert env["category"] == "user"
    assert env["retry_safe"] is False
    assert "remediation" in env, "every error must include actionable remediation"


def test_malformed_spec_returns_user_error(tmp_path: Path) -> None:
    """An unparseable --spec file must surface as config_invalid, not crash."""
    spec = tmp_path / "bad.json"
    spec.write_text("not json {")
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        "--dry-run",
    )
    assert rc == 1
    env = _parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "config_invalid"
    assert env["category"] == "user"


def test_missing_spec_required_field_returns_user_error(tmp_path: Path) -> None:
    spec = tmp_path / "incomplete.json"
    spec.write_text(json.dumps({"profile": "x"}))  # missing required fields
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
    )
    assert rc == 1
    env = _parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"



# ─── envelope schema validation (structural) ───────────────────────────────


def test_every_envelope_has_required_keys() -> None:
    """Smoke check: every successful envelope has ok/idempotent/data."""
    for argv in (["capabilities"], ["clusters", "list"]):
        rc, out, _ = _run_cli(*argv)
        assert rc == 0
        env = _parse_envelope(out)
        assert set(env.keys()) >= {"ok", "idempotent", "data"}


def test_internal_main_function_returns_zero_on_capabilities() -> None:
    """The cli.main() entry can be called in-process for fast tests."""
    rc = cli.main(["capabilities"])
    assert rc == 0


# ─── CLI help text quality (LLM-readable) ──────────────────────────────────


@pytest.mark.parametrize(
    "subcommand",
    ["submit", "status", "aggregate", "preflight", "build-executor"],
)
def test_subcommand_help_is_non_empty(subcommand: str) -> None:
    """Every subcommand's --help must produce non-empty output (LLMs read this)."""
    rc, out, _ = _run_cli(subcommand, "--help")
    assert rc == 0
    assert len(out.strip()) > 50, f"{subcommand} --help is too sparse"



# ─── Bug 16: spec validation surfaces a schema-pointed message ────────────


def test_submit_spec_with_wrong_type_fails_with_schema_message(tmp_path: Path) -> None:
    """``total_tasks: "five"`` would previously fall through to ``int()``
    and raise a Python traceback.  Schema validation now flags it before
    dispatch with an actionable message.
    """
    import os

    bad_spec = {**SUBMIT_SPEC, "total_tasks": "five"}  # type: ignore[dict-item]
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(bad_spec))
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(tmp_path / "j")}

    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc != 0
    payload = _parse_envelope(out)
    assert payload["error_code"] == "spec_invalid"


# ─── Bug 17: cmd_resubmit rejects categories outside the documented enum ─


def test_resubmit_rejects_off_enum_category(tmp_path: Path) -> None:
    """The resubmit input schema constrains category to seven values; the
    CLI used to accept any string and silently record garbage in the
    journal.
    """
    import os

    spec = tmp_path / "rs.json"
    spec.write_text(
        json.dumps(
            {
                "failed_task_ids": [1],
                "category": "totally_made_up",
            }
        )
    )
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(tmp_path / "j")}

    rc, out, _ = _run_cli(
        "resubmit",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "doesnt_matter",
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc != 0
    payload = _parse_envelope(out)
    assert payload["error_code"] == "spec_invalid"


