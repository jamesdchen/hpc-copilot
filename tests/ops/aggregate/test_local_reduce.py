"""Unit tests for ``local_reduce`` — the pure-API local reducer runner.

``local_reduce`` is the ``requires_ssh=False`` analogue of ``cluster_reduce``:
it runs the user's reducer-contract command as a LOCAL subprocess over the
artifacts a backend's ``fetch_results`` fetched, instead of over SSH on the
cluster. These tests pin the contract it implements — the three env vars it
threads, the JSON it parses back, and the failure modes — without any network
or backend. The reducer scripts are written to ``tmp_path`` and invoked via the
current interpreter, so the tests are hermetic.
"""

from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.aggregate.local_reduce import local_reduce

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260623-090000-loc"


def _reducer(tmp_path: Path, body: str) -> str:
    """Write a reducer script and return an ``aggregate_cmd`` that runs it."""
    script = tmp_path / "reducer.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    # Quote both paths: aggregate_cmd runs under shell=True, and an install
    # path with a space (e.g. sys.executable under "...\CC Allowed\...") would
    # otherwise split — `'C:\\...\\CC' is not recognized` on Windows cmd.exe.
    return f'"{sys.executable}" "{script}"'


def test_runs_cmd_over_results_and_parses_json(tmp_path: Path) -> None:
    results = tmp_path / "fetched"
    (results / "task-0").mkdir(parents=True)
    (results / "task-1").mkdir(parents=True)
    (results / "task-0" / "value.txt").write_text("2", encoding="utf-8")
    (results / "task-1" / "value.txt").write_text("5", encoding="utf-8")

    cmd = _reducer(
        tmp_path,
        """
        import glob, json, os
        d = os.environ["HPC_RESULTS_DIR"]
        paths = glob.glob(os.path.join(d, "task-*", "value.txt"))
        total = sum(float(open(p).read()) for p in paths)
        json.dump({"total": total}, open(os.environ["HPC_AGGREGATED_OUTPUT"], "w"))
        """,
    )

    env = local_reduce(run_id=_RUN_ID, results_dir=results, aggregate_cmd=cmd)

    assert env["ok"] is True
    assert env["reduced"] == {"total": 7.0}  # read RAW value.txt, not metrics.json
    assert env["exit_code"] == 0
    # Output anchored under the results dir at the default rel path.
    assert env["output_path_local"] == str(results / "_aggregated" / f"{_RUN_ID}.json")


def test_threads_the_three_contract_env_vars(tmp_path: Path) -> None:
    results = tmp_path / "fetched"
    results.mkdir()
    cmd = _reducer(
        tmp_path,
        """
        import json, os
        out = os.environ["HPC_AGGREGATED_OUTPUT"]
        json.dump(
            {
                "run_id": os.environ["HPC_RUN_ID"],
                "results_dir": os.environ["HPC_RESULTS_DIR"],
                "output": out,
            },
            open(out, "w"),
        )
        """,
    )

    env = local_reduce(run_id=_RUN_ID, results_dir=results, aggregate_cmd=cmd)

    assert env["reduced"]["run_id"] == _RUN_ID
    assert env["reduced"]["results_dir"] == str(results)
    assert env["reduced"]["output"] == str(results / "_aggregated" / f"{_RUN_ID}.json")


def test_custom_output_path_with_run_id_substitution(tmp_path: Path) -> None:
    results = tmp_path / "fetched"
    results.mkdir()
    cmd = _reducer(
        tmp_path,
        """
        import json, os
        json.dump({"k": 1}, open(os.environ["HPC_AGGREGATED_OUTPUT"], "w"))
        """,
    )

    env = local_reduce(
        run_id=_RUN_ID,
        results_dir=results,
        aggregate_cmd=cmd,
        output_path="out/{run_id}.json",
    )

    assert env["output_path_local"] == str(results / "out" / f"{_RUN_ID}.json")
    assert env["reduced"] == {"k": 1}


def test_nonzero_exit_raises_with_stderr(tmp_path: Path) -> None:
    results = tmp_path / "fetched"
    results.mkdir()
    cmd = _reducer(
        tmp_path,
        """
        import sys
        print("boom: my reducer failed", file=sys.stderr)
        sys.exit(3)
        """,
    )

    with pytest.raises(errors.RemoteCommandFailed) as exc:
        local_reduce(run_id=_RUN_ID, results_dir=results, aggregate_cmd=cmd)
    assert "exited 3" in str(exc.value)
    assert "boom: my reducer failed" in str(exc.value)


def test_exit_zero_but_no_output_raises(tmp_path: Path) -> None:
    results = tmp_path / "fetched"
    results.mkdir()
    # Exits 0 but never writes $HPC_AGGREGATED_OUTPUT.
    cmd = _reducer(tmp_path, "pass\n")

    with pytest.raises(errors.RemoteCommandFailed) as exc:
        local_reduce(run_id=_RUN_ID, results_dir=results, aggregate_cmd=cmd)
    assert "is missing" in str(exc.value)


def test_invalid_json_output_raises(tmp_path: Path) -> None:
    results = tmp_path / "fetched"
    results.mkdir()
    cmd = _reducer(
        tmp_path,
        """
        import os
        open(os.environ["HPC_AGGREGATED_OUTPUT"], "w").write("not json{")
        """,
    )

    with pytest.raises(errors.RemoteCommandFailed) as exc:
        local_reduce(run_id=_RUN_ID, results_dir=results, aggregate_cmd=cmd)
    assert "not valid JSON" in str(exc.value)


@pytest.mark.parametrize(("run_id", "cmd"), [("", "true"), (_RUN_ID, "")])
def test_empty_run_id_or_cmd_raises_spec_invalid(tmp_path: Path, run_id: str, cmd: str) -> None:
    with pytest.raises(errors.SpecInvalid):
        local_reduce(run_id=run_id, results_dir=tmp_path, aggregate_cmd=cmd)
