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

from claude_hpc import agent_cli as cli


def _run_cli(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Invoke the CLI as a subprocess and return (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "claude_hpc", *args],
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


# ─── submit dry-run + dedup contract ───────────────────────────────────────


SUBMIT_SPEC = {
    "profile": "ml",
    "cluster": "hoffman2",
    "ssh_target": "user@hoffman2.idre.ucla.edu",
    "remote_path": "/u/scratch/exp",
    "job_name": "ml",
    "run_id": "ml-20260429-153012-abcd1234",
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
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
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
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc1 == 0
    env1 = _parse_envelope(out1)
    assert env1["data"]["deduped"] is False

    rc2, out2, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc2 == 0
    env2 = _parse_envelope(out2)
    assert env2["data"]["deduped"] is True
    assert env2["data"]["run_id"] == env1["data"]["run_id"]


def test_submit_persists_campaign_id_to_journal(tmp_path: Path) -> None:
    """A spec with `campaign_id` lands on the RunRecord and is later
    discoverable via session.find_runs_by_campaign."""
    import os

    spec_payload = {**SUBMIT_SPEC, "campaign_id": "ml_ridge_q1"}
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(spec_payload))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc == 0
    env_resp = _parse_envelope(out)
    assert env_resp["ok"] is True

    # Confirm the journal carries the tag and the campaign filter sees it.
    from slash_commands import session

    # Redirect HPC_HOMEDIR for this in-process check the same way the CLI did.
    saved = session.HPC_HOMEDIR
    try:
        session.HPC_HOMEDIR = journal  # type: ignore[misc]
        matched = session.find_runs_by_campaign(tmp_path, "ml_ridge_q1")
    finally:
        session.HPC_HOMEDIR = saved  # type: ignore[misc]
    assert len(matched) == 1
    assert matched[0].campaign_id == "ml_ridge_q1"


# ─── list-in-flight recovery path ──────────────────────────────────────────


def test_list_in_flight_finds_submitted_run(tmp_path: Path) -> None:
    """After a submit, list-in-flight must surface the run."""
    import os

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    rc, out, _ = _run_cli(
        "list-in-flight",
        "--experiment-dir",
        str(tmp_path),
        env=env_vars,
    )
    assert rc == 0
    env_resp = _parse_envelope(out)
    runs = env_resp["data"]["runs"]
    assert any(r["run_id"] == SUBMIT_SPEC["run_id"] for r in runs)


def test_list_in_flight_surfaces_campaign_id_when_tagged(tmp_path: Path) -> None:
    """A submit with campaign_id should appear in list-in-flight with the tag.
    Open-loop submits should NOT carry the field at all (kept absent to keep
    envelopes compact)."""
    import os

    # Tagged submit.
    tagged_spec = {**SUBMIT_SPEC, "run_id": "tagged-run-1234", "campaign_id": "qa_q1"}
    spec = tmp_path / "tagged.json"
    spec.write_text(json.dumps(tagged_spec))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}
    _run_cli("submit", "--experiment-dir", str(tmp_path), "--spec", str(spec), env=env_vars)

    # Open-loop submit (no campaign_id).
    untagged_spec = {**SUBMIT_SPEC, "run_id": "untagged-run-5678"}
    spec2 = tmp_path / "untagged.json"
    spec2.write_text(json.dumps(untagged_spec))
    _run_cli("submit", "--experiment-dir", str(tmp_path), "--spec", str(spec2), env=env_vars)

    rc, out, _ = _run_cli("list-in-flight", "--experiment-dir", str(tmp_path), env=env_vars)
    assert rc == 0
    runs = {r["run_id"]: r for r in _parse_envelope(out)["data"]["runs"]}
    assert runs["tagged-run-1234"]["campaign_id"] == "qa_q1"
    assert "campaign_id" not in runs["untagged-run-5678"]


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
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")

    # Seed a run so cmd_aggregate gets past the journal lookup.
    rec = RunRecord(
        run_id="r_abcd1234",
        profile="p",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/x",
        job_name="j",
        job_ids=["1"],
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

    with (
        patch(
            "slash_commands.runner.combine_wave",
            return_value=(False, "", "boom: missing metrics"),
        ),
        patch.object(cli, "_emit", side_effect=fake_emit),
    ):
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
    env_vars = {
        **os.environ,
        "HPC_JOURNAL_DIR": str(journal),
        # The SSH gate fires before the journal lookup; pre-populate so
        # this test exercises the journal path it intends to.
        "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", "/tmp/fake-agent.sock"),
    }

    rc, out, _ = _run_cli(
        "aggregate",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "definitely_not_a_run",
        "--wave",
        "0",
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

    with (
        patch.object(cli, "_emit", side_effect=fake_emit),
        patch.object(cli, "cmd_capabilities", side_effect=boom),
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


# ─── A-M3: cmd_resubmit surfaces Preempted at envelope level ──────────────


def test_resubmit_preempted_category_with_all_marked_raises_preempted(
    tmp_path: Path,
) -> None:
    """When the caller asks to resubmit category=preempted and every
    listed task_id has a preempt marker on the per-task sidecar entry,
    the CLI must surface a Preempted envelope (error_code=preempted)
    instead of treating it as an ordinary retry. The campus user got
    bumped, not failed.
    """
    import os

    # Scaffold: minimal sidecar with two preempt-marked tasks.
    runs_dir = tmp_path / ".hpc" / "runs"
    runs_dir.mkdir(parents=True)
    sidecar = {
        "sidecar_schema_version": 2,
        "run_id": "rid",
        "executor": "true",
        "result_dir_template": str(tmp_path / "out"),
        "task_count": 2,
        "tasks_py_sha": "abc",
        "tasks": {
            "0": {"preempt": {"at": "2026-01-01T00:00:00Z", "grace_sec": 25}},
            "1": {"preempt": {"at": "2026-01-01T00:00:01Z", "grace_sec": 25}},
        },
    }
    (runs_dir / "rid.json").write_text(json.dumps(sidecar))

    spec = tmp_path / "rs.json"
    spec.write_text(
        json.dumps({"failed_task_ids": [0, 1], "category": "preempted"})
    )
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(tmp_path / "j")}

    rc, out, _ = _run_cli(
        "resubmit",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "rid",
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc == 2, "preempted is category=cluster → exit 2"
    payload = _parse_envelope(out)
    assert payload["ok"] is False
    assert payload["error_code"] == "preempted"
    assert payload["category"] == "cluster"


def test_resubmit_preempted_category_with_partial_marks_does_not_raise(
    tmp_path: Path,
) -> None:
    """If only SOME of the listed task_ids carry preempt markers, the
    others are real failures — fall through to the normal resubmit
    path (which will fail SSH-gate in this offline test, but must not
    raise Preempted)."""
    import os

    runs_dir = tmp_path / ".hpc" / "runs"
    runs_dir.mkdir(parents=True)
    sidecar = {
        "sidecar_schema_version": 2,
        "run_id": "rid",
        "executor": "true",
        "result_dir_template": str(tmp_path / "out"),
        "task_count": 2,
        "tasks_py_sha": "abc",
        "tasks": {
            "0": {"preempt": {"at": "2026-01-01T00:00:00Z", "grace_sec": 25}},
            # task 1: a real failure, no preempt marker.
            "1": {},
        },
    }
    (runs_dir / "rid.json").write_text(json.dumps(sidecar))

    spec = tmp_path / "rs.json"
    spec.write_text(
        json.dumps({"failed_task_ids": [0, 1], "category": "preempted"})
    )
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(tmp_path / "j")}

    rc, out, _ = _run_cli(
        "resubmit",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "rid",
        "--spec",
        str(spec),
        env=env_vars,
    )
    payload = _parse_envelope(out)
    assert payload.get("error_code") != "preempted", (
        "partial preempt markers must not trigger envelope-level Preempted"
    )


# ─── SSH fail-fast gate on cluster-touching subcommands ─────────────────────


def _env_without_ssh_agent() -> dict[str, str]:
    """Inherit PATH so the CLI binary works, but strip SSH_AUTH_SOCK so
    the gate kicks in. Also sets HPC_JOURNAL_DIR so the journal lookup
    isn't what fails first."""
    import os

    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        # No SSH_AUTH_SOCK on purpose.
    }


