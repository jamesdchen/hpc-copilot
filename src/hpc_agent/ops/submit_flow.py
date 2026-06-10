"""``submit-flow``: workflow atom that does pre-flight + rsync + deploy + qsub + record.

A workflow atom (vs a primitive atom) chains multiple SSH/scheduler/disk
operations into one composable unit with a single envelope output. Where
:func:`hpc_agent.ops.submit.runner.submit_and_record` is the bookkeeping
primitive (writes a sidecar; never touches the cluster), ``submit_flow``
is the end-to-end pipeline: it actually rsyncs, deploys framework files,
optionally fires a 1-task canary, qsubs the array, and records to the
journal — emitting one JSON envelope at the end.

Why this exists: ``/campaign-hpc`` and other higher-level workflows
need to invoke the submit pipeline as a single CLI/Python call. The
slash-command surface (``/submit-hpc``) bundles interactive prompts
around this pipeline; the agent or another workflow can bypass the
prompts entirely by going straight to ``submit_flow``.

Idempotency
-----------
Idempotent on ``run_id`` — a replay returns ``deduped=True`` and
performs no SSH or scheduler side effects. The dedup check delegates
to :func:`runner.submit_and_record`, which has been the canonical
journal arbiter since the framework began.
"""

from __future__ import annotations

import contextlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.cli._dispatch import CliArg, CliShape, SchemaRef
from hpc_agent.infra.backends.remote_factory import build_remote_backend
from hpc_agent.infra.remote import ssh_run
from hpc_agent.infra.ssh_validation import validate_ssh_target
from hpc_agent.infra.transport import deploy_runtime, rsync_push
from hpc_agent.ops.submit.runner import submit_and_record
from hpc_agent.state.journal import is_resubmittable_terminal, load_run


def _submit_flow_handler(ns):  # type: ignore[no-untyped-def]
    """Tier 2 handler — delegates to the hand-written cmd_submit_flow shim.

    submit-flow's CLI adapter auto-routes to ``submit-flow-batch`` when
    the spec carries a ``specs`` list, injects ``--partial-ok`` into the
    spec, and emits a dry-run envelope whose shape diverges from the
    success path. None of that fits the auto-dispatcher's hook surface.
    """
    from hpc_agent.cli.submit import cmd_submit_flow

    return cmd_submit_flow(ns)


def _submit_flow_batch_handler(ns):  # type: ignore[no-untyped-def]
    """Tier 2 handler — delegates to the hand-written cmd_submit_flow_batch shim.

    submit-flow-batch runs TWO schema passes (the outer wrapper against
    ``submit_flow_batch.input.json`` + a per-entry pass against
    ``submit_flow.input.json``) and the dry-run envelope diverges from
    the success path.
    """
    from hpc_agent.cli.submit import cmd_submit_flow_batch

    return cmd_submit_flow_batch(ns)


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from hpc_agent._wire.workflows.submit_flow_batch import SubmitFlowBatchSpec
    from hpc_agent.infra.backends import HPCBackend

__all__ = ["SubmitFlowResult", "submit_flow", "submit_flow_batch"]


@dataclass(frozen=True)
class SubmitFlowResult:
    """Return shape of :func:`submit_flow`."""

    run_id: str
    job_ids: list[str]
    total_tasks: int
    deduped: bool
    canary_done: bool
    canary_run_id: str | None = None
    canary_job_ids: list[str] | None = None
    main_launched: bool = True

    def to_envelope_data(self) -> dict[str, Any]:
        """Render to the shape pinned by ``schemas/submit_flow.output.json``."""
        return {
            "run_id": self.run_id,
            "job_ids": list(self.job_ids),
            "total_tasks": self.total_tasks,
            "deduped": self.deduped,
            "canary_done": self.canary_done,
            "canary_run_id": self.canary_run_id,
            "canary_job_ids": list(self.canary_job_ids) if self.canary_job_ids else None,
            "main_launched": self.main_launched,
        }


def _validate_ssh_target(ssh_target: str) -> str:
    """Adapt :func:`validate_ssh_target` to ``SpecInvalid`` for this
    flow's wire surface. The shared helper raises ``ValueError``; the
    submit flow surfaces ``SpecInvalid`` so the caller sees a typed
    envelope error. Workflow-private — ``ops/recover_flow.py`` does the
    same inline at its single call site rather than reaching into
    submit's source tree.
    """
    try:
        return validate_ssh_target(ssh_target)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc


def _preflight_probe(ssh_target: str, *, skip: bool) -> None:
    """Single ssh probe to verify cluster reachability. Caller may skip."""
    if skip:
        return
    probe = ssh_run("true", ssh_target=ssh_target)
    if probe.returncode != 0:
        raise errors.SshUnreachable(
            f"pre-flight ssh probe to {ssh_target} failed (exit {probe.returncode}): "
            f"{(probe.stderr or '').strip()[:200]}"
        )


# #275: the cluster-side ``command -v uv`` probe is ONE implementation, in
# ``infra.runtime_preflight`` so ``ops/preflight/check`` can run the SAME check
# (Fix 1) without a cross-subject import into ``ops/submit_flow`` (the
# subject-import boundary). Re-export it under the private name that
# ``_run_uv_preflight_for_batch`` and the cache test already reference + patch,
# so those seams are unchanged.
from hpc_agent.infra.runtime_preflight import (  # noqa: E402
    runtime_uv_preflight as _preflight_runtime_check,
)


def _canary_skip_threshold(spec: SubmitFlowSpec) -> int:
    """Effective tiny-batch canary-skip threshold (#263): env over spec field."""
    raw = os.environ.get("HPC_CANARY_SKIP_THRESHOLD")
    if raw:
        try:
            val = int(raw)
        except ValueError:
            val = -1
        if val >= 0:
            return val
    return int(getattr(spec, "canary_skip_threshold", 4))


def _should_run_canary(spec: SubmitFlowSpec) -> bool:
    """Decide whether to fire a canary for *spec* (#263 + #249).

    Order:

    * ``canary=false`` → no canary (the caller's explicit opt-out).
    * ``canary_only=true`` → ALWAYS canary — the two-phase gate is an explicit
      request to validate before main; neither optimization applies.
    * ``force_canary=true`` → ALWAYS canary (override both skips).
    * ``total_tasks <= threshold`` (#263) → skip: for a tiny batch the main
      array's own first tasks catch a broken executor as fast as a canary would.
    * same ``cmd_sha`` validated within TTL (#249) → skip: a canary for this
      exact ``cmd_sha`` already proved the runtime boots; re-running it gets
      nothing new.

    Otherwise → canary.
    """
    if not spec.canary:
        return False
    if spec.canary_only or getattr(spec, "force_canary", False):
        return True
    # #263: tiny-batch auto-skip.
    if spec.total_tasks <= _canary_skip_threshold(spec):
        return False
    # #249: skip when this cmd_sha was canary-validated within the TTL.
    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.state import canary_cache

    cmd_sha = (spec.job_env or {}).get("HPC_CMD_SHA") or ""
    if cmd_sha and not canary_cache.cache_disabled():
        key = canary_cache.canary_cache_key(cmd_sha=cmd_sha, version=_pkg_version or "")
        if canary_cache.is_canary_validated_fresh(key):
            return False
    return True


def _run_uv_preflight_for_batch(
    *,
    ssh_target: str,
    job_envs: list[dict[str, str]],
    skip_preflight: bool,
) -> None:
    """Cluster-side ``uv`` preflight for the first uv-runtime spec, TTL-cached (#255).

    A batch's specs share ``(ssh_target, remote_path)`` ⇒ same cluster, so one
    probe on the first ``runtime=uv`` spec's activation fields covers the
    batch. A *successful* probe is cached per
    ``(host, env-activation, framework-version)`` for a TTL (default 15min): a
    re-submit of the same target within the window skips the SSH round-trip.

    The env-activation (``MODULES`` + ``CONDA_SOURCE`` + ``CONDA_ENV``) and the
    framework version are folded into the cache key, so a conda-env edit or a
    ``pip install -U`` misses and re-probes. ``skip_preflight`` and
    ``HPC_NO_PREFLIGHT_CACHE=1`` both bypass the cache. Only successes are
    recorded — a failure surfaces as :class:`errors.SpecInvalid` from
    :func:`_preflight_runtime_check` and is never cached.
    """
    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.state import preflight_cache

    for job_env in job_envs:
        if (job_env or {}).get("HPC_RUNTIME") != "uv":
            continue
        activation = "|".join(
            (
                (job_env.get("MODULES") or "").strip(),
                (job_env.get("CONDA_SOURCE") or "").strip(),
                (job_env.get("CONDA_ENV") or "").strip(),
            )
        )
        cache_key = preflight_cache.preflight_cache_key(
            host=ssh_target, activation=activation, version=_pkg_version or ""
        )
        if not skip_preflight and preflight_cache.is_preflight_fresh(cache_key):
            return  # validated within TTL — skip the cluster round-trip (#255)
        _preflight_runtime_check(ssh_target, job_env=dict(job_env), skip=skip_preflight)
        if not skip_preflight:
            preflight_cache.record_preflight(cache_key, checks=["uv_present"])
        return


