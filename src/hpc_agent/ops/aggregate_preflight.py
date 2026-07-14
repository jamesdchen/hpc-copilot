"""``aggregate-preflight``: composite primitive — top-of-aggregate boilerplate.

WS5 #3b. Third and final member of the ``<skill>-preflight`` family
(after :mod:`submit_preflight` and :mod:`status_preflight`): collapses
the ``install-commands`` + ``load-context`` + (optional) ``reconcile``
calls at the top of every ``hpc-aggregate`` invocation into ONE CLI
call so the agent's role shrinks to one tool call.

The reconcile branch is the structural twist that distinguishes
aggregate from its two siblings. ``hpc-aggregate`` Step 1b reconciles a
journal-only ``in_flight`` run against the cluster before refusing with
"nothing to aggregate" — the journal lags the cluster, so a run the
journal still marks ``monitor`` may have actually terminated, failed, or
been purged. Unlike submit's ``check-preflight`` (whose argv is known
statically from ``--cluster``), reconcile fires only when ``load-context``'s
*output* reports ``next_step_hint == "monitor"`` AND the caller supplied
``--reconcile-scheduler``. So the reconcile sub-call is built *after*
load-context runs, from its envelope, not pre-composed up front.

Internal composition (#291): ``install-commands`` and ``load-context``
fan out CONCURRENTLY on a thread pool — they are write-disjoint AND
read-disjoint (install writes only ``~/.claude/{commands,skills,agents}``
+ ``settings.json``; load-context reads only the experiment's
``.hpc/{runs,journal,campaigns}`` tree, never ``~/.claude``), so the
prior "install must succeed first to register framework paths" claim was
inert. The reconcile sub-call is then run last (and only conditionally)
because (a) it's the SSH-touching call and (b) its argv is read from
load-context's runtime output — a real data dependency.

I/O contracts:

* Input: see ``hpc_agent/schemas/aggregate_preflight.input.json``.
* Output: a ``dict`` matching ``schemas/aggregate_preflight.output.json``.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
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
    "aggregate_preflight",
]

# install-commands and load-context are write-disjoint AND read-disjoint
# (#291), so they fan out concurrently on a thread pool. reconcile stays
# sequential AFTER the fan — its argv is read from load-context's
# envelope, a real data dependency.
_PARALLEL_SUBCALLS = frozenset({"install-commands", "load-context"})


def _build_subcalls(*, experiment_dir: Path, skip: list[str]) -> list[SubCall]:
    """Construct the always-run base sub-steps per *skip*.

    install-commands and load-context are both members of
    :data:`_PARALLEL_SUBCALLS` and fan out concurrently at run time
    (#291) — they are write-disjoint AND read-disjoint, so the listing
    order here is purely conventional. The conditional ``reconcile``
    sub-call is NOT built here — it depends on load-context's runtime
    output and is assembled by :func:`_maybe_build_reconcile` after the
    fan completes.
    """
    exp_str = str(experiment_dir)
    calls: list[SubCall] = []

    if "install-commands" not in skip:
        calls.append(SubCall(name="install-commands", argv=["hpc-agent", "install-commands"]))

    if "load-context" not in skip:
        calls.append(
            SubCall(
                name="load-context",
                argv=["hpc-agent", "load-context", "--experiment-dir", exp_str],
            )
        )

    return calls


def _maybe_build_reconcile(
    *,
    load_context_result: dict[str, Any] | None,
    experiment_dir: Path,
    reconcile_scheduler: str | None,
    skip: list[str],
) -> SubCall | None:
    """Decide the conditional reconcile sub-call from load-context's output.

    Mirrors ``hpc-aggregate`` SKILL.md Step 1b: reconcile a journal-only
    ``in_flight`` run against the cluster before the skill can trust the
    journal. Fires only when ALL hold:

    * ``reconcile`` not in *skip*,
    * the caller supplied ``--reconcile-scheduler`` (reconcile needs the
      scheduler family to query alive job IDs; without it we cannot run),
    * load-context ran and returned ``ok`` with
      ``envelope.data.next_step_hint == "monitor"`` (the journal still
      says a run is in flight),
    * that ``envelope.data.in_flight`` carries at least one ``run_id`` to
      target.

    Returns the SubCall, or ``None`` when reconcile is not applicable.
    The run_id is the first in-flight row's; one ``reconcile`` call also
    settles that run's paired ``-canary`` sibling (#258), so a single
    sub-call clears both journal entries.
    """
    if "reconcile" in skip or reconcile_scheduler is None:
        return None
    if load_context_result is None or not load_context_result.get("ok"):
        return None
    data = (load_context_result.get("envelope") or {}).get("data") or {}
    if data.get("next_step_hint") != "monitor":
        return None
    in_flight = data.get("in_flight") or []
    run_id = next(
        (row.get("run_id") for row in in_flight if isinstance(row, dict) and row.get("run_id")),
        None,
    )
    if run_id is None:
        return None
    return SubCall(
        name="reconcile",
        argv=[
            "hpc-agent",
            "reconcile",
            "--run-id",
            run_id,
            "--scheduler",
            reconcile_scheduler,
            "--experiment-dir",
            str(experiment_dir),
        ],
    )


def _run_subcalls(calls: list[SubCall], *, timeout_sec: float) -> dict[str, dict[str, Any]]:
    """Run *calls*: members of :data:`_PARALLEL_SUBCALLS` fan out concurrently.

    install-commands and load-context are write-disjoint AND read-disjoint
    (#291), so they fan out on a thread pool. With a single call (e.g. one
    arm skipped) the pool is unnecessary and we run inline. Any sub-call
    NOT in ``_PARALLEL_SUBCALLS`` runs sequentially after the fan — but
    in this composite the only such sub-call is ``reconcile``, which is
    built post-hoc by :func:`_maybe_build_reconcile` rather than mixed in
    here. Returns ``{name: SubResult}``; a sub-call failure surfaces
    inside its ``SubResult.envelope`` rather than raising.
    """
    results: dict[str, dict[str, Any]] = {}

    parallel = [c for c in calls if c.name in _PARALLEL_SUBCALLS]
    sequential = [c for c in calls if c.name not in _PARALLEL_SUBCALLS]

    if len(parallel) == 1:
        results[parallel[0].name] = _run_subprocess(parallel[0], timeout_sec=timeout_sec)
    elif parallel:
        with ThreadPoolExecutor(max_workers=len(parallel)) as pool:
            futures = {
                pool.submit(_run_subprocess, c, timeout_sec=timeout_sec): c.name for c in parallel
            }
            for fut, name in futures.items():
                results[name] = fut.result()

    for c in sequential:
        results[c.name] = _run_subprocess(c, timeout_sec=timeout_sec)

    return results


@primitive(
    name="aggregate-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Composite preflight at the top of aggregate: install-commands ∥ "
            "load-context fanned concurrently, then (when the journal says "
            "'monitor' and --reconcile-scheduler is supplied) reconcile."
        ),
        verb="aggregate-preflight",
        args=(
            CliArg(
                "--experiment-dir",
                type=str,
                required=True,
                help="Absolute path to the experiment directory.",
            ),
            CliArg(
                "--reconcile-scheduler",
                type=str,
                default=None,
                # No static ``choices`` — the value is forwarded verbatim to
                # ``reconcile --scheduler``, which validates against the live
                # backend registry (#337).
                help=(
                    "Scheduler family of the in-flight run. When supplied AND "
                    "load-context reports next_step_hint == 'monitor', reconcile "
                    "the journal-only in-flight run against the cluster (Step 1b) "
                    "before aggregation trusts the journal. Omit to skip reconcile."
                ),
            ),
        ),
        # reconcile is the SSH-touching sub-call when it fires; declare
        # requires_ssh so the capabilities catalog marks this verb as
        # cluster-touching (matches submit-preflight's check-preflight).
        requires_ssh=True,
    ),
    agent_facing=True,
)
def aggregate_preflight(
    *,
    experiment_dir: str | Path,
    reconcile_scheduler: str | None = None,
    skip: list[str] | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Run install-commands ∥ load-context, then (conditional) reconcile.

    Returns a dict matching ``schemas/aggregate_preflight.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir*
    accepts both ``str`` (the CLI path) and ``Path`` (the in-process
    path) and is coerced internally.

    install-commands and load-context fan out CONCURRENTLY on a thread
    pool (#291); the composite's wall-clock for the pair is bounded by
    the slower of the two rather than their sum. The reconcile sub-call
    is built from load-context's envelope *after* the fan completes (see
    :func:`_maybe_build_reconcile`) — it fires only when the journal
    reports ``next_step_hint == "monitor"`` and the caller supplied
    ``reconcile_scheduler``. When it doesn't fire, the ``reconcile`` slot
    stays ``null``.

    The composite never raises on a sub-call failure — failures surface
    inside ``SubResult.envelope`` so the cheaper sub-calls' work is
    preserved even when reconcile blows up. ``overall`` is ``fail`` iff
    any non-skipped sub-call returned ``ok: false`` — fanning never
    swallows a failure.
    """
    experiment_dir_path = (
        experiment_dir if isinstance(experiment_dir, Path) else Path(experiment_dir)
    )
    skip_list = list(skip or [])
    base_calls = _build_subcalls(experiment_dir=experiment_dir_path, skip=skip_list)

    started = time.monotonic()
    by_name = _run_subcalls(base_calls, timeout_sec=timeout_sec)

    reconcile_call = _maybe_build_reconcile(
        load_context_result=by_name.get("load-context"),
        experiment_dir=experiment_dir_path,
        reconcile_scheduler=reconcile_scheduler,
        skip=skip_list,
    )
    if reconcile_call is not None:
        by_name[reconcile_call.name] = _run_subprocess(reconcile_call, timeout_sec=timeout_sec)

    elapsed_total_sec = time.monotonic() - started
    overall = "fail" if any(not r["ok"] for r in by_name.values()) else "pass"

    return {
        "overall": overall,
        "elapsed_total_sec": elapsed_total_sec,
        "install_commands": by_name.get("install-commands"),
        "load_context": by_name.get("load-context"),
        "reconcile": by_name.get("reconcile"),
    }
