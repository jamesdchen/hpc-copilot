"""``submit-preflight``: composite primitive — top-of-submit boilerplate.

WS5 #1 (repurposed). Mirror of :mod:`status_preflight` with a cluster
SSH-connectivity check on top: collapses the ``install-commands`` +
``load-context`` + (optional) ``check-preflight`` + (optional)
``resolve-resources`` calls at the top of every ``hpc-submit`` invocation
into ONE CLI call so the agent's role shrinks to one tool call.

**Note on the prior incarnation.** This module previously fanned
``export-package`` + ``plan-throughput`` + ``validate-campaign`` out in
parallel — the framing the 2026-06-04 demo agent improvised. Inspection
showed the trio is at THREE separate Steps in the canonical
``worker_prompts/submit.md`` (Step 0 / 4b / 6c) with hard data
dependencies (plan-throughput needs ``total_tasks`` from grid expansion;
validate-campaign needs the assembled spec), so it can't actually be
parallelised without flow restructuring. The repurposed verb is the
genuinely-composable boilerplate the audit's ``<skill>-preflight`` row
described.

Internal composition (#277, #289): all four sub-calls fan out
CONCURRENTLY on a thread pool — they share no data dependency, so the
composite's wall-clock is bounded by the slowest arm (the cluster ssh
probe), not their sum. The earlier "``install-commands`` must succeed
first so ``load-context`` can resolve framework paths" prelude was based
on a claim #289's source-walk DISPROVED (mirrored from
:mod:`status_preflight`): ``load-context`` reads only
``$EXPERIMENT/.hpc/{runs,journal,campaigns}``, never ``~/.claude`` (the
only thing ``install-commands`` writes). The independence holds across all
four: ``install-commands`` writes ``~/.claude/{commands,skills,agents,settings.json}``;
``load-context`` reads ``.hpc/``; ``check-preflight`` probes
``SSH_AUTH_SOCK`` / the ssh|rsync|scp binaries / ``clusters.yaml`` / TCP
reachability (1-2s ssh round-trip on the slow path); ``resolve-resources``
reads runtime priors + ``clusters.yaml`` (local journal I/O). No arm reads
what another writes, so running them on a pool is race-free and the
resource resolution ``hpc-submit`` Step 6 needs comes back "for free" under
the cluster ssh probe. ``resolve-resources`` only joins the fan-out when a
``--cluster`` is supplied (it is that verb's one required argument).

**Invariant (#277, #289).** The fan-out is correct only while the arms stay
mutually independent: ``resolve-resources`` reads runtime priors +
clusters.yaml (never cluster-connectivity state), and ``load-context``
never reads ``~/.claude``. If a future change makes resource resolution
depend on a live cluster probe (e.g. cluster-specific spot pricing), or
makes ``load-context`` read what ``install-commands`` writes, sequence the
dependent arm after its producer then.

I/O contracts:

* Input: see ``hpc_agent/schemas/submit_preflight.input.json``.
* Output: a ``dict`` matching ``schemas/submit_preflight.output.json``.
"""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = [
    "SubCall",
    "submit_preflight",
]

# All four sub-calls are mutually independent (#277, #289): install-commands
# writes ~/.claude/*, load-context reads .hpc/* (verified to never read
# ~/.claude), check-preflight probes ssh/binaries/clusters.yaml/TCP, and
# resolve-resources reads runtime priors + clusters.yaml. No arm reads what
# another writes, so they all fan out concurrently and nothing is sequenced.
_SEQUENTIAL_SUBCALLS: tuple[str, ...] = ()
_PARALLEL_SUBCALLS = (
    "install-commands",
    "load-context",
    "check-preflight",
    "resolve-resources",
)


@dataclass(frozen=True)
class SubCall:
    """One sub-call within submit-preflight (name + full argv)."""

    name: str
    argv: list[str]


def _resolve_resources_argv(
    *,
    experiment_dir: str,
    cluster: str,
    profile: str | None = None,
    cmd_sha: str | None = None,
    walltime_sec: int | None = None,
    gpu_type: str | None = None,
    safety_mult: float | None = None,
    partition: str | None = None,
    user_preferred_partition: str | None = None,
) -> list[str]:
    """Compose the ``resolve-resources`` argv, forwarding only set overrides.

    Every optional field is omitted from argv when ``None`` so the
    sub-verb applies its own default (a caller override only when actually
    supplied). ``--cluster`` is mandatory — it is the verb's one required
    argument and the gate on whether resolve-resources joins the fan-out.
    """
    argv = [
        "hpc-agent",
        "resolve-resources",
        "--cluster",
        cluster,
        "--experiment-dir",
        experiment_dir,
    ]
    if profile is not None:
        argv += ["--profile", profile]
    if cmd_sha is not None:
        argv += ["--cmd-sha", cmd_sha]
    if walltime_sec is not None:
        argv += ["--walltime-sec", str(walltime_sec)]
    if gpu_type is not None:
        argv += ["--gpu-type", gpu_type]
    if safety_mult is not None:
        argv += ["--safety-mult", str(safety_mult)]
    if partition is not None:
        argv += ["--partition", partition]
    if user_preferred_partition is not None:
        argv += ["--user-preferred-partition", user_preferred_partition]
    return argv


