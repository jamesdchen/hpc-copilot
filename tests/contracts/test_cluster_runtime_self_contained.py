"""The deployed cluster runtime imports from its own copy — stdlib only (#349).

The framework ships a small subset of ``hpc_agent`` under the cluster's
``<remote>/`` (see :func:`hpc_agent.infra.transport._build_deploy_items`).
Three entry points run there:

* ``.hpc/_hpc_combiner.py``  — per-wave metric combiner
* ``.hpc/_hpc_dispatch.py``  — per-task framework executor
* ``python -m hpc_agent.execution.mapreduce.reduce.status`` — status reporter
  (reconcile's ``remote_activation`` path, 0.10.12)

This test PINS the invariant that each of those imports/runs from the
*deployed copy alone*, using ONLY the standard library — no full
``hpc_agent`` install, no third-party deps (pandas / numpy / pydantic /
yaml / jsonschema). It materializes exactly what ``_build_deploy_items``
ships into a temp dir, then invokes each entry point in a subprocess under
``python -S`` (no site-packages) with ``PYTHONPATH`` pointing at ONLY that
temp dir and ``PYTHONNOUSERSITE=1`` — so the installed ``hpc_agent`` and any
``~/.local`` install are invisible. The deployed tree is a PEP 420 namespace
package (no ``__init__.py`` anywhere); this is what makes the subset
importable while still yielding to a real install when one is present.

Scope note: this pins the IMPORT-TIME (``--help`` / module-load) closure of
the reporter, which #349's additive core makes self-contained. The reporter's
function-local RUNTIME closure (``state.runs``, ``infra.backends``,
``infra.clusters``, ``recovery.registry``, ``hpc_agent/__init__.py``) still
pulls pydantic / yaml / jsonschema and is intentionally NOT deployed — those
are the experiment env's job, and flipping the env to python-only is the
separate, cluster-gated half of #349.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hpc_agent.infra import transport

# Top-level third-party packages that must NEVER be reachable from the
# cluster-side import closure. If an entry point imports one of these at
# load time, the subprocess (run with site-packages stripped) raises
# ModuleNotFoundError naming it — caught by the stderr assertions below.
_FORBIDDEN_THIRD_PARTY = (
    "pandas",
    "numpy",
    "pydantic",
    "yaml",
    "jsonschema",
    "referencing",
)


def _materialize_deploy_tree(dest: Path, *, scheduler: str = "sge") -> list[str]:
    """Write exactly what ``_build_deploy_items`` ships into *dest*.

    Returns the list of dst_rel paths materialized. Mirrors
    :func:`hpc_agent.infra.transport._deploy_transfer`'s staging step (copy a
    verbatim ``src_path``, else write rendered ``content``).
    """
    items = transport._build_deploy_items(scheduler=scheduler)
    for it in items:
        out = dest / it.dst_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if it.src_path is not None:
            out.write_bytes(it.src_path.read_bytes())
        else:
            out.write_text(it.content or "", encoding="utf-8", newline="")
    return [it.dst_rel for it in items]


def _isolated_env(tree: Path) -> dict[str, str]:
    """Env where ONLY *tree* + the stdlib are importable.

    ``PYTHONPATH`` is the tree alone (not appended to the parent's, so the
    installed ``hpc_agent`` cannot leak in); ``PYTHONNOUSERSITE`` blocks a
    ``~/.local`` install. The subprocess additionally runs with ``-S`` to
    skip the ``site`` module entirely.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree)
    env["PYTHONNOUSERSITE"] = "1"
    # Drop anything that could re-add site-packages or a parent src tree.
    env.pop("PYTHONSTARTUP", None)
    return env