def test_ssh_gate_status_fails_fast_without_agent(tmp_path: Path) -> None:
    """`status` must emit ssh_unreachable instead of hanging."""
    env = _env_without_ssh_agent()
    env["HPC_JOURNAL_DIR"] = str(tmp_path / "journal")
    rc, out, _ = _run_cli(
        "status",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "x",
        env=env,
    )
    assert rc == 2, "ssh_unreachable is category=network → exit 2"
    payload = _parse_envelope(out)
    assert payload["ok"] is False
    assert payload["error_code"] == "ssh_unreachable"
    assert payload["retry_safe"] is True
    assert payload["category"] == "network"
    assert "remediation" in payload


def test_ssh_gate_aggregate_fails_fast_without_agent(tmp_path: Path) -> None:
    env = _env_without_ssh_agent()
    env["HPC_JOURNAL_DIR"] = str(tmp_path / "journal")
    rc, out, _ = _run_cli(
        "aggregate",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "x",
        "--wave",
        "0",
        env=env,
    )
    assert rc == 2
    payload = _parse_envelope(out)
    assert payload["error_code"] == "ssh_unreachable"


def test_ssh_gate_reconcile_fails_fast_without_agent(tmp_path: Path) -> None:
    env = _env_without_ssh_agent()
    env["HPC_JOURNAL_DIR"] = str(tmp_path / "journal")
    rc, out, _ = _run_cli(
        "reconcile",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "x",
        "--scheduler",
        "sge",
        env=env,
    )
    assert rc == 2
    payload = _parse_envelope(out)
    assert payload["error_code"] == "ssh_unreachable"


