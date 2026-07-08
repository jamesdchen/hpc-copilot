"""The S1 output-contract probe — run-#10 finding F-C moved to disclosure.

An entry point that never reads ``$HPC_RESULT_DIR`` writes to literal paths
the dispatcher's write-isolation gate discards; before this probe the first
surface was a burned canary. These pin the S1 static disclosure: WARN when
the executor's own script never mentions the contract var, silent when it
does (or when no resolvable script exists — a wrapper may own the contract).
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.ops.resolve_submit_inputs import _probe_result_dir_contract


def test_warns_when_script_never_references_result_dir(tmp_path: Path) -> None:
    (tmp_path / "run.py").write_text("print('hello')\n", encoding="utf-8")
    warn = _probe_result_dir_contract("python run.py --config $CONFIG", tmp_path)
    assert warn is not None
    assert "$HPC_RESULT_DIR" in warn
    assert "output_contract" in warn


def test_silent_when_script_reads_the_contract_var(tmp_path: Path) -> None:
    (tmp_path / "run.py").write_text(
        "import os\nd = os.environ.get('HPC_RESULT_DIR')\n", encoding="utf-8"
    )
    assert _probe_result_dir_contract("python run.py", tmp_path) is None


def test_silent_when_no_resolvable_script(tmp_path: Path) -> None:
    # Framework dispatch commands / module executors carry no caller .py —
    # the framework's own runners handle the contract.
    cmd = "python3 -m hpc_agent.executor_cli run-module m:f"
    assert _probe_result_dir_contract(cmd, tmp_path) is None
    assert _probe_result_dir_contract("python missing.py", tmp_path) is None


def test_fail_open_on_unreadable_script(tmp_path: Path) -> None:
    d = tmp_path / "run.py"
    d.mkdir()  # a DIRECTORY named run.py: read_text raises -> probe degrades silent
    assert _probe_result_dir_contract("python run.py", tmp_path) is None