_SKIP_PREFLIGHT_ENV = "HPC_AGENT_SKIP_PREFLIGHT"


def _skip_preflight_requested(internal: bool | None) -> bool:
    """Resolve whether to skip the pre-flight probes — operator-only (#275).

    Two honoured sources, neither reachable by an agent-authored spec:

    * *internal* — a Python-only kwarg threaded by a trusted internal caller
      (``submit_and_verify``'s Phase-2 main-array launch, where the canary
      already paid the preflight). It is on no wire schema. ``None`` means "no
      internal opinion; consult the environment."
    * ``HPC_AGENT_SKIP_PREFLIGHT=1`` — an operator who just ran
      ``check-preflight`` and wants to save the duplicate probe.

    Mirrors the ``--inline`` / ``HPC_AGENT_INVOKER`` precedent (#155): an
    agent-supplied bypass is refused; an operator env var is honoured.
    """
    if internal is not None:
        return internal
    return os.environ.get(_SKIP_PREFLIGHT_ENV) == "1"


_SKIP_RSYNC_DEPLOY_ENV = "HPC_AGENT_SKIP_RSYNC_DEPLOY"


def _skip_rsync_deploy_requested(internal: bool | None) -> bool:
    """Resolve whether to skip the rsync+deploy prelude — operator-only (#283).

    Two honoured sources, neither reachable by an agent-authored spec:

    * *internal* — a Python-only kwarg threaded by a trusted internal caller
      (``submit_and_verify``'s Phase-2 main-array launch, where Phase 1 just
      rsync+deployed the SAME tree moments earlier — a structural fact, not an
      assertion). It is on no wire schema. ``None`` means "no internal opinion;
      consult the environment."
    * ``HPC_AGENT_SKIP_RSYNC_DEPLOY=1`` — an operator who knows the cluster
      already holds the current tree and wants to save the redundant transfer.

    Mirrors the ``skip_preflight`` (#275) and ``--inline`` / ``HPC_AGENT_INVOKER``
    (#155) precedents: an agent-supplied bypass is refused (the field is off the
    wire spec, so ``extra="forbid"`` rejects it); an operator env var or a
    trusted in-process kwarg is honoured. A hand-authored ``skip_rsync_deploy``
    on a raw submit-flow spec used to silently run the cluster on stale code if
    the local tree drifted since the asserted deploy (#185).
    """
    if internal is not None:
        return internal
    return os.environ.get(_SKIP_RSYNC_DEPLOY_ENV) == "1"


def _run_shared_prelude(
    *,
    experiment_dir: Path,
    ssh_target: str,
    remote_path: str,
    rsync_excludes: list[str] | None,
    scheduler: str | None,
    job_envs: list[dict[str, str]],
    skip_preflight: bool,
    skip_prelude_io: bool,
) -> None:
    """Connectivity gate, then rsync+deploy CONCURRENT with the uv probe (#280).

    Audit of everything ``submit_flow_batch`` does between spec-build and qsub,
    classified depends-on-rsync / independent / gate:

    * ``_ensure_run_sidecar`` / ``_mirror_canary_sidecar`` — local fs writes
      the sidecar SHIP in via rsync, so they must precede it: done by the
      caller, before this prelude.
    * ``_validate_ssh_target`` + ``_preflight_probe`` — the cheap connectivity
      gate that also establishes the ssh ControlMaster both cluster arms
      reuse. Kept FIRST and sequential so a dead host fails fast before any
      rsync, and so the two arms below never race two cold connection setups.
    * ``_run_uv_preflight_for_batch`` — ssh ``command -v uv`` against the
      *activated cluster env*; it does NOT read the deployed tree, so it is
      independent of rsync → overlap it.
    * ``_push_and_deploy`` — rsync_push + deploy_runtime; the network-bound
      long pole.

    The independent uv probe runs concurrently with rsync+deploy, so this
    block's wall-clock is ``max(rsync, uv_probe)`` not their sum. On a uv-probe
    failure (``SpecInvalid``) the rsync arm is still allowed to finish — a
    completed deploy with no qsub is harmless and idempotent — but the
    exception propagates so the caller never qsubs a uv-less run; the uv
    failure is preferred over a concurrent deploy failure as the more
    actionable error. ``skip_prelude_io`` (the operator/internal
    ``skip_rsync_deploy`` request — #185/#283, never a per-spec agent field)
    drops the deploy arm; the uv probe still runs.
    """
    _validate_ssh_target(ssh_target)
    _preflight_probe(ssh_target, skip=skip_preflight)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_uv = pool.submit(
            _run_uv_preflight_for_batch,
            ssh_target=ssh_target,
            job_envs=job_envs,
            skip_preflight=skip_preflight,
        )
        fut_deploy = (
            None
            if skip_prelude_io
            else pool.submit(
                _push_and_deploy,
                experiment_dir=experiment_dir,
                ssh_target=ssh_target,
                remote_path=remote_path,
                rsync_excludes=rsync_excludes,
                scheduler=scheduler,
            )
        )
        # The `with` exit joins both threads regardless of which raised, so a
        # uv-failure still lets the deploy arm complete (tolerated). Collect
        # both outcomes, then prefer the uv SpecInvalid as the more actionable.
        uv_exc: Exception | None = None
        deploy_exc: Exception | None = None
        try:
            fut_uv.result()
        except Exception as exc:  # noqa: BLE001 — re-raised below after deploy joins
            uv_exc = exc
        if fut_deploy is not None:
            try:
                fut_deploy.result()
            except Exception as exc:  # noqa: BLE001 — re-raised below
                deploy_exc = exc
        if uv_exc is not None:
            raise uv_exc
        if deploy_exc is not None:
            raise deploy_exc


# Paths a scaffolded ``.gitignore`` marks as generated but the cluster
# node *needs*: the executor package built at Step 0 (``src/``) and the
# dispatch contract (``.hpc/tasks.py`` / ``.hpc/cli.py``). A caller derives
# rsync excludes from ``.gitignore``, so these would otherwise be stripped
# from the deploy bundle. The carve-out lives here — not in caller prose —
# so every submit path ships them. ``.hpc/.build-cache.json`` is NOT listed:
# it stays excluded (a local-build artifact the node never reads).
_GENERATED_SHIPPABLE: frozenset[str] = frozenset({"src", ".hpc/tasks.py", ".hpc/cli.py"})


def _keep_generated_shippable(excludes: list[str] | None) -> list[str] | None:
    """Drop excludes that would block shipping generated-but-needed files.

    Normalises each pattern (strips surrounding ``/``) and removes any that
    match a :data:`_GENERATED_SHIPPABLE` path, so a ``.gitignore``-derived
    exclude list still deploys ``src/`` and the ``.hpc/`` dispatch files.
    """
    if not excludes:
        return excludes
    return [e for e in excludes if e.strip().strip("/") not in _GENERATED_SHIPPABLE]


def _push_and_deploy(
    *,
    experiment_dir: Path,
    ssh_target: str,
    remote_path: str,
    rsync_excludes: list[str] | None,
    scheduler: str | None = None,
) -> None:
    """rsync_push + deploy_runtime — the expensive ssh fan-out, done once.

    Extracted so :func:`submit_flow_batch` can run it once across N
    specs that share ``(ssh_target, remote_path)``. The previous
    architecture re-ran both for every spec, which is what tripped
    cluster sshd MaxStartups under campaign-time fan-out (see commit
    0c99e1f / the SSH-backoff commit).
    """
    push_result = rsync_push(
        ssh_target=ssh_target,
        remote_path=remote_path,
        local_path=experiment_dir,
        exclude=_keep_generated_shippable(rsync_excludes),
    )
    if push_result.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"rsync push failed (exit {push_result.returncode}): "
            f"{(push_result.stderr or '').strip()[:300]}"
        )
    deploy_runtime(ssh_target=ssh_target, remote_path=remote_path, scheduler=scheduler)


