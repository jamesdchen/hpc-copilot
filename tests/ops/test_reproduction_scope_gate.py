"""Cross-feature composition tests: reproduce-run × scope-gate × block-gate (T7).

``reproduce-run`` carries the original's caller-attached ``scopes`` VERBATIM onto
the reproduction's sidecar spec (``ops/reproduce_run`` step 4), and stamps
``resolved["next_block"] = "submit-s2"`` on its greenlight hand-off. Neither the
scope gate nor the block gate knows anything about reproductions — yet both
COMPOSE for free precisely because the repro sidecar reconstructs the original's
run-owned inputs:

* :func:`hpc_agent.ops.scope_gate.assert_scopes_unlocked` (the one reduction-seam
  precondition) reads the run sidecar's ``scopes`` and refuses when any is locked.
  Because the tag rides the repro sidecar, a lock on the original's scope refuses
  the REPRODUCTION's reduce too — the rigor precondition is inherited, not
  re-implemented (reproduce-run finding 7).
* :func:`hpc_agent.ops.block_gate.assert_greenlit_target` (the sequenced-block
  precondition) passes when the latest run-scoped ``y`` names the verb via
  ``resolved.next_block``. reproduce-run's ``next_block=submit-s2`` hand-off is the
  retarget-style contract, so journaling the repro's greenlight greenlights S2 on
  the repro run_id exactly as it would on a fresh run.

Idiom mirrors tests/ops/submit/test_reproduce_run.py: a REAL journal /
clusters.yaml via env vars, fixtures through the real sidecar/journal writers,
with compute-run-id + find-prior-run mocked at the module boundaries (no SSH,
cluster-free). The gates run for real against the on-disk repro sidecar the mint
actually wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.reproduce_run import ReproduceRunInput
from hpc_agent.ops.block_gate import assert_greenlit_target
from hpc_agent.ops.reproduce_run import reproduce_run
from hpc_agent.ops.scope_gate import assert_scopes_unlocked
from hpc_agent.state import scopes as scope_state
from hpc_agent.state.runs import read_run_sidecar
from tests.ops._block_fixtures import greenlight

if TYPE_CHECKING:
    from pathlib import Path

_RESOLVE_SEAM = "hpc_agent.ops.resolve_submit_inputs"
_REPRO_SEAM = "hpc_agent.ops.reproduce_run"
_ORIG_RUN_ID = "exp-abcd1234"
_ORIG_RUN_NAME = "exp"
_ORIG_CMD_SHA = "a" * 64
_REPRO_RUN_ID = "exp-repro-a1b2c3d4"  # what the mocked compute-run-id mints for the repro
_ORIG_REMOTE = "/scratch/old/exp"
_SCOPE_TAG = "holdout-2026"

_CLUSTERS_YAML = """\
h2old:
  scheduler: sge
  host: old.example.edu
  user: me
  scratch: /scratch/old
  conda_source: /opt/old/conda.sh
  conda_envs: [old_env]
"""


def _cr(run_id: str = _REPRO_RUN_ID, cmd_sha: str = _ORIG_CMD_SHA) -> dict[str, Any]:
    """compute-run-id's return (mocked) — mints the repro id, echoes the params."""
    return {
        "run_id": run_id,
        "cmd_sha": cmd_sha,
        "trial_tokens": None,
        "trial_params": [{"seed": 0}, {"seed": 1}],
        "total": 2,
    }


def _fp(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "found": False,
        "prior_run_id": None,
        "is_orphan": False,
        "status": None,
        "age_sec": None,
        "profile": None,
        "cluster": None,
        "job_ids": [],
        "campaign_id": None,
        "submitted_at": None,
    }
    base.update(over)
    return base


def _setup(tmp_path: Path, monkeypatch: Any, *, scopes: list[str] | None) -> str:
    """Lay down clusters.yaml + journal + tasks.py + the FINISHED original sidecar.

    *scopes* is stamped VERBATIM onto the original's sidecar (``None`` = untagged).
    Returns the original run_id being reproduced.
    """
    from hpc_agent.ops.write_run_sidecar import write_run_sidecar

    clusters = tmp_path / "clusters.yaml"
    clusters.write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))

    hpc = tmp_path / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text("# stub\n", encoding="utf-8")

    write_run_sidecar(
        experiment_dir=tmp_path,
        spec=WriteRunSidecarInput(
            run_id=_ORIG_RUN_ID,
            cmd_sha=_ORIG_CMD_SHA,
            executor="python -m src.exp --seed $seed",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=2,
            cluster="h2old",
            profile="exp",
            remote_path=_ORIG_REMOTE,
            resources={"walltime_sec": 3600, "cpus": 2},
            env={"conda_env": "old_env"},
            tasks_py_sha="1" * 64,
            trial_params=[{"seed": 0}, {"seed": 1}],  # the cmd_sha pre-image
            scopes=scopes,
        ),
    )
    return _ORIG_RUN_ID


