"""Hermetic tests for the U1 sandbox experiment generator (``sandbox_fixture``).

Covers the plan §4-U1 contract (``docs/plans/sandbox-proving-run-2026-07-18.md``):

* the fixture materializes ``train.py`` + the three framework files
  (``.hpc/tasks.py`` / ``interview.json`` / ``.hpc/axes.yaml``) through the
  REAL primitives (interview / axes-init / classify-axis / compute-run-id) —
  nothing mocked, nothing hand-written;
* successive parameterizations mint FRESH ``run_id``s (the 2026-07-18
  determinism lesson: an identical sweep re-mints the same identity);
* the §3 trust-doctrine guard REFUSES to run when ``HPC_JOURNAL_DIR`` is
  unset or resolves inside the production home ``~/.claude/hpc``;
* the ``"failing"`` executor variant (the U6 canary-fail hook) still onboards
  cleanly — the failure is a cluster-side runtime property.

No docker, no SSH, no cluster: every test runs in the default tier against
``tmp_path`` + a monkeypatched ``HPC_JOURNAL_DIR``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from sandbox_fixture import (
    SANDBOX_OPERATOR,
    SandboxTrustError,
    build_sandbox_experiment,
    require_sandbox_journal_home,
)

import hpc_agent


@pytest.fixture
def sandbox_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HPC_JOURNAL_DIR`` at an ephemeral home and return it.

    The top-level autouse ``_isolated_journal_home`` has already popped any
    shell-inherited value, so this monkeypatch is the ONLY journal channel —
    exactly the ephemeral-home posture plan §3 mandates.
    """
    home = tmp_path / "sandbox_journal_home"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    return home


# ── materialization ──────────────────────────────────────────────────────────


def test_materializes_three_files_via_real_primitives(
    tmp_path: Path, sandbox_journal: Path
) -> None:
    exp = build_sandbox_experiment(tmp_path / "experiment")

    # The executor + the three framework files the plan names.
    assert exp.train_py.is_file()
    assert exp.tasks_py.is_file()
    assert exp.tasks_py == exp.experiment_dir / ".hpc" / "tasks.py"
    assert exp.interview_json.is_file()
    assert exp.axes_yaml.is_file()

    # Identity: the REAL compute-run-id shape, cross-checked against the
    # cmd_sha the REAL interview embedded in interview.json.
    assert exp.run_id == f"{exp.run_name}-{exp.cmd_sha[:8]}"
    assert len(exp.cmd_sha) == 64
    assert exp.total_tasks == len(exp.seeds) == 8

    doc = json.loads(exp.interview_json.read_text(encoding="utf-8"))
    assert doc["_materialized"]["cmd_sha"] == exp.cmd_sha
    assert doc["_materialized"]["total_tasks"] == exp.total_tasks
    assert doc["_materialized"]["tasks_py_origin"] == "interview_materialized"


def test_interview_json_stamps_sandbox_provenance(tmp_path: Path, sandbox_journal: Path) -> None:
    exp = build_sandbox_experiment(tmp_path / "experiment")
    doc = json.loads(exp.interview_json.read_text(encoding="utf-8"))

    # produced_by: {kind: agent, operator: sandbox-proving} per the U1 spec,
    # plus the session_sha the wire model requires for kind="agent".
    assert doc["produced_by"]["kind"] == "agent"
    assert doc["produced_by"]["operator"] == SANDBOX_OPERATOR
    assert doc["produced_by"]["session_sha"] == SANDBOX_OPERATOR

    # The typed recipe (never a hand-written tasks.py) and the real
    # register_run entry point, with the same executor derivation the live
    # pi drills shipped.
    assert doc["task_generator"]["kind"] == "items_x_seeds"
    assert doc["task_generator"]["params"]["seeds"] == list(range(8))
    assert doc["entry_point"]["kind"] == "register_run"
    assert doc["entry_point"]["run_name"] == "run"
    assert doc["_materialized"]["entry_point"]["executor_cmd"] == (
        "python3 -m hpc_agent.executor_cli run-registered train.py --run-name run"
    )


def test_materialized_tasks_py_loads_and_resolves(tmp_path: Path, sandbox_journal: Path) -> None:
    exp = build_sandbox_experiment(tmp_path / "experiment", seeds=(0, 1, 2), n_samples=123_456)
    mod = hpc_agent.load_tasks_module(exp.tasks_py)
    assert mod.total() == 3
    assert mod.resolve(0) == {"n_samples": 123_456, "seed": 0}
    assert mod.resolve(2) == {"n_samples": 123_456, "seed": 2}
    # Generated-by-interview header — never a hand-written tasks.py.
    assert "hpc-agent interview" in exp.tasks_py.read_text(encoding="utf-8")


