"""Wiring tests for DAG-node identity (``docs/design/dag-kernel.md``).

``compose_node_sha``'s algebra is pinned by
``test_node_sha_properties.py``; this file pins the wiring around it:

* ``resolve_node_sha`` — identity is DERIVED from parents' on-disk
  sidecars (recorded ``node_sha``, else bare ``cmd_sha``), fails loud on
  a missing parent or a non-64-hex identity, and returns ``None`` for a
  parentless run (sidecar stays compact).
* sidecar round-trip — ``parent_run_ids`` + ``node_sha`` persist and
  backfill to ``None`` on pre-DAG sidecars.
* ``find_run_by_cmd_sha`` effective-identity matching — all four
  bare/parented query-vs-candidate quadrants, including the invariant
  the kernel exists for: a parented query never dedups against a run
  with the same params but different ancestry.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.state.run_sha import compose_node_sha
from hpc_agent.state.runs import (
    find_run_by_cmd_sha,
    read_run_sidecar,
    resolve_node_sha,
    write_run_sidecar,
)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _write(
    experiment_dir: Path,
    *,
    run_id: str,
    cmd_sha: str,
    parent_run_ids: list[str] | None = None,
    node_sha: str | None = None,
) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-06-10T00:00:00Z",
        executor="python3 src/test.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="",
        wave_map={"0": [0, 1]},
        parent_run_ids=parent_run_ids,
        node_sha=node_sha,
    )


class TestResolveNodeSha:
    def test_no_parents_returns_none(self, tmp_path: Path) -> None:
        assert resolve_node_sha(tmp_path, cmd_sha=_sha("c"), parent_run_ids=None) is None
        assert resolve_node_sha(tmp_path, cmd_sha=_sha("c"), parent_run_ids=[]) is None

    def test_parentless_parent_contributes_its_cmd_sha(self, tmp_path: Path) -> None:
        parent_cmd = _sha("parent")
        _write(tmp_path, run_id="p1", cmd_sha=parent_cmd)
        child_cmd = _sha("child")
        got = resolve_node_sha(tmp_path, cmd_sha=child_cmd, parent_run_ids=["p1"])
        assert got == compose_node_sha(child_cmd, [parent_cmd])

    def test_parented_parent_contributes_its_node_sha(self, tmp_path: Path) -> None:
        """The Merkle chain: a grandparent change reaches the child through
        the parent's recorded node_sha, not its bare cmd_sha."""
        grandparent_cmd = _sha("gp")
        _write(tmp_path, run_id="gp1", cmd_sha=grandparent_cmd)
        parent_cmd = _sha("parent")
        parent_node = resolve_node_sha(tmp_path, cmd_sha=parent_cmd, parent_run_ids=["gp1"])
        assert parent_node is not None
        _write(
            tmp_path,
            run_id="p1",
            cmd_sha=parent_cmd,
            parent_run_ids=["gp1"],
            node_sha=parent_node,
        )
        child_cmd = _sha("child")
        got = resolve_node_sha(tmp_path, cmd_sha=child_cmd, parent_run_ids=["p1"])
        assert got == compose_node_sha(child_cmd, [parent_node])
        # A different grandparent would have produced a different parent_node,
        # hence a different child identity — the propagation the kernel needs.
        other_parent_node = compose_node_sha(parent_cmd, [_sha("other-gp")])
        assert got != compose_node_sha(child_cmd, [other_parent_node])

    def test_missing_parent_raises_spec_invalid(self, tmp_path: Path) -> None:
        with pytest.raises(errors.SpecInvalid, match="no sidecar"):
            resolve_node_sha(tmp_path, cmd_sha=_sha("c"), parent_run_ids=["nope"])

    def test_prefix_cmd_sha_raises_spec_invalid(self, tmp_path: Path) -> None:
        """8-char cmd_sha prefixes satisfy some wire patterns but cannot
        participate in a DAG node — compose requires full digests."""
        _write(tmp_path, run_id="p1", cmd_sha=_sha("parent"))
        with pytest.raises(errors.SpecInvalid, match="64-hex"):
            resolve_node_sha(tmp_path, cmd_sha="abcdef12", parent_run_ids=["p1"])


class TestSidecarRoundTrip:
    def test_lineage_fields_persist(self, tmp_path: Path) -> None:
        parent_cmd = _sha("parent")
        _write(tmp_path, run_id="p1", cmd_sha=parent_cmd)
        child_cmd = _sha("child")
        node = compose_node_sha(child_cmd, [parent_cmd])
        _write(tmp_path, run_id="c1", cmd_sha=child_cmd, parent_run_ids=["p1"], node_sha=node)
        data = read_run_sidecar(tmp_path, "c1")
        assert data["parent_run_ids"] == ["p1"]
        assert data["node_sha"] == node

    def test_pre_dag_sidecar_backfills_none(self, tmp_path: Path) -> None:
        _write(tmp_path, run_id="r1", cmd_sha=_sha("c"))
        data = read_run_sidecar(tmp_path, "r1")
        assert data["parent_run_ids"] is None
        assert data["node_sha"] is None


class TestFindRunEffectiveIdentity:
    def test_bare_query_matches_bare_sidecar(self, tmp_path: Path) -> None:
        cmd = _sha("c")
        _write(tmp_path, run_id="r1", cmd_sha=cmd)
        hit = find_run_by_cmd_sha(tmp_path, cmd)
        assert hit is not None and hit.stem == "r1"

    def test_bare_query_skips_parented_sidecar(self, tmp_path: Path) -> None:
        """Same params consuming declared inputs are a different experiment:
        a bare submit must not replay a parented run's results."""
        parent_cmd = _sha("parent")
        _write(tmp_path, run_id="p1", cmd_sha=parent_cmd)
        cmd = _sha("c")
        node = compose_node_sha(cmd, [parent_cmd])
        _write(tmp_path, run_id="c1", cmd_sha=cmd, parent_run_ids=["p1"], node_sha=node)
        assert find_run_by_cmd_sha(tmp_path, cmd) is None

    def test_node_query_skips_bare_sidecar(self, tmp_path: Path) -> None:
        cmd = _sha("c")
        _write(tmp_path, run_id="r1", cmd_sha=cmd)
        node = compose_node_sha(cmd, [_sha("parent")])
        assert find_run_by_cmd_sha(tmp_path, cmd, node_sha=node) is None

    def test_node_query_matches_same_ancestry(self, tmp_path: Path) -> None:
        parent_cmd = _sha("parent")
        _write(tmp_path, run_id="p1", cmd_sha=parent_cmd)
        cmd = _sha("c")
        node = compose_node_sha(cmd, [parent_cmd])
        _write(tmp_path, run_id="c1", cmd_sha=cmd, parent_run_ids=["p1"], node_sha=node)
        hit = find_run_by_cmd_sha(tmp_path, cmd, node_sha=node)
        assert hit is not None and hit.stem == "c1"

    def test_node_query_skips_different_ancestry(self, tmp_path: Path) -> None:
        """The stale-subgraph invariant: same params, different (e.g.
        since-changed) parent → not a dedup target."""
        old_parent_cmd = _sha("parent-v1")
        _write(tmp_path, run_id="p1", cmd_sha=old_parent_cmd)
        cmd = _sha("c")
        old_node = compose_node_sha(cmd, [old_parent_cmd])
        _write(tmp_path, run_id="c1", cmd_sha=cmd, parent_run_ids=["p1"], node_sha=old_node)
        new_node = compose_node_sha(cmd, [_sha("parent-v2")])
        assert find_run_by_cmd_sha(tmp_path, cmd, node_sha=new_node) is None