def _build_subcalls(
    *,
    experiment_dir: Path,
    cluster: str | None,
    skip: list[str],
    resolve_kwargs: dict[str, Any] | None = None,
) -> list[SubCall]:
    """Construct one :class:`SubCall` per non-skipped sub-step.

    Builds in the stable list order install-commands → load-context →
    check-preflight → resolve-resources (the output-field order), but all four
    fan out concurrently at run time (:func:`_run_subcalls`) — they are
    mutually independent (#277, #289), so the build order is cosmetic, not an
    execution dependency. ``resolve-resources`` is only built when ``cluster``
    is supplied — it is the verb's one required argument — and
    ``resolve_kwargs`` forwards the optional Step-6 overrides (profile /
    walltime / gpu_type / partition …).
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

    if "check-preflight" not in skip:
        argv = ["hpc-agent", "preflight"]
        if cluster is not None:
            argv += ["--cluster", cluster]
        calls.append(SubCall(name="check-preflight", argv=argv))

    # resolve-resources requires a cluster (its one mandatory argument), so it
    # only joins the fan-out when one is known. It overlaps check-preflight's
    # ssh round-trip with local journal/clusters.yaml I/O (#277).
    if cluster is not None and "resolve-resources" not in skip:
        calls.append(
            SubCall(
                name="resolve-resources",
                argv=_resolve_resources_argv(
                    experiment_dir=exp_str,
                    cluster=cluster,
                    **(resolve_kwargs or {}),
                ),
            )
        )

    return calls


def _synth_error_subresult(
    *, error_code: str, message: str, category: str, elapsed_sec: float
) -> dict[str, Any]:
    """Build a SubResult whose envelope is a synthesised ErrorEnvelope.

    Used when the sub-call could not emit its own JSON (spawn failure,
    timeout, non-JSON stdout). Matches ErrorEnvelope in envelope.json.
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