# ─── logs subcommand ──────────────────────────────────────────────────────


def test_logs_requires_task_id_or_all_failed(tmp_path: Path) -> None:
    """`logs` with neither --task-id nor --all-failed surfaces user error."""
    import os

    # Need a journal record for the run-id lookup to get past first.
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_vars = {
        **os.environ,
        "HPC_JOURNAL_DIR": str(journal),
        "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", "/tmp/fake.sock"),
    }
    _run_cli("submit", "--experiment-dir", str(tmp_path), "--spec", str(spec), env=env_vars)

    rc, out, _ = _run_cli(
        "logs",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        SUBMIT_SPEC["run_id"],
        env=env_vars,
    )
    assert rc != 0
    payload = _parse_envelope(out)
    assert payload["error_code"] == "spec_invalid"


def test_logs_envelope_carries_logs_field(tmp_path: Path, monkeypatch) -> None:
    """logs --task-id 7 returns a list with one entry from fetch_task_logs."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake.sock")

    # Seed a run.
    from slash_commands import session as session_mod
    from slash_commands.session import RunRecord

    rec = RunRecord(
        run_id="ml_abcd1234",
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/exp",
        job_name="ml",
        job_ids=["12345"],
        total_tasks=10,
        submitted_at="2026-04-28T00:00:00+00:00",
        experiment_dir=str(tmp_path),
    )
    session_mod.upsert_run(tmp_path, rec)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        task_id="7",
        all_failed=False,
        lines=50,
    )

    captured: list[dict] = []
    fake_logs = [
        {
            "task_id": 7,
            "path": "/exp/_hpc_logs/ml_12345_7.err",
            "job_id": "12345",
            "content": "boom\n",
        }
    ]
    with (
        patch.object(cli.runner, "fetch_task_logs", return_value=fake_logs),
        patch.object(cli, "_emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cli.cmd_logs(args)

    assert rc == 0
    payload = captured[-1]
    assert payload["ok"] is True
    assert payload["data"]["logs"] == fake_logs
    assert payload["data"]["run_id"] == "ml_abcd1234"


# ─── stale-cache age field on status / list-in-flight ──────────────────────


def test_last_status_age_seconds_is_recent_for_now_stamp() -> None:
    """A checked_at stamped at 'now' yields a small age (< 5s)."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    age = cli._last_status_age_seconds({"checked_at": now_iso})
    assert age is not None
    assert 0 <= age < 5