def _is_runnable_executor(executor: str | None) -> bool:
    """True when *executor* is a real per-task command, not the dispatcher/empty.

    A sidecar's ``executor`` must be the REAL per-task command (e.g.
    ``python train.py --seed $SEED``). ``job_env["EXECUTOR"]``, by contrast, is
    the *job-script* command — it runs the dispatcher
    (``python3 .hpc/_hpc_dispatch.py``), which then reads the sidecar to find the
    per-task command. So a sidecar whose ``executor`` is empty or itself the
    dispatcher is "pending with no executor": shipping it makes the dispatcher
    run itself and the array self-recurses (#162).
    """
    if not executor:
        return False
    return ("_hpc_dispatch.py" not in executor) and ("dispatch.py" not in executor)


def _write_first_error(run_id: str, *, detail: str) -> errors.SpecInvalid:
    """The actionable 'write the sidecar first' error (#171 / #150 / #162 / #200).

    A single phrasing so the absent-and-unsynthesizable, present-but-pending,
    and dispatcher-only-executor paths all surface the same actionable unblock
    instead of three near-identical ad-hoc messages.

    The message names CLI verbs (not Python functions) — agents previously
    introspected ``hpc_agent.state.runs.write_run_sidecar``'s signature to
    satisfy the guard because the only documented path was a Python call
    (#200). Three concrete unblock paths the agent can act on directly:

      (a) ``hpc-agent write-run-sidecar --spec <file>``  — direct write
      (b) Populate ``result_dir_template`` + a real per-task ``EXECUTOR``
          in the SubmitFlowSpec ``job_env`` — submit-flow synthesizes
      (c) Re-run ``/wrap-entry-point``  — full rescaffold

    Path (b) only fires when the sidecar is *missing*; a *pending* sidecar
    (present but with empty / dispatcher-only executor) needs (a) or (c).
    """
    return errors.SpecInvalid(
        f"per-run sidecar for run_id {run_id!r} {detail} "
        "Three ways to unblock: "
        "(a) `hpc-agent write-run-sidecar --spec <file>` to write it directly "
        "with the real per-task command (e.g. `python train.py --seed $SEED`); "
        "(b) re-submit with a SubmitFlowSpec carrying `result_dir_template` "
        "AND a real per-task `EXECUTOR` in `job_env` — submit-flow synthesizes "
        "the sidecar (only works when the sidecar is missing, not pending); "
        "(c) re-run `/wrap-entry-point` for a full rescaffold."
    )


