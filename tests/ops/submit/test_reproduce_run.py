"""Tests for ``reproduce-run`` (reproduction-receipt wave, task T5, the MINT half).

``reproduce-run`` borrows ``retarget-run``'s NON-BLOCKING shape: re-resolve a
FINISHED run under a NEW run_name (``<orig>-repro``) against the SAME identity,
under a DISJOINT remote_path, and HAND OFF to submit-s2 via ``next_block`` —
returning in seconds. It supersedes NOTHING (a reproduction closes nothing) and
never runs the canary inline (S2's detached worker owns the poll). These assert:

* the DRIFT GUARD, both dimensions — param drift (current vs recorded cmd_sha,
  naming the first differing task index) AND code drift (executor / tasks_py_sha
  via ``detect_code_drift``) each refuse ``SpecInvalid``; plus the legit pass
  (an identical tree proceeds to the mint);
* the derived remote_path DISJOINTNESS property (never equal / a prefix / nested);
* the original's scopes carried VERBATIM onto the repro sidecar spec;
* ``reproduction_of`` + ``reproduces`` stamped through the resolve;
* the non-blocking contract — NO inline canary seam (AST/import), ``force_canary``
  never set, and ``supersede_run`` never imported (a reproduction supersedes
  nothing);
* the hand-off — ``next_block=submit-s2`` + the resolved's ``next_block`` stamp;
* the ``prior_repro_exists`` and ``resolve_blocked`` branches.

Idiom mirrors tests/ops/submit/test_retarget_run.py: a REAL journal /
clusters.yaml via env vars, with compute-run-id + find-prior-run mocked at the
resolve seam (so build-submit-spec runs for real) AND compute-run-id mocked at
the reproduce seam (the drift guard's own materialization).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.reproduce_run import ReproduceRunInput
from hpc_agent.ops.reproduce_run import reproduce_run

if TYPE_CHECKING:
    from pathlib import Path

_RESOLVE_SEAM = "hpc_agent.ops.resolve_submit_inputs"
_REPRO_SEAM = "hpc_agent.ops.reproduce_run"
_ORIG_RUN_ID = "exp-abcd1234"
_ORIG_RUN_NAME = "exp"
_ORIG_CMD_SHA = "a" * 64
_REPRO_RUN_ID = "exp-repro-a1b2c3d4"  # what the mocked compute-run-id mints for the repro
_ORIG_REMOTE = "/scratch/old/exp"

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


def _setup(tmp_path: Path, monkeypatch: Any) -> str:
    """Lay down clusters.yaml + journal + tasks.py + the FINISHED original sidecar.

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
            scopes=["holdout-2026"],
        ),
    )
    return _ORIG_RUN_ID


def _reproduce(tmp_path: Path, **kw: Any) -> Any:
    """Run reproduce-run with BOTH compute-run-id seams mocked to an identical tree.

    The drift guard calls compute-run-id at the reproduce seam (for the ORIGINAL's
    run_name → the recorded cmd_sha, so no param drift), and resolve calls it at
    the resolve seam (for the repro run_name → the minted repro id). tasks_py_sha
    matches the sidecar's, and no interview.json exists (executor dim disabled),
    so the code-drift dimension is clean too.
    """

    def _repro_cr(experiment_dir: Any, *, run_name: str) -> dict[str, Any]:
        # The drift guard asks for the ORIGINAL's run_name → recorded cmd_sha,
        # no run_id change; resolve asks for the repro run_name → the minted id.
        if run_name == _ORIG_RUN_NAME:
            return _cr(run_id=_ORIG_RUN_ID, cmd_sha=_ORIG_CMD_SHA)
        return _cr(run_id=_REPRO_RUN_ID, cmd_sha=_ORIG_CMD_SHA)

    with (
        mock.patch(f"{_REPRO_SEAM}.compute_run_id", side_effect=_repro_cr),
        mock.patch(f"{_REPRO_SEAM}.compute_tasks_py_sha", return_value="1" * 64),
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
    ):
        return reproduce_run(tmp_path, spec=ReproduceRunInput(**kw))


