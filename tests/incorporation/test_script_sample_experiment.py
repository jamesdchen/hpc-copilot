"""End-to-end test for the script-shape sample experiment fixture.

Proves that a repo whose entry point is a `.py` script (not a notebook)
runs the full discovery + dispatch loop without any notebook-specific
step. The fixture lives at ``tests/fixtures/sample_experiments/script/``
and carries:

- ``train.py`` with ``@register_run def run(seed, lr)``
- ``.hpc/tasks.py`` enumerating (seed, lr) tuples
- a minimal ``pyproject.toml``

This is the canonical companion to the notebook-shape fixtures and the
``test_notebook_skeleton_is_a_discoverable_register_run`` assertion in
``test_template.py``: same contract, different on-disk shape.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import load_tasks_module
from hpc_agent.experiment_kit import discover_runs

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_experiments" / "script"


def test_fixture_is_well_formed() -> None:
    """Sanity: the on-disk fixture has the files we expect."""
    assert (_FIXTURE / "train.py").is_file()
    assert (_FIXTURE / ".hpc" / "tasks.py").is_file()
    assert (_FIXTURE / "pyproject.toml").is_file()


def test_discover_runs_finds_register_run_in_script() -> None:
    """``discover_runs`` walks the script fixture and finds ``run``.

    Critical anchor for the .py-shape on-ramp: discovery is genuinely
    file-agnostic, the same primitive resolves the decorator in a
    standalone ``.py`` as it does in a notebook.
    """
    runs = discover_runs(_FIXTURE / "train.py")
    names = [r.name for r in runs]
    assert names == ["run"], names

    (info,) = runs
    flag_names = {f.name for f in info.flags}
    assert flag_names == {"seed", "lr"}
    assert info.gpu is False


def test_tasks_py_total_and_resolve_match_fixture() -> None:
    """``load_tasks_module`` round-trips the hand-written tasks.py."""
    tasks = load_tasks_module(_FIXTURE / ".hpc" / "tasks.py")
    assert tasks.total() == 4
    assert tasks.resolve(0) == {"seed": 0, "lr": 1e-3}
    assert tasks.resolve(3) == {"seed": 1, "lr": 1e-2}
    # FLAGS keyed by the executor's module name.
    assert "train" in tasks.FLAGS


def test_script_function_runs_under_resolved_kwargs() -> None:
    """Drive the resolved kwargs through the decorated function.

    This is the dispatch-side proof: ``tasks.resolve(i)`` produces the
    exact kwargs ``run`` accepts, and the function returns observable
    metrics. The cluster-side dispatcher does the same thing — read
    kwargs from tasks, call the function.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("script_train", _FIXTURE / "train.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tasks = load_tasks_module(_FIXTURE / ".hpc" / "tasks.py")
    for i in range(tasks.total()):
        kwargs = tasks.resolve(i)
        out = module.run(**kwargs)
        assert out["seed"] == kwargs["seed"]
        assert out["lr"] == kwargs["lr"]
        assert out["score"] == kwargs["lr"] * (kwargs["seed"] + 1)