def _run_constant_spec_kwargs(experiment_dir: Path) -> dict[str, Any]:
    """Read the run's declared run-constant task kwargs from ``interview.json``.

    The resolver's context discriminator (``ops.recover.resolve``) routes a
    ``gpu_oom`` on a parallelism/width knob (``tp_size`` / ``batch_size`` / …)
    to the *right* fix, but it only sees those knobs through the sidecar's
    ``extra.spec_kwargs`` pocket (``ops.recover.features_glue``). The only knobs
    we can SOUNDLY stamp there are the ones that are constant across the run:
    ``entry_point.fixed_params`` (#195 — the non-axis params the interview bakes
    into every task's ``resolve()`` kwargs). A run-constant knob is unambiguous
    at the failure-cluster level; a *swept* axis value is NOT run-constant and
    must never be stamped, so we deliberately read ONLY ``fixed_params`` and
    never the ``task_generator`` axes.

    ``interview.json`` is written at the campaign workdir root, which is the
    same directory the sidecar/``.hpc`` tree hangs off (see
    ``ops.memory.interview`` + ``_kernel.contract.layout.RepoLayout``), so it
    lives at ``experiment_dir / "interview.json"``. Defensive by design:
    absent / unreadable / no ``entry_point`` / no ``fixed_params`` (a
    hand-written ``tasks.py`` run, which legitimately has none — an accepted,
    documented limitation) all yield ``{}`` — a clean no-op, never an error.
    """
    import json

    interview_path = experiment_dir / "interview.json"
    try:
        doc = json.loads(interview_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    entry_point = doc.get("entry_point")
    if not isinstance(entry_point, dict):
        return {}
    fixed_params = entry_point.get("fixed_params")
    if not isinstance(fixed_params, dict) or not fixed_params:
        return {}
    # Return a fresh dict (string keys, values verbatim) — never a live ref into
    # the parsed doc, and never the swept axes under ``task_generator``.
    return {str(k): v for k, v in fixed_params.items()}


def _ensure_run_sidecar(experiment_dir: Path, spec: SubmitFlowSpec) -> None:
    """Guarantee the cluster-required per-run sidecar exists before rsync.

    The cluster dispatcher hard-requires ``.hpc/runs/<run_id>.json`` (it
    reads ``executor`` + ``result_dir_template`` from it) — if it is
    missing at rsync time, ``.hpc/runs/`` ships empty and every task fails
    with ``run sidecar not found``. submit-flow therefore OWNS this
    artifact instead of trusting a prior step (Step 6d / write_run_sidecar)
    to have written it (#148 / #150).

    Behaviour:

    * Sidecar already present AND it carries a real per-task ``executor`` (the
      normal flow — Step 6d wrote it with the full wave_map / config snapshot):
      no-op, we never overwrite it.
    * Sidecar present but "pending" — empty / dispatcher-only / unreadable
      ``executor`` (Step 6d skipped or half-written): raise ``SpecInvalid``.
      Presence alone does NOT satisfy the guard (#171); shipping such a sidecar
      gives the dispatcher nothing to run, or makes it run itself (#162).
    * Sidecar missing + ``result_dir_template`` AND a real per-task executor
      available: synthesize a minimal-but-valid sidecar from the spec.
    * Sidecar missing + no ``result_dir_template`` **or** no real per-task
      executor (only the job script's dispatcher command is available): raise
      ``SpecInvalid`` — fail fast locally rather than ship an empty ``runs/``
      OR a self-recursive sidecar that dooms the whole array (#148 / #162).

    Every refuse-path raises the SAME actionable error (:func:`_write_first_error`):
    write the per-run sidecar first (Step 6d / write_run_sidecar) with the real
    per-task command. Write-first is thus a hard precondition the primitive
    owns, not a manual unblock step (#171 / #150).
    """
    import json

    from hpc_agent.state.runs import resolve_node_sha, run_sidecar_path, write_run_sidecar

    target = run_sidecar_path(experiment_dir, spec.run_id)
    if target.is_file() and target.stat().st_size > 0:
        # A sidecar file exists — but presence alone is NOT enough (#171).
        # Enforce write-first: it must carry a REAL per-task executor. A
        # "pending" sidecar with an empty / dispatcher-only executor (Step 6d
        # skipped or half-written) would ship and leave the dispatcher with
        # nothing to run, or make it run itself (#162). Read only the executor
        # field via raw JSON: forward-compatible (a future sidecar version with
        # a real executor is still accepted — the cluster does its own version
        # check) and an unreadable/corrupt file falls through to the refuse path.
        try:
            existing_executor = json.loads(target.read_text(encoding="utf-8")).get("executor")
        except (OSError, ValueError, AttributeError):
            existing_executor = None
        if _is_runnable_executor(existing_executor):
            return
        raise _write_first_error(
            spec.run_id,
            detail=(
                f"exists but carries no real per-task executor (found "
                f"{existing_executor!r} — empty, the dispatcher command, or "
                "unreadable), so the cluster dispatcher would have nothing to run "
                "or would run itself (#162)."
            ),
        )

    if not spec.result_dir_template:
        raise errors.SpecInvalid(
            f"per-run sidecar for run_id {spec.run_id!r} is missing and the "
            "spec carries no result_dir_template, so submit-flow cannot "
            "synthesize the artifact the cluster dispatcher requires. Either "
            "run write_run_sidecar first (Step 6d / wrap-entry-point) or pass "
            "result_dir_template in the spec."
        )

    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.infra.time import utcnow_iso

    job_env = spec.job_env or {}
    # job_env["EXECUTOR"] is the *job-script* command — it runs the dispatcher
    # (`python3 .hpc/_hpc_dispatch.py`), NOT a per-task command. Writing it into
    # the sidecar's `executor` makes the dispatcher run itself: the #162 live
    # incident (~8,647 retries, 8 nodes burned). There is no real per-task
    # command to synthesize from here, so fail loud — the same posture as a
    # missing result_dir_template above — rather than ship a structurally broken
    # sidecar that the old `or "...dispatch.py"` default silently produced.
    executor = job_env.get("EXECUTOR") or ""
    if not _is_runnable_executor(executor):
        raise _write_first_error(
            spec.run_id,
            detail=(
                f"is missing and submit-flow cannot synthesize a valid one: the only "
                f"available executor ({executor!r}) is the job-script command (it runs "
                "the dispatcher), not a per-task command, so synthesizing it would make "
                "the dispatcher run itself (#162)."
            ),
        )
    cmd_sha = job_env.get("HPC_CMD_SHA", "")

    # tasks_py_sha is provenance only (drift detection); compute it from the
    # local tasks.py when present, else leave empty — the dispatcher does
    # not require it.
    #
    # #207: this is the explicit boundary between the two identities the
    # sidecar carries. ``cmd_sha`` (above) is PARAMETER identity — the
    # dedup key, hashed solely from the materialized swept params (see
    # compute_cmd_sha). ``tasks_py_sha`` (below) is CODE identity —
    # provenance, NOT folded into the dedup key by default. An
    # executor-body edit with unchanged params keeps the same cmd_sha and
    # dedups against the prior run BY DESIGN (params define the
    # experiment). The opt-in --invalidate-on-code-change lever
    # (spec.invalidate_on_code_change → submit_and_record →
    # find_run_by_cmd_sha) is what folds this tasks_py_sha into the dedup
    # decision when a caller wants a code-only change to force a fresh run.
    tasks_py_sha = ""
    tasks_py = experiment_dir / ".hpc" / "tasks.py"
    if tasks_py.is_file():
        from hpc_agent.state.run_sha import compute_tasks_py_sha

        try:
            tasks_py_sha = compute_tasks_py_sha(tasks_py)
        except OSError as exc:
            # tasks_py_sha is the drift guard that flags in-place edits to
            # tasks.py; an empty sha silently disables it for this run. A read
            # error is the only expected failure — surface it (#165) rather than
            # swallow; anything else is a bug in compute_tasks_py_sha and should
            # propagate, not be masked by a broad suppress.
            import warnings

            warnings.warn(
                f"could not compute tasks.py drift sha for run {spec.run_id!r} "
                f"({exc}); its sidecar ships without it, disabling drift "
                "detection for this run.",
                stacklevel=2,
            )

    resources = spec.resources.model_dump(exclude_none=True) if spec.resources else None

    # Stamp the run-constant task kwargs into ``extra.spec_kwargs`` so a later
    # gpu_oom can be discriminated by parallelism/width (see
    # ``_run_constant_spec_kwargs`` / ``ops.recover.features_glue``). Only the
    # declared ``fixed_params`` are sound to stamp; swept axes are never read.
    spec_kwargs = _run_constant_spec_kwargs(experiment_dir)
    extra = {"spec_kwargs": spec_kwargs} if spec_kwargs else None

    write_run_sidecar(
        experiment_dir,
        run_id=spec.run_id,
        extra=extra,
        cmd_sha=cmd_sha,
        hpc_agent_version=_pkg_version or "",
        submitted_at=utcnow_iso(),
        executor=executor,
        result_dir_template=spec.result_dir_template,
        task_count=int(spec.total_tasks),
        tasks_py_sha=tasks_py_sha,
        cluster=spec.cluster,
        profile=spec.profile,
        remote_path=spec.remote_path,
        campaign_id=spec.campaign_id or None,
        runtime=spec.runtime,
        resources=resources or None,
        parent_run_ids=spec.parents or None,
        # Derived from the parents' on-disk sidecars (recursive identity,
        # docs/design/dag-kernel.md). SpecInvalid on a missing parent or a
        # non-64-hex cmd_sha — a parented run needs full parameter identity.
        node_sha=resolve_node_sha(
            experiment_dir, cmd_sha=cmd_sha, parent_run_ids=spec.parents or None
        ),
    )


def _mirror_canary_sidecar(experiment_dir: Path, main_run_id: str, canary_run_id: str) -> None:
    """Ensure the canary's per-run sidecar exists by mirroring the main run's.

    The dispatcher hard-requires ``.hpc/runs/<run_id>.json``; the canary uses
    run_id ``<main>-canary``, which Step 6d never writes and
    :func:`_ensure_run_sidecar` only covers for the main spec — so the canary
    errored ``sidecar not found`` and the gate was a no-op (#160 / #162). Copy
    the main sidecar's per-task executor + result_dir_template to the canary
    path (``task_count=1``) so the canary dispatches the SAME command. No-op
    when the canary sidecar already exists or the main one is unreadable.
    """
    from hpc_agent.infra.time import utcnow_iso
    from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path, write_run_sidecar

    target = run_sidecar_path(experiment_dir, canary_run_id)
    if target.is_file() and target.stat().st_size > 0:
        return
    try:
        main = read_run_sidecar(experiment_dir, main_run_id)
    except Exception:  # noqa: BLE001 — best-effort mirror; a missing main is handled below
        return
    executor = main.get("executor")
    result_dir_template = main.get("result_dir_template")
    if not executor or not result_dir_template:
        return  # main sidecar lacks the dispatch essentials; nothing to mirror
    # Mirror the run-constant spec_kwargs pocket so a canary gpu_oom is
    # discriminated by the same parallelism/width knobs as the main run.
    main_extra = main.get("extra")
    canary_extra = main_extra if isinstance(main_extra, dict) and main_extra else None
    write_run_sidecar(
        experiment_dir,
        run_id=canary_run_id,
        extra=canary_extra,
        cmd_sha=str(main.get("cmd_sha", "")),
        hpc_agent_version=str(main.get("hpc_agent_version", "")),
        submitted_at=utcnow_iso(),
        executor=str(executor),
        result_dir_template=str(result_dir_template),
        task_count=1,
        tasks_py_sha=str(main.get("tasks_py_sha", "")),
        wave_map={"0": [0]},
        cluster=main.get("cluster"),
        profile=main.get("profile"),
        remote_path=main.get("remote_path"),
        campaign_id=main.get("campaign_id") or None,
        runtime=main.get("runtime"),
        resources=main.get("resources") or None,
        # Carry the env snapshot so the canary's control-plane status
        # reporter activates the SAME conda env as the main run — that is
        # the activation verify-canary derives from this sidecar (#176).
        env=main.get("env") or None,
        env_group=main.get("env_group") or None,
        # Mirror lineage verbatim: the canary IS the main run's first task,
        # so it shares the main run's ancestry and composed identity.
        parent_run_ids=main.get("parent_run_ids") or None,
        node_sha=main.get("node_sha") or None,
    )


def _ensure_job_script_executor(run_id: str, job_env: dict[str, str]) -> None:
    """Refuse a submission whose job-script ``EXECUTOR`` is empty/missing (#191).

    ``job_env["EXECUTOR"]`` is the *job-script* command — it runs the dispatcher
    (``python3 .hpc/_hpc_dispatch.py``), which then reads the sidecar for the
    per-task command. If it is absent or ``""``, the cluster job runs
    ``time $EXECUTOR`` with no command: it prints ``0.000`` and exits 0 in
    milliseconds, the canary "succeeds", and the main array fires the same
    broken qsub — every task exits cleanly having done nothing (the #162 class).

    This validates *non-emptiness only*, deliberately NOT runnability: unlike
    the sidecar's per-task ``executor`` (guarded by :func:`_is_runnable_executor`,
    which rejects the dispatcher command), the job-script ``EXECUTOR`` is
    *supposed* to be that dispatcher command. The cluster-side templates also
    fence ``$EXECUTOR`` with ``: "${EXECUTOR:?...}"`` as defense-in-depth; this
    intake guard fails faster and clearer than a vanished canary's stderr.
    """
    executor = (job_env or {}).get("EXECUTOR") or ""
    if not executor.strip():
        raise errors.SpecInvalid(
            f"job_env['EXECUTOR'] is missing or empty for run_id {run_id!r}. The "
            "job-script EXECUTOR is the dispatcher command (typically "
            "'python3 .hpc/_hpc_dispatch.py'); an empty value makes the cluster job "
            "run `time` with no command and exit 0 instantly — the canary would "
            "'succeed' and the main array would fire the same broken qsub. Pass "
            "EXECUTOR through build-submit-spec (which defaults it) or set "
            "job_env['EXECUTOR'] explicitly in the fields-file."
        )


def _augment_job_env(
    *,
    job_env: dict[str, str],
    runtime: str | None,
    campaign_id: str | None,
    cluster: str,
) -> dict[str, str]:
    """Layer the framework-driven env vars on top of the caller's job_env.

    Three augmentations: ``HPC_RUNTIME=uv`` when the spec asks for it,
    ``HPC_CAMPAIGN_ID`` when the run is part of a closed-loop campaign,
    and ``HPC_NFS_DATA_DIR`` from the cluster's ``nfs_data_dir`` setting
    (NFS-staging survival). Caller-supplied keys win via setdefault.
    """
    out = dict(job_env)
    if runtime == "uv":
        out.setdefault("HPC_RUNTIME", "uv")
    if campaign_id:
        out.setdefault("HPC_CAMPAIGN_ID", campaign_id)
    from hpc_agent.infra.clusters import get_nfs_data_dir, load_clusters_config

    cluster_cfg = load_clusters_config().get(cluster, {})
    try:
        nfs_dir = get_nfs_data_dir(cluster_cfg) if cluster_cfg else None
    except (errors.SpecInvalid, TypeError):
        # Treat a malformed nfs_data_dir as "no NFS staging" rather
        # than failing the whole submission — the rest of the cluster
        # config (scheduler, cold_start_mem_buffer, ...) is still
        # usable. Pre-migration this caught the underlying
        # ``ValueError``; the typed migration replaced it with
        # ``SpecInvalid``.
        nfs_dir = None
    if nfs_dir:
        out.setdefault("HPC_NFS_DATA_DIR", nfs_dir)
    return out


def _mpi_canary_resources(resources: object) -> tuple[object, int | None]:
    """Shrink a multi-rank ``resources`` to the smallest meaningful canary (#293 PR4).

    A full MPI run might ask for hundreds of ranks across many nodes; the
    canary only needs to prove the launcher resolves and the MPI library loads,
    so it runs ``ranks=2`` on a single node (``ranks_per_node=2`` → nodes=1).
    Returns ``(canary_resources, canary_ranks)``; ``canary_ranks`` is ``None``
    for a non-MPI submit, signalling the caller to leave the canary env alone.
    """
    mpi = getattr(resources, "mpi", None)
    if mpi is None or not hasattr(resources, "model_copy"):
        return resources, None
    canary_ranks = min(2, int(mpi.ranks))
    new_mpi = mpi.model_copy(update={"ranks": canary_ranks, "ranks_per_node": canary_ranks})
    return resources.model_copy(update={"mpi": new_mpi}), canary_ranks


def _make_single_array_submission(
    backend: HPCBackend,
    *,
    job_name: str,
    total_tasks: int,
    job_env: dict[str, str],
    cwd: Path,
    resources: object = None,
    extra_flags: list[str] | None = None,
) -> list[str]:
    """Submit one array of size ``total_tasks`` and return the job IDs.

    Bypasses :class:`SubmissionPlan` for the simple case (no waves,
    no batching). Wave-based submissions are out of scope for v1 of
    submit-flow; callers needing them should use the legacy interactive
    ``/submit-hpc`` path or extend this function with a ``plan`` input.

    *resources* (a ``SubmitResources`` or ``None``) is translated by the
    backend into scheduler resource flags; ``None``/empty emits none, so
    the template directives apply unchanged. *extra_flags* (e.g. an afterok
    scheduler-dependency, #250) are appended after the resource flags.
    """
    backend._setup_log_dir()  # type: ignore[attr-defined]
    flags = backend.resource_flags(resources) + list(extra_flags or [])
    # #293: a single multi-rank MPI job is ONE job whose parallelism is the
    # rank count, not a scheduler array — submit it non-array (no --array/-t).
    # build-submit-spec refuses an mpi block with total_tasks > 1 (array-of-MPI
    # is deferred), so an mpi run always has total_tasks == 1 here; the
    # ``and total_tasks == 1`` is defense-in-depth against a hand-rolled spec.
    mpi = getattr(resources, "mpi", None) if resources is not None else None
    single_mpi_job = mpi is not None and total_tasks == 1
    if single_mpi_job:
        cmd = backend._build_command(  # type: ignore[attr-defined]
            None, job_name, job_env, extra_flags=flags, array=False
        )
    else:
        cmd = backend._build_command(  # type: ignore[attr-defined]
            f"1-{total_tasks}", job_name, job_env, extra_flags=flags
        )
    result = backend._execute_command(cmd, job_env, cwd)  # type: ignore[attr-defined]
    if result.returncode != 0:
        stderr_msg = result.stderr.strip() if result.stderr else "(no stderr)"
        raise errors.RemoteCommandFailed(f"submit failed (exit {result.returncode}): {stderr_msg}")
    match = backend.JOB_ID_REGEX.search(result.stdout)
    if not match:
        raise errors.RemoteCommandFailed(
            f"could not parse job id from scheduler output: {result.stdout!r}"
        )
    return [match.group(1)]


@primitive(
    name="submit-flow",
    verb="workflow",
    # ``submit_and_record`` is the only atom this workflow actually invokes
    # at runtime. ``discover_executors`` is imported
    # for type hints / pre-submit advisory paths but not in the composition
    # itself; advertising it here previously made operations.json over-
    # promise the workflow's dependency graph.
    composes=[submit_and_record],
    side_effects=[
        SideEffect("sync-push", "<ssh_target>:<remote_path>"),
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json"),
    ],
    # ``SchedulerThrottled`` was declared but never raised: real
    # throttling currently surfaces as ``RemoteCommandFailed``. Removed
    # to stop callers wiring retry policy against a code that never
    # fires. ``RemoteCommandFailed`` IS raised by ssh_run helpers in
    # this primitive's transitive path.
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
    cli=CliShape(
        help=(
            "Workflow atom: pre-flight + rsync + deploy + qsub + record in "
            "one shot. Auto-dispatches to submit-flow-batch when the spec "
            "is a {specs: [...]} object — callers always invoke this one "
            "subcommand whether the iteration emits 1 spec or N. Idempotent "
            "on run_id (or per-spec run_id when batched)."
        ),
        requires_ssh=True,
        spec_arg=True,
        spec_required=True,
        schema_ref=SchemaRef(input="submit_flow"),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--dry-run",
                action="store_true",
                help=("Validate the spec and report what would be launched; no SSH/rsync/qsub."),
            ),
            CliArg(
                "--partial-ok",
                action="store_true",
                help=(
                    "Tolerate per-task failures: when the wave finishes, classify "
                    "as `complete` if at least one task succeeded; record failed "
                    "task IDs in <run_id>.failed.json so aggregate-flow can skip "
                    "them. Without this flag (the default), any failure aborts "
                    "the wave with lifecycle_state=failed."
                ),
            ),
            CliArg(
                "--invalidate-on-code-change",
                action="store_true",
                help=(
                    "Opt-in code-iteration safety (#207). cmd_sha (the dedup key) "
                    "is PARAMETER identity only — editing the executor body "
                    "without changing any swept parameter keeps the same cmd_sha, "
                    "so a cross-machine resubmit could silently replay the prior "
                    "run's OLD code. With this flag, the run's tasks.py drift sha "
                    "is folded into the cmd_sha dedup so a code-only change forces "
                    "a fresh run. Default off (param-only dedup); a detected drift "
                    "still warns regardless."
                ),
            ),
        ),
        handler=_submit_flow_handler,
    ),
    agent_facing=True,
)
def submit_flow(
    experiment_dir: Path,
    *,
    spec: SubmitFlowSpec,
    _skip_preflight: bool | None = None,
    _skip_rsync_deploy: bool | None = None,
) -> SubmitFlowResult:
    """Execute the full submit pipeline and emit a single result.

    Pipeline:

    1. **Idempotency check** — if a journal record for ``spec.run_id``
       exists, return ``deduped=True`` immediately. No SSH, no scheduler
       calls.
    2. **Pre-flight gate** (operator-skippable via ``HPC_AGENT_SKIP_PREFLIGHT``
       or the internal ``_skip_preflight`` kwarg — never via the agent's spec,
       #275) — verifies SSH agent forwarding + cluster reachability. Aborts on
       failure.
    3. **rsync_push** — sync ``experiment_dir`` to ``spec.remote_path``
       (skippable via ``HPC_AGENT_SKIP_RSYNC_DEPLOY`` or the internal
       ``_skip_rsync_deploy`` kwarg — never via the agent's spec, #283).
    4. **deploy_runtime** — scp framework files into
       ``<remote_path>/.hpc/`` (skipped alongside step 3).
    5. **Optional canary** — submit a 1-task array (``job_name +
       "_canary"``, ``total_tasks=1``) and record it as a separate sidecar
       tagged with the same campaign. Caller waits and verifies — this
       atom only checks that qsub accepted the submission. Set
       ``spec.canary=False`` to skip when the caller has just
       smoke-tested.
    6. **Main submit** — qsub/sbatch the full ``1-total_tasks`` array.
    7. **Record** — :func:`runner.submit_and_record` writes the per-run
       sidecar + journal entry tagged with ``spec.campaign_id``.

    Errors raise the existing :class:`errors.HpcError` hierarchy so the
    CLI subcommand layer can convert them to error envelopes uniformly.

    ``spec.partial_ok`` records ``extra.partial_ok=True`` on the sidecar
    so a downstream monitor-flow wave with at least one success is
    classified ``complete`` (not ``failed``); aggregate-flow then skips
    the failed task IDs listed under ``<run_id>.failed.json``.
    """
    from hpc_agent._wire.workflows.submit_flow_batch import (
        SubmitFlowBatchSpec as _BatchSpec,
    )

    batch_spec = _BatchSpec(
        specs=[spec],
        rsync_excludes=spec.rsync_excludes,
    )
    return submit_flow_batch(
        experiment_dir,
        spec=batch_spec,
        _skip_preflight=_skip_preflight,
        _skip_rsync_deploy=_skip_rsync_deploy,
    )[0]


