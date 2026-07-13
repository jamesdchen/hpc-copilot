"""``_classify_runtime_exit`` — the dispatcher scaffold-code vocabulary.

Run-#10 finding: the write-isolation violation (dispatcher exit 4) surfaced
as ``failure_kind="timeout"`` after the full wait budget — a correct gate
wearing the wrong name. Contracts must fail in their own vocabulary; these
pin the mapping, kept in lock-step with ``dispatch.py``'s
``_EXIT_NO_RUNNER`` (3) / ``_EXIT_NO_OUTPUT`` (4).
"""

from __future__ import annotations

from hpc_agent.ops.verify_canary import _classify_runtime_exit


def _classify(code: int):
    return _classify_runtime_exit(code, canary_run_id="r-canary", result_dir="results/r-canary")


def test_exit_zero_is_no_verdict() -> None:
    assert _classify(0) is None


def test_exit_four_is_output_contract() -> None:
    kind, details = _classify(4)
    assert kind == "output_contract"
    assert "$HPC_RESULT_DIR" in details
    assert "retrying cannot fix this" in details


def test_exit_three_is_no_runner() -> None:
    kind, details = _classify(3)
    assert kind == "no_runner"
    assert "retrying cannot fix this" in details


def test_other_nonzero_stays_nonzero_exit() -> None:
    kind, details = _classify(7)
    assert kind == "nonzero_exit"
    assert "exit_code=7" in details
    assert "results/r-canary/_runtime.json" in details