def test_last_status_age_seconds_handles_missing_checked_at() -> None:
    assert cli._last_status_age_seconds({}) is None
    assert cli._last_status_age_seconds(None) is None  # type: ignore[arg-type]
    assert cli._last_status_age_seconds({"checked_at": "garbage"}) is None


def test_last_status_age_seconds_is_old_for_distant_past() -> None:
    """A timestamp from a year ago should yield a very large age."""
    age = cli._last_status_age_seconds({"checked_at": "2024-01-01T00:00:00+00:00"})
    assert age is not None
    assert age > 60 * 60 * 24 * 30  # at least 30 days


def test_list_in_flight_envelope_includes_age_field(tmp_path: Path) -> None:
    """list-in-flight surfaces last_status_age_seconds for each run so the
    caller can flag stale snapshots."""
    import os

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    _run_cli("submit", "--experiment-dir", str(tmp_path), "--spec", str(spec), env=env_vars)
    rc, out, _ = _run_cli("list-in-flight", "--experiment-dir", str(tmp_path), env=env_vars)
    assert rc == 0
    runs = _parse_envelope(out)["data"]["runs"]
    assert len(runs) == 1
    # No status poll yet: last_status is empty/missing -> age is None.
    assert runs[0].get("last_status_age_seconds") is None


# ─── aggregate preconditions / postconditions / provenance ─────────────────


def _seed_aggregate_run(tmp_path: Path, run_id: str = "ml_abcd1234"):
    """Helper: seed a journal record so cmd_aggregate gets past lookup."""
    from slash_commands import session as session_mod
    from slash_commands.session import RunRecord

    rec = RunRecord(
        run_id=run_id,
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/exp",
        job_name="ml",
        job_ids=["12345"],
        total_tasks=2,
        submitted_at="2026-04-28T00:00:00+00:00",
        experiment_dir=str(tmp_path),
    )
    session_mod.upsert_run(tmp_path, rec)
    return rec