# ── the drift guard ───────────────────────────────────────────────────────────


def test_no_sidecar_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A scope with no resolved-run sidecar is refused — reproduce amends a RESOLVED prior."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(tmp_path / "clusters.yaml"))
    (tmp_path / "clusters.yaml").write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    with pytest.raises(errors.SpecInvalid) as excinfo:
        reproduce_run(tmp_path, spec=ReproduceRunInput(original_run_id=_ORIG_RUN_ID))
    assert "no resolved-run sidecar" in str(excinfo.value)


def test_param_drift_refused_naming_first_differing_task(tmp_path: Path, monkeypatch: Any) -> None:
    """A CURRENT tree whose cmd_sha differs from the recorded one is refused, naming
    BOTH shas + the first differing task index (from the trial_params pre-image)."""
    old = _setup(tmp_path, monkeypatch)
    drifted = _cr(run_id=_ORIG_RUN_ID, cmd_sha="b" * 64)
    drifted["trial_params"] = [{"seed": 0}, {"seed": 99}]  # task 1 diverges
    with (
        mock.patch(f"{_REPRO_SEAM}.compute_run_id", return_value=drifted),
        pytest.raises(errors.SpecInvalid) as excinfo,
    ):
        reproduce_run(tmp_path, spec=ReproduceRunInput(original_run_id=old))
    msg = str(excinfo.value)
    assert "DRIFTED" in msg
    assert "a" * 64 in msg and "b" * 64 in msg
    assert "first differing task index 1" in msg


def test_code_drift_executor_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """cmd_sha matches but the EXECUTOR drifted → refused (cmd_sha is param identity
    only; an executor-body edit is not a reproduction, finding 3)."""
    old = _setup(tmp_path, monkeypatch)
    with (
        mock.patch(
            f"{_REPRO_SEAM}.compute_run_id",
            return_value=_cr(run_id=_ORIG_RUN_ID, cmd_sha=_ORIG_CMD_SHA),
        ),
        mock.patch(f"{_REPRO_SEAM}.compute_tasks_py_sha", return_value="1" * 64),
        mock.patch(
            f"{_REPRO_SEAM}._materialized_executor_cmd",
            return_value="python -m src.exp --seed $seed --DIFFERENT",
        ),
        pytest.raises(errors.SpecInvalid) as excinfo,
    ):
        reproduce_run(tmp_path, spec=ReproduceRunInput(original_run_id=old))
    msg = str(excinfo.value)
    assert "CODE" in msg and "DRIFTED" in msg
    assert "executor" in msg


def test_code_drift_tasks_py_sha_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """cmd_sha matches but tasks.py BYTES drifted (tasks_py_sha) → refused."""
    old = _setup(tmp_path, monkeypatch)
    # current tasks_py_sha 2*64 != recorded 1*64 → code drift.
    with (
        mock.patch(
            f"{_REPRO_SEAM}.compute_run_id",
            return_value=_cr(run_id=_ORIG_RUN_ID, cmd_sha=_ORIG_CMD_SHA),
        ),
        mock.patch(f"{_REPRO_SEAM}.compute_tasks_py_sha", return_value="2" * 64),
        pytest.raises(errors.SpecInvalid) as excinfo,
    ):
        reproduce_run(tmp_path, spec=ReproduceRunInput(original_run_id=old))
    msg = str(excinfo.value)
    assert "CODE" in msg and "DRIFTED" in msg
    assert "tasks.py" in msg


def test_identical_tree_proceeds_to_mint(tmp_path: Path, monkeypatch: Any) -> None:
    """An identical tree (matching cmd_sha + tasks_py_sha, no interview) passes the
    drift guard and mints the reproduction, handing off to submit-s2."""
    old = _setup(tmp_path, monkeypatch)
    res = _reproduce(tmp_path, original_run_id=old)
    assert res.stage_reached == "repro_pending_canary"
    assert res.needs_decision is True
    assert res.run_id == _REPRO_RUN_ID
    assert res.reproduces == old


