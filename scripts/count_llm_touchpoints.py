"""Measure the "LLM control-flow touchpoint" surface in the worker prompts.

Each worker prompt
(``src/hpc_agent/_kernel/extension/worker_prompts/{submit,status,aggregate,campaign}.md``)
narrates, in prose, control flow the LLM is expected to *execute* at run
time: branch-on-a-field, stop-gates, retry/poll loops. Every such marker
is deterministic control flow that has NOT yet been absorbed into a
workflow composite (submit-and-verify, submit-pipeline, ...) — so the
count is a direct, regression-gated proxy for **how much of the spine is
still narrated for the LLM to run by hand**.

``total_touchpoints = branches + stop_gates + retry_loops`` is that
deterministic surface. It is expected to go DOWN over time: as composites
chain the branches/gates/loops into code under a single envelope, the
prose that the LLM had to follow shrinks, and so does this number. A PR
that *adds* deterministic narration (re-growing the surface) trips the
``--check`` gate and must update the baseline deliberately.

``escalation_points`` is tracked SEPARATELY and is NOT part of
``total_touchpoints``: it counts the places where the workflow records a
genuine judgement/anomaly and hands back to the caller. Those are the
LEGITIMATE LLM residual — the judgement points the deterministic layer
*cannot* decide — and they are expected to STAY, not shrink to zero. The
goal is to drive ``total_touchpoints`` down toward the irreducible
``escalation_points``, not to delete the escalation points themselves.

Same ``--check`` / ``--write`` / diff CLI shape as
``scripts/bake_operations_json.py``: pre-commit + CI run ``--check`` so a
prompt edit that moves a count without regenerating the baseline is a CI
failure.

Usage::

    uv run python scripts/count_llm_touchpoints.py            # diff
    uv run python scripts/count_llm_touchpoints.py --check    # CI gate
    uv run python scripts/count_llm_touchpoints.py --write    # apply
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "src" / "hpc_agent" / "_kernel" / "extension" / "worker_prompts"
WORKFLOWS = ("aggregate", "campaign", "status", "submit")
BASELINE_PATH = Path(__file__).resolve().parent / "llm_touchpoints_baseline.json"

# --- Precise, per-line markers ---------------------------------------------
#
# Each pattern is matched per line so a markdown table row (`a | b | c`)
# cannot be miscounted as a branch: a branch-bullet must START with a
# list dash and carry a backtick-quoted field + an arrow / em-dash, or be
# explicit `Branch on ...` / bold-backtick-bullet / `if \`data.\`` prose.

# branch-bullets: `- \`<field>\` → ...` or `- \`<field>\` — ...`
_BRANCH_BULLET = re.compile(r"^\s*-\s+`[^`]+`\s*(?:→|—)")
# explicit "Branch on" lead-in (e.g. "Branch on `action`:", "Branch on the ...")
_BRANCH_ON = re.compile(r"Branch on")
# bold-backtick bullet: `- **\`<thing>\` ...`
_BRANCH_BOLD = re.compile(r"^\s*-\s+\*\*`")
# inline conditional on a data field: "if `data.foo`" / "if data.bar"
_BRANCH_IF_DATA = re.compile(r"if\s+`?data\.")

_STOP_GATE = re.compile(r"\bstop\.|\bstop —|do not (?:proceed|run|re-)|never launch")

_RETRY_LOOP = re.compile(
    r"retry|re-invoke|re-run|\bpoll\b|every \d+\s*s|max_retries|\bloop\b|until terminal"
)

# escalation points: a decision/anomaly recorded and handed back to caller.
_ESCALATION = re.compile(
    r"record (?:a|an)?\s*`?\w+`?\s*decision"
    r"|needs_resolution"
    r"|re-invoke(?:s)? this workflow"
    r"|for the caller to (?:handle|confirm)"
)


def _count_branches(line: str) -> int:
    """1 if *line* carries any branch marker, else 0 (one count per line).

    Counting per line (not per regex) keeps a single bullet that happens
    to match two of the branch shapes from being double-counted.
    """
    if (
        _BRANCH_BULLET.search(line)
        or _BRANCH_ON.search(line)
        or _BRANCH_BOLD.search(line)
        or _BRANCH_IF_DATA.search(line)
    ):
        return 1
    return 0


def _count_pattern(pattern: re.Pattern[str], line: str) -> int:
    """1 if *line* matches *pattern*, else 0 (one count per line)."""
    return 1 if pattern.search(line) else 0


def _count_one(text: str) -> dict[str, int]:
    branches = stop_gates = retry_loops = escalation_points = 0
    for line in text.splitlines():
        branches += _count_branches(line)
        stop_gates += _count_pattern(_STOP_GATE, line)
        retry_loops += _count_pattern(_RETRY_LOOP, line)
        escalation_points += _count_pattern(_ESCALATION, line)
    return {
        "branches": branches,
        "stop_gates": stop_gates,
        "retry_loops": retry_loops,
        # total_touchpoints is the DETERMINISTIC surface only; escalation
        # points are the legitimate LLM residual, tracked but excluded.
        "escalation_points": escalation_points,
        "total_touchpoints": branches + stop_gates + retry_loops,
    }


def _emit() -> str:
    """Render the per-workflow touchpoint counts as stable, sorted JSON."""
    result: dict[str, object] = {}
    for workflow in sorted(WORKFLOWS):
        text = (PROMPTS_DIR / f"{workflow}.md").read_text(encoding="utf-8")
        result[workflow] = _count_one(text)
    result["_meta"] = {
        "description": (
            "Deterministic control-flow markers still narrated in prose "
            "for the LLM to execute, per worker prompt. total_touchpoints "
            "= branches + stop_gates + retry_loops (expected to DROP as "
            "workflow composites absorb the spine into code). "
            "escalation_points is the legitimate LLM residual (judgement "
            "points handed back to the caller) and is expected to STAY."
        ),
        # ``as_posix()`` so the baseline is byte-identical on Windows and POSIX
        # (``str()`` would emit backslashes on win32 and break the --check gate).
        "prompts_dir": PROMPTS_DIR.relative_to(REPO_ROOT).as_posix(),
        "workflows": list(sorted(WORKFLOWS)),
    }
    return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    write = "--write" in sys.argv
    check = "--check" in sys.argv

    new = _emit()
    old = BASELINE_PATH.read_text(encoding="utf-8") if BASELINE_PATH.is_file() else ""
    rel = BASELINE_PATH.relative_to(REPO_ROOT)

    if old == new:
        payload = json.loads(new)
        total = sum(v["total_touchpoints"] for k, v in payload.items() if k != "_meta")
        print(f"llm_touchpoints_baseline.json up to date ({total} total touchpoints)")
        return 0

    if check:
        print(
            f"ERROR: {rel} is out of date — "
            "run scripts/count_llm_touchpoints.py --write to regenerate",
            file=sys.stderr,
        )
        # Show the drift so CI logs explain the failure.
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
        sys.stderr.write("".join(diff))
        return 1

    if write:
        BASELINE_PATH.write_text(new, encoding="utf-8")
        print(f"  wrote {rel}")
        payload = json.loads(new)
        total = sum(v["total_touchpoints"] for k, v in payload.items() if k != "_meta")
        print(f"counted {total} total deterministic touchpoints")
        return 0

    # Default: print a diff so the human can preview without writing.
    print(f"--- a/{rel}")
    print(f"+++ b/{rel}")
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        n=3,
    )
    sys.stdout.write("".join(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
