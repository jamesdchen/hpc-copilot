"""WP-I: state, identity & journal hygiene fire paths (F42/F46/F30/F44/F45/F41).

Each test exercises a guard's FIRE PATH (the repo's own doctrine): the terminal
run that must not read in-flight (F42), the read that must not scaffold a ghost
namespace (F46), the renamed-away forked namespace detection (F30), the live run
the retention cap must not evict (F44), the sidecar write-lock that must actually
be taken (F45), and the canary key that must fold code identity (F41).
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import pytest

from hpc_agent.state.index import (
    find_held_runs,
    find_in_flight_runs,
    find_runs_by_campaign,
)
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import (
    RunRecord,
    _atomic_write_json,
    _run_path,
    detect_forked_namespace,
    journal_dir,
    journal_root_if_exists,
    repo_hash,
)
from hpc_agent.state.runs import prune_old_runs, write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _record(run_id: str, experiment_dir: Path, **overrides: object) -> RunRecord:
    base: dict = {
        "run_id": run_id,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@h",
        "remote_path": "/remote",
        "job_name": "j",
        "job_ids": ["100"],
        "total_tasks": 4,
        "submitted_at": "2026-01-01T00:00:00+00:00",
        "experiment_dir": str(experiment_dir),
    }
    base.update(overrides)
    return RunRecord(**base)


def _sidecar_kwargs(run_id: str) -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
    )


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


# ── F42: terminal run must not read in_flight through a stale index ────────────


def test_find_in_flight_excludes_terminal_run_with_stale_index(
    journal_home: Path, experiment: Path
) -> None:
    """A crash between the terminal run-write and the index refresh leaves
    index.json tagging a terminal run ``in_flight``; a later sibling write bumps
    the index mtime past the run file so the staleness rebuild never fires. The
    record loaded from disk is terminal, so find_in_flight_runs must drop it."""
    upsert_run(experiment, _record("run_aaaa0001", experiment))
    # Simulate the crash: write A terminal on disk directly (NO index refresh).
    _atomic_write_json(
        _run_path(experiment, "run_aaaa0001"),
        _record("run_aaaa0001", experiment, status="complete", stage="done").to_dict(),
    )
    # Age A so the sibling write makes the index strictly newer (the F42 window).
    _age(_run_path(experiment, "run_aaaa0001"), 120)
    upsert_run(experiment, _record("run_bbbb0002", experiment))

    idx = json.loads((journal_dir(experiment) / "index.json").read_text(encoding="utf-8"))
    assert idx["run_aaaa0001"]["status"] == "in_flight"  # index still lies

    ids = [r.run_id for r in find_in_flight_runs(experiment)]
    assert "run_aaaa0001" not in ids  # F42: terminal run dropped
    assert "run_bbbb0002" in ids


# ── F46: read paths must not scaffold a ghost namespace ────────────────────────


def test_journal_root_if_exists_is_non_creating(journal_home: Path, experiment: Path) -> None:
    journal_home.mkdir(parents=True)
    root = journal_root_if_exists(experiment)
    assert not root.exists()  # probe alone creates nothing
    created = journal_dir(experiment)  # the WRITER accessor does create
    assert created.exists()
    assert created == root  # same path, different side-effect contract


@pytest.mark.parametrize(
    "call",
    [
        lambda exp: find_in_flight_runs(exp),
        lambda exp: find_held_runs(exp),
        lambda exp: find_runs_by_campaign(exp, "some-campaign"),
    ],
)
def test_read_paths_do_not_scaffold_namespace(journal_home: Path, experiment: Path, call) -> None:
    """F46 fire path: with the home present but this experiment's namespace
    absent, a read returns empty AND leaves no ``~/.claude/hpc/<hash>/`` behind —
    the guard that ``journal_dir().exists()`` could never be."""
    journal_home.mkdir(parents=True)
    root = journal_root_if_exists(experiment)
    assert not root.exists()

    assert call(experiment) == []
    assert not root.exists(), "a read must never scaffold a journal namespace (F46)"


# ── F30: forked-namespace detection after a mid-campaign rename ────────────────


def test_detect_forked_namespace_after_rename(journal_home: Path, tmp_path: Path) -> None:
    old_dir = tmp_path / "exp1"
    old_dir.mkdir()
    old_resolved = str(old_dir.resolve())
    upsert_run(old_dir, _record("run_old00001", old_dir))

    # Rename the experiment dir → repo_hash changes → the new dir hashes to a
    # fresh empty namespace and the populated one becomes invisible.
    new_dir = tmp_path / "exp1-v2"
    old_dir.rename(new_dir)

    report = detect_forked_namespace(new_dir)
    assert report is not None
    assert report["forked_experiment_dir"] == old_resolved
    assert report["run_count"] == 1
    assert report["current_hash"] == repo_hash(new_dir)


def test_detect_forked_namespace_none_when_healthy(journal_home: Path, experiment: Path) -> None:
    upsert_run(experiment, _record("run_live0001", experiment))
    assert detect_forked_namespace(experiment) is None


# ── F44: retention cap must not evict a live run ───────────────────────────────


def test_prune_old_runs_keeps_in_flight_run_past_cap(
    journal_home: Path, tmp_path: Path, caplog
) -> None:
    """keep=2 over four sidecars: the two oldest are prune candidates, but the
    very oldest is still ``in_flight`` — it must survive while the terminal
    candidate is evicted, and the cap firing must be logged (never silent)."""
    live = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260101-000000-live0001"), job_ids=["1"])
    _age(live, 400)
    upsert_run(tmp_path, _record("20260101-000000-live0001", tmp_path, status="in_flight"))
    term = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260102-000000-term0002"))
    _age(term, 300)
    keep_a = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260103-000000-keep0003"))
    _age(keep_a, 200)
    keep_b = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260104-000000-keep0004"))
    _age(keep_b, 100)

    import logging

    with caplog.at_level(logging.WARNING):
        deleted = prune_old_runs(tmp_path, keep=2)

    deleted_stems = [p.stem for p in deleted]
    assert "20260102-000000-term0002" in deleted_stems  # terminal candidate evicted
    assert "20260101-000000-live0001" not in deleted_stems  # F44: live run kept
    assert live.is_file()
    assert not term.is_file()
    assert "20260101-000000-live0001" in caplog.text  # cap firing is logged


def test_prune_old_runs_keeps_canary_paired_with_retained_main(
    journal_home: Path, tmp_path: Path
) -> None:
    """A ``<id>-canary`` mirror whose main sidecar is retained must not be evicted
    — splitting the pair halves the effective cap and orphans the mirror."""
    main = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260104-000000-main0001"))
    _age(main, 50)  # newest → retained
    canary = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260104-000000-main0001-canary"))
    _age(canary, 500)  # oldest → prune candidate, but paired

    deleted = prune_old_runs(tmp_path, keep=1)
    assert canary.is_file()  # F44: retained pair not split
    assert [p.stem for p in deleted] == []


# ── F45: write_run_sidecar takes the sibling lock its finalize twin claims ─────


def test_write_run_sidecar_takes_sibling_lock(
    journal_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update_run_sidecar_job_ids' comment claims it serializes against
    write_run_sidecar — true only if write_run_sidecar takes the SAME
    ``<run_id>.json.lock``. Pin that it now does."""
    import hpc_agent.infra.io as io_mod

    real = io_mod.advisory_flock
    seen: list[str] = []

    def _spy(lock_path, **kw):
        from pathlib import Path as _P

        seen.append(_P(lock_path).name)
        return real(lock_path, **kw)

    monkeypatch.setattr(io_mod, "advisory_flock", _spy)
    write_run_sidecar(tmp_path, **_sidecar_kwargs("20260101-000000-lock0001"))

    assert "20260101-000000-lock0001.json.lock" in seen


# ── F41: canary cache key folds code identity ─────────────────────────────────


def test_canary_cache_key_folds_code_identity(journal_home: Path) -> None:
    from hpc_agent.state import canary_cache as cc

    # Back-compat: the original three-part key is byte-identical.
    assert cc.canary_cache_key(cmd_sha="X", version="1", cluster="c") == "X|1|c"

    k1 = cc.canary_cache_key(
        cmd_sha="X", version="1", cluster="c", tasks_py_sha="T", executor_sha="E1"
    )
    k2 = cc.canary_cache_key(
        cmd_sha="X", version="1", cluster="c", tasks_py_sha="T", executor_sha="E2"
    )
    assert k1 != k2  # different executor → different key

    # Fire path: a canary validated for code E1 must NOT satisfy a run whose only
    # difference is the executor (the cross-code / cross-repo hole F41 names).
    cc.record_canary_validated(k1)
    assert cc.is_canary_validated_fresh(k1) is True
    assert cc.is_canary_validated_fresh(k2) is False