def _run_isolated(args: list[str], *, tree: Path) -> subprocess.CompletedProcess[str]:
    """Run ``python -S <args>`` in the isolated, deployed-copy-only env."""
    return subprocess.run(
        [sys.executable, "-S", *args],
        cwd=str(tree),
        env=_isolated_env(tree),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_no_import_failure(proc: subprocess.CompletedProcess[str], what: str) -> None:
    """Fail if the run died on a missing module / shadowing third-party dep."""
    blob = f"{proc.stdout}\n{proc.stderr}"
    assert "ModuleNotFoundError" not in blob, (
        f"{what} hit ModuleNotFoundError under the deployed-copy-only env — "
        f"its import closure is not self-contained:\n{proc.stderr}"
    )
    assert "No module named" not in blob, (
        f"{what} could not resolve a module from the deployed copy:\n{proc.stderr}"
    )
    for dep in _FORBIDDEN_THIRD_PARTY:
        assert dep not in blob, (
            f"{what} reached forbidden third-party dep {dep!r} in the "
            f"cluster-side import closure:\n{proc.stderr}"
        )


@pytest.fixture
def deploy_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "remote"
    tree.mkdir()
    _materialize_deploy_tree(tree)
    return tree


def test_deployed_tree_has_no_init_files(deploy_tree: Path) -> None:
    """The deployed ``hpc_agent/`` must stay a PEP 420 namespace package.

    An ``__init__.py`` anywhere would bind ``hpc_agent`` to the deployed
    subset and shadow a real install in the conda env. The deploy ships
    none; this guards against a regression where a closure module drags one
    in.
    """
    inits = sorted(str(p.relative_to(deploy_tree)) for p in deploy_tree.rglob("__init__.py"))
    assert inits == [], f"deployed tree must have no __init__.py, found: {inits}"


def test_combiner_help_runs_from_deployed_copy(deploy_tree: Path) -> None:
    """``.hpc/_hpc_combiner.py --help`` imports + exits 0 with stdlib only."""
    proc = _run_isolated(
        [str(deploy_tree / ".hpc" / "_hpc_combiner.py"), "--help"], tree=deploy_tree
    )
    _assert_no_import_failure(proc, "combiner --help")
    assert proc.returncode == 0, f"combiner --help exit={proc.returncode}: {proc.stderr}"
    assert "usage" in proc.stdout.lower()


def test_dispatch_imports_from_deployed_copy(deploy_tree: Path) -> None:
    """``.hpc/_hpc_dispatch.py`` resolves its imports from the deployed copy.

    Dispatch does not use argparse ``--help``; invoked with no usable
    tasks.py / env it exits 1 with a clean ``tasks.py not found`` message.
    The contract here is that it gets *that far* — i.e. its module-load
    imports all resolve from the deployed copy (no ModuleNotFoundError) —
    not that it produces a report.
    """
    proc = _run_isolated(
        [str(deploy_tree / ".hpc" / "_hpc_dispatch.py"), "--help"], tree=deploy_tree
    )
    _assert_no_import_failure(proc, "dispatch")
    # Reached its own arg/tasks handling rather than dying on an import.
    assert "[dispatch]" in proc.stderr or proc.returncode == 0, (
        f"dispatch did not reach its own main(): exit={proc.returncode}\n{proc.stderr}"
    )


def test_reporter_help_runs_from_deployed_copy(deploy_tree: Path) -> None:
    """``python -S -m hpc_agent.execution.mapreduce.reduce.status --help``.

    The reporter's eager (import-time) closure must resolve entirely from
    the deployed copy + stdlib. ``--help`` short-circuits via argparse
    *before* the #159 import-sanity guard, which (by design) would reject the
    deployed namespace package's missing ``__file__``; the guard still fires
    on a real ``--run-id`` run. This is the central self-containment pin
    #349's additive core establishes.
    """
    proc = _run_isolated(
        ["-m", "hpc_agent.execution.mapreduce.reduce.status", "--help"], tree=deploy_tree
    )
    _assert_no_import_failure(proc, "reporter --help")
    assert proc.returncode == 0, f"reporter --help exit={proc.returncode}: {proc.stderr}"
    assert "usage" in proc.stdout.lower()