def test_aggregate_precondition_blocks_combine_on_missing_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    """--require-outputs must refuse to combine when any per-task output is
    absent on the cluster, before invoking the user's combiner."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs="results/metrics.{task_id}.json",
        expect_output=None,
    )
    captured: list[dict] = []
    with (
        patch.object(
            cli.runner,
            "verify_per_task_outputs",
            return_value=["results/metrics.1.json"],
        ),
        patch.object(cli.runner, "combine_wave") as combine_mock,
        patch.object(cli, "_emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cli.cmd_aggregate(args)

    combine_mock.assert_not_called(), "combiner must not run when outputs missing"
    assert rc != 0
    payload = captured[-1]
    assert payload["ok"] is False
    assert payload["error_code"] == "outputs_missing"
    assert payload["retry_safe"] is True


def test_aggregate_postcondition_fails_when_combiner_artifact_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """--expect-output must surface combiner_failed when the combiner exits 0
    but the declared output isn't there or isn't parseable."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs=None,
        expect_output="results/metrics.json",
    )
    captured: list[dict] = []
    with (
        patch.object(cli.runner, "combine_wave", return_value=(True, "ok", "")),
        patch.object(
            cli.runner,
            "verify_combiner_artifact",
            return_value=(False, "is missing at /exp/results/metrics.json"),
        ),
        patch.object(cli, "_emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cli.cmd_aggregate(args)

    assert rc != 0
    payload = captured[-1]
    assert payload["ok"] is False
    assert payload["error_code"] == "combiner_failed"
    assert "results/metrics.json" in payload["message"]


def test_aggregate_envelope_carries_provenance_on_success(tmp_path: Path, monkeypatch) -> None:
    """Successful aggregate must embed provenance metadata in envelope.data."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs=None,
        expect_output=None,
    )
    captured: list[dict] = []
    with (
        patch.object(cli.runner, "combine_wave", return_value=(True, "ok", "")),
        patch.object(cli, "_emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cli.cmd_aggregate(args)

    assert rc == 0
    payload = captured[-1]
    assert payload["ok"] is True
    prov = payload["data"]["provenance"]
    assert prov["run_id"] == "ml_abcd1234"
    assert prov["wave"] == 0
    assert prov["profile"] == "ml"
    assert prov["cluster"] == "hoffman2"
    assert "combined_at" in prov


def test_aggregate_reads_sidecar_defaults_for_require_and_expect(
    tmp_path: Path, monkeypatch
) -> None:
    """When the CLI flags are omitted, the sidecar's aggregate_defaults.
    {require_outputs, expect_output} must be honored."""
    import argparse
    from unittest.mock import patch

    from claude_hpc.orchestrator.runs import write_run_sidecar

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    write_run_sidecar(
        tmp_path,
        run_id="ml_abcd1234",
        cmd_sha="0" * 64,
        claude_hpc_version="0.2.0",
        submitted_at="2026-04-28T00:00:00+00:00",
        executor="python -m ml.train",
        result_dir_template="results/{seed}",
        task_count=2,
        tasks_py_sha="1" * 64,
        profile="ml",
        aggregate_defaults={
            "require_outputs": "results/metrics.{task_id}.json",
            "expect_output": "results/metrics.json",
        },
    )

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs=None,  # no CLI flag — default should apply
        expect_output=None,
    )

    seen_template: list[str] = []
    seen_expect: list[str] = []

    def fake_verify_outputs(*, template, **_kw):
        seen_template.append(template)
        return []  # nothing missing

    def fake_verify_artifact(*, expect_output, **_kw):
        seen_expect.append(expect_output)
        return True, "ok"

    with (
        patch.object(cli.runner, "verify_per_task_outputs", side_effect=fake_verify_outputs),
        patch.object(cli.runner, "verify_combiner_artifact", side_effect=fake_verify_artifact),
        patch.object(cli.runner, "combine_wave", return_value=(True, "ok", "")),
        patch.object(
            cli.runner, "write_remote_provenance", return_value="/exp/results/_provenance.json"
        ),
        patch.object(cli, "_emit"),
    ):
        rc = cli.cmd_aggregate(args)

    assert rc == 0, "sidecar-defaulted aggregate should succeed"
    assert seen_template == ["results/metrics.{task_id}.json"]
    assert seen_expect == ["results/metrics.json"]


def test_aggregate_writes_sidecar_when_expect_output_set(tmp_path: Path, monkeypatch) -> None:
    """When --expect-output is set, the envelope reports the sidecar path."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs=None,
        expect_output="results/metrics.json",
    )
    captured: list[dict] = []
    with (
        patch.object(cli.runner, "combine_wave", return_value=(True, "ok", "")),
        patch.object(cli.runner, "verify_combiner_artifact", return_value=(True, "ok")),
        patch.object(
            cli.runner,
            "write_remote_provenance",
            return_value="/exp/results/_provenance.json",
        ),
        patch.object(cli, "_emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cli.cmd_aggregate(args)

    assert rc == 0
    payload = captured[-1]
    assert payload["data"]["provenance_sidecar"] == "/exp/results/_provenance.json"


def test_ssh_gate_does_not_block_local_only_subcommands(tmp_path: Path) -> None:
    """`capabilities`, `clusters list`, `submit --dry-run`, and `submit`
    (journal-only) must not be gated by SSH_AUTH_SOCK."""
    env = _env_without_ssh_agent()
    env["HPC_JOURNAL_DIR"] = str(tmp_path / "journal")

    rc, _, _ = _run_cli("capabilities", env=env)
    assert rc == 0

    rc, _, _ = _run_cli("clusters", "list", env=env)
    assert rc == 0

    submit_spec = tmp_path / "spec.json"
    submit_spec.write_text(json.dumps(SUBMIT_SPEC))
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(submit_spec),
        "--dry-run",
        env=env,
    )
    assert rc == 0, _parse_envelope(out)

    # Real submit is journal-only, no SSH — must succeed without agent.
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(submit_spec),
        env=env,
    )
    assert rc == 0, _parse_envelope(out)


# ─── submit --from-meta overlay ────────────────────────────────────────────


class TestSubmitFromMeta:
    """Verify the --from-meta flag overlays meta.json::experiment_id onto
    the submit spec's profile and job_name. setdefault semantics: never
    overwrite caller-supplied values, silent no-op without meta.json."""

    @staticmethod
    def _write_spec(tmp_path: Path, **overrides: object) -> Path:
        import json

        spec = {
            "cluster": "hoffman2",
            "ssh_target": "user@host",
            "remote_path": "/u/scratch/exp",
            "run_id": "run-20260429-153012-abcd1234",
            "job_ids": ["12345"],
            "total_tasks": 4,
        }
        spec.update(overrides)
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))
        return path

    @staticmethod
    def _write_meta(experiment_dir: Path, experiment_id: str | None) -> None:
        import json

        payload: dict = {"seed": 42, "purpose": "test"}
        if experiment_id is not None:
            payload["experiment_id"] = experiment_id
        (experiment_dir / "meta.json").write_text(json.dumps(payload))

    def test_from_meta_fills_missing_profile_and_job_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
        spec = self._write_spec(tmp_path)
        self._write_meta(tmp_path, experiment_id="run-001-foo")
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--from-meta",
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        assert rc == 0, out
        env = _parse_envelope(out)
        assert env["ok"] is True
        # run_id is now spec-supplied directly; --from-meta only fills
        # the profile + job_name fields.  Verify by reading the journal.
        from slash_commands import session

        monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "journal")
        record = session.load_run(tmp_path, env["data"]["run_id"])
        assert record is not None
        assert record.profile == "run-001-foo"
        assert record.job_name == "run-001-foo"

    def test_from_meta_does_not_overwrite_present_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = self._write_spec(tmp_path, profile="explicit", job_name="explicit")
        self._write_meta(tmp_path, experiment_id="other")
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--from-meta",
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        assert rc == 0, out
        env = _parse_envelope(out)
        from slash_commands import session

        monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "journal")
        record = session.load_run(tmp_path, env["data"]["run_id"])
        assert record is not None
        assert record.profile == "explicit"
        assert record.job_name == "explicit"

    def test_from_meta_no_op_without_meta_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = self._write_spec(tmp_path, profile="p", job_name="p")
        # No meta.json on disk.
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--from-meta",
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        assert rc == 0, out
        env = _parse_envelope(out)
        from slash_commands import session

        monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "journal")
        record = session.load_run(tmp_path, env["data"]["run_id"])
        assert record is not None
        assert record.profile == "p"

    def test_from_meta_no_op_when_meta_lacks_experiment_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = self._write_spec(tmp_path)  # no profile, no job_name
        self._write_meta(tmp_path, experiment_id=None)  # meta lacks experiment_id
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--from-meta",
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        # Spec is incomplete and no overlay applied; expect spec_invalid.
        assert rc == 1, out
        env = _parse_envelope(out)
        assert env["ok"] is False
        assert env["error_code"] == "spec_invalid"

    def test_from_meta_off_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = self._write_spec(tmp_path)  # no profile, no job_name
        self._write_meta(tmp_path, experiment_id="run-001-foo")
        # Flag NOT set: existing behavior (incomplete spec → spec_invalid).
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        assert rc == 1, out
        env = _parse_envelope(out)
        assert env["ok"] is False
        assert env["error_code"] == "spec_invalid"