def _dedup_existing(experiment_dir: Path, spec: SubmitFlowSpec) -> SubmitFlowResult | None:
    """Return a deduped SubmitFlowResult if a *live* journal record already exists."""
    existing = load_run(experiment_dir, spec.run_id)
    if existing is None:
        return None
    # #276: a terminal-but-not-``complete`` record (``failed`` / ``abandoned``)
    # is not a live run — its ``job_ids`` are forensic, not an in-flight marker.
    # Deduping against it blocked every future submit for this run_id (a single
    # transient status-probe flake was enough to mint an ``abandoned`` corpse).
    # Fall through to a fresh submission. ``complete`` still dedups (idempotency);
    # ``in_flight`` (including a timed-out run) still blocks — don't double-submit.
    if is_resubmittable_terminal(existing):
        return None
    return SubmitFlowResult(
        run_id=existing.run_id,
        job_ids=list(existing.job_ids),
        total_tasks=int(existing.total_tasks),
        deduped=True,
        canary_done=False,
    )


def _submit_one_spec(
    *,
    experiment_dir: Path,
    spec: SubmitFlowSpec,
) -> SubmitFlowResult:
    """Per-spec submission work — backend build + (canary?) + main qsub + record.

    The expensive shared steps (preflight + rsync + deploy) MUST already
    have run on this ``(ssh_target, remote_path)`` before reaching here;
    :func:`submit_flow_batch` is responsible for that prelude.
    """
    job_env_full = _augment_job_env(
        job_env=spec.job_env,
        runtime=spec.runtime,
        campaign_id=spec.campaign_id,
        cluster=spec.cluster,
    )
    # Refuse an empty/missing job-script EXECUTOR before anything is qsub'd —
    # the augmented dict is what actually ships to the scheduler (#191).
    _ensure_job_script_executor(spec.run_id, job_env_full)
    backend_obj = build_remote_backend(
        backend_name=spec.backend,
        script=spec.script,
        ssh_target=spec.ssh_target,
        remote_path=spec.remote_path,
        pass_env_keys=tuple(spec.pass_env_keys) if spec.pass_env_keys is not None else None,
        job_env_keys=tuple(job_env_full.keys()),
        slurm_account=spec.slurm_account,
        slurm_cluster=spec.slurm_cluster,
        scheduler_profile=spec.scheduler_profile,
    )

    canary_run_id: str | None = None
    canary_job_ids: list[str] | None = None
    canary_done = False
    # #263/#249: a tiny batch (total_tasks <= threshold) or a cmd_sha already
    # canary-validated within the TTL skips the canary and goes straight to
    # main; canary_only / force_canary always canary. See _should_run_canary.
    if _should_run_canary(spec):
        canary_run_id = f"{spec.run_id}-canary"
        existing_canary = load_run(experiment_dir, canary_run_id)
        if existing_canary is not None and not is_resubmittable_terminal(existing_canary):
            # Replay: a prior call landed the canary but failed before
            # recording the main run, so the main-run dedup check (keyed
            # on spec.run_id) misses it. Reuse the recorded canary
            # job_ids instead of firing a duplicate canary qsub —
            # submit_flow is documented idempotent on run_id.
            #
            # #276: a terminal-but-not-``complete`` canary (``failed`` /
            # ``abandoned``) is excluded — it is NOT a live canary to reuse (the
            # monitor gave up, e.g. on a transient status-probe flake). Fall
            # through and fire a fresh one rather than gating main on a corpse.
            canary_job_ids = list(existing_canary.job_ids)
            canary_done = True
        else:
            # Mirror the main sidecar to <run_id>-canary.json so the canary
            # dispatches the SAME per-task executor (#162a) — otherwise it
            # errors 'sidecar not found' and the canary gate is a no-op (#160).
            _mirror_canary_sidecar(experiment_dir, spec.run_id, canary_run_id)
            canary_env = dict(job_env_full)
            canary_env["HPC_RUN_ID"] = canary_run_id
            canary_env["HPC_TASK_COUNT"] = "1"
            # #294 PR4: a run that opted into auto_resume_on_kill must prove its
            # checkpoint format round-trips BEFORE the long main array launches —
            # otherwise it discovers an unreloadable checkpoint only at resume,
            # hours in. Stamp the canary as a CHECKPOINT canary: an executor
            # driving its loop through run_iterations then writes a checkpoint at
            # iteration 1 and kills itself at iteration 2 (the dispatcher SIGTERM
            # path), and verify-canary (verify_checkpoint=True) asserts the
            # checkpoint survived + reloads. No-op for executors that don't use
            # run_iterations, so a non-checkpoint run is unaffected.
            if spec.auto_resume_on_kill:
                canary_env["HPC_CHECKPOINT_CANARY"] = "1"
            # #293 PR4: an MPI canary runs the smallest meaningful job — ranks=2,
            # one node — so it validates the launcher + MPI library without
            # queueing for the full multi-node allocation. The reduced rank count
            # must reach the in-job launcher too, so override HPC_MPI_RANKS.
            canary_resources, canary_mpi_ranks = _mpi_canary_resources(spec.resources)
            if canary_mpi_ranks is not None:
                canary_env["HPC_MPI_RANKS"] = str(canary_mpi_ranks)
            canary_job_ids = _make_single_array_submission(
                backend_obj,
                job_name=f"{spec.job_name}_canary",
                total_tasks=1,
                job_env=canary_env,
                cwd=experiment_dir,
                resources=canary_resources,
            )
            from hpc_agent._wire.actions.submit import SubmitSpec as _SubmitSpec

            submit_and_record(
                experiment_dir,
                spec=_SubmitSpec(
                    profile=spec.profile,
                    cluster=spec.cluster,
                    ssh_target=spec.ssh_target,
                    remote_path=spec.remote_path,
                    job_name=f"{spec.job_name}_canary",
                    run_id=canary_run_id,
                    job_ids=canary_job_ids,
                    total_tasks=1,
                    campaign_id=spec.campaign_id or None,
                ),
            )
            canary_done = True

    if spec.canary_only:
        # Two-phase canary gate (#160): the canary is submitted; do NOT launch
        # the main array. The caller verifies the canary (verify-canary) and
        # re-invokes submit-flow with canary=false to launch the main only on
        # success — so a broken dispatch can't sail past the canary.
        return SubmitFlowResult(
            run_id=spec.run_id,
            job_ids=[],
            total_tasks=spec.total_tasks,
            deduped=False,
            canary_done=canary_done,
            canary_run_id=canary_run_id,
            canary_job_ids=canary_job_ids,
            main_launched=False,
        )

    # #250: gate the main array on the canary SUCCEEDING via a scheduler-level
    # afterok dependency, so it co-submits now (no orchestrator wait+verify
    # round-trip) yet the scheduler drops main if the canary fails. Only when
    # the canary actually fired this call (canary_job_ids), the spec opted in,
    # and the scheduler supports afterok (SGE has none → left un-gated, as today).
    afterok_flags: list[str] = []
    if (
        spec.enable_afterok_dependency and canary_job_ids and backend_obj.supports_afterok  # type: ignore[attr-defined]
    ):
        afterok_flags = backend_obj._build_afterok_dependency_flag(  # type: ignore[attr-defined]
            list(canary_job_ids)
        )

    job_ids = _make_single_array_submission(
        backend_obj,
        job_name=spec.job_name,
        total_tasks=spec.total_tasks,
        job_env=job_env_full,
        cwd=experiment_dir,
        resources=spec.resources,
        extra_flags=afterok_flags,
    )
    from hpc_agent._wire.actions.submit import SubmitSpec as _SubmitSpec

    # #207 opt-in code-iteration lever. Default (flag off): pass NO cmd_sha
    # here so _submit_one_spec's dedup behaviour is byte-for-byte what it
    # was — the only gate stays the journal run_id check in
    # submit_and_record, and submit-flow never folds parameter-identity
    # cmd_sha into a new dedup. Flag on: thread cmd_sha (PARAMETER identity,
    # from job_env['HPC_CMD_SHA']) so submit_and_record's cross-machine
    # fallback engages, then invalidate_on_code_change folds the run's
    # tasks.py drift sha into it — an executor-body edit with unchanged
    # swept params forces a FRESH run instead of replaying the prior
    # submission's code, while a same-code resubmit still dedups.
    dedup_cmd_sha = (
        (spec.job_env.get("HPC_CMD_SHA") or None) if spec.invalidate_on_code_change else None
    )
    # DAG lineage rides the same opt-in gate: when the cross-machine cmd_sha
    # dedup engages AND this spec declared parents, the lookup keys on the
    # composed node identity (params + ancestry) so a stale child computed
    # from a since-changed parent is never replayed. resolve_node_sha
    # re-reads the parents' sidecars here (cheap local reads) rather than
    # trusting earlier flow state — same recompute-don't-trust stance as
    # the sidecar write.
    dedup_node_sha = None
    if dedup_cmd_sha and spec.parents:
        from hpc_agent.state.runs import resolve_node_sha as _resolve_node_sha

        dedup_node_sha = _resolve_node_sha(
            experiment_dir, cmd_sha=dedup_cmd_sha, parent_run_ids=spec.parents
        )
    submit_and_record(
        experiment_dir,
        spec=_SubmitSpec(
            profile=spec.profile,
            cluster=spec.cluster,
            ssh_target=spec.ssh_target,
            remote_path=spec.remote_path,
            job_name=spec.job_name,
            run_id=spec.run_id,
            job_ids=job_ids,
            total_tasks=spec.total_tasks,
            campaign_id=spec.campaign_id or None,
            invalidate_on_code_change=spec.invalidate_on_code_change,
        ),
        cmd_sha=dedup_cmd_sha,
        node_sha=dedup_node_sha,
        invalidate_on_code_change=spec.invalidate_on_code_change,
        # #299 auto-resume keystone — persist what a monitor-side auto-resume
        # would re-submit *with* (the actual augmented env that shipped to the
        # scheduler, the cluster script, and the backend), plus the opt-in
        # policy + cap. Default-OFF: a spec that didn't set auto_resume_on_kill
        # is never auto-resubmitted. The canary record above deliberately omits
        # these — a canary is never auto-resumed.
        script=spec.script,
        backend=spec.backend,
        job_env=job_env_full,
        auto_resume_on_kill=spec.auto_resume_on_kill,
        max_auto_resumes=spec.max_auto_resumes,
        # #240 resolve-and-recover opt-in — persist the general-resolver
        # auto-act policy + cap alongside the #299 auto-resume keystone. Same
        # default-OFF zero-blast-radius posture: a spec that didn't set
        # auto_recover_on_failure is never auto-recovered.
        auto_recover_on_failure=spec.auto_recover_on_failure,
        max_auto_recovers=spec.max_auto_recovers,
    )

    if spec.partial_ok:
        from hpc_agent.state.runs import run_sidecar_path

        marker = run_sidecar_path(experiment_dir, spec.run_id).with_suffix(".partial_ok")
        with contextlib.suppress(OSError):
            marker.write_text("1", encoding="utf-8")

    return SubmitFlowResult(
        run_id=spec.run_id,
        job_ids=job_ids,
        total_tasks=spec.total_tasks,
        deduped=False,
        canary_done=canary_done,
        canary_run_id=canary_run_id,
        canary_job_ids=canary_job_ids,
    )