def _reproduce(tmp_path: Path, old: str) -> Any:
    """Run reproduce-run with BOTH compute-run-id seams mocked to an identical tree.

    The drift guard's reproduce-seam compute-run-id returns the ORIGINAL's recorded
    cmd_sha for the original run_name (no param drift) and the minted repro id for
    the repro run_name; tasks_py_sha matches the sidecar's and no interview.json
    exists (executor dim disabled), so code drift is clean. The mint therefore
    reaches ``repro_pending_canary`` and writes the repro sidecar on disk.
    """

    def _repro_cr(experiment_dir: Any, *, run_name: str) -> dict[str, Any]:
        if run_name == _ORIG_RUN_NAME:
            return _cr(run_id=_ORIG_RUN_ID, cmd_sha=_ORIG_CMD_SHA)
        return _cr(run_id=_REPRO_RUN_ID, cmd_sha=_ORIG_CMD_SHA)

    with (
        mock.patch(f"{_REPRO_SEAM}.compute_run_id", side_effect=_repro_cr),
        mock.patch(f"{_REPRO_SEAM}.compute_tasks_py_sha", return_value="1" * 64),
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
    ):
        return reproduce_run(tmp_path, spec=ReproduceRunInput(original_run_id=old))


# ── scope-gate composition ────────────────────────────────────────────────────


def test_repro_of_locked_scope_run_is_refused_at_its_reduce(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A locked original-scope refuses the REPRODUCTION's reduce — for FREE, because
    the tag rode the repro sidecar verbatim (reproduce-run finding 7 × scope-gate T3).

    The reproduce mint carried ``scopes=[T]`` onto the repro sidecar (assert that
    reconstruction), then a lock on T makes ``assert_scopes_unlocked`` — the one
    reduction-seam precondition ``aggregate_flow`` calls — refuse the repro's run_id.
    """
    old = _setup(tmp_path, monkeypatch, scopes=[_SCOPE_TAG])
    res = _reproduce(tmp_path, old)
    assert res.stage_reached == "repro_pending_canary"
    assert res.run_id == _REPRO_RUN_ID

    # The scope rode onto the reproduction's ON-DISK sidecar verbatim.
    repro_sidecar = read_run_sidecar(tmp_path, _REPRO_RUN_ID)
    assert repro_sidecar["scopes"] == [_SCOPE_TAG]

    # Lock the ORIGINAL's scope; the gate composes on the REPRO's run_id.
    scope_state.record_lock(tmp_path, _SCOPE_TAG, reason="embargo until preregistration")
    with pytest.raises(errors.ScopeLocked) as ei:
        assert_scopes_unlocked(tmp_path, _REPRO_RUN_ID)
    assert _SCOPE_TAG in str(ei.value)  # the gate named the inherited tag


def test_repro_of_unlocked_scope_reduces_cleanly(tmp_path: Path, monkeypatch: Any) -> None:
    """The legit-pass twin: the same inherited scope, UNLOCKED, lets the repro's
    reduce precondition pass silently (fail-safe — the tag rode over but is free)."""
    old = _setup(tmp_path, monkeypatch, scopes=[_SCOPE_TAG])
    res = _reproduce(tmp_path, old)
    assert res.run_id == _REPRO_RUN_ID

    repro_sidecar = read_run_sidecar(tmp_path, _REPRO_RUN_ID)
    assert repro_sidecar["scopes"] == [_SCOPE_TAG]
    assert scope_state.is_scope_locked(tmp_path, _SCOPE_TAG) is False

    # Never locked → the reduction-seam precondition passes on the repro run_id.
    assert_scopes_unlocked(tmp_path, _REPRO_RUN_ID)  # no raise


# ── block-gate composition ────────────────────────────────────────────────────


def test_repro_greenlight_composes_with_block_gate(tmp_path: Path, monkeypatch: Any) -> None:
    """reproduce-run's ``next_block=submit-s2`` hand-off greenlights S2 on the repro
    run_id — the retarget-style contract, proven for repro (× block-gate design §2).

    The success result carries ``resolved.next_block == "submit-s2"``; journaling
    that greenlight for the repro run_id makes ``assert_greenlit_target`` PASS for
    ``submit-s2`` on that run_id (its shared-journal newest-first read)."""
    old = _setup(tmp_path, monkeypatch, scopes=[_SCOPE_TAG])
    res = _reproduce(tmp_path, old)
    assert res.brief["resolved"]["next_block"] == "submit-s2"
    assert res.next_block is not None and res.next_block["verb"] == "submit-s2"
    assert res.run_id == _REPRO_RUN_ID

    # Journal the human's `y` naming submit-s2 for the REPRO run_id (the hand-off's
    # greenlight), exactly as the block loop would after surfacing the repro brief.
    greenlight(tmp_path, "submit-s2", run_id=_REPRO_RUN_ID)

    # The sequenced-block precondition passes for submit-s2 on the repro run_id.
    assert_greenlit_target(tmp_path, run_id=_REPRO_RUN_ID, verb="submit-s2", predecessor="S1")


# ── the untagged twin (no phantom scope) ──────────────────────────────────────


def test_repro_scopes_absent_when_original_untagged(tmp_path: Path, monkeypatch: Any) -> None:
    """An UNTAGGED original yields an UNTAGGED repro — no phantom scopes tag is
    conjured, and the reduction-seam precondition passes (scope-less → PASS)."""
    old = _setup(tmp_path, monkeypatch, scopes=None)
    res = _reproduce(tmp_path, old)
    assert res.run_id == _REPRO_RUN_ID

    repro_sidecar = read_run_sidecar(tmp_path, _REPRO_RUN_ID)
    assert repro_sidecar.get("scopes") is None  # no phantom tag inherited
    assert_scopes_unlocked(tmp_path, _REPRO_RUN_ID)  # scope-less → passes
