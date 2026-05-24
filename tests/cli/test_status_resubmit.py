"""Subset of the CLI smoke tests, split out from the previously
~1380-LOC ``test_agent_cli.py`` for navigability.

Shared subprocess + envelope helpers live in :mod:`._helpers`.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent import agent_cli as cli

from ._helpers import SUBMIT_SPEC
from ._helpers import env_without_ssh_agent as _env_without_ssh_agent
from ._helpers import parse_envelope as _parse_envelope
from ._helpers import run_cli as _run_cli

# ─── A-M1: cmd_status surfaces preempted tasks from sidecar ───────────────


def test_status_helper_returns_none_when_no_preempt_marks(tmp_path: Path) -> None:
    """When no per-task entry carries a preempt block, the helper
    returns None — /status output then omits the preempted_* keys."""
    runs_dir = tmp_path / ".hpc" / "runs"
    runs_dir.mkdir(parents=True)
    sidecar = {
        "sidecar_schema_version": 2,
        "run_id": "rid",
        "executor": "true",
        "result_dir_template": str(tmp_path / "out"),
        "task_count": 1,
        "tasks_py_sha": "abc",
        "tasks": {"0": {}},
    }
    (runs_dir / "rid.json").write_text(json.dumps(sidecar))

    assert cli._preempted_summary_from_sidecar(tmp_path, "rid") is None


def test_status_helper_aggregates_preempted_task_ids(tmp_path: Path) -> None:
    """When tasks 0 and 2 carry preempt blocks (task 1 doesn't), the
    helper returns (2, [0, 2]) — caller surfaces those keys on
    /status so the campus user's harness can see scheduler pressure
    on a partially-bumped run while it's still in flight."""
    runs_dir = tmp_path / ".hpc" / "runs"
    runs_dir.mkdir(parents=True)
    sidecar = {
        "sidecar_schema_version": 2,
        "run_id": "rid",
        "executor": "true",
        "result_dir_template": str(tmp_path / "out"),
        "task_count": 3,
        "tasks_py_sha": "abc",
        "tasks": {
            "0": {"preempt": {"at": "2026-01-01T00:00:00Z", "grace_sec": 25}},
            "1": {},
            "2": {"preempt": {"at": "2026-01-01T00:00:01Z", "grace_sec": 25}},
        },
    }
    (runs_dir / "rid.json").write_text(json.dumps(sidecar))

    summary = cli._preempted_summary_from_sidecar(tmp_path, "rid")
    assert summary == (2, [0, 2])


def test_status_helper_returns_none_on_missing_sidecar(tmp_path: Path) -> None:
    """No sidecar file → None (treated as 'nothing to surface', not an
    error). Keeps cmd_status robust when called against a run that
    hasn't completed its first wave yet."""
    assert cli._preempted_summary_from_sidecar(tmp_path, "missing_run") is None


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
    spec.write_text(json.dumps({"failed_task_ids": [0, 1], "category": "preempted"}))
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
    spec.write_text(json.dumps({"failed_task_ids": [0, 1], "category": "preempted"}))
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
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

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
    upsert_run(tmp_path, rec)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        task_ids="7",  # CliShape uses dest="task_ids"; arg_pre parses to list[int]
        all_failed=False,
        lines=50,
    )

    captured: list[dict] = []
    fake_logs = [
        {
            "task_id": 7,
            "path": "/exp/logs/ml_12345_8.err",
            "job_id": "12345",
            "content": "boom\n",
        }
    ]
    with (
        patch(
            "hpc_agent.ops.monitor.logs_atom.fetch_task_logs",
            return_value=fake_logs,
        ),
        patch("hpc_agent.cli._helpers._emit", side_effect=lambda p: captured.append(p)),
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