@primitive(
    name="submit-flow-batch",
    verb="workflow",
    # ``submit_and_record`` is the only atom this workflow actually invokes
    # at runtime. ``discover_executors`` is imported
    # for type hints / pre-submit advisory paths but not in the composition
    # itself; advertising it here previously made operations.json over-
    # promise the workflow's dependency graph.
    composes=[submit_and_record],
    side_effects=[
        SideEffect("sync-push", "<ssh_target>:<remote_path>"),
        SideEffect("scheduler-submit", "<cluster> (one qsub per spec)"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (per spec)"),
    ],
    # See submit-flow above: ``SchedulerThrottled`` removed because
    # nothing actually raises it; real throttling surfaces as
    # ``RemoteCommandFailed``.
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="specs.run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
    cli=CliShape(
        help=(
            "Workflow atom: rsync + deploy ONCE, then qsub N specs sharing "
            "the same (ssh_target, remote_path). Use whenever a campaign or "
            "sweep submits >1 specs to the same cluster — bundles 13×N ssh "
            "handshakes into ~3 (rsync + deploy + multiplexed qsubs). Spec "
            "file is a JSON list."
        ),
        requires_ssh=True,
        spec_arg=True,
        spec_required=True,
        schema_ref=SchemaRef(input="submit_flow_batch"),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--dry-run",
                action="store_true",
                help="Validate the batch + report shared targets; no SSH/rsync/qsub.",
            ),
        ),
        handler=_submit_flow_batch_handler,
    ),
    agent_facing=True,
)
def submit_flow_batch(
    experiment_dir: Path,
    *,
    spec: SubmitFlowBatchSpec,
    _skip_preflight: bool | None = None,
    _skip_rsync_deploy: bool | None = None,
) -> list[SubmitFlowResult]:
    """Submit N specs that share ``(ssh_target, remote_path)`` in one shot.

    The Pydantic ``SubmitFlowBatchSpec`` is the canonical wire +
    Python authoring surface; ``spec.specs`` is a list of full
    :class:`SubmitFlowSpec` models (the same type the standalone
    ``submit-flow`` atom takes). ``spec.rsync_excludes`` applies once across
    the bundle; preflight is operator-gated via ``HPC_AGENT_SKIP_PREFLIGHT`` /
    the internal ``_skip_preflight`` kwarg (#275), not a per-spec field. The
    rsync+deploy prelude is likewise operator/internal-gated via
    ``HPC_AGENT_SKIP_RSYNC_DEPLOY`` / the internal ``_skip_rsync_deploy`` kwarg
    (#283) — a batch-level decision, not a per-spec ``skip_rsync_deploy`` field
    an agent could assert against a stale tree.

    The motivating problem: a campaign-time fan-out of N submissions
    used to do N × (rsync + deploy_runtime + qsub), which sent ~13×N
    ssh handshakes at the cluster's sshd and tripped MaxStartups
    (CARC, typically). The bundle collapses that to:

    * 1 ssh probe (preflight)
    * 1 ``rsync_push`` (the codebase is identical across specs)
    * 1 ``deploy_runtime`` (the framework files are identical across specs)
    * N × (qsub + ``submit_and_record``) — sequential, but reusing the
      ssh ControlMaster socket established by the probe, so each
      additional qsub is ~free.

    Specs that already have a journal record are deduped up front and
    contribute a ``deduped=True`` :class:`SubmitFlowResult` without any
    cluster traffic — the same idempotency contract :func:`submit_flow`
    has always offered, applied per-spec.

    ``spec.specs`` MUST share ``ssh_target`` and ``remote_path`` —
    different targets/paths can't share an rsync. Heterogeneous batches
    raise :class:`errors.SpecInvalid`; the caller (campaign driver /
    agent) is responsible for grouping specs by ``(ssh_target,
    remote_path)`` before calling.

    Order of returned results matches the order of ``spec.specs``.
    """
    rsync_excludes = list(spec.rsync_excludes) if spec.rsync_excludes is not None else None
    # #275: skip_preflight is operator-only — resolve from the internal kwarg
    # (trusted callers like submit_and_verify) or HPC_AGENT_SKIP_PREFLIGHT, never
    # from the spec an agent authors, so an agent can't silence the uv guard.
    skip_preflight = _skip_preflight_requested(_skip_preflight)
    # #283: skip_rsync_deploy is operator/internal-only too — resolve from the
    # internal kwarg (trusted callers like submit_and_verify, where Phase 1 just
    # deployed the same tree) or HPC_AGENT_SKIP_RSYNC_DEPLOY, never from a
    # per-spec field an agent could assert against a tree that drifted since the
    # last deploy (#185).
    skip_rsync_deploy = _skip_rsync_deploy_requested(_skip_rsync_deploy)
    inner_specs = list(spec.specs)

    # Per-repo advisory submit lock: serialize multiple `submit-flow` /
    # `submit-flow-batch` invocations against the same experiment so two
    # shells firing simultaneously don't BOTH fan out N qsubs at the
    # cluster's sshd. The lock is advisory (other code paths don't take
    # it) and per-repo (`<journal_home>/.submit_lock`); cross-cluster
    # parallelism is still allowed when each cluster has its own
    # experiment_dir. Disable via ``HPC_SUBMIT_NO_LOCK=1`` — kept
    # narrowly for (a) the test suite, which exercises submit_flow in
    # parallel with mocked subprocess so there's no real qsub to race,
    # and (b) operators who deliberately want concurrent submits and
    # have confirmed the cluster's sshd / scheduler tolerates the
    # burst. Disabling outside those two cases risks a scheduler-
    # throttling stampede; see ``docs/reference/env-vars.md``.
    import os

    from hpc_agent.infra import io
    from hpc_agent.state.run_record import journal_dir

    use_lock = os.environ.get("HPC_SUBMIT_NO_LOCK") != "1"
    lock_path = journal_dir(experiment_dir) / ".submit_lock"
    lock_ctx = io.advisory_flock(lock_path) if use_lock else _noop_lock_ctx()
    with lock_ctx:
        return _submit_flow_batch_locked(
            experiment_dir=experiment_dir,
            specs=inner_specs,
            rsync_excludes=rsync_excludes,
            skip_preflight=skip_preflight,
            skip_rsync_deploy=skip_rsync_deploy,
        )


