"""``classify-axis-preflight``: composite primitive (WS5 #6).

Collapses the top of ``hpc-classify-axis`` — Step 1 (``discover-runs``),
Step 2 (cache-check: reuse a still-valid classification from
``.hpc/axes.yaml``) and Step 3 (``recall``: pre-fill from prior
campaigns) — into ONE CLI call so the agent's role at the top of the
skill shrinks to one tool call plus a branch on the returned ``data``.

Mirror of :mod:`submit_preflight` / :mod:`status_preflight`: a sequence
of sub-calls, each returning a ``SubResult`` (verbatim envelope +
elapsed wall-clock + ``ok`` mirror), with an ``overall`` verdict derived
from them and skipped / not-applicable slots returned as ``null``.

Sequencing + conditionality:

* ``discover-runs`` always runs (a subprocess against the existing CLI
  verb). It resolves the ``@register_run`` functions; the skill picks
  the single run from its ``data.runs``.
* ``cache-check`` always runs (an in-process ``axes.yaml`` read — there
  is no CLI verb for it; Step 2 of the skill is a plain file read).
  It reports whether ``executors.<run>`` exists AND its
  ``run_signature_sha`` still matches the run's current signature — a
  match means the stored ``DataAxis`` is reusable and the skill returns
  early.
* ``recall`` is CONDITIONAL — it runs only when (a) the caller did NOT
  supply ``data_axis`` (the interview / slash path resolves the axis up
  front and skips recall) AND (b) cache-check did NOT find a still-valid
  classification (a cache hit makes recall moot). When skipped its slot
  is ``null``. This is the "read the prior sub-call's output to decide"
  branch — the analogue of submit-preflight's ``--cluster``-gated
  check-preflight, but data-driven off the cache-check result rather
  than off a flag.

``requires_ssh`` is ``False``: ``discover-runs`` walks ``notebooks/``
locally, the cache-check reads ``axes.yaml`` locally, and ``recall``
walks ``interview.json`` files under ``--root`` locally — none of the
three reach the cluster.

I/O contracts:

* Input: see ``hpc_agent/schemas/classify_axis_preflight.input.json``.
* Output: a ``dict`` matching ``schemas/classify_axis_preflight.output.json``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.ops._preflight_common import (
    SubCall,
    _run_subprocess,
    _synth_error_subresult,  # noqa: F401 - re-export (tests reference it via the module)
)

__all__ = [
    "SubCall",
    "classify_axis_preflight",
]


def _build_subcalls(
    *, experiment_dir: Path, root: str | None, task_kind: str | None, run_recall: bool
) -> list[SubCall]:
    """Construct one :class:`SubCall` per non-skipped subprocess sub-step.

    Covers the two subprocess sub-calls only — ``discover-runs`` (always)
    and ``recall`` (only when *run_recall*). The cache-check is an
    in-process ``axes.yaml`` read (no CLI verb) and is run separately by
    :func:`_run_cache_check`, not enumerated here.

    Order is discover-runs first; recall, when run, is appended after the
    cache-check has already decided it is needed.
    """
    exp_str = str(experiment_dir)
    calls: list[SubCall] = [
        SubCall(
            name="discover-runs",
            argv=["hpc-agent", "discover-runs", "--experiment-dir", exp_str],
        )
    ]

    if run_recall:
        argv = ["hpc-agent", "recall"]
        if root is not None:
            argv += ["--root", root]
        if task_kind is not None:
            argv += ["--task-kind", task_kind]
        calls.append(SubCall(name="recall", argv=argv))

    return calls


def _run_cache_check(
    *, experiment_dir: Path, run_name: str | None, run_signature_sha: str | None
) -> dict[str, Any]:
    """Read ``axes.yaml`` and report whether a still-valid classification exists.

    This is Step 2 of the skill — a plain ``.hpc/axes.yaml`` read with no
    CLI verb behind it, so it runs in-process rather than via
    :func:`_run_subprocess`. The result is still shaped as a ``SubResult``
    (verbatim ``data`` envelope + elapsed + ``ok``) so consumers introspect
    every sub-call uniformly.

    A "hit" requires both the resolved *run_name* and the run's current
    *run_signature_sha*: ``executors.<run_name>`` must exist AND its stored
    ``run_signature_sha`` must equal the current one. A missing run_name (an
    ambiguous-run discovery the skill will reject) or a missing/absent entry
    is reported as ``hit: false`` — never a hard error; the cache check is
    advisory and an absent cache is the normal cold-start case.

    The envelope's ``ok`` is ``True`` whenever the read itself succeeded
    (whether or not it hit) — a cache miss is not a failure. Only a
    corrupt / schema-violating ``axes.yaml`` flips ``ok: false``.
    """
    from hpc_agent.state.axes import read_executor

    started = time.monotonic()
    try:
        entry = read_executor(experiment_dir, run_name) if run_name is not None else None
    except Exception as exc:  # noqa: BLE001 - surface corruption as a SubResult, never raise
        return {
            "envelope": {
                "ok": False,
                "error_code": "config_invalid",
                "message": f"cache-check could not read axes.yaml: {exc}",
                "category": "user",
                "retry_safe": False,
            },
            "elapsed_sec": time.monotonic() - started,
            "ok": False,
        }

    stored_sha = entry.get("run_signature_sha") if isinstance(entry, dict) else None
    hit = entry is not None and run_signature_sha is not None and stored_sha == run_signature_sha
    return {
        "envelope": {
            "ok": True,
            "idempotent": True,
            "data": {
                "hit": bool(hit),
                "run_name": run_name,
                "stored": entry,
                "stored_run_signature_sha": stored_sha,
                "current_run_signature_sha": run_signature_sha,
            },
        },
        "elapsed_sec": time.monotonic() - started,
        "ok": True,
    }


@primitive(
    name="classify-axis-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Composite preflight at the top of classify-axis: discover-runs + "
            "cache-check (axes.yaml reuse) + (when no cache hit and no caller-"
            "supplied data_axis) recall, sequenced, returned as one envelope."
        ),
        verb="classify-axis-preflight",
        args=(
            CliArg(
                "--experiment-dir",
                type=str,
                required=True,
                help="Absolute path to the experiment directory.",
            ),
            CliArg(
                "--run-name",
                type=str,
                default=None,
                help=(
                    "Name of the @register_run function to cache-check. When "
                    "omitted, the cache-check reports a miss (the skill resolves "
                    "the run from discover-runs' output before reusing)."
                ),
            ),
            CliArg(
                "--run-signature-sha",
                type=str,
                default=None,
                help=(
                    "The run's current run_signature_sha. A cache hit requires "
                    "the stored executors.<run> entry's sha to match this."
                ),
            ),
            CliArg(
                "--root",
                type=str,
                default=None,
                help=(
                    "Experiments root passed to recall --root. When omitted, "
                    "recall falls back to ~/.hpc-agent/config.json:experiment_roots."
                ),
            ),
            CliArg(
                "--task-kind",
                type=str,
                default=None,
                help="Passed to recall --task-kind to scope prior campaigns.",
            ),
            CliArg(
                "--data-axis-supplied",
                action="store_true",
                help=(
                    "Set when the caller already resolved data_axis (the slash / "
                    "interview path). Skips recall — the classification is already "
                    "decided, so pre-filling from memory is moot."
                ),
            ),
        ),
        # discover-runs walks notebooks/, the cache-check reads axes.yaml,
        # and recall walks interview.json under --root — all local. No SSH.
        requires_ssh=False,
    ),
    agent_facing=True,
)
def classify_axis_preflight(
    *,
    experiment_dir: str | Path,
    run_name: str | None = None,
    run_signature_sha: str | None = None,
    root: str | None = None,
    task_kind: str | None = None,
    data_axis_supplied: bool = False,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Run discover-runs → cache-check → (conditional) recall.

    Returns a dict matching ``schemas/classify_axis_preflight.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir*
    accepts both ``str`` (the CLI path) and ``Path`` (the in-process path)
    and is coerced internally.

    The composite never raises on a sub-call failure — failures surface
    inside ``SubResult.envelope`` so the discover-runs / cache-check work
    is preserved even when recall blows up.

    ``recall`` is skipped (its slot is ``null``) when *data_axis_supplied*
    is set OR when the cache-check found a still-valid classification —
    either way pre-filling from memory is unnecessary.
    """
    experiment_dir_path = (
        experiment_dir if isinstance(experiment_dir, Path) else Path(experiment_dir)
    )

    started = time.monotonic()

    # 1. discover-runs (always, subprocess).
    discover_call = _build_subcalls(
        experiment_dir=experiment_dir_path,
        root=root,
        task_kind=task_kind,
        run_recall=False,
    )[0]
    discover_result = _run_subprocess(discover_call, timeout_sec=timeout_sec)

    # 2. cache-check (always, in-process axes.yaml read).
    cache_result = _run_cache_check(
        experiment_dir=experiment_dir_path,
        run_name=run_name,
        run_signature_sha=run_signature_sha,
    )
    cache_hit = bool(cache_result["ok"] and cache_result["envelope"]["data"]["hit"])

    # 3. recall (conditional, subprocess) — read the prior sub-call's
    #    output to decide. Skip when the caller already resolved the axis
    #    or when the cache-check already found a reusable classification.
    recall_result: dict[str, Any] | None = None
    if not data_axis_supplied and not cache_hit:
        recall_call = _build_subcalls(
            experiment_dir=experiment_dir_path,
            root=root,
            task_kind=task_kind,
            run_recall=True,
        )[-1]
        recall_result = _run_subprocess(recall_call, timeout_sec=timeout_sec)

    elapsed_total_sec = time.monotonic() - started

    ran = [discover_result, cache_result]
    if recall_result is not None:
        ran.append(recall_result)
    overall = "fail" if any(not r["ok"] for r in ran) else "pass"

    return {
        "overall": overall,
        "elapsed_total_sec": elapsed_total_sec,
        "discover_runs": discover_result,
        "cache_check": cache_result,
        "recall": recall_result,
    }
