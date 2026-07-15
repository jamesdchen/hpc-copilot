"""Run-13 finding 4: the stack-minted local pull destinations must be in the
default deploy-exclude set, pinned LOCKSTEP to the aggregate-flow mint constants.

The aggregate flow's no-combiner fallbacks pull each task's metrics / trace
sidecars into local mirror dirs (``_per_task_results`` / ``_per_task_traces``)
under its ``out`` dir. Those are analysis OUTPUTS, not code — but run 13 deployed
run 12's 2,700-file ``_per_task_results`` mirror to the cluster as a 1.18 GB
"changed/new" payload because the mirror names were not in the deploy excludes.

These tests hold three things:

* the exclude set COVERS every minted mirror name (the lockstep pin — it FIRES
  if a mint-site constant is renamed without updating the exclude set);
* a tree containing those mirrors DEPLOYS WITHOUT them (fires-and-passes for the
  new default excludes);
* the check-time payload summary that feeds the S2 greenlight brief also drops
  them (the same exclude core the transfer uses).
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.infra import transport
from hpc_agent.ops.aggregate_flow import (
    LOCAL_PULL_MIRROR_DIRNAMES,
    PER_TASK_RESULTS_DIRNAME,
    PER_TASK_TRACES_DIRNAME,
)
from hpc_agent.ops.export_dossier import DOSSIER_DIRNAME

#: Every experiment-root directory CORE MINTS as a local output/pull store — the
#: aggregate pull mirrors plus the dossier export store. None of these are code;
#: all must be deploy-excluded so a code push never re-ships a prior run's
#: analysis outputs (run-13 finding 4 + its render-store sibling).
MINTED_OUTPUT_DIRNAMES: tuple[str, ...] = (*LOCAL_PULL_MIRROR_DIRNAMES, DOSSIER_DIRNAME)


def _uncovered_mirrors(minted: tuple[str, ...], protected: list[str]) -> list[str]:
    """Minted pull-destination names NOT covered by *protected* (as ``name/``).

    The pure coverage check the lockstep pin asserts is empty. Factored out so a
    test can prove it FIRES on a hypothetical rename.
    """
    protected_set = set(protected)
    return [name for name in minted if f"{name}/" not in protected_set]


def test_minted_pull_dests_are_all_deploy_excluded() -> None:
    """LOCKSTEP PIN: every experiment-root output/pull store core mints (the
    aggregate pull mirrors + the dossier export store) is in PROTECTED_OUTPUT_DIRS.
    Renaming a mint constant without updating the exclude set (the run-13 finding-4
    class) breaks this — the store would once again ride a code deploy back to the
    cluster."""
    assert _uncovered_mirrors(MINTED_OUTPUT_DIRNAMES, transport.PROTECTED_OUTPUT_DIRS) == []


def test_lockstep_pin_fires_on_rename() -> None:
    """The coverage check is not vacuous: a renamed mint constant that the exclude
    set does not know about is reported as uncovered."""
    renamed = ("_per_task_results_RENAMED", PER_TASK_TRACES_DIRNAME)
    uncovered = _uncovered_mirrors(renamed, transport.PROTECTED_OUTPUT_DIRS)
    assert uncovered == ["_per_task_results_RENAMED"]


def _write(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")


def test_pull_mirror_tree_deploys_without_the_mirrors(tmp_path: Path) -> None:
    """A deploy tree carrying the pull mirrors AT THE EXPERIMENT ROOT (the shape a
    caller ``output_dir`` produces) ships the code but NOT the mirrors."""
    _write(tmp_path, "src/train.py")
    _write(tmp_path, "tasks.py")
    _write(tmp_path, f"{PER_TASK_RESULTS_DIRNAME}/task-0/metrics.json")
    _write(tmp_path, f"{PER_TASK_RESULTS_DIRNAME}/task-1/metrics.json")
    _write(tmp_path, f"{PER_TASK_TRACES_DIRNAME}/task-0/_trace.jsonl")
    # And nested under the default _aggregated/<run_id>/ home.
    _write(tmp_path, f"_aggregated/run-abc/{PER_TASK_RESULTS_DIRNAME}/task-0/metrics.json")
    # The dossier export store (a prior run's exported archive).
    _write(tmp_path, f"{DOSSIER_DIRNAME}/run-abc.zip")

    exclude = transport._effective_excludes(None)
    shipped = transport._pushable_relpaths(tmp_path, exclude)

    assert "src/train.py" in shipped
    assert "tasks.py" in shipped
    assert not any(PER_TASK_RESULTS_DIRNAME in rel for rel in shipped)
    assert not any(PER_TASK_TRACES_DIRNAME in rel for rel in shipped)
    assert not any(rel.startswith("_aggregated/") for rel in shipped)
    assert not any(rel.startswith(f"{DOSSIER_DIRNAME}/") for rel in shipped)


def test_payload_summary_drops_the_pull_mirrors(tmp_path: Path) -> None:
    """The check-time payload summary (S2 brief disclosure) uses the same exclude
    core, so the mirrors never count toward the disclosed payload — and the code
    the deploy actually ships does."""
    _write(tmp_path, "src/train.py")
    _write(tmp_path, f"{PER_TASK_RESULTS_DIRNAME}/task-0/metrics.json")
    _write(tmp_path, f"{PER_TASK_TRACES_DIRNAME}/task-0/_trace.jsonl")

    summary = transport.deploy_payload_summary(tmp_path, None)

    assert summary.file_count == 1  # only src/train.py
    root_names = {name for name, _ in summary.top_roots}
    assert PER_TASK_RESULTS_DIRNAME not in root_names
    assert PER_TASK_TRACES_DIRNAME not in root_names
    assert "src" in root_names
