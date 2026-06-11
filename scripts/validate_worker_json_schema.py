#!/usr/bin/env python3
"""Live validation for issue #269: decode-schema constraints × the agent loop.

Each worker harness gained an optional decode-time output constraint whose
default is gated on a LIVE per-harness validation run (the gate split of #318):

* **claude** — `claude -p --json-schema` with the lenient ``worker.output.json``.
  Validated 2026-06-10 (CLI 2.1.170, three consecutive passes) and flipped on
  by default in 0.10.59; this harness re-validates after CLI upgrades.
* **codex** — `codex exec --output-schema` with the API-strict
  ``worker.strict.output.json``. The still-pending #269 half: its gate
  (``HPC_AGENT_CODEX_OUTPUT_SCHEMA``) stays off by default until this harness
  passes on a real `codex` CLI with credentials.

The two empirical questions are the same for every harness:

1. **Composition** — does the decode constraint bind only the worker's FINAL
   message, leaving the multi-step tool loop (rsync / qsub / canary in
   production) intact?
2. **Schema acceptance** — does this CLI accept this schema shape (lenient for
   claude, API-strict for codex)?

The harness exercises the production spawn path for the selected harness —
`_run_claude_worker` (argv assembly, temp-file + stdin prompt transport, JSON
result-envelope unwrap) or `CodexCliInvoker.invoke` (execpolicy fence,
`--output-schema` file, `--output-last-message` report) — with the gate forced
on, and gives the worker a deterministic multi-step task with observable side
effects:

  * Write a token to ``<workdir>/step1_token.txt``        (tool turn 1)
  * Read it back                                          (tool turn 2)
  * Write the uppercased token to ``<workdir>/step2_echo.txt`` (tool turn 3)
  * Emit a WorkerReport carrying the token as its final message

PASS requires: exit 0, both side-effect files present with the right bytes
(the loop ran — question 1), the final output a schema-valid WorkerReport
(question 2), and the report's token matching the loop's (the constrained
decode reflects work actually done in the loop, not a hallucinated report).
On the claude harness, if the CLI rejects the lenient schema the harness
retries with the strict variant and reports which shape was accepted.

Claude auth modes (``--mode``, default auto-detect):

  * ``bare``    — production `ClaudeCliInvoker` argv (`--bare`, API key auth).
  * ``ambient`` — same argv minus `--bare`, relying on the calling
    environment's own `claude` login. This is what the production
    `ClaudeCliOAuthInvoker` mode reduces to (it too drops `--bare`); use it
    where credentials are host-managed (e.g. Claude Code remote containers).

Codex authenticates per production: ``CODEX_API_KEY`` (mapped onto the child's
``OPENAI_API_KEY``) or a stored ChatGPT login in ``~/.codex/auth.json``.

Run:  python scripts/validate_worker_json_schema.py [--harness claude|codex]
                                                    [--mode bare|ambient]
Exit: 0 all checks pass, 1 otherwise. Evidence JSON on stdout either way.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from hpc_agent._kernel.lifecycle.invoke import (  # noqa: E402
    _CODEX_OUTPUT_SCHEMA_ENV,
    _WORKER_ALLOWED_TOOLS,
    _WORKER_DISALLOWED_TOOLS,
    _WORKER_JSON_SCHEMA_ENV,
    _WORKER_MODEL,
    CodexCliInvoker,
    InvocationResult,
    RenderedPrompt,
    _load_schema_resource,
    _run_claude_worker,
    _worker_output_schema,
)

_PROCEDURE = """\
You are a delegated hpc-agent validation worker. Execute the numbered steps
exactly, using your tools; do not ask questions, do not skip steps.

After the steps, your FINAL message must be ONLY a JSON object of this shape
(no prose, no code fences):

{"result": {"validated": true, "token": "<the token you wrote in step 1>",
 "steps_completed": 3}, "decisions": [], "anomalies": ""}

Set "anomalies" to a short description if any step failed; otherwise "".
"""

_TASK_TEMPLATE = """\
Invocation context:
- workdir: {workdir}
- token: {token}

Steps:
1. Write a file {workdir}/step1_token.txt whose entire content is exactly the
   token above (no trailing newline).
2. Read {workdir}/step1_token.txt back to confirm the content.
3. Write a file {workdir}/step2_echo.txt whose entire content is exactly the
   token uppercased (no trailing newline).

