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


def test_wrapper_scan_follows_argv_target_one_hop(tmp_path) -> None:
    """Run-#12 finding 15: a shell_command wrapper subprocess-invokes the real
    script with env inherited — the contract lives in the TARGET. The probe
    follows the wrapper's referenced .py one hop and stays silent when the
    target honors HPC_RESULT_DIR."""
    from hpc_agent.ops.resolve_submit_inputs import _probe_result_dir_contract

    wrappers = tmp_path / ".hpc" / "wrappers"
    wrappers.mkdir(parents=True)
    (wrappers / "wrap.py").write_text(
        'import subprocess\nsubprocess.check_call(["python3", "specs/real.py"])\n',
        encoding="utf-8",
    )
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "real.py").write_text(
        'import os\nROOT = os.environ.get("HPC_RESULT_DIR", "results")\n',
        encoding="utf-8",
    )
    assert _probe_result_dir_contract("python3 .hpc/wrappers/wrap.py", tmp_path) is None


def test_wrapper_scan_still_warns_when_target_lacks_contract(tmp_path) -> None:
    from hpc_agent.ops.resolve_submit_inputs import _probe_result_dir_contract

    wrappers = tmp_path / ".hpc" / "wrappers"
    wrappers.mkdir(parents=True)
    (wrappers / "wrap.py").write_text(
        'import subprocess\nsubprocess.check_call(["python3", "specs/real.py"])\n',
        encoding="utf-8",
    )
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "real.py").write_text('open("literal.txt", "w").write("x")\n', encoding="utf-8")
    warning = _probe_result_dir_contract("python3 .hpc/wrappers/wrap.py", tmp_path)
    assert warning is not None and "never references" in warning