def test_axes_yaml_carries_scheduling_and_classification(
    tmp_path: Path, sandbox_journal: Path
) -> None:
    exp = build_sandbox_experiment(tmp_path / "experiment", seeds=(0, 1, 2, 3, 4))
    doc = yaml.safe_load(exp.axes_yaml.read_text(encoding="utf-8"))

    # The scheduling block (axes-init), mirroring the live demo's order.
    assert doc["axes"] == [
        {"name": "seed", "size": 5},
        {"name": "n_samples", "size": 1},
    ]
    assert doc["homogeneous_axes"] == []

    # The DataAxis block (classify-axis recorder), interview-classified.
    entry = doc["executors"]["run"]
    assert entry["data_axis"]["kind"] == "sequential"
    assert entry["classified_by"] == "interview"
    assert entry["run_signature_sha"]


def test_executor_writes_the_pi_shape(tmp_path: Path, sandbox_journal: Path) -> None:
    exp = build_sandbox_experiment(tmp_path / "experiment")
    src = exp.train_py.read_text(encoding="utf-8")
    assert "@register_run" in src
    assert "def run(seed: int = 0, n_samples: int = 100000) -> dict:" in src
    assert "pi_estimate" in src


# ── fresh run_ids across parameterizations (the determinism lesson) ─────────


def test_successive_parameterizations_mint_fresh_run_ids(
    tmp_path: Path, sandbox_journal: Path
) -> None:
    base = build_sandbox_experiment(tmp_path / "a", n_samples=100_000)
    bumped = build_sandbox_experiment(tmp_path / "b", n_samples=100_001)
    assert base.cmd_sha != bumped.cmd_sha
    assert base.run_id != bumped.run_id

    reseeded = build_sandbox_experiment(tmp_path / "c", seeds=(0, 1, 2, 3))
    assert reseeded.run_id != base.run_id

    # The same parameterization re-mints the SAME identity — cmd_sha is
    # parameter identity, so an identical sweep dedups against the prior
    # sandbox run (exactly why callers must bump the sweep).
    same = build_sandbox_experiment(tmp_path / "a2", n_samples=100_000)
    assert same.cmd_sha == base.cmd_sha
    assert same.run_id == base.run_id


# ── the §3 trust-doctrine guard ──────────────────────────────────────────────


def test_guard_refuses_unset_journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_JOURNAL_DIR", raising=False)
    with pytest.raises(SandboxTrustError, match="HPC_JOURNAL_DIR is unset"):
        build_sandbox_experiment(tmp_path / "experiment")
    # The refusal fires BEFORE anything is written.
    assert not (tmp_path / "experiment").exists()


def test_guard_refuses_empty_journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", "")
    with pytest.raises(SandboxTrustError, match="HPC_JOURNAL_DIR is unset"):
        require_sandbox_journal_home()


def test_guard_refuses_production_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prod = Path.home() / ".claude" / "hpc"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(prod))
    with pytest.raises(SandboxTrustError, match="production journal home"):
        build_sandbox_experiment(tmp_path / "experiment")
    assert not (tmp_path / "experiment").exists()


def test_guard_refuses_nested_inside_production_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prod = Path.home() / ".claude" / "hpc"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(prod / "nested" / "namespace"))
    with pytest.raises(SandboxTrustError, match="production journal home"):
        require_sandbox_journal_home()


def test_guard_accepts_ephemeral_and_sibling_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain tmpdir (the CI $RUNNER_TEMP shape) resolves and is returned.
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "ephemeral"))
    assert require_sandbox_journal_home() == (tmp_path / "ephemeral").resolve()

    # A SIBLING of the production home is outside ~/.claude/hpc — journal
    # writes land under <home>/<repo_hash>/ and never reach the production
    # namespace, so the guard passes it.
    sibling = Path.home() / ".claude" / "hpc-sandbox"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(sibling))
    assert require_sandbox_journal_home() == sibling.resolve()


# ── the U6 failing-executor hook + input validation ─────────────────────────


def test_failing_executor_variant_onboards_cleanly(tmp_path: Path, sandbox_journal: Path) -> None:
    exp = build_sandbox_experiment(tmp_path / "experiment", executor_variant="failing")
    assert exp.executor_variant == "failing"
    src = exp.train_py.read_text(encoding="utf-8")
    assert "RuntimeError" in src
    # Same signature as the pi executor — the interview materializes and the
    # swept-flag cross-check passes; the failure is a cluster-side property.
    assert "def run(seed: int = 0, n_samples: int = 100000) -> dict:" in src
    assert exp.tasks_py.is_file()
    assert hpc_agent.load_tasks_module(exp.tasks_py).total() == exp.total_tasks

    # The two variants differ ONLY in the executor body — same recipe, same
    # identity knobs available to the driver.
    pi = build_sandbox_experiment(tmp_path / "pi")
    assert pi.train_py.read_text(encoding="utf-8") != src


def test_unknown_executor_variant_refused(tmp_path: Path, sandbox_journal: Path) -> None:
    with pytest.raises(ValueError, match="unknown executor_variant 'bogus'"):
        build_sandbox_experiment(tmp_path / "experiment", executor_variant="bogus")


def test_empty_seeds_refused(tmp_path: Path, sandbox_journal: Path) -> None:
    with pytest.raises(ValueError, match="seeds must be non-empty"):
        build_sandbox_experiment(tmp_path / "experiment", seeds=())
