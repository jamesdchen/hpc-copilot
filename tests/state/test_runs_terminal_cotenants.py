"""``find_existing_runs`` must not sweep up block-terminal co-tenants.

``state/block_terminal.py`` stores each detached block's terminal record in
the SAME ``.hpc/runs/`` directory as the run sidecars, named
``<run_id>.<block>.terminal.json``. Matching them as sidecars (the pre-fix
"any ``.json``" scan) had three reproduced consequences, each pinned below:

1. ``prune_orphan_sidecars`` (called with ``min_age_seconds=0`` on every
   ``submit_flow_batch``) deleted every prior run's terminal record — they
   read back jobless, i.e. "orphan".
2. ``find_run_by_cmd_sha`` could return a terminal record (same top-level
   ``cmd_sha``, newer mtime), which reads back jobless → dedup falls through
   → duplicate submission of work the cluster already runs.
3. Terminal records counted toward the ``MAX_RUNS`` retention cap, evicting
   real sidecars.

The exclusion keys on the ``.terminal.json`` suffix (run_ids may legally
contain dots, so name-shape parsing can't discriminate); the first test pins
``runs._TERMINAL_RECORD_SUFFIX`` against ``block_terminal.terminal_path``'s
actual output so the two modules cannot drift apart.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from hpc_agent.state import runs as runs_module
from hpc_agent.state.block_terminal import record_terminal, terminal_path
from hpc_agent.state.runs import (
    find_existing_runs,
    find_run_by_cmd_sha,
    prune_old_runs,
    prune_orphan_sidecars,
    write_run_sidecar,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def _journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "journal_home"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    return home


def _sidecar_kwargs(run_id: str, cmd_sha: str = "0" * 64) -> dict:
    return dict(
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
    )


def _write_terminal(experiment_dir: Path, run_id: str, cmd_sha: str = "0" * 64) -> Path:
    record_terminal(
        experiment_dir,
        run_id=run_id,
        block="submit-s2",
        cmd_sha=cmd_sha,
        result_dump={"block": "s2", "needs_decision": False},
    )
    return terminal_path(experiment_dir, run_id, "submit-s2")


def _age(path: Path, seconds: float) -> None:
    """Backdate *path*'s mtime so ordering / age cutoffs are deterministic."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_exclusion_suffix_pins_block_terminal_naming(tmp_path: Path) -> None:
    """The ONE-place drift pin: the suffix ``find_existing_runs`` excludes is
    exactly the shape ``block_terminal.terminal_path`` produces — including
    for a run_id that legally contains dots — and a real sidecar never
    carries it. If block_terminal renames its records, this fires."""
    dotted_run_id = "20260101-000000-abc.v1.2"
    tpath = terminal_path(tmp_path, dotted_run_id, "submit-s2")
    assert tpath.name.endswith(runs_module._TERMINAL_RECORD_SUFFIX)
    assert tpath.parent == tmp_path / ".hpc" / "runs"  # same dir — a true co-tenant
    sidecar = write_run_sidecar(tmp_path, **_sidecar_kwargs(dotted_run_id))
    assert not sidecar.name.endswith(runs_module._TERMINAL_RECORD_SUFFIX)


def test_find_existing_runs_skips_terminal_records(tmp_path: Path) -> None:
    sidecar = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260101-000000-run0001"))
    _write_terminal(tmp_path, "20260101-000000-run0001")
    assert find_existing_runs(tmp_path) == [sidecar]


def test_prune_orphan_sidecars_never_deletes_terminal_records(
    _journal_home: Path, tmp_path: Path
) -> None:
    """The ``submit_flow_batch`` shape: prune with ``min_age_seconds=0`` after
    prior runs recorded their block terminals. A genuine orphan sidecar is
    still pruned; the terminal records survive."""
    committed = _sidecar_kwargs("20260101-000000-done001", cmd_sha="a" * 64)
    write_run_sidecar(tmp_path, **committed, job_ids=["12345"])
    orphan = _sidecar_kwargs("20260101-000000-orphan1", cmd_sha="b" * 64)
    write_run_sidecar(tmp_path, **orphan)
    tpath = _write_terminal(tmp_path, "20260101-000000-done001", cmd_sha="a" * 64)

    deleted = prune_orphan_sidecars(tmp_path, min_age_seconds=0)

    assert deleted == ["20260101-000000-orphan1"]
    assert tpath.is_file(), "block-terminal record must survive the orphan prune"


def test_find_run_by_cmd_sha_never_returns_a_terminal_record(
    _journal_home: Path, tmp_path: Path
) -> None:
    """A terminal record carries the tree's ``cmd_sha`` at top level and can be
    NEWER than the matching run sidecar — pre-fix the scan returned it, it
    read back jobless, and the dedup fell through to a duplicate submission."""
    cmd_sha = "f" * 64
    sidecar = write_run_sidecar(
        tmp_path, **_sidecar_kwargs("20260101-000000-real001", cmd_sha=cmd_sha), job_ids=["12345"]
    )
    _age(sidecar, seconds=60)
    tpath = _write_terminal(tmp_path, "20260101-000000-real001", cmd_sha=cmd_sha)
    assert tpath.stat().st_mtime > sidecar.stat().st_mtime

    assert find_run_by_cmd_sha(tmp_path, cmd_sha) == sidecar


def test_terminal_records_do_not_count_toward_retention(tmp_path: Path) -> None:
    """Retention cap: terminal records neither consume ``keep`` slots (which
    evicted real sidecars) nor get evicted themselves."""
    old_a = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260101-000000-old0001"))
    _age(old_a, seconds=120)
    old_b = write_run_sidecar(tmp_path, **_sidecar_kwargs("20260102-000000-old0002"))
    _age(old_b, seconds=60)
    t1 = _write_terminal(tmp_path, "20260101-000000-old0001")
    t2 = _write_terminal(tmp_path, "20260102-000000-old0002")

    # 2 sidecars + 2 terminals on disk; keep=2 must delete NOTHING — pre-fix
    # the scan saw 4 "runs" and evicted the 2 oldest entries.
    assert prune_old_runs(tmp_path, keep=2) == []
    for path in (old_a, old_b, t1, t2):
        assert path.is_file()
