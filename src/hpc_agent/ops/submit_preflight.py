"""``submit-preflight``: composite primitive (WS5 #1, scaffold).

Fans ``export-package`` + ``plan-throughput`` + ``validate-campaign`` out in
parallel as a single CLI call. Replaces the three-call sequence the
``/submit-hpc`` agent currently narrates as "Running … in parallel" — the
live-witnessed 2026-06-04 failure mode where the agent has to remember to
fire all three (and historically has forgotten ``validate-campaign``).

Internal composition: ``asyncio.gather`` over three
``asyncio.create_subprocess_exec`` calls to the existing ``hpc-agent``
verbs. All three are independent (no shared file writes; the cluster-side
ssh in ``export-package`` is the long pole — running the other two in its
shadow is essentially free wall-clock).

**Scaffold only.** This module exposes the public ``submit_preflight()``
function but is **NOT yet registered as a CLI verb**. The CLI dispatcher
registration is held until WS2 (sub-skill return file primitive) lands,
because WS2 is editing the same dispatcher and adding the corresponding
``operations.json`` entries — racing on either would force a merge. After
WS2 lands, the follow-up is:

1. Register the verb in :mod:`hpc_agent.cli.dispatch` (parse args against
   ``schemas/submit_preflight.input.json``; serialize the return into
   ``SuccessEnvelope`` shape and print).
2. Regenerate ``hpc_agent/operations.json`` via
   ``scripts/bake_operations_json.py --write``.
3. Add the verb to ``hpc-agent describe`` discovery if it's not picked up
   automatically by the bake step.
4. Update ``/submit-hpc`` SKILL.md to invoke ``hpc-agent submit-preflight``
   instead of the three separate calls.

I/O contracts:

* Input: see ``hpc_agent/schemas/submit_preflight.input.json``.
* Output: a ``dict`` matching ``schemas/submit_preflight.output.json`` —
  the caller (CLI dispatcher) wraps it into the standard
  ``SuccessEnvelope`` from ``envelope.json``.

Failure semantics: a sub-call failure surfaces as ``overall: "fail"`` in
the composite ``data`` block, with the failing sub-call's verbatim
envelope nested under its ``SubResult``. The composite itself still
returns successfully (no exception, no ``ok: false`` at the outer level)
so the parallel siblings' successful work is not lost.
"""

from __future__ import annotations

import asyncio
import json
import subprocess  # noqa: F401  # used in type-only signatures via CompletedProcess shape
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = [
    "SubCall",
    "submit_preflight",
]


@dataclass(frozen=True)
class SubCall:
    """One fanned-out sub-call within submit-preflight.

    ``name`` is the user-visible sub-call identifier (matches the ``skip``
    enum values and the output-schema property name). ``argv`` is the full
    argv (starting with ``hpc-agent``) to spawn.
    """

    name: str
    argv: list[str]


def _build_subcalls(
    *,
    experiment_dir: Path,
    cluster: str,
    profile: str,
    campaign_id: str | None,
    expected_cmd_sha: str | None,
    force_export: bool,
    notebooks_dir: str,
    skip: list[str],
) -> list[SubCall]:
    """Construct one :class:`SubCall` per non-skipped sub-step.

    Order matters only for sequential mode (humans expect
    export → plan → validate left-to-right); asyncio mode collapses the
    distinction.
    """
    exp_str = str(experiment_dir)
    calls: list[SubCall] = []

    if "export-package" not in skip:
        argv = ["hpc-agent", "export-package", "--experiment-dir", exp_str]
        if force_export:
            argv.append("--force")
        if notebooks_dir != "notebooks":
            argv += ["--notebooks-dir", notebooks_dir]
        calls.append(SubCall(name="export-package", argv=argv))

    if "plan-throughput" not in skip:
        argv = [
            "hpc-agent",
            "plan-throughput",
            "--experiment-dir",
            exp_str,
            "--cluster",
            cluster,
        ]
        calls.append(SubCall(name="plan-throughput", argv=argv))

    if "validate-campaign" not in skip:
        argv = [
            "hpc-agent",
            "validate-campaign",
            "--experiment-dir",
            exp_str,
            "--cluster",
            cluster,
            "--profile",
            profile,
        ]
        if campaign_id is not None:
            argv += ["--campaign-id", campaign_id]
        if expected_cmd_sha is not None:
            argv += ["--expected-cmd-sha", expected_cmd_sha]
        calls.append(SubCall(name="validate-campaign", argv=argv))

    return calls


def _synth_error_subresult(
    *,
    error_code: str,
    message: str,
    category: str,
    elapsed_sec: float,
) -> dict[str, Any]:
    """Build a SubResult whose envelope is a synthesised ErrorEnvelope.

    Used for failures BEFORE the sub-call could emit its own JSON — spawn
    failures, timeouts, JSON parse errors on stdout. Conforms to
    ``ErrorEnvelope`` in ``envelope.json``.
    """
    return {
        "envelope": {
            "ok": False,
            "error_code": error_code,
            "message": message,
            "category": category,
            "retry_safe": False,
        },
        "elapsed_sec": elapsed_sec,
        "ok": False,
    }


