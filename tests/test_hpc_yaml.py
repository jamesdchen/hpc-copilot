"""Shape and DAG contract tests for the multi-stage ``hpc.yaml`` fixture.

These tests document the expected shape of a multi-stage HPC config
without requiring a parser in ``hpc_mapreduce``.  They load the fixture
with :func:`yaml.safe_load` and assert invariants:

* top-level keys exist (``cluster``, ``remote_path``, ``results``, ``profiles``);
* every stage has a ``run`` command;
* ``depends_on`` references resolve to defined stages;
* the stage DAG is acyclic (via a simple Kahn topological sort);
* if a profile has ``resources``, it has the expected shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

FIXTURE = Path(__file__).parent / "fixtures" / "hpc_multistage.yaml"


@pytest.fixture(scope="module")
def config() -> dict:
    with FIXTURE.open() as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# top-level shape
# ---------------------------------------------------------------------------


def test_top_level_keys(config):
    for key in ("cluster", "remote_path", "results", "profiles"):
        assert key in config, f"missing top-level key: {key}"


def test_results_block_shape(config):
    results = config["results"]
    assert isinstance(results, dict)
    for key in ("dir", "summary_pattern", "aggregate_cmd"):
        assert key in results


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def _stages(config: dict) -> dict:
    profiles = config["profiles"]
    assert "stages" in profiles, "profiles.stages missing"
    stages = profiles["stages"]
    assert isinstance(stages, dict)
    return stages


def test_every_stage_has_a_run_command(config):
    for name, stage in _stages(config).items():
        assert isinstance(stage, dict), f"stage {name!r} is not a mapping"
        assert "run" in stage, f"stage {name!r} missing 'run'"
        assert isinstance(stage["run"], str) and stage["run"].strip(), (
            f"stage {name!r} has empty 'run'"
        )


def test_depends_on_references_resolve(config):
    stages = _stages(config)
    for name, stage in stages.items():
        deps = stage.get("depends_on", [])
        assert isinstance(deps, list), f"{name}.depends_on must be a list"
        for dep in deps:
            assert dep in stages, f"stage {name!r} depends on undefined stage {dep!r}"


def test_stage_dag_is_acyclic(config):
    """Kahn's algorithm: if we can emit every node, there is no cycle."""
    stages = _stages(config)
    indeg = {n: 0 for n in stages}
    for n, stage in stages.items():
        for _dep in stage.get("depends_on", []):
            indeg[n] += 1

    ready = [n for n, d in indeg.items() if d == 0]
    emitted: list[str] = []
    while ready:
        node = ready.pop()
        emitted.append(node)
        # Find successors: nodes that list `node` in their depends_on.
        for other, stage in stages.items():
            if node in stage.get("depends_on", []):
                indeg[other] -= 1
                if indeg[other] == 0:
                    ready.append(other)

    assert len(emitted) == len(stages), (
        f"cycle detected: only emitted {emitted} out of {list(stages)}"
    )


# ---------------------------------------------------------------------------
# resources (constraint merging shape)
# ---------------------------------------------------------------------------


def test_profile_resources_shape(config):
    """If a profile defines ``resources``, it is a dict with known keys.

    This is a *shape* assertion, not an assertion about merging behaviour.
    It documents the intent that ``resources`` at a profile level can
    override cluster defaults, and at a stage level can override the
    profile's.
    """
    train = config["profiles"]["train"]
    assert "resources" in train
    resources = train["resources"]
    assert isinstance(resources, dict)
    for key in ("cpus", "memory", "walltime"):
        assert key in resources, f"expected 'resources.{key}' in train profile"
