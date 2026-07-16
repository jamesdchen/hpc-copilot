"""Unit coverage for the deploy ship-list helpers (``transport._deploy_items``).

Two surfaces under test:

* :func:`reducer_relpath_from_aggregate_cmd` — how core derives "which repo file
  is the run's reducer" from the declared ``aggregate_cmd`` (spec §3.C.2). A
  literal ``specs/…​.py`` script path is returned; a ``python -m`` module reducer
  (or a command with no ``.py`` token, or an empty/None command) returns ``None``.
* the ``extra_items`` thread through :func:`_build_deploy_items` /
  :func:`_local_deploy_manifest` — the run's custom reducer ships as a
  content-hashed ``_DeployItem`` exactly like the framework combiner, and its sha
  lands in the deploy-cache manifest so the cache knows to ship it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.infra.transport import (
    _build_deploy_items,
    _local_deploy_manifest,
    _sha256_bytes,
    reducer_relpath_from_aggregate_cmd,
)

if TYPE_CHECKING:
    from pathlib import Path


# ── reducer_relpath_from_aggregate_cmd ─────────────────────────────────────


def test_literal_script_path_is_returned():
    assert reducer_relpath_from_aggregate_cmd("python3 specs/reduce_x.py") == "specs/reduce_x.py"


def test_module_reducer_returns_none():
    # `python -m pkg.reducer` names an installed module, not a repo file.
    assert reducer_relpath_from_aggregate_cmd("python -m pkg.reducer") is None


def test_inline_program_returns_none():
    # `-c` is an inline program, not a shippable file path.
    assert reducer_relpath_from_aggregate_cmd('python -c "import x"') is None


def test_empty_and_none_return_none():
    assert reducer_relpath_from_aggregate_cmd("") is None
    assert reducer_relpath_from_aggregate_cmd(None) is None


def test_no_py_token_returns_none():
    assert reducer_relpath_from_aggregate_cmd("bash run.sh") is None


def test_script_path_before_positional_args():
    # The reducer path is picked out even with trailing args.
    assert (
        reducer_relpath_from_aggregate_cmd("python3 specs/reduce_x.py --tol 0.1")
        == "specs/reduce_x.py"
    )


def test_unbalanced_quotes_fall_back_to_str_split():
    # A genuinely unbalanced-quote command makes shlex.split raise; the fallback
    # str.split still recovers the .py token rather than aborting the submit.
    assert (
        reducer_relpath_from_aggregate_cmd("python3 specs/reduce_x.py 'oops") == "specs/reduce_x.py"
    )


# ── extra_items in _build_deploy_items ─────────────────────────────────────


def test_extra_items_ship_as_content_hashed_deploy_item(tmp_path: Path):
    reducer = tmp_path / "specs" / "reduce_x.py"
    reducer.parent.mkdir(parents=True)
    reducer.write_text("print('reduce')\n", encoding="utf-8")

    items = _build_deploy_items(scheduler="slurm", extra_items=[(reducer, "specs/reduce_x.py")])
    matches = [it for it in items if it.dst_rel == "specs/reduce_x.py"]
    assert len(matches) == 1, [it.dst_rel for it in items]
    item = matches[0]
    assert item.sha == _sha256_bytes(reducer.read_bytes())
    assert item.src_path == reducer
    assert item.content is None


def test_nonexistent_extra_item_is_silently_omitted(tmp_path: Path):
    # The LOUD refusal for an absent reducer lives at the submit stage-gate, not
    # here — _build_deploy_items must never crash on a transiently-absent path.
    missing = tmp_path / "specs" / "reduce_gone.py"
    items = _build_deploy_items(scheduler="slurm", extra_items=[(missing, "specs/reduce_gone.py")])
    assert not any(it.dst_rel == "specs/reduce_gone.py" for it in items)


def test_no_extra_items_matches_bare_build(tmp_path: Path):
    # Additive + defaulting to no-op: extra_items=None is byte-identical to the
    # bare framework build.
    bare = _build_deploy_items(scheduler="slurm")
    with_none = _build_deploy_items(scheduler="slurm", extra_items=None)
    assert [it.dst_rel for it in bare] == [it.dst_rel for it in with_none]


# ── extra_items in _local_deploy_manifest ──────────────────────────────────


def test_manifest_includes_reducer_sha(tmp_path: Path):
    reducer = tmp_path / "specs" / "reduce_x.py"
    reducer.parent.mkdir(parents=True)
    reducer.write_text("print('reduce')\n", encoding="utf-8")

    manifest = _local_deploy_manifest(
        scheduler="slurm", extra_items=[(reducer, "specs/reduce_x.py")]
    )
    assert manifest["files"]["specs/reduce_x.py"] == _sha256_bytes(reducer.read_bytes())
    # And the bare manifest (no extra) does NOT carry it — proving the thread is
    # what put the reducer into the cache's view.
    bare = _local_deploy_manifest(scheduler="slurm")
    assert "specs/reduce_x.py" not in bare["files"]