# ── the composition on the legit path ─────────────────────────────────────────


def test_disjoint_remote_path_property(tmp_path: Path, monkeypatch: Any) -> None:
    """The derived remote_path is <orig>-repro AND genuinely disjoint — never equal,
    a path-prefix, or nested under the original (finding 4)."""
    from hpc_agent.ops.reproduce_run import _repro_remote_path

    derived = _repro_remote_path(_ORIG_REMOTE)
    assert derived == _ORIG_REMOTE + "-repro"
    # Disjoint: neither is a path-ancestor of the other.
    assert derived != _ORIG_REMOTE
    assert not derived.startswith(_ORIG_REMOTE + "/")
    assert not _ORIG_REMOTE.startswith(derived + "/")

    old = _setup(tmp_path, monkeypatch)
    res = _reproduce(tmp_path, original_run_id=old)
    submit_spec = res.brief["resolve"]["submit_spec"]
    assert submit_spec["remote_path"] == _ORIG_REMOTE + "-repro"
    assert res.brief["remote_path"] == _ORIG_REMOTE + "-repro"


def test_scopes_carried_verbatim_onto_repro_sidecar(tmp_path: Path, monkeypatch: Any) -> None:
    """The original's scopes ride onto the reproduction's sidecar spec VERBATIM (the
    scope gate composes on the repro, finding 7)."""
    old = _setup(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    real = __import__(
        "hpc_agent.ops.reproduce_run", fromlist=["resolve_submit_inputs"]
    ).resolve_submit_inputs

    def _capture(experiment_dir: Any, *, spec: Any) -> Any:
        captured["scopes"] = spec.sidecar.scopes
        captured["reproduces"] = spec.sidecar.reproduces
        captured["reproduction_of"] = spec.reproduction_of
        return real(experiment_dir, spec=spec)

    with mock.patch(f"{_REPRO_SEAM}.resolve_submit_inputs", side_effect=_capture):
        _reproduce_with_capture(tmp_path, old)

    assert captured["scopes"] == ["holdout-2026"]
    assert captured["reproduces"] == old
    assert captured["reproduction_of"] == old


def _reproduce_with_capture(tmp_path: Path, old: str) -> Any:
    """_reproduce, but leaving resolve_submit_inputs already patched by the caller."""

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


def test_reproduction_of_and_reproduces_stamped(tmp_path: Path, monkeypatch: Any) -> None:
    """The written reproduction sidecar records reproduces=<original> (the resolve
    stamps it from reproduction_of), and the submit spec carries reproduction_of."""
    old = _setup(tmp_path, monkeypatch)
    res = _reproduce(tmp_path, original_run_id=old)
    # The submit-flow spec carries the reproduction_of dedup lever.
    assert res.brief["resolve"]["submit_spec"]["reproduction_of"] == old
    # The written sidecar records the reproduces provenance back-link.
    from hpc_agent.state.runs import read_run_sidecar

    repro_sidecar = read_run_sidecar(tmp_path, _REPRO_RUN_ID)
    assert repro_sidecar["reproduces"] == old
    assert repro_sidecar["scopes"] == ["holdout-2026"]


def test_hands_off_to_submit_s2_with_resolved_stamp(tmp_path: Path, monkeypatch: Any) -> None:
    """The hand-off names submit-s2 with the repro run_id, and the resolved carries
    the next_block stamp assert_greenlit_target reads."""
    old = _setup(tmp_path, monkeypatch)
    res = _reproduce(tmp_path, original_run_id=old)
    assert res.next_block is not None
    assert res.next_block["verb"] == "submit-s2"
    assert res.next_block["spec_hint"] == {"run_id": _REPRO_RUN_ID}
    assert res.brief["resolved"]["next_block"] == "submit-s2"
    assert "PENDING" in res.reason


# ── branches ──────────────────────────────────────────────────────────────────


def test_resolve_blocked_short_circuits_before_handoff(tmp_path: Path, monkeypatch: Any) -> None:
    """An UNRELATED live same-params prior makes the fresh resolve surface its own
    decision → resolve_blocked with NO hand-off (next_block null); supersedes nothing."""
    old = _setup(tmp_path, monkeypatch)

    def _repro_cr(experiment_dir: Any, *, run_name: str) -> dict[str, Any]:
        if run_name == _ORIG_RUN_NAME:
            return _cr(run_id=_ORIG_RUN_ID, cmd_sha=_ORIG_CMD_SHA)
        return _cr(run_id=_REPRO_RUN_ID, cmd_sha=_ORIG_CMD_SHA)

    with (
        mock.patch(f"{_REPRO_SEAM}.compute_run_id", side_effect=_repro_cr),
        mock.patch(f"{_REPRO_SEAM}.compute_tasks_py_sha", return_value="1" * 64),
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_RESOLVE_SEAM}.find_prior_run",
            return_value=_fp(
                found=True, is_orphan=False, status="in_flight", prior_run_id="other-run-9999"
            ),
        ),
    ):
        res = reproduce_run(tmp_path, spec=ReproduceRunInput(original_run_id=old))
    assert res.stage_reached == "resolve_blocked"
    assert res.needs_decision is True
    assert res.next_block is None
    assert res.reproduces == old