def _run_subprocess(call: SubCall, *, timeout_sec: float) -> dict[str, Any]:
    """Run *call.argv* synchronously; return its SubResult dict.

    Captures stdout + stderr; parses stdout as a JSON envelope. Spawn
    failure, timeout, and non-JSON stdout all synthesise a uniform
    ErrorEnvelope so the outer composite can branch consistently.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            call.argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return _synth_error_subresult(
            error_code="cluster_timeout",
            message=f"{call.name} exceeded {timeout_sec}s timeout",
            category="cluster",
            elapsed_sec=time.monotonic() - started,
        )
    except OSError as exc:
        return _synth_error_subresult(
            error_code="internal",
            message=f"failed to spawn {call.name}: {exc}",
            category="internal",
            elapsed_sec=time.monotonic() - started,
        )

    elapsed_sec = time.monotonic() - started

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        stderr_tail = (proc.stderr or "")[-400:]
        return {
            "envelope": {
                "ok": False,
                "error_code": "internal",
                "message": (
                    f"{call.name} did not emit a JSON envelope on stdout; "
                    f"stderr tail: {stderr_tail}"
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


def _run_subcalls(calls: list[SubCall], *, timeout_sec: float) -> dict[str, dict[str, Any]]:
    """Run *calls* concurrently on a thread pool (#277, #289).

    All sub-calls are mutually independent, so they fan out on a thread pool
    and the composite's wall-clock is bounded by the slowest arm (the cluster
    ssh round-trip) rather than the sum. (``_SEQUENTIAL_SUBCALLS`` is empty;
    the split is retained so a future data dependency can be re-sequenced by
    moving a name back into it.) Returns ``{name: SubResult}``; a sub-call
    failure surfaces inside its ``SubResult.envelope`` rather than raising, so
    the other sub-calls' work is preserved.
    """
    results: dict[str, dict[str, Any]] = {}

    sequential = [c for c in calls if c.name in _SEQUENTIAL_SUBCALLS]
    parallel = [c for c in calls if c.name in _PARALLEL_SUBCALLS]

    for c in sequential:
        results[c.name] = _run_subprocess(c, timeout_sec=timeout_sec)

    if len(parallel) == 1:
        # A single independent call: no pool needed (cluster-less submit, or
        # a skip that left only one of the pair).
        results[parallel[0].name] = _run_subprocess(parallel[0], timeout_sec=timeout_sec)
    elif parallel:
        with ThreadPoolExecutor(max_workers=len(parallel)) as pool:
            futures = {
                pool.submit(_run_subprocess, c, timeout_sec=timeout_sec): c.name for c in parallel
            }
            for fut, name in futures.items():
                results[name] = fut.result()

    return results


@primitive(
    name="submit-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Composite preflight at the top of submit: install-commands + "
            "load-context, then (when --cluster is supplied) check-preflight "
            "and resolve-resources fanned out concurrently."
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
                default=None,
                help=(
                    "Optional cluster name. When supplied, check-preflight runs "
                    "the cluster_ssh_echo functional probe through the production "
                    "ssh path AND resolve-resources runs concurrently to resolve "
                    "walltime/gpu_type/partition; without it, only the local-env "
                    "checks fire."
                ),
            ),
            # resolve-resources passthrough overrides (#277). Each is forwarded
            # only when set; an omitted field lets resolve-resources apply its
            # own auto-resolution rule. All no-op when --cluster is absent.
            CliArg(
                "--profile",
                type=str,
                default=None,
                help="Run profile (run_name) forwarded to resolve-resources' prior lookup.",
            ),
            CliArg(
                "--cmd-sha",
                type=str,
                default=None,
                help="Optional cmd_sha to filter resolve-resources' runtime prior.",
            ),
            CliArg(
                "--walltime-sec",
                type=int,
                default=None,
                help="Caller override for walltime_sec; skips the runtime-prior probe.",
            ),
            CliArg(
                "--gpu-type",
                type=str,
                default=None,
                help="Caller override for gpu_type; skips the cluster gpu_types[0] default.",
            ),
            CliArg(
                "--safety-mult",
                type=float,
                default=None,
                help="Multiplier applied to the prior p95 to size walltime (default 1.30).",
            ),
            CliArg(
                "--partition",
                type=str,
                default=None,
                help="Caller override for partition; skips recommend-partition.",
            ),
            CliArg(
                "--user-preferred-partition",
                type=str,
                default=None,
                help="Soft partition preference forwarded to resolve-resources.",
            ),
        ),
        # check-preflight is the SSH-touching sub-call when --cluster is set;
        # declare requires_ssh so WS4's contract test is satisfied.
        requires_ssh=True,
    ),
    agent_facing=True,
)
def submit_preflight(
    *,
    experiment_dir: str | Path,
    cluster: str | None = None,
    profile: str | None = None,
    cmd_sha: str | None = None,
    walltime_sec: int | None = None,
    gpu_type: str | None = None,
    safety_mult: float | None = None,
    partition: str | None = None,
    user_preferred_partition: str | None = None,
    skip: list[str] | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Run install-commands → load-context, then check-preflight ∥ resolve-resources.

    Returns a dict matching ``schemas/submit_preflight.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir*
    accepts both ``str`` (the CLI path) and ``Path`` (the in-process
    path) and is coerced internally.

    When ``cluster`` is supplied, ``check-preflight`` (cluster ssh probe)
    and ``resolve-resources`` (local walltime/gpu/partition resolution) run
    CONCURRENTLY after the sequential install→load prelude, so the
    composite's wall-clock for that pair is the slower of the two, not their
    sum (#277). Without a cluster, ``resolve-resources`` is omitted (it
    requires one) and ``check-preflight`` runs its local-env checks alone.

    The composite never raises on a sub-call failure — failures surface
    inside ``SubResult.envelope`` so the cheaper sub-calls' work is
    preserved even when one arm blows up. ``overall`` is ``fail`` iff any
    non-skipped sub-call (either parallel arm included) returned ``ok:
    false`` — parallelising the two arms never swallows a failure.
    """
    experiment_dir_path = (
        experiment_dir if isinstance(experiment_dir, Path) else Path(experiment_dir)
    )
    skip_list = list(skip or [])
    resolve_kwargs: dict[str, Any] = {
        "profile": profile,
        "cmd_sha": cmd_sha,
        "walltime_sec": walltime_sec,
        "gpu_type": gpu_type,
        "safety_mult": safety_mult,
        "partition": partition,
        "user_preferred_partition": user_preferred_partition,
    }
    calls = _build_subcalls(
        experiment_dir=experiment_dir_path,
        cluster=cluster,
        skip=skip_list,
        resolve_kwargs=resolve_kwargs,
    )

    started = time.monotonic()
    by_name = _run_subcalls(calls, timeout_sec=timeout_sec)
    elapsed_total_sec = time.monotonic() - started

    overall = "fail" if any(not r["ok"] for r in by_name.values()) else "pass"

    return {
        "overall": overall,
        "elapsed_total_sec": elapsed_total_sec,
        "install_commands": by_name.get("install-commands"),
        "load_context": by_name.get("load-context"),
        "check_preflight": by_name.get("check-preflight"),
        "resolve_resources": by_name.get("resolve-resources"),
    }
