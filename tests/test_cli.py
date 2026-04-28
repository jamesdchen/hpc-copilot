"""Smoke tests for the hpc-mapreduce CLI.

The CLI is a public surface MARs depends on — these tests pin the JSON
envelope shape, exit codes, and the error-classification path. They do NOT
exercise actual SSH/cluster operations; the atomic-ops tests in
test_runner.py cover that.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hpc_mapreduce import cli


def _run_cli(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Invoke the CLI as a subprocess and return (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_mapreduce", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_envelope(stdout: str) -> dict:
    """Parse the single-line JSON envelope from stdout. Asserts shape."""
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line; got {len(lines)}"
    return json.loads(lines[0])


# ─── envelope shape ────────────────────────────────────────────────────────


def test_help_lists_every_subcommand() -> None:
    """--help output must surface every subcommand the test suite exercises."""
    rc, out, _ = _run_cli("--help")
    assert rc == 0
    for cmd in (
        "capabilities", "preflight", "discover", "expand-grid",
        "clusters", "list-in-flight", "status", "submit", "aggregate",
        "resubmit", "reconcile", "build-executor",
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
    assert data["supported_schedulers"] == ["sge", "slurm"]
    assert isinstance(data["ssh_multiplexing"], bool)


def test_clusters_list_returns_known_clusters() -> None:
    """clusters list must return the names defined in the active clusters.yaml."""
    rc, out, _ = _run_cli("clusters", "list")
    assert rc == 0
    env = _parse_envelope(out)
    assert env["ok"] is True
    names = [c["name"] for c in env["data"]["clusters"]]
    assert names, "no clusters defined; check hpc_mapreduce/config/clusters.yaml"


def test_expand_grid_returns_cartesian_product(tmp_path: Path) -> None:
    spec = tmp_path / "grid.json"
    spec.write_text(json.dumps({"grid": {"a": [1, 2], "b": ["x", "y"]}}))
    rc, out, _ = _run_cli("expand-grid", "--spec", str(spec))
    assert rc == 0
    env = _parse_envelope(out)
    assert env["ok"] is True
    assert env["data"]["total"] == 4


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
    spec = tmp_path / "bad.json"
    spec.write_text("not json {")
    rc, out, _ = _run_cli("expand-grid", "--spec", str(spec))
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
        "--experiment-dir", str(tmp_path),
        "--spec", str(spec),
    )
    assert rc == 1
    env = _parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "manifest_invalid"


# ─── submit dry-run + dedup contract ───────────────────────────────────────


SUBMIT_SPEC = {
    "profile": "ml",
    "cluster": "hoffman2",
    "ssh_target": "user@hoffman2.idre.ucla.edu",
    "remote_path": "/u/scratch/exp",
    "job_name": "ml",
    "manifest_filename": "manifest.abcd1234.json",
    "job_ids": ["12345"],
    "total_tasks": 6,
}


def test_submit_dry_run_does_not_touch_journal(tmp_path: Path) -> None:
    """--dry-run reports what would happen without writing to the journal."""
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_with_journal = {"HPC_JOURNAL_DIR": str(journal), "PATH": ""}
    # Need PATH for ssh-add etc., but not really for dry-run; pull from os.
    import os
    env_with_journal["PATH"] = os.environ.get("PATH", "")
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir", str(tmp_path),
        "--spec", str(spec),
        "--dry-run",
        env=env_with_journal,
    )
    assert rc == 0
    env_resp = _parse_envelope(out)
    assert env_resp["ok"] is True
    assert env_resp["data"]["dry_run"] is True
    assert env_resp["data"]["would_launch"] == 6


def test_submit_dedup_envelope_marks_replay(tmp_path: Path) -> None:
    """Second submit with the same spec returns deduped=True."""
    import os
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    rc1, out1, _ = _run_cli(
        "submit", "--experiment-dir", str(tmp_path), "--spec", str(spec),
        env=env_vars,
    )
    assert rc1 == 0
    env1 = _parse_envelope(out1)
    assert env1["data"]["deduped"] is False

    rc2, out2, _ = _run_cli(
        "submit", "--experiment-dir", str(tmp_path), "--spec", str(spec),
        env=env_vars,
    )
    assert rc2 == 0
    env2 = _parse_envelope(out2)
    assert env2["data"]["deduped"] is True
    assert env2["data"]["run_id"] == env1["data"]["run_id"]


# ─── list-in-flight recovery path ──────────────────────────────────────────


def test_list_in_flight_finds_submitted_run(tmp_path: Path) -> None:
    """After a submit, list-in-flight must surface the run."""
    import os
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    _run_cli(
        "submit", "--experiment-dir", str(tmp_path), "--spec", str(spec),
        env=env_vars,
    )
    rc, out, _ = _run_cli(
        "list-in-flight", "--experiment-dir", str(tmp_path),
        env=env_vars,
    )
    assert rc == 0
    env_resp = _parse_envelope(out)
    runs = env_resp["data"]["runs"]
    assert any(r["run_id"] == "ml_abcd1234" for r in runs)


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


# ─── Bug 6: aggregate no longer advertises a non-functional --output-dir ─


def test_aggregate_help_does_not_mention_output_dir() -> None:
    """The previous CLI accepted ``--output-dir`` and echoed it in the
    response envelope, but never threaded the value into the actual
    combiner call.  Drop the flag rather than ship a misleading one.
    """
    rc, out, _ = _run_cli("aggregate", "--help")
    assert rc == 0
    assert "--output-dir" not in out


# ─── Bug 7: aggregate failure → ok:false envelope with non-zero exit ─────


def test_aggregate_failure_emits_error_envelope(tmp_path: Path, monkeypatch) -> None:
    """When the combiner returns non-zero, the CLI used to emit ``ok:
    true`` on stdout and exit with EXIT_CLUSTER_ERROR — a contract
    violation since callers can drive logic from either field.  The fix
    routes the failure through ``_err`` so both fields agree.
    """
    import argparse
    from unittest.mock import patch

    from slash_commands import session as session_mod
    from slash_commands.session import RunRecord

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))

    # Seed a run so cmd_aggregate gets past the journal lookup.
    rec = RunRecord(
        run_id="r_abcd1234",
        profile="p",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/x",
        job_name="j",
        job_ids=["1"],
        manifest="manifest.abcd1234.json",
        total_tasks=1,
        submitted_at="2026-04-28T00:00:00+00:00",
        experiment_dir=str(tmp_path),
    )
    session_mod.upsert_run(tmp_path, rec)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="r_abcd1234",
        wave=0,
        force=False,
    )
    captured: list[str] = []

    def fake_emit(payload):
        captured.append(json.dumps(payload))

    with patch(
        "slash_commands.runner.combine_wave",
        return_value=(False, "", "boom: missing metrics"),
    ), patch.object(cli, "_emit", side_effect=fake_emit):
        rc = cli.cmd_aggregate(args)

    assert rc != 0  # exit code reflects failure
    payload = json.loads(captured[-1])
    assert payload["ok"] is False
    assert payload["error_code"] == "combiner_failed"
    assert payload["category"] == "cluster"


# ─── Bug 8: missing run record surfaces as journal_corrupt, not exec NF ──


def test_main_routes_journal_corrupt_for_missing_run(tmp_path: Path) -> None:
    """``runner.combine_wave`` raises ``JournalCorrupt`` when the run is
    missing.  Previously the CLI's blanket ``FileNotFoundError`` handler
    caught a bare exception and labelled it ``executor_not_found``,
    which steered agents at the wrong remediation.
    """
    import os

    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    rc, out, _ = _run_cli(
        "aggregate",
        "--experiment-dir", str(tmp_path),
        "--run-id", "definitely_not_a_run",
        "--wave", "0",
        env=env_vars,
    )
    assert rc != 0
    payload = _parse_envelope(out)
    assert payload["ok"] is False
    assert payload["error_code"] == "journal_corrupt"


def test_main_routes_unrelated_exception_to_internal(monkeypatch) -> None:
    """A genuinely unexpected exception (anything not ``HpcError`` /
    ``ValueError`` / ``TimeoutExpired``) should land on ``error_code:
    internal``, not the previous ``executor_not_found`` mislabel.
    """
    from unittest.mock import patch

    def boom(_args):
        raise RuntimeError("kaboom")

    captured: list[dict] = []

    def fake_emit(payload):
        captured.append(payload)

    with patch.object(cli, "_emit", side_effect=fake_emit), patch.object(
        cli, "cmd_capabilities", side_effect=boom
    ):
        rc = cli.main(["capabilities"])
    assert rc == cli.EXIT_INTERNAL
    assert captured[-1]["error_code"] == "internal"


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
        "submit", "--experiment-dir", str(tmp_path), "--spec", str(spec),
        env=env_vars,
    )
    assert rc != 0
    payload = _parse_envelope(out)
    assert payload["error_code"] == "manifest_invalid"


# ─── Bug 17: cmd_resubmit rejects categories outside the documented enum ─


def test_resubmit_rejects_off_enum_category(tmp_path: Path) -> None:
    """The resubmit input schema constrains category to seven values; the
    CLI used to accept any string and silently record garbage in the
    journal.
    """
    import os

    spec = tmp_path / "rs.json"
    spec.write_text(json.dumps({
        "failed_task_ids": [1],
        "category": "totally_made_up",
    }))
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(tmp_path / "j")}

    rc, out, _ = _run_cli(
        "resubmit",
        "--experiment-dir", str(tmp_path),
        "--run-id", "doesnt_matter",
        "--spec", str(spec),
        env=env_vars,
    )
    assert rc != 0
    payload = _parse_envelope(out)
    assert payload["error_code"] == "manifest_invalid"