def test_prior_repro_exists_directs_to_verify(tmp_path: Path, monkeypatch: Any) -> None:
    """A COMPLETE reproduction already at the derived run_id → prior_repro_exists,
    directing to verify-reproduction, with NO re-mint (next_block null)."""
    old = _setup(tmp_path, monkeypatch)

    # Lay down a COMPLETE prior reproduction at the derived run_id.
    from hpc_agent.ops.write_run_sidecar import write_run_sidecar
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    write_run_sidecar(
        experiment_dir=tmp_path,
        spec=WriteRunSidecarInput(
            run_id=_REPRO_RUN_ID,
            cmd_sha=_ORIG_CMD_SHA,
            executor="python -m src.exp --seed $seed",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=2,
            cluster="h2old",
            remote_path=_ORIG_REMOTE + "-repro",
            reproduces=old,
        ),
    )
    upsert_run(
        tmp_path,
        RunRecord(
            run_id=_REPRO_RUN_ID,
            profile="exp",
            cluster="h2old",
            ssh_target="me@old.example.edu",
            remote_path=_ORIG_REMOTE + "-repro",
            job_name="exp-repro",
            job_ids=[],
            total_tasks=2,
            submitted_at="2026-07-05T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status="complete",
            backend="sge",
        ),
    )

    def _repro_cr(experiment_dir: Any, *, run_name: str) -> dict[str, Any]:
        if run_name == _ORIG_RUN_NAME:
            return _cr(run_id=_ORIG_RUN_ID, cmd_sha=_ORIG_CMD_SHA)
        return _cr(run_id=_REPRO_RUN_ID, cmd_sha=_ORIG_CMD_SHA)

    with (
        mock.patch(f"{_REPRO_SEAM}.compute_run_id", side_effect=_repro_cr),
        mock.patch(f"{_REPRO_SEAM}.compute_tasks_py_sha", return_value="1" * 64),
    ):
        res = reproduce_run(tmp_path, spec=ReproduceRunInput(original_run_id=old))
    assert res.stage_reached == "prior_repro_exists"
    assert res.needs_decision is True
    assert res.next_block is None
    assert res.run_id == _REPRO_RUN_ID
    assert "verify-reproduction" in res.reason