Then emit the final JSON report described in your procedure.
"""


def _mode_args(mode: str) -> list[str]:
    args = [] if mode == "ambient" else ["--bare"]
    return [
        *args,
        "--model",
        _WORKER_MODEL,
        "--settings",
        '{"sandbox": {"enabled": false}}',
        "--allowedTools",
        _WORKER_ALLOWED_TOOLS,
        "--disallowedTools",
        _WORKER_DISALLOWED_TOOLS,
    ]


def _detect_mode() -> str:
    return "bare" if os.environ.get("ANTHROPIC_API_KEY") else "ambient"


def _check_run(result: InvocationResult, workdir: Path, token: str, evidence: dict) -> dict:
    """The harness-independent verdict: tool-loop side effects + report shape."""
    checks: dict[str, bool] = {}
    checks["exit_zero"] = result.exit_code == 0

    step1 = workdir / "step1_token.txt"
    step2 = workdir / "step2_echo.txt"
    step1_text = step1.read_text(encoding="utf-8").strip() if step1.is_file() else None
    step2_text = step2.read_text(encoding="utf-8").strip() if step2.is_file() else None
    checks["tool_loop_step1"] = step1_text == token
    checks["tool_loop_step2"] = step2_text == token.upper()

    report_obj = None
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        report_obj = json.loads(result.output)
    checks["final_message_is_json_object"] = isinstance(report_obj, dict)

    if isinstance(report_obj, dict):
        from hpc_agent._wire.spawn_contract import WorkerReport

        try:
            report = WorkerReport.model_validate(report_obj)
            checks["workerreport_valid"] = True
            checks["token_round_trip"] = report.result.get("token") == token
        except Exception as exc:  # pydantic ValidationError
            checks["workerreport_valid"] = False
            checks["token_round_trip"] = False
            evidence["validation_error"] = str(exc)[:2000]
    else:
        checks["workerreport_valid"] = False
        checks["token_round_trip"] = False

    evidence["checks"] = checks
    evidence["passed"] = all(checks.values())
    evidence["report"] = report_obj if isinstance(report_obj, dict) else None
    if not evidence["passed"]:
        evidence["stdout_tail"] = (result.output or "")[-2000:]
        evidence["stderr_tail"] = (result.stderr or "")[-2000:]
    return evidence


def _task_prompt() -> tuple[RenderedPrompt, Path, str]:
    token = secrets.token_hex(8)
    workdir = Path(tempfile.mkdtemp(prefix="hpc-agent-269-"))
    prompt = RenderedPrompt(
        cacheable_prefix=_PROCEDURE,
        variable_suffix=_TASK_TEMPLATE.format(workdir=workdir, token=token),
    )
    return prompt, workdir, token


def _run_claude_once(mode: str, schema: str, schema_name: str) -> dict:
    prompt, workdir, token = _task_prompt()
    with tempfile.TemporaryDirectory(prefix="hpc-agent-269-cwd-") as cwd:
        result = _run_claude_worker(
            executable="claude",
            mode_args=_mode_args(mode),
            prompt=prompt,
            cwd=cwd,
            output_schema=schema,
        )
    evidence: dict = {
        "harness": "claude",
        "schema": schema_name,
        "mode": mode,
        "exit_code": result.exit_code,
        "workdir": str(workdir),
    }
    return _check_run(result, workdir, token, evidence)


def _run_codex_once() -> dict:
    # The production invoker end-to-end: execpolicy fence, strict schema file
    # via --output-schema (the gate is forced on below), --output-last-message
    # report. Auth per production: CODEX_API_KEY or a stored ChatGPT login.
    prompt, workdir, token = _task_prompt()
    with tempfile.TemporaryDirectory(prefix="hpc-agent-269-cwd-") as cwd:
        result = CodexCliInvoker().invoke(prompt, cwd=Path(cwd))
    evidence: dict = {
        "harness": "codex",
        "schema": "worker.strict.output.json (strict)",
        "exit_code": result.exit_code,
        "workdir": str(workdir),
    }
    return _check_run(result, workdir, token, evidence)


def _claude_runs(mode: str) -> list[dict]:
    # Force the gate on so the production loader (_worker_output_schema) is
    # what supplies the minified lenient schema — the exact bytes the default
    # spawn puts on every worker's argv.
    os.environ[_WORKER_JSON_SCHEMA_ENV] = "1"
    lenient = _worker_output_schema()
    if lenient is None:
        raise SystemExit("FATAL: _worker_output_schema() returned None with the gate on")
    runs = [_run_claude_once(mode, lenient, "worker.output.json (lenient)")]
    if not runs[0]["passed"] and not runs[0]["checks"]["exit_zero"]:
        # Question 2 contingency: lenient rejected → try the strict variant.
        strict = _load_schema_resource("worker.strict.output.json")
        if strict:
            runs.append(_run_claude_once(mode, strict, "worker.strict.output.json (strict)"))
    return runs


def _codex_runs() -> list[dict]:
    remediation = CodexCliInvoker().missing_credential_remediation()
    if remediation and not Path.home().joinpath(".codex", "auth.json").is_file():
        print(f"NOTE: {remediation}", file=sys.stderr)
    os.environ[_CODEX_OUTPUT_SCHEMA_ENV] = "1"
    return [_run_codex_once()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--harness", choices=["claude", "codex"], default="claude")
    parser.add_argument("--mode", choices=["bare", "ambient"], default=_detect_mode())
    args = parser.parse_args()

    runs = _claude_runs(args.mode) if args.harness == "claude" else _codex_runs()

    print(json.dumps({"issue": 269, "runs": runs}, indent=2))
    final = runs[-1]
    print()
    for name, ok in final["checks"].items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{'PASS' if final['passed'] else 'FAIL'}: {final['harness']}, {final['schema']}")
    return 0 if final["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
