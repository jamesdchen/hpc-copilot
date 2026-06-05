"""Regression gate for the LLM control-flow touchpoint count.

``scripts/count_llm_touchpoints.py`` measures how much deterministic
control flow each worker prompt still narrates in prose for the LLM to
execute (branches + stop-gates + retry/poll loops), plus the legitimate
LLM residual (``escalation_points`` — judgement points handed back to the
caller). The committed baseline
(``scripts/llm_touchpoints_baseline.json``) pins those numbers; this test
is the gate that fails when a prompt edit moves a count without
regenerating the baseline.

Mirrors ``tests/scripts/test_bake_operations_json.py``: the ``--check``
mode is exercised via subprocess exactly as CI / pre-commit would run it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "count_llm_touchpoints.py"
BASELINE = REPO_ROOT / "scripts" / "llm_touchpoints_baseline.json"

_EXPECTED_KEYS = {
    "branches",
    "stop_gates",
    "retry_loops",
    "escalation_points",
    "total_touchpoints",
}


def test_check_mode_reports_clean():
    """The CI gate path: ``--check`` exits 0 against the committed baseline."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"count_llm_touchpoints.py --check failed unexpectedly — the prompts "
        f"drifted from the baseline. Run "
        f"scripts/count_llm_touchpoints.py --write to regenerate.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "up to date" in result.stdout


def test_every_workflow_has_the_five_integer_keys():
    """Each workflow entry exposes exactly the five integer touchpoint keys."""
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    workflows = [k for k in payload if k != "_meta"]
    assert workflows, "baseline has no workflow entries"
    for workflow in workflows:
        entry = payload[workflow]
        assert set(entry.keys()) == _EXPECTED_KEYS, (
            f"{workflow} entry has keys {sorted(entry.keys())}, expected {sorted(_EXPECTED_KEYS)}"
        )
        for key, value in entry.items():
            assert isinstance(value, int) and not isinstance(value, bool), (
                f"{workflow}.{key} is {value!r}, expected an int"
            )
        # total_touchpoints is the deterministic surface only — it must
        # equal branches + stop_gates + retry_loops (escalation excluded).
        assert entry["total_touchpoints"] == (
            entry["branches"] + entry["stop_gates"] + entry["retry_loops"]
        ), f"{workflow}.total_touchpoints is not branches+stop_gates+retry_loops"


def test_submit_has_the_most_branches():
    """``submit`` is the biggest spine, so it carries the most branch-bullets."""
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    submit_branches = payload["submit"]["branches"]
    for workflow, entry in payload.items():
        if workflow in {"_meta", "submit"}:
            continue
        assert submit_branches > entry["branches"], (
            f"submit ({submit_branches} branches) should exceed "
            f"{workflow} ({entry['branches']} branches)"
        )