def test_explicit_new_run_name_forces_fresh(tmp_path: Path, monkeypatch: Any) -> None:
    """An explicit new_run_name is threaded to the repro resolve (overriding the
    code-derived <orig>-repro default)."""
    old = _setup(tmp_path, monkeypatch)
    seen: dict[str, Any] = {}

    def _repro_cr(experiment_dir: Any, *, run_name: str) -> dict[str, Any]:
        seen.setdefault("names", []).append(run_name)
        if run_name == _ORIG_RUN_NAME:
            return _cr(run_id=_ORIG_RUN_ID, cmd_sha=_ORIG_CMD_SHA)
        return _cr(run_id=_REPRO_RUN_ID, cmd_sha=_ORIG_CMD_SHA)

    with (
        mock.patch(f"{_REPRO_SEAM}.compute_run_id", side_effect=_repro_cr),
        mock.patch(f"{_REPRO_SEAM}.compute_tasks_py_sha", return_value="1" * 64),
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()) as cri,
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
    ):
        reproduce_run(
            tmp_path,
            spec=ReproduceRunInput(original_run_id=old, new_run_name="exp-rerun-2026"),
        )
    assert "exp-rerun-2026" in seen["names"]
    assert cri.call_args.kwargs["run_name"] == "exp-rerun-2026"


# ── the non-blocking / no-supersession structural contracts ───────────────────


def test_reproduce_module_has_no_inline_canary_seam() -> None:
    """The non-blocking contract, pinned structurally (the retarget sibling): the
    reproduce module must not import (or call) submit-and-verify — the canary
    belongs to submit-s2's detached worker."""
    from pathlib import Path as _Path

    import hpc_agent.ops.reproduce_run as m

    assert not hasattr(m, "submit_and_verify")
    src = _Path(m.__file__).read_text(encoding="utf-8")
    assert "stop_after_canary" not in src
    assert "force_canary" not in src  # the validated-fresh canary skip is legitimate


def test_reproduce_module_does_not_supersede() -> None:
    """A reproduction closes nothing — supersede_run is NEVER imported/called
    (decision record, finding 2)."""
    from pathlib import Path as _Path

    import hpc_agent.ops.reproduce_run as m

    assert not hasattr(m, "supersede_run")
    src = _Path(m.__file__).read_text(encoding="utf-8")
    assert "supersede_run" not in src


def test_result_declares_next_block_for_mcp_curation() -> None:
    """The Result model declares a ``next_block`` field — what derives reproduce-run
    into the curated MCP catalog (the run-#8 MCP-reachability lesson)."""
    from hpc_agent._wire.workflows.reproduce_run import ReproduceRunResult

    assert "next_block" in ReproduceRunResult.model_fields


# ── derived subsets (T6, design center 5) ─────────────────────────────────────


def _write_axes(experiment_dir: Path, axes: list[dict[str, Any]]) -> None:
    from hpc_agent.state.axes import write_axes

    write_axes(experiment_dir, axes=axes)


def test_derive_stride_subset_is_pure_canary_and_per_axis() -> None:
    """The derivation is a PURE function of axis structure: canary task 0 + one
    task per distinct axis value at that axis's row-major stride; reproducible."""
    import tempfile
    from pathlib import Path as _Path

    from hpc_agent.state.axes import derive_stride_subset

    with tempfile.TemporaryDirectory() as td:
        exp = _Path(td)
        _write_axes(exp, [{"name": "a", "size": 2}, {"name": "b", "size": 3}])
        # strides = [3, 1] (last axis varies fastest). axis a: {0, 3}; axis b:
        # {0, 1, 2}; ∪ {0 canary} = [0, 1, 2, 3].
        first = derive_stride_subset(exp)
        assert first == [0, 1, 2, 3]
        assert 0 in first  # the canary is always present
        assert derive_stride_subset(exp) == first  # deterministic / reproducible


def test_derive_stride_subset_refuses_without_axes(tmp_path: Path) -> None:
    """Derived mode cannot invent a subset without the axis structure."""
    from hpc_agent.state.axes import derive_stride_subset

    with pytest.raises(errors.SpecInvalid):
        derive_stride_subset(tmp_path)


