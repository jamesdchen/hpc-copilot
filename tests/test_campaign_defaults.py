"""Tests for ``hpc_mapreduce.campaign.defaults``.

The poll / submit defaults shell out to ``hpc-mapreduce status`` /
``submit`` via subprocess. These tests mock the subprocess to keep them
fast and SSH-free; the integration with the real CLI is covered by
``tests/test_campaign_e2e.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from hpc_mapreduce.campaign import defaults

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# tasks_py_total_predicate
# ---------------------------------------------------------------------------


def _write_tasks_py(tmp_path: Path, body: str) -> None:
    hpc = tmp_path / ".hpc"
    hpc.mkdir(exist_ok=True)
    (hpc / "tasks.py").write_text(body)


def test_predicate_returns_true_when_total_positive(tmp_path: Path) -> None:
    _write_tasks_py(tmp_path, "def total(): return 5\ndef resolve(i): return {}\n")
    assert defaults.tasks_py_total_predicate(tmp_path)() is True


def test_predicate_returns_false_when_total_zero(tmp_path: Path) -> None:
    _write_tasks_py(tmp_path, "def total(): return 0\ndef resolve(i): return {}\n")
    assert defaults.tasks_py_total_predicate(tmp_path)() is False


def test_predicate_re_imports_tasks_py_each_call(tmp_path: Path) -> None:
    """Critical: the user's tasks.py reads prior() at module load. The
    predicate must re-import each call so newly-landed sidecars are seen."""
    import os

    _write_tasks_py(tmp_path, "def total(): return 1\ndef resolve(i): return {}\n")
    pred = defaults.tasks_py_total_predicate(tmp_path)
    assert pred() is True

    # Edit tasks.py mid-loop. A re-importing predicate must see the change.
    # Advance mtime explicitly so Python's source-file loader doesn't hit
    # its bytecode cache (in production the gap is seconds — between
    # /submit + /status — but the test races that by orders of magnitude).
    tasks_path = tmp_path / ".hpc" / "tasks.py"
    tasks_path.write_text("def total(): return 0\ndef resolve(i): return {}\n")
    future = tasks_path.stat().st_mtime + 5
    os.utime(tasks_path, (future, future))
    assert pred() is False


# ---------------------------------------------------------------------------
# poll_until_terminal
# ---------------------------------------------------------------------------


def _make_completed(
    stdout: str, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope(state: str) -> str:
    return json.dumps({"ok": True, "idempotent": True, "data": {"lifecycle_state": state}})


def test_poll_returns_immediately_on_terminal_state(tmp_path: Path) -> None:
    poll = defaults.poll_until_terminal(tmp_path, poll_interval_seconds=0.001)
    with patch.object(
        subprocess,
        "run",
        return_value=_make_completed(_envelope("complete")),
    ):
        asyncio.run(poll("run_xyz"))


def test_poll_continues_until_state_terminal(tmp_path: Path) -> None:
    poll = defaults.poll_until_terminal(tmp_path, poll_interval_seconds=0.001)
    states = iter(
        [
            _make_completed(_envelope("in_flight")),
            _make_completed(_envelope("in_flight")),
            _make_completed(_envelope("complete")),
        ]
    )
    with patch.object(subprocess, "run", side_effect=lambda *a, **kw: next(states)):
        asyncio.run(poll("run_xyz"))


@pytest.mark.parametrize("state", ["complete", "failed", "abandoned"])
def test_poll_returns_for_every_terminal_state(tmp_path: Path, state: str) -> None:
    poll = defaults.poll_until_terminal(tmp_path, poll_interval_seconds=0.001)
    with patch.object(subprocess, "run", return_value=_make_completed(_envelope(state))):
        asyncio.run(poll("run_xyz"))


def test_poll_raises_on_nonzero_exit(tmp_path: Path) -> None:
    poll = defaults.poll_until_terminal(tmp_path, poll_interval_seconds=0.001)
    with (
        patch.object(
            subprocess,
            "run",
            return_value=_make_completed("", returncode=2, stderr="ssh down"),
        ),
        pytest.raises(RuntimeError, match="exited 2"),
    ):
        asyncio.run(poll("run_xyz"))


def test_poll_raises_on_error_envelope(tmp_path: Path) -> None:
    poll = defaults.poll_until_terminal(tmp_path, poll_interval_seconds=0.001)
    err = json.dumps(
        {"ok": False, "error_code": "journal_corrupt", "message": "boom", "category": "internal"}
    )
    with (
        patch.object(subprocess, "run", return_value=_make_completed(err)),
        pytest.raises(RuntimeError, match="journal_corrupt"),
    ):
        asyncio.run(poll("run_xyz"))


# ---------------------------------------------------------------------------
# submit_via_cli
# ---------------------------------------------------------------------------


def test_submit_writes_spec_to_campaign_dir_when_campaign_id_set(tmp_path: Path) -> None:
    """Spec lands at .hpc/campaigns/<cid>/spec-<run_id>.json so the user
    can audit what was submitted."""
    captured: dict = {}

    def fake_run(argv, **_kw):
        # Pull the --spec argument and read the file.
        spec_idx = argv.index("--spec") + 1
        spec_path = argv[spec_idx]
        captured["argv"] = argv
        captured["spec_path"] = spec_path
        with open(spec_path) as fh:
            captured["spec"] = json.loads(fh.read())
        return _make_completed("{}", returncode=0)

    spec = {
        "profile": "ml_ridge",
        "cluster": "hoffman2",
        "ssh_target": "u@h",
        "remote_path": "/r",
        "job_name": "ml_ridge",
        "run_id": "run-aaaa",
        "job_ids": ["1"],
        "total_tasks": 1,
        "campaign_id": "ml_ridge_q1",
    }
    submit = defaults.submit_via_cli(lambda: spec, experiment_dir=tmp_path)

    with patch.object(subprocess, "run", side_effect=fake_run):
        run_id = asyncio.run(submit())

    assert run_id == "run-aaaa"
    assert captured["spec"] == spec
    expected_path = tmp_path / ".hpc" / "campaigns" / "ml_ridge_q1" / "spec-run-aaaa.json"
    assert captured["spec_path"] == str(expected_path)
    assert expected_path.is_file()


def test_submit_writes_spec_to_dot_hpc_when_no_campaign_id(tmp_path: Path) -> None:
    captured: dict = {}

    def fake_run(argv, **_kw):
        spec_idx = argv.index("--spec") + 1
        captured["spec_path"] = argv[spec_idx]
        return _make_completed("{}", returncode=0)

    spec = {"run_id": "run-bbbb", "profile": "p", "cluster": "c"}
    submit = defaults.submit_via_cli(lambda: spec, experiment_dir=tmp_path)
    with patch.object(subprocess, "run", side_effect=fake_run):
        asyncio.run(submit())
    assert captured["spec_path"] == str(tmp_path / ".hpc" / "spec-run-bbbb.json")


def test_submit_raises_when_spec_lacks_run_id(tmp_path: Path) -> None:
    submit = defaults.submit_via_cli(lambda: {"profile": "p"}, experiment_dir=tmp_path)
    with pytest.raises(ValueError, match="must return a dict with 'run_id'"):
        asyncio.run(submit())


def test_submit_propagates_cli_failure(tmp_path: Path) -> None:
    spec = {"run_id": "run-cccc"}
    submit = defaults.submit_via_cli(lambda: spec, experiment_dir=tmp_path)
    with (
        patch.object(
            subprocess,
            "run",
            return_value=_make_completed("", returncode=1, stderr="spec_invalid"),
        ),
        pytest.raises(RuntimeError, match="exited 1"),
    ):
        asyncio.run(submit())


def test_submit_calls_spec_builder_per_invocation(tmp_path: Path) -> None:
    """Each submit_one() call must invoke spec_builder() afresh so the
    user's strategy library can propose new params per iteration."""
    counter = {"n": 0}

    def builder() -> dict:
        counter["n"] += 1
        return {"run_id": f"run-{counter['n']:04d}"}

    submit = defaults.submit_via_cli(builder, experiment_dir=tmp_path)
    with patch.object(subprocess, "run", return_value=_make_completed("{}", returncode=0)):
        rid1 = asyncio.run(submit())
        rid2 = asyncio.run(submit())
    assert (rid1, rid2) == ("run-0001", "run-0002")
    assert counter["n"] == 2
