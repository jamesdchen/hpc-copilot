"""``migrate-remainder`` verb unit tests [SPEC §3 Step D, M-BRIEF acceptance].

Pins the M-BRIEF acceptance list:

1. a 216/900 source → ``needs_decision=True``, ``next_block=submit-s2``,
   ``resolved["next_block"] == "submit-s2"``, and the verb returns fast (no inline
   canary — the census seam is stubbed, so no SSH);
2. the brief carries ``undone=684``, an ``est_core_hours`` derived from the
   source-observed canary runtime, the ``footprint_unknown`` honesty flag, and the
   what-dies task range;
3. a same-cluster target → REFUSE (nothing to migrate; route to revise/resubmit);
4. a missing source sidecar / an empty undone set → REFUSE;
5. the brief is PERSISTED (append_brief) so the rule-9 provenance gate can diff the y.

The census (the source's per-task done-set) is the ONE SSH-touching seam; every
test stubs ``_census_source`` so the composition is exercised without a cluster.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent import errors, load_tasks_module
from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent._wire.workflows.migrate_remainder import (
    MigrateRemainderInput,
    MigrateRemainderResult,
)
from hpc_agent.ops.migrate import migrate_remainder as mr
from hpc_agent.state.run_sha import compute_cmd_sha

SOURCE = "20260716-000000-" + "0b5ef197"[:8].ljust(8, "0")
TARGET = "carc"
SOURCE_CLUSTER = "hoffman2"

TOTAL = 900
DONE_IDS = frozenset(range(216))
UNDONE_COUNT = TOTAL - 216  # 684

_SOURCE_TASKS_SRC = (
    "_TASKS = [{'cell': k, 'bucket': k // 100} for k in range(900)]\n"
    "def total() -> int: return len(_TASKS)\n"
    "def resolve(i: int) -> dict: return _TASKS[i]\n"
)


def _wave_map() -> dict[str, list[int]]:
    """Bucket-major waves: 9 waves of 100 global ids each (matches the live case)."""
    return {str(w): list(range(w * 100, (w + 1) * 100)) for w in range(9)}


def _make_source(
    tmp_path: Path,
    *,
    write_sidecar: bool = True,
    cluster: str = SOURCE_CLUSTER,
    canary_elapsed_sec: int | None = 120,
    resources: dict | None = None,
    task_count: int = TOTAL,
) -> Path:
    exp = tmp_path / "exp"
    layout = RepoLayout(exp)
    layout.tasks.parent.mkdir(parents=True, exist_ok=True)
    layout.tasks.write_text(_SOURCE_TASKS_SRC, encoding="utf-8")
    src_cmd_sha = compute_cmd_sha(load_tasks_module(layout.tasks))
    res = resources if resources is not None else {"walltime_sec": 600, "cpus": 2}
    if write_sidecar:
        layout.run_sidecar(SOURCE).write_text(
            json.dumps(
                {
                    "cmd_sha": src_cmd_sha,
                    "cluster": cluster,
                    "task_count": task_count,
                    "wave_map": _wave_map(),
                    "resources": res,
                }
            ),
            encoding="utf-8",
        )
    if canary_elapsed_sec is not None:
        layout.run_sidecar(f"{SOURCE}-canary").write_text(
            json.dumps({"cmd_sha": src_cmd_sha, "canary_elapsed_sec": canary_elapsed_sec}),
            encoding="utf-8",
        )
    return exp


def _stub_census(
    monkeypatch: pytest.MonkeyPatch,
    done_ids: frozenset[int],
    *,
    present: bool = True,
    target_global_index: bool = True,
) -> None:
    """Stub ``_census`` by driving the REAL ``census_remainder`` with a fake reader.

    Exercises the true wave-alignment / range-shape / refusal logic through the verb
    without any SSH — the announce reader is the only injected seam.
    """
    from hpc_agent.ops.migrate.census import census_remainder
    from hpc_agent.ops.monitor.announce import AnnouncedTaskIds

    def _fake(experiment_dir, *, source_run_id, total_tasks, target_cluster, wave_map):  # noqa: ANN001, ANN202
        def _reader(*, ssh_target, remote_path, run_id):  # noqa: ANN001, ANN202
            return AnnouncedTaskIds(present=present, done_ids=frozenset(done_ids))

        return census_remainder(
            ssh_target="u@h",
            remote_path="/tmp/exp",
            source_run_id=source_run_id,
            total_tasks=total_tasks,
            target_uses_global_array_index=target_global_index,
            wave_map=wave_map,
            _read_ids=_reader,
        )

    monkeypatch.setattr(mr, "_census", _fake)


def _run(exp: Path, *, target: str = TARGET) -> MigrateRemainderResult:
    return mr.migrate_remainder(
        exp, spec=MigrateRemainderInput(source_run_id=SOURCE, target_cluster=target)
    )


# ── Acceptance 1: the gated brief + S2 hand-off ───────────────────────────────


def test_216_of_900_yields_gated_submit_s2_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source(tmp_path)
    _stub_census(monkeypatch, DONE_IDS)
    res = _run(exp)

    assert res.needs_decision is True
    assert res.stage_reached == "migration_pending_canary"
    assert res.next_block is not None
    assert res.next_block["verb"] == "submit-s2"
    # The greenlit target is stamped so assert_greenlit_target reads it (block_gate.py:86).
    assert res.brief["resolved"]["next_block"] == "submit-s2"
    assert res.derived_run_id is not None
    assert res.next_block["spec_hint"]["run_id"] == res.derived_run_id


# ── Acceptance 2: brief contents — undone=684, cost, honesty, what-dies ───────


def test_brief_carries_undone_count_cost_and_what_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source(tmp_path)
    _stub_census(monkeypatch, DONE_IDS)
    res = _run(exp)

    what_moves = res.brief["what_moves"]
    assert what_moves["undone_count"] == UNDONE_COUNT  # 684
    assert what_moves["done_count"] == 216
    assert what_moves["total_tasks"] == TOTAL
    # 216 done splits wave 2 (ids 200..299: 200..215 done, 216..299 undone), so the
    # remainder is NOT whole-wave aligned — it falls to the arbitrary id range.
    assert what_moves["wave_aligned"] is False
    assert what_moves["whole_waves"] is None
    assert what_moves["task_range"] == "216-899"

    # Cost: source-observed canary runtime (120s), 684 tasks × 2 cores.
    assert res.brief["footprint_unknown"] is False
    assert res.brief["est_core_hours"] > 0
    ce = res.brief["cost_estimate"]
    assert ce["undone_count"] == UNDONE_COUNT
    assert ce["calibrated_from_canary"] is True

    # what-dies: the source remainder range, killed ONLY after the canary is green.
    what_dies = res.brief["what_dies"]
    assert what_dies["task_range"] == "216-899"
    assert what_dies["killed_only_after_derived_canary_green"] is True
    assert what_dies["source_run_id"] == SOURCE


def test_footprint_unknown_when_no_canary_and_no_walltime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No canary sidecar AND no requested walltime → unknown, never a false "0".
    exp = _make_source(tmp_path, canary_elapsed_sec=None, resources={"cpus": 2})
    _stub_census(monkeypatch, DONE_IDS)
    res = _run(exp)
    assert res.brief["footprint_unknown"] is True
    assert "unknown core-hours" in res.reason


# ── Acceptance 3: same-cluster / clusterless target REFUSES ───────────────────


def test_same_cluster_target_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = _make_source(tmp_path)
    _stub_census(monkeypatch, DONE_IDS)
    with pytest.raises(errors.SpecInvalid, match="SAME as"):
        _run(exp, target=SOURCE_CLUSTER)


# ── Acceptance 4: missing sidecar / empty undone REFUSE ───────────────────────


def test_missing_source_sidecar_refuses(tmp_path: Path) -> None:
    exp = _make_source(tmp_path, write_sidecar=False, canary_elapsed_sec=None)
    with pytest.raises(errors.SpecInvalid, match="no sidecar"):
        _run(exp)


def test_empty_undone_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = _make_source(tmp_path)
    _stub_census(monkeypatch, frozenset(range(TOTAL)))  # all done
    with pytest.raises(errors.PreconditionFailed, match="every task done"):
        _run(exp)


def test_no_per_task_census_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = _make_source(tmp_path)
    # An absent announce dir (present=False) must REFUSE, never "all undone".
    _stub_census(monkeypatch, frozenset(), present=False)
    with pytest.raises(errors.PreconditionFailed, match="no per-task census"):
        _run(exp)


def test_index_bounded_target_with_arbitrary_range_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source(tmp_path)
    # A non-contiguous remainder (holes) on an index-bounded target cannot be
    # expressed — census_remainder REFUSES, surfacing the range shape.
    scattered = frozenset(range(0, TOTAL, 2))  # every even id done → odd ids undone (arbitrary)
    _stub_census(monkeypatch, scattered, target_global_index=False)
    with pytest.raises(errors.SpecInvalid, match="index-bounded"):
        _run(exp)


# ── Acceptance 5: the brief is persisted (rule-9 provenance can diff the y) ────


def test_brief_is_persisted_to_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exp = _make_source(tmp_path)
    _stub_census(monkeypatch, DONE_IDS)
    res = _run(exp)
    assert res.derived_run_id is not None

    from hpc_agent.state.decision_briefs import read_briefs

    briefs = read_briefs(exp, res.derived_run_id)
    assert briefs, "the migration brief must be persisted for the provenance gate"
    latest = briefs[-1]
    assert latest["block"] == "migrate-remainder"
    assert latest["brief"]["what_moves"]["undone_count"] == UNDONE_COUNT


# ── The derived-run + ownership artifacts are materialized (per-run-scoped) ────


def test_derived_artifacts_are_per_run_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source(tmp_path)
    shared_before = RepoLayout(exp).tasks.read_bytes()
    _stub_census(monkeypatch, DONE_IDS)
    res = _run(exp)

    rid = res.derived_run_id
    assert rid is not None
    migrate_dir = exp.resolve() / ".hpc" / "migrate" / rid
    assert (migrate_dir / "tasks.py").is_file()
    assert (migrate_dir / "ownership.json").is_file()
    # The shared singleton is byte-unchanged (the LIVE-4 hazard).
    assert RepoLayout(exp).tasks.read_bytes() == shared_before
    # The ownership map covers all 900 exactly once.
    assert res.brief["ownership_map"]["exactly_once"] is True
    assert res.brief["ownership_map"]["derived_cells"] == UNDONE_COUNT


# ── Wave alignment: a wave-boundary remainder is reported as whole waves [LIVE-1] ─


def test_wave_boundary_remainder_reports_whole_waves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source(tmp_path)
    # done = 0..199 → waves 0,1 fully done; undone 200..899 = whole waves 2..8.
    _stub_census(monkeypatch, frozenset(range(200)))
    res = _run(exp)
    what_moves = res.brief["what_moves"]
    assert what_moves["undone_count"] == 700
    assert what_moves["wave_aligned"] is True
    assert what_moves["whole_waves"] == [str(w) for w in range(2, 9)]
    assert what_moves["task_range"] == "200-899"


# ── Wave alignment: a split wave falls to the arbitrary range ─────────────────


def test_split_wave_falls_to_arbitrary_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _make_source(tmp_path)
    # done = 0..249 → wave 2 (ids 200..299) has 200..249 done, 250..299 undone → SPLIT.
    _stub_census(monkeypatch, frozenset(range(250)))
    res = _run(exp)
    assert res.brief["what_moves"]["wave_aligned"] is False
    assert res.brief["what_moves"]["whole_waves"] is None
    assert res.brief["what_moves"]["task_range"] == "250-899"