@contextlib.contextmanager
def _noop_lock_ctx() -> Iterator[bool]:
    """Stand-in for advisory_flock when HPC_SUBMIT_NO_LOCK=1."""
    yield True


def _submit_flow_batch_locked(
    *,
    experiment_dir: Path,
    specs: list[SubmitFlowSpec],
    rsync_excludes: list[str] | None,
    skip_preflight: bool,
    skip_rsync_deploy: bool,
) -> list[SubmitFlowResult]:
    """Body of :func:`submit_flow_batch`, executed under the per-repo lock."""
    # Auto-cleanup: drop sidecars from earlier failed batches before doing
    # anything else. Without this, a half-baked sidecar from yesterday's
    # rate-limited submit would still surface to find_run_by_cmd_sha and
    # to the agent's resume-detection prompts. The prune is silent on
    # success (returns []); if it deletes anything, the cluster traffic
    # we're about to send is fresh anyway.
    #
    # ``min_age_seconds=0`` is safe here: the per-repo lock above
    # serialises submit_flow_batch invocations against the same
    # experiment, so the only sidecars present at this point are from
    # PRIOR batches (which had to complete or fail before releasing the
    # lock). The default min_age_seconds guard is for ad-hoc invocations
    # that don't hold the lock and could race a concurrent submit.
    #
    # ``exclude`` protects the run_ids in THIS batch: the slash flow
    # writes each run's sidecar jobless at Step 6d *before* calling
    # submit_flow_batch, so those sidecars are present inside the lock
    # and are indistinguishable (jobless + journal-less) from a prior
    # failed batch's orphan. Without the exclude the prune would delete
    # the very sidecars we're about to finalize post-qsub. The canary
    # sibling (``{run_id}-canary``) is written the same way.
    from hpc_agent.state.runs import prune_orphan_sidecars

    protected = {s.run_id for s in specs} | {f"{s.run_id}-canary" for s in specs}
    prune_orphan_sidecars(experiment_dir, min_age_seconds=0, exclude=protected)

    # Single-target invariant: rsync + deploy can only target one place.
    targets = {(s.ssh_target, s.remote_path) for s in specs}
    if len(targets) > 1:
        raise errors.SpecInvalid(
            f"submit_flow_batch requires all specs to share (ssh_target, remote_path); "
            f"got {len(targets)} distinct combinations: {sorted(targets)}"
        )

    # Per-spec idempotency: dedup against the journal up front, never
    # touch the cluster for already-submitted run_ids.
    results: list[SubmitFlowResult | None] = [_dedup_existing(experiment_dir, s) for s in specs]
    fresh_indices = [i for i, r in enumerate(results) if r is None]
    if not fresh_indices:
        # Every spec was already on the journal — return the deduped
        # results without firing rsync/deploy. ``# type: ignore`` would
        # otherwise be needed because mypy can't see the None elimination.
        return [r for r in results if r is not None]

    # Guarantee the cluster-required per-run sidecar exists for every
    # fresh spec BEFORE rsync — submit-flow owns this artifact rather than
    # trusting a prior step to have written it. Missing + synthesizable →
    # written here; missing + not synthesizable → fail fast locally
    # (see _ensure_run_sidecar). #148 / #150.
    for i in fresh_indices:
        _ensure_run_sidecar(experiment_dir, specs[i])
        # The canary dispatches the SAME per-task command as the main run,
        # so its sidecar (``<run_id>-canary.json``) must ALSO exist on disk
        # before the shared rsync below — otherwise it never ships to the
        # cluster and every canary task dies ``sidecar_not_found`` (#175).
        # Mirror it here, in the pre-rsync prelude, so it rides the same
        # rsync as the main sidecar. ``_submit_one_spec`` keeps its own
        # ``_mirror_canary_sidecar`` call as an idempotent guard — that one
        # runs post-rsync (too late to reach the cluster) and early-returns
        # once this sidecar exists. (``canary_only`` requires ``canary``.)
        if specs[i].canary:
            _mirror_canary_sidecar(experiment_dir, specs[i].run_id, f"{specs[i].run_id}-canary")

    # Shared prelude (#280): one connectivity gate, then rsync+deploy run
    # CONCURRENT with the independent ``command -v uv`` probe. Still 1 ×
    # (probe + rsync + deploy) for N specs reusing the ssh ControlMaster, but
    # the uv probe no longer stacks ahead of rsync — see _run_shared_prelude
    # for the per-operation audit. #185/#283: when the batch-level
    # ``skip_rsync_deploy`` is set (Phase 2 of the two-phase canary gate, where
    # Phase 1 just deployed — resolved from the in-process ``_skip_rsync_deploy``
    # kwarg or ``HPC_AGENT_SKIP_RSYNC_DEPLOY``, NOT a per-spec agent field), the
    # rsync+deploy arm is dropped; the uv probe still runs.
    ssh_target, remote_path = next(iter(targets))
    skip_prelude_io = skip_rsync_deploy
    _run_shared_prelude(
        experiment_dir=experiment_dir,
        ssh_target=ssh_target,
        remote_path=remote_path,
        rsync_excludes=rsync_excludes,
        # All specs in a batch share (ssh_target, remote_path) ⇒ same cluster ⇒
        # same scheduler; deploy only that family's scripts.
        scheduler=specs[0].backend if specs else None,
        job_envs=[dict(specs[i].job_env or {}) for i in fresh_indices],
        skip_preflight=skip_preflight,
        skip_prelude_io=skip_prelude_io,
    )

    # Per-spec submission work.
    #
    # If spec ``i`` raises mid-loop, specs ``0..i-1`` are already on the
    # cluster (qsubbed AND journal-recorded by submit_and_record); we
    # can't recall them. Attach the partial result list to the
    # exception so the caller can recover state (which run_ids landed,
    # which to retry) instead of getting a bare raise with no
    # accounting.
    for i in fresh_indices:
        try:
            results[i] = _submit_one_spec(experiment_dir=experiment_dir, spec=specs[i])
        except Exception as exc:
            # Mutate the exception to carry the partial results. The
            # caller can branch on ``hasattr(exc, "partial_submit_results")``
            # to recover the (succeeded, failed_index) split.
            partial = [r for r in results if r is not None]
            exc.partial_submit_results = partial  # type: ignore[attr-defined]
            exc.failed_spec_index = i  # type: ignore[attr-defined]
            raise
    # mypy: every slot is now non-None.
    return [r for r in results if r is not None]
