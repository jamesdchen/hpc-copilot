"""Derived-enumerated-run unit tests [SPEC §3 Step B, §8, LIVE-4].

Pins the four named acceptance numbers:

1. 684 undone cells → enumerated tasks.py at ``.hpc/migrate/<rid>/tasks.py``,
   ``total() == 684``, ``resolve(0)`` == the first undone cell;
2. the shared ``.hpc/tasks.py`` is BYTE-UNCHANGED after derive (singleton hazard);
3. the derived run declares ``parents=[source]``, ``node_sha != source cmd_sha``,
   and a missing source sidecar REFUSES;
4. the ownership map covers all 900 cells exactly once (undone→derived, done→source).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import compute_cmd_sha, errors, load_tasks_module
from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.ops.migrate.derive import derive_enumerated_run

SOURCE = "20260716-000000-" + "0b5ef197"[:8].ljust(8, "0")
DERIVED = "20260716-010000-" + "d" * 8
TARGET = "carc"

# The live case: 900 bucket-major cells (5 buckets × 100 waves × ...), first 216
# done on the source, the remaining 684 to migrate.
TOTAL = 900
DONE_IDS = list(range(216))
UNDONE_IDS = list(range(216, TOTAL))

_SOURCE_TASKS_SRC = (
    "_TASKS = [{'cell': k, 'bucket': k // 100} for k in range(900)]\n"
    "def total() -> int: return len(_TASKS)\n"
    "def resolve(i: int) -> dict: return _TASKS[i]\n"
)


def _make_source_experiment(tmp_path: Path, *, write_sidecar: bool = True) -> Path:
    """A minimal experiment: source .hpc/tasks.py (900 cells) + source sidecar."""
    exp = tmp_path / "exp"
    layout = RepoLayout(exp)
    layout.tasks.parent.mkdir(parents=True, exist_ok=True)
    layout.tasks.write_text(_SOURCE_TASKS_SRC, encoding="utf-8")
    if write_sidecar:
        src_mod = load_tasks_module(layout.tasks)
        src_cmd_sha = compute_cmd_sha(src_mod)
        import json

        layout.run_sidecar(SOURCE).write_text(
            json.dumps({"cmd_sha": src_cmd_sha, "cluster": "hoffman2"}),
            encoding="utf-8",
        )
    return exp


def _source_cmd_sha(exp: Path) -> str:
    sha: str = compute_cmd_sha(load_tasks_module(RepoLayout(exp).tasks))
    return sha


# ── Acceptance 1: enumerated derived tasks.py, per-run path, correct cells ────


def test_derive_materializes_684_undone_cells_per_run() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        exp = _make_source_experiment(Path(td))
        res = derive_enumerated_run(
            exp,
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
            target_cluster=TARGET,
            undone_ids=UNDONE_IDS,
            done_ids=DONE_IDS,
        )
        assert res.task_count == 684
        # Per-run path, NOT the shared singleton.
        assert res.tasks_py_path == exp.resolve() / ".hpc" / "migrate" / DERIVED / "tasks.py"
        assert res.tasks_py_path.is_file()

        derived_mod = load_tasks_module(res.tasks_py_path)
        assert derived_mod.total() == 684
        # resolve(0) == the first undone cell (source id 216, bucket 2).
        assert derived_mod.resolve(0) == {"cell": 216, "bucket": 2}
        # resolve(last) == the last undone cell (source id 899).
        assert derived_mod.resolve(683) == {"cell": 899, "bucket": 8}
        # The preview reports the correct source-global first-undone id.
        assert res.preview["first_undone_cell_id"] == 216
        assert res.preview["first"] == {"cell": 216, "bucket": 2}


# ── Acceptance 2: the singleton is byte-unchanged (the LIVE-4 hazard) ─────────


def test_shared_singleton_is_byte_unchanged(tmp_path: Path) -> None:
    exp = _make_source_experiment(tmp_path)
    shared = RepoLayout(exp).tasks
    before = shared.read_bytes()
    res = derive_enumerated_run(
        exp,
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
        target_cluster=TARGET,
        undone_ids=UNDONE_IDS,
        done_ids=DONE_IDS,
    )
    after = shared.read_bytes()
    assert after == before, "derive must NOT touch the shared .hpc/tasks.py singleton"
    # The derived tasks.py is a DIFFERENT file at a per-run path.
    assert res.tasks_py_path != shared
    # The flip-back backs up the singleton executably, and discloses the sequence.
    assert res.flip_back.required is True
    assert res.flip_back.singleton_backup is not None
    assert res.flip_back.singleton_backup.read_bytes() == before
    assert res.flip_back.singleton_untouched_by_derive is True
    assert "GATED" in res.flip_back.gated_clean_fix


# ── Acceptance 3: lineage identity + missing-sidecar refusal ──────────────────


def test_derived_declares_parents_and_derives_node_sha(tmp_path: Path) -> None:
    exp = _make_source_experiment(tmp_path)
    src_cmd_sha = _source_cmd_sha(exp)
    res = derive_enumerated_run(
        exp,
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
        target_cluster=TARGET,
        undone_ids=UNDONE_IDS,
        done_ids=DONE_IDS,
    )
    assert res.parents == [SOURCE]
    # Distinct identity, not a resume-reattach: derived cmd_sha differs from source.
    assert res.cmd_sha != src_cmd_sha
    # node_sha is DERIVED from the source sidecar and differs from both cmd_shas.
    assert res.node_sha is not None
    assert res.node_sha != src_cmd_sha
    assert res.node_sha != res.cmd_sha
    # The minted interview spec carries the enumerated recipe + the undone count.
    assert res.interview_spec["task_count"] == 684
    assert res.interview_spec["task_generator"]["kind"] == "enumerated"
    assert len(res.interview_spec["task_generator"]["params"]["items"]) == 684


def test_missing_source_sidecar_refuses(tmp_path: Path) -> None:
    exp = _make_source_experiment(tmp_path, write_sidecar=False)
    with pytest.raises(errors.SpecInvalid, match="no sidecar"):
        derive_enumerated_run(
            exp,
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
            target_cluster=TARGET,
            undone_ids=UNDONE_IDS,
            done_ids=DONE_IDS,
        )


# ── Acceptance 4: ownership map covers all 900 exactly once ───────────────────


def test_ownership_map_persisted_and_covers_900_exactly_once(tmp_path: Path) -> None:
    exp = _make_source_experiment(tmp_path)
    res = derive_enumerated_run(
        exp,
        source_run_id=SOURCE,
        derived_run_id=DERIVED,
        target_cluster=TARGET,
        undone_ids=UNDONE_IDS,
        done_ids=DONE_IDS,
    )
    # Migrate-scoped artifact, NOT a sidecar write.
    assert res.ownership_path == exp.resolve() / ".hpc" / "migrate" / DERIVED / "ownership.json"
    assert res.ownership_path.is_file()

    om = res.ownership
    assert om is not None
    assert set(om.owner) == set(range(900))
    assert all(om.owner_of(c) == DERIVED for c in UNDONE_IDS)
    assert all(om.owner_of(c) == SOURCE for c in DONE_IDS)
    assert res.ownership_digest["source_cells"] == 216
    assert res.ownership_digest["derived_cells"] == 684
    assert res.ownership_digest["exactly_once"] is True


# ── Refusals: empty remainder, corrupt census ─────────────────────────────────


def test_empty_undone_refuses(tmp_path: Path) -> None:
    exp = _make_source_experiment(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="nothing to migrate"):
        derive_enumerated_run(
            exp,
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
            target_cluster=TARGET,
            undone_ids=[],
            done_ids=list(range(TOTAL)),
        )


def test_corrupt_census_non_partition_refuses(tmp_path: Path) -> None:
    exp = _make_source_experiment(tmp_path)
    # done + undone leave cells uncovered → the ownership validator refuses.
    with pytest.raises(errors.SpecInvalid):
        derive_enumerated_run(
            exp,
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
            target_cluster=TARGET,
            undone_ids=[216, 217],
            done_ids=list(range(216)),
        )


def test_missing_source_tasks_py_refuses(tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    _ = RepoLayout(exp).hpc  # create .hpc but no tasks.py
    # Write the sidecar so we get past the sidecar check to the tasks.py check.
    import json

    RepoLayout(exp).run_sidecar(SOURCE).write_text(
        json.dumps({"cmd_sha": "a" * 64}), encoding="utf-8"
    )
    with pytest.raises(errors.SpecInvalid, match="tasks.py not found"):
        derive_enumerated_run(
            exp,
            source_run_id=SOURCE,
            derived_run_id=DERIVED,
            target_cluster=TARGET,
            undone_ids=UNDONE_IDS,
            done_ids=DONE_IDS,
        )