async def _run_subprocess(call: SubCall, *, timeout_sec: float) -> dict[str, Any]:
    """Run *call.argv* via asyncio and return its :class:`SubResult` dict.

    Captures stdout + stderr; parses stdout as a JSON envelope. On spawn
    failure / timeout / non-JSON stdout, synthesises an ErrorEnvelope so
    the outer composite can surface a uniform SubResult shape regardless
    of where the failure was.
    """
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *call.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return _synth_error_subresult(
            error_code="internal",
            message=f"failed to spawn {call.name}: {exc}",
            category="internal",
            elapsed_sec=time.monotonic() - started,
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return _synth_error_subresult(
            error_code="cluster_timeout",
            message=f"{call.name} exceeded {timeout_sec}s timeout",
            category="cluster",
            elapsed_sec=time.monotonic() - started,
        )

    elapsed_sec = time.monotonic() - started
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "envelope": {
                "ok": False,
                "error_code": "internal",
                "message": (
                    f"{call.name} did not emit a JSON envelope on stdout; "
                    f"stderr tail: {stderr[-400:]}"
                ),
                "category": "internal",
                "retry_safe": False,
            },
            "elapsed_sec": elapsed_sec,
            "ok": False,
        }

    return {
        "envelope": envelope,
        "elapsed_sec": elapsed_sec,
        "ok": bool(envelope.get("ok", False)),
    }


async def _run_async(
    calls: list[SubCall], *, fanout_strategy: str, timeout_sec: float
) -> list[dict[str, Any]]:
    """Dispatch *calls* per *fanout_strategy*; return SubResults in input order."""
    if fanout_strategy == "sequential":
        return [await _run_subprocess(c, timeout_sec=timeout_sec) for c in calls]
    return await asyncio.gather(*(_run_subprocess(c, timeout_sec=timeout_sec) for c in calls))


def _derive_overall(sub_results: list[dict[str, Any]], by_name: dict[str, dict[str, Any]]) -> str:
    """Derive the composite ``overall`` per the output schema.

    Precedence: any sub-call failure → ``fail``; else validate-campaign's
    own ``overall: warn`` → ``warn``; else ``pass``.
    """
    if any(not r["ok"] for r in sub_results):
        return "fail"
    vc = by_name.get("validate-campaign")
    if vc is not None and vc["ok"]:
        vc_overall = vc["envelope"].get("data", {}).get("overall")
        if vc_overall == "warn":
            return "warn"
    return "pass"


@primitive(
    name="submit-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Composite preflight before submit: fans out export-package + "
            "plan-throughput + validate-campaign in parallel as one call."
        ),
        verb="submit-preflight",
        args=(
            CliArg(
                "--experiment-dir",
                type=str,
                required=True,
                help="Absolute path to the experiment directory.",
            ),
            CliArg(
                "--cluster",
                type=str,
                required=True,
                help="Cluster name from clusters.yaml.",
            ),
            CliArg(
                "--profile",
                type=str,
                required=True,
                help="Scheduler profile (sge / slurm / pbspro / torque).",
            ),
            CliArg(
                "--campaign-id",
                type=str,
                default=None,
                help="Optional campaign slug for validate-campaign's stochastic-marker check.",
            ),
            CliArg(
                "--expected-cmd-sha",
                type=str,
                default=None,
                help="Required alongside --campaign-id to enable stochastic-marker.",
            ),
        ),
        # export-package SSHes to push the package; declare it.
        requires_ssh=True,
    ),
    agent_facing=True,
)
def submit_preflight(
    *,
    experiment_dir: str | Path,
    cluster: str,
    profile: str,
    campaign_id: str | None = None,
    expected_cmd_sha: str | None = None,
    force_export: bool = False,
    notebooks_dir: str = "notebooks",
    skip: list[str] | None = None,
    fanout_strategy: str = "asyncio",
    timeout_sec: float = 600.0,
) -> dict[str, Any]:
    """Fan out ``export-package`` + ``plan-throughput`` + ``validate-campaign``.

    Returns the composite's ``data`` block matching
    ``schemas/submit_preflight.output.json``. The CLI dispatcher wraps
    this in a ``SuccessEnvelope``; in-process callers consume the dict
    directly. *experiment_dir* accepts both ``str`` (the CLI path) and
    ``Path`` (the in-process path) and is coerced internally.

    The composite itself only raises on programmer error (invalid
    ``fanout_strategy``); every external failure surfaces inside a
    ``SubResult.envelope`` so the parallel siblings' work is preserved.
    """
    if fanout_strategy not in ("asyncio", "sequential"):
        raise ValueError(
            f"unknown fanout_strategy {fanout_strategy!r}; expected 'asyncio' or 'sequential'"
        )

    experiment_dir_path = (
        experiment_dir if isinstance(experiment_dir, Path) else Path(experiment_dir)
    )
    skip_list = list(skip or [])
    calls = _build_subcalls(
        experiment_dir=experiment_dir_path,
        cluster=cluster,
        profile=profile,
        campaign_id=campaign_id,
        expected_cmd_sha=expected_cmd_sha,
        force_export=force_export,
        notebooks_dir=notebooks_dir,
        skip=skip_list,
    )

    started = time.monotonic()
    sub_results = asyncio.run(
        _run_async(calls, fanout_strategy=fanout_strategy, timeout_sec=timeout_sec)
    )
    elapsed_total_sec = time.monotonic() - started

    by_name = {c.name: r for c, r in zip(calls, sub_results, strict=False)}
    overall = _derive_overall(sub_results, by_name)

    return {
        "overall": overall,
        "elapsed_total_sec": elapsed_total_sec,
        "fanout_strategy": fanout_strategy,
        "export_package": by_name.get("export-package"),
        "plan_throughput": by_name.get("plan-throughput"),
        "validate_campaign": by_name.get("validate-campaign"),
    }