def test_caller_list_wins_and_threads_both_seams(tmp_path: Path, monkeypatch: Any) -> None:
    """An explicit caller list wins over the derived mode, and threads through BOTH
    seams: HPC_TASK_INCLUDE on the job env (execution restriction) AND
    extra.task_sample on the sidecar (T5's per-task read)."""
    old = _setup(tmp_path, monkeypatch)
    # An axes.yaml exists (derived mode would pick [0, 1]); the caller list [1]
    # must WIN — distinct from the derived set, so the win is observable.
    _write_axes(tmp_path, [{"name": "seed", "size": 2}])
    res = _reproduce(tmp_path, original_run_id=old, task_sample=[1])

    submit_spec = res.brief["resolve"]["submit_spec"]
    assert submit_spec["job_env"]["HPC_TASK_INCLUDE"] == "1"  # caller list, NOT derived [0,1]
    # The array stays FULL-SIZE — subsetting restricts execution, never the shape
    # (the identity constraint: a rebuilt smaller task list would move cmd_sha).
    assert submit_spec["total_tasks"] == 2

    from hpc_agent.state.runs import read_run_sidecar

    repro_sidecar = read_run_sidecar(tmp_path, _REPRO_RUN_ID)
    assert repro_sidecar["extra"]["task_sample"] == [1]
    assert res.brief["partial"] is True
    assert res.brief["task_sample"] == [1]
    assert res.brief["uncompared_task_count"] == 1


def test_derived_mode_threads_both_seams(tmp_path: Path, monkeypatch: Any) -> None:
    """task_sample='derived' selects the mechanical per-axis subset and records it
    on both the job env and the sidecar."""
    old = _setup(tmp_path, monkeypatch)
    _write_axes(tmp_path, [{"name": "seed", "size": 2}])  # product == sidecar task_count 2
    res = _reproduce(tmp_path, original_run_id=old, task_sample="derived")

    submit_spec = res.brief["resolve"]["submit_spec"]
    assert submit_spec["job_env"]["HPC_TASK_INCLUDE"] == "0,1"

    from hpc_agent.state.runs import read_run_sidecar

    repro_sidecar = read_run_sidecar(tmp_path, _REPRO_RUN_ID)
    assert repro_sidecar["extra"]["task_sample"] == [0, 1]


def test_recorded_subset_readable_by_t5_partial_path(tmp_path: Path, monkeypatch: Any) -> None:
    """The indices reproduce-run records are exactly what verify-reproduction's
    per-task load path reads back (extra['task_sample'])."""
    from hpc_agent.ops.verify_reproduction import _partial_indices
    from hpc_agent.state.runs import read_run_sidecar

    old = _setup(tmp_path, monkeypatch)
    _reproduce(tmp_path, original_run_id=old, task_sample=[1, 0])
    repro_sidecar = read_run_sidecar(tmp_path, _REPRO_RUN_ID)
    assert _partial_indices(repro_sidecar) == [0, 1]


def test_subset_out_of_range_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A caller index outside the run's [0, task_count) range is refused — a partial
    reproduction can only select tasks the run has."""
    old = _setup(tmp_path, monkeypatch)  # task_count == 2
    with pytest.raises(errors.SpecInvalid) as excinfo:
        _reproduce(tmp_path, original_run_id=old, task_sample=[0, 5])
    assert "outside the run's range" in str(excinfo.value)


def test_full_reproduction_has_no_include_or_partiality(tmp_path: Path, monkeypatch: Any) -> None:
    """task_sample=None (default) reproduces the FULL list — no HPC_TASK_INCLUDE,
    no sidecar extra.task_sample, partial=False."""
    old = _setup(tmp_path, monkeypatch)
    res = _reproduce(tmp_path, original_run_id=old)

    submit_spec = res.brief["resolve"]["submit_spec"]
    assert "HPC_TASK_INCLUDE" not in (submit_spec.get("job_env") or {})
    assert res.brief["partial"] is False
    assert res.brief["task_sample"] is None
    assert res.brief["uncompared_task_count"] == 0

    from hpc_agent.state.runs import read_run_sidecar

    repro_sidecar = read_run_sidecar(tmp_path, _REPRO_RUN_ID)
    assert "task_sample" not in (repro_sidecar.get("extra") or {})
