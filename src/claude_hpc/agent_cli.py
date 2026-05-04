"""Command-line interface — the agent surface.

Designed to be invoked by automation (MARs orchestrator agents via the
Bash tool, cron, scripts). Conventions:

- Stdout is exclusively a single-line JSON envelope. Exception:
  ``capabilities --full`` emits a plain-text ``llms-full`` dump (one-shot
  LLM context loading, analogous to ``--help``). Every other invocation
  preserves the JSON-envelope contract.
- Stderr carries free-form diagnostic prose (e.g. ``[dispatch] ERROR: …``
  emitted by ``claude_hpc.mapreduce.dispatch`` and ``…map.combiner``); it is
  intended for humans tailing logs. Do not parse it as JSON.
- Exit codes: 0 success, 1 user error, 2 cluster/network error, 3 internal.
- Every subcommand accepts ``--experiment-dir`` (defaults to CWD).
- Subcommands with non-trivial inputs accept ``--spec path/to/spec.json``.

The full schema for each subcommand is documented in ``docs/reference/cli-spec.md``
and shipped as JSON Schema files under ``claude_hpc/schemas/``.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

import claude_hpc
from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc.orchestrator import runner
from claude_hpc.orchestrator.discover import (
    detect_mars_tier,
    discover_executors,
    read_meta_json,
)

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_CLUSTER_ERROR = 2
EXIT_INTERNAL = 3

# error_code → exit code mapping. Stable contract; documented in docs/reference/cli-spec.md.
_EXIT_CODE_BY_CATEGORY = {
    "user": EXIT_USER_ERROR,
    "cluster": EXIT_CLUSTER_ERROR,
    "network": EXIT_CLUSTER_ERROR,
    "internal": EXIT_INTERNAL,
}


# ─── envelope helpers ──────────────────────────────────────────────────────


def _emit(envelope: dict[str, Any]) -> None:
    """Print a single-line JSON envelope to stdout."""
    print(json.dumps(envelope, sort_keys=True), flush=True)


@functools.cache
def _meta_idempotent(name: str) -> bool:
    """Look up a primitive's idempotency declaration from the catalog.

    B4 rewire: callers used to hardcode ``_ok(idempotent=True/False, ...)``
    which forked the truth between the @primitive decorator (consumed by
    docs / lint) and the runtime envelope (consumed by caller policy).
    Routing through the catalog collapses both to the decorator.

    Cached because the catalog walks every primitive's frontmatter on
    first call. Falls back to True on miss (consistent with the
    pre-B4 default for query-style commands; the cross-validation test
    in tests/test_idempotency.py guards against silent drift).
    """
    try:
        from claude_hpc._internal.operations import operations_catalog

        for entry in operations_catalog():
            if entry.get("name") == name:
                return bool(entry.get("idempotent", True))
    except Exception:
        pass
    return True


def _ok(
    data: dict[str, Any],
    *,
    idempotent: bool | None = None,
    name: str | None = None,
    partial_errors: list[dict[str, str]] | None = None,
) -> None:
    """Emit an ok-true envelope.

    *idempotent* (B4 rewire): preferred spelling is to pass *name* — the
    primitive's catalog name — and let the envelope read the
    ``idempotent`` flag from ``operations_catalog()``. The legacy
    ``idempotent=True/False`` kwarg is still honoured for callsites that
    don't have a primitive mapping (e.g. cmd_aggregate which wraps a
    pure mapreduce reduce). When both are supplied, the explicit kwarg
    wins so callers can opt out of the catalog lookup if needed.

    *partial_errors*: optional list of ``{code, detail}`` dicts surfaced
    at the top level of the envelope — distinct from ``data.errors``.
    Used by primitives like ``inspect-cluster`` whose underlying data
    source can be partially degraded (qhost timed out, sacct
    unavailable) without the operation as a whole failing.
    """
    if idempotent is None:
        idempotent = _meta_idempotent(name) if name else True
    env: dict[str, Any] = {"ok": True, "idempotent": idempotent, "data": data}
    if partial_errors:
        env["partial_errors"] = list(partial_errors)
    _emit(env)


def _err(
    *,
    error_code: str,
    message: str,
    category: str,
    retry_safe: bool,
    remediation: str | None = None,
) -> int:
    payload = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "category": category,
        "retry_safe": retry_safe,
    }
    if remediation is not None:
        payload["remediation"] = remediation
    _emit(payload)
    return _EXIT_CODE_BY_CATEGORY.get(category, EXIT_INTERNAL)


def _err_from_hpc(exc: errors.HpcError) -> int:
    return _err(
        error_code=exc.error_code,
        message=str(exc),
        category=exc.category,
        retry_safe=exc.retry_safe,
        remediation=exc.remediation,
    )


def _require_ssh_agent() -> int | None:
    # Cluster-touching subcommands hang silently when SSH_AUTH_SOCK is
    # missing — the most common Bun.spawn failure mode for orchestrators
    # like MARs. Fail fast with a typed error instead of stalling on auth.
    if os.environ.get("SSH_AUTH_SOCK"):
        return None
    return _err_from_hpc(
        errors.SshUnreachable(
            "SSH_AUTH_SOCK is not set; cannot reach the cluster.",
            remediation=(
                "Forward SSH_AUTH_SOCK (and SSH_AGENT_PID) into the spawn "
                "environment, then run `hpc-mapreduce preflight` to verify. "
                "See docs/workflows/mars-integration.md for the Bun.spawn env block."
            ),
        )
    )


# ─── shared option helpers ─────────────────────────────────────────────────


def _add_experiment_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Path to the experiment repo (default: current working directory).",
    )


def _add_profile_cluster_cmdsha(
    parser: argparse.ArgumentParser,
    *,
    cmd_sha_help: str | None = None,
) -> None:
    """Add the ``--profile`` (required), ``--cluster`` (required), and
    ``--cmd-sha`` (optional) trio used by every smart-submit pipeline
    subcommand: ``plan-submit``, ``runtime-prior``, ``walltime-drift``,
    ``house-edge``.

    *cmd_sha_help* lets each subcommand explain how it consumes the
    filter; defaults to a generic note.
    """
    parser.add_argument("--profile", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument(
        "--cmd-sha",
        default=None,
        help=cmd_sha_help or "If set, filter runtime priors to samples with this cmd_sha.",
    )


def _add_run_id(parser: argparse.ArgumentParser) -> None:
    """Add the canonical ``--run-id`` argument (always required)."""
    parser.add_argument("--run-id", required=True)


def _add_spec_and_dry_run(
    parser: argparse.ArgumentParser,
    *,
    schema_hint: str,
    dry_run_help: str,
) -> None:
    """Add the ``--spec`` (required) + ``--dry-run`` pair used by the
    workflow-flow subcommands (``submit-flow``, ``monitor-flow``,
    ``aggregate-flow``).

    *schema_hint* is the schema filename mentioned in the spec help
    (e.g. ``"schemas/submit_flow.input.json"``); *dry_run_help* lets
    each subcommand explain what dry-run skips.
    """
    parser.add_argument(
        "--spec",
        type=Path,
        required=True,
        help=f"JSON spec file ({schema_hint})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=dry_run_help,
    )


def _load_spec(spec_path: Path | None, *, schema_name: str | None = None) -> dict[str, Any]:
    """Load and (optionally) JSON-Schema-validate ``--spec`` input.

    Validation is opt-in via *schema_name* so callers without a matching
    schema (e.g. ad-hoc dicts) still work, but every CLI subcommand that
    has one in ``claude_hpc/schemas/<name>.input.json`` should pass
    it.  Validation failures map to ``SpecInvalid`` with the schema
    field path in the message — far more useful to a calling agent than
    the Python ``int("abc")`` traceback we used to surface.
    """
    if spec_path is None:
        return {}
    try:
        loaded = json.loads(spec_path.read_text())
    except FileNotFoundError as exc:
        raise errors.ConfigInvalid(f"--spec file not found: {spec_path}") from exc
    except json.JSONDecodeError as exc:
        raise errors.ConfigInvalid(f"--spec is not valid JSON ({spec_path}): {exc}") from exc
    if not isinstance(loaded, dict):
        raise errors.ConfigInvalid(f"--spec must be a JSON object; got {type(loaded).__name__}")
    if schema_name is not None:
        _validate_against_schema(loaded, schema_name)
    return loaded


def _validate_against_schema(payload: dict[str, Any], schema_name: str) -> None:
    """Validate *payload* against ``claude_hpc/schemas/<schema_name>.input.json``.

    Raises :class:`errors.SpecInvalid` on schema mismatch.  When the
    ``jsonschema`` library is unavailable (older installs that haven't
    picked up the runtime dep), this falls back to a no-op so the CLI
    keeps working — schema validation is defence in depth, not the only
    line of defence (``submit_and_record`` etc. still validate inputs).
    """
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        return
    try:
        schema_text = (
            _resource_files("claude_hpc.schemas") / f"{schema_name}.input.json"
        ).read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        return
    schema = json.loads(schema_text)
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise errors.SpecInvalid(
            f"--spec failed schema {schema_name}.input.json at {path}: {exc.message}"
        ) from exc


# ─── subcommand: capabilities ──────────────────────────────────────────────


# Re-exported from claude_hpc.atoms.capabilities for back-compat with
# tests that import the constant directly from agent_cli.
from claude_hpc.atoms.capabilities import _MARS_SKILL_NAMES  # noqa: E402,F401


def _live_subcommands() -> list[str]:
    """Derive the subcommand list from the actual argparse tree.

    Replaces the hand-typed literal that used to live here — the literal
    drifted (it missed ``walltime-drift``, ``house-edge``, etc.) and had
    no test backing it. Walking ``parser._subparsers._group_actions[0]
    .choices`` gives the single source of truth.
    """
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.capabilities."""
    from claude_hpc._internal.operations import render_llms_full
    from claude_hpc.atoms.capabilities import capabilities

    if getattr(args, "full", False):
        # Human/LLM-mode: emit a multi-section text blob (NOT the JSON
        # envelope) modeled on Modal's llms-full.txt pattern. Documented
        # exception to the stdout-is-JSON contract; analogous to --help.
        sys.stdout.write(render_llms_full())
        sys.stdout.flush()
        return EXIT_OK

    _ok(capabilities(subcommands=_live_subcommands()), name="capabilities")
    return EXIT_OK


# ─── subcommand: preflight ─────────────────────────────────────────────────


def cmd_preflight(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.preflight."""
    from claude_hpc.atoms.preflight import check_preflight

    data = check_preflight(cluster=getattr(args, "cluster", None))
    _ok(data, name="check-preflight")
    return EXIT_OK if data["all_ok"] else EXIT_CLUSTER_ERROR


# ─── subcommand: interview ─────────────────────────────────────────────────


def cmd_interview(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.interview."""
    from claude_hpc.atoms.interview import record_interview

    intent = _load_spec(args.spec, schema_name="interview")
    if not intent:
        raise errors.SpecInvalid("--spec is required for `interview`")
    campaign_dir = Path(args.campaign_dir).resolve()
    try:
        data = record_interview(intent, campaign_dir=campaign_dir)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc
    _ok(data, name="interview")
    return EXIT_OK


# ─── subcommand: recall ────────────────────────────────────────────────────


def cmd_recall(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.recall."""
    from claude_hpc.atoms.recall import recall_campaigns, resolve_roots

    roots = resolve_roots(getattr(args, "root", None))
    try:
        data = recall_campaigns(
            roots,
            task_kind=getattr(args, "task_kind", None),
            operator=getattr(args, "operator", None),
            since=getattr(args, "since", None),
            limit=int(getattr(args, "limit", 20)),
            include_runtime=bool(getattr(args, "include_runtime", False)),
            include_generator_stats=bool(getattr(args, "include_generator_stats", False)),
        )
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc
    _ok(data, name="recall")
    return EXIT_OK


# ─── subcommand: discover ──────────────────────────────────────────────────


def cmd_discover(args: argparse.Namespace) -> int:
    infos = discover_executors(args.experiment_dir)
    data: dict[str, Any] = {
        "executors": [
            {
                "name": i.name,
                "path": str(i.path),
                "cli_framework": i.cli_framework,
                "has_main_guard": i.has_main_guard,
            }
            for i in infos
        ]
    }
    meta = _build_mars_meta_block(Path(args.experiment_dir))
    if meta is not None:
        data["meta"] = meta
    _ok(data, name="discover-executors")
    return EXIT_OK


def _build_mars_meta_block(experiment_dir: Path) -> dict[str, Any] | None:
    """Assemble the ``meta`` block for the discover envelope.

    Returns ``None`` when *experiment_dir* is not a MARs experiment
    (no ``meta.json`` present). Otherwise extracts the fields claude-hpc
    knows about and adds a path-derived ``tier``.
    """
    raw = read_meta_json(experiment_dir)
    if raw is None:
        return None
    block: dict[str, Any] = {}
    for key in ("experiment_id", "seed", "purpose"):
        if key in raw:
            block[key] = raw[key]
    block["tier"] = detect_mars_tier(experiment_dir)
    return block


# ─── subcommand: clusters ──────────────────────────────────────────────────


# ─── subcommand: inspect-cluster ───────────────────────────────────────────


def cmd_inspect_cluster(args: argparse.Namespace) -> int:
    """Read-only snapshot of a cluster's node states.

    Used by /hpc-submit Phase 4 (planner) to see allocation pressure,
    co-tenants, and drain/down state. Useful standalone for ad-hoc
    debugging when a job is queueing slowly or running on a hot node.
    """
    if (rc := _require_ssh_agent()) is not None:
        return rc
    from claude_hpc.infra.inspect import inspect_cluster

    snap = inspect_cluster(
        args.cluster,
        sacct_window_hours=args.sacct_window_hours,
        stress_alloc_mem_pct=args.stress_alloc_mem_pct,
        stress_cpu_load_frac=args.stress_cpu_load_frac,
        use_cache=not args.no_cache,
    )
    # B3: surface cluster-side soft failures (qhost timed out, scontrol
    # parse error, sacct unavailable) at envelope-level ``partial_errors``
    # rather than burying them inside ``data.errors`` where machine
    # consumers tend to miss them. The legacy ``data.errors`` shape is
    # kept (snap.to_dict() includes it) for one release as back-compat.
    payload = snap.to_dict()
    partial = list(payload.get("errors", []))
    _ok(payload, name="inspect-cluster", partial_errors=partial or None)
    return EXIT_OK


# ─── subcommand: runtime-prior ─────────────────────────────────────────────


@primitive(
    name="read-runtime-prior",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
)
def cmd_runtime_prior(args: argparse.Namespace) -> int:
    from claude_hpc.orchestrator.runtime_prior import roll_up_quantiles

    out = roll_up_quantiles(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        cmd_sha=args.cmd_sha,
    )
    _ok(out, name="read-runtime-prior")
    return EXIT_OK


# ─── subcommand: walltime-drift / house-edge (calibration) ────────────────


def cmd_walltime_drift(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.walltime_drift."""
    from claude_hpc.atoms.walltime_drift import walltime_drift

    _ok(
        walltime_drift(
            experiment_dir=args.experiment_dir,
            profile=args.profile,
            cluster=args.cluster,
            cmd_sha=args.cmd_sha,
            base_safety_mult=float(args.base_safety_mult),
        ),
        name="walltime-drift",
    )
    return EXIT_OK


@primitive(
    name="best-submit-window",
    verb="query",
    side_effects=[],
    error_codes=[errors.HpcError],
    idempotent=True,
)
def cmd_best_submit_window(args: argparse.Namespace) -> int:
    """Surface the top_k lowest-wait submit windows in the next horizon.

    Sweeps the diurnal queue-wait predictor at hourly offsets up to
    ``--within-hours`` and returns the top ``--top-k`` candidates.
    Cold-start hours are excluded from the ranking. The slash command
    consumes the result to suggest "submit now" vs. "wait until
    <hour>".
    """
    from claude_hpc.forecast.best_submit_window import best_submit_windows

    candidates = best_submit_windows(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        within_hours=int(args.within_hours),
        top_k=int(args.top_k),
    )
    _ok(
        {
            "profile": args.profile,
            "cluster": args.cluster,
            "within_hours": int(args.within_hours),
            "top_k": int(args.top_k),
            "candidates": [c.to_dict() for c in candidates],
        },
        name="best-submit-window",
    )
    return EXIT_OK


@primitive(
    name="predict-queue-wait",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
)
def cmd_predict_queue_wait(args: argparse.Namespace) -> int:
    """Forecast queue-wait seconds for a hypothetical submit.

    Dispatches to the discrete-event simulator (Phase 4 DES backend)
    when a recent ClusterSnapshot + user_profiles coverage are present;
    falls back to the diurnal moving-average baseline otherwise. The
    result's ``method`` field reports which backend won.
    """
    from claude_hpc.forecast.queue_wait_baseline import predict_queue_wait

    out = predict_queue_wait(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        at_iso=args.at_iso,
        backend=args.backend,
        n_replications=int(args.n_replications),
        seed=args.seed,
    )
    _ok(out.to_dict(), name="predict-queue-wait")
    return EXIT_OK


def cmd_house_edge(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.house_edge."""
    from claude_hpc.atoms.house_edge import house_edge

    _ok(
        house_edge(
            experiment_dir=args.experiment_dir,
            profile=args.profile,
            cluster=args.cluster,
            cmd_sha=args.cmd_sha,
        ),
        name="house-edge",
    )
    return EXIT_OK


# ─── subcommand: plan-submit ───────────────────────────────────────────────


@primitive(
    name="score-submit-plan",
    verb="query",
    side_effects=[SideEffect("ssh", "<cluster> (delegates to inspect-cluster)")],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.ClusterUnknown],
    idempotent=True,
)
def cmd_plan_submit(args: argparse.Namespace) -> int:
    if (rc := _require_ssh_agent()) is not None:
        return rc
    from claude_hpc.orchestrator.planner import plan_submit

    candidates: list[str] | None = None
    if args.candidates:
        candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    out = plan_submit(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        candidates=candidates,
        cmd_sha=args.cmd_sha,
        adversarial=not bool(getattr(args, "no_adversarial", False)),
        walltime_safety_mult=float(getattr(args, "walltime_safety_mult", 1.30)),
        target_backfill_window_sec=getattr(args, "target_backfill_window_sec", None),
        current_max_array_size=getattr(args, "current_max_array_size", None),
        est_per_task_sec=getattr(args, "est_per_task_sec", None),
    )
    _ok(out, name="score-submit-plan")
    return EXIT_OK


def cmd_clusters_list(_args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.clusters."""
    from claude_hpc.atoms.clusters import list_clusters

    _ok(list_clusters(), name="clusters-list")
    return EXIT_OK


def cmd_clusters_describe(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.clusters."""
    from claude_hpc.atoms.clusters import describe_cluster

    _ok(describe_cluster(name=args.name), name="clusters-describe")
    return EXIT_OK


# ─── subcommand: list-in-flight ────────────────────────────────────────────


# ``_last_status_age_seconds`` lives at the atom layer (it's the
# freshness helper used by both list-in-flight and the cmd_status
# adapter); re-exported here so cmd_status can keep its existing
# import-free callsite without a layering inversion.
from claude_hpc.atoms.list_in_flight import _last_status_age_seconds  # noqa: E402,F401


def cmd_list_in_flight(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.list_in_flight."""
    from claude_hpc.atoms.list_in_flight import list_in_flight

    _ok(list_in_flight(experiment_dir=args.experiment_dir), name="list-in-flight")
    return EXIT_OK


# ─── subcommand: campaign status / list ────────────────────────────────────


def cmd_campaign_status(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.campaign_status."""
    from claude_hpc.atoms.campaign_status import campaign_status

    _ok(
        campaign_status(experiment_dir=args.experiment_dir, campaign_id=args.campaign_id),
        name="campaign-status",
    )
    return EXIT_OK


def cmd_campaign_list(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.campaign_list."""
    from claude_hpc.atoms.campaign_list import campaign_list

    _ok(campaign_list(experiment_dir=args.experiment_dir), name="campaign-list")
    return EXIT_OK


# ─── subcommand: status ────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(
            f"no journal record for run_id {args.run_id!r} in {args.experiment_dir}"
        )
    updated = runner.record_status(
        args.experiment_dir,
        args.run_id,
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        job_ids=record.job_ids,
        job_name=record.job_name,
    )
    data: dict[str, Any] = {
        "run_id": updated.run_id,
        "lifecycle_state": updated.status,
        "last_status": updated.last_status,
        "last_status_age_seconds": _last_status_age_seconds(updated.last_status),
        "combined_waves": updated.combined_waves,
        "failed_waves": updated.failed_waves,
    }
    # Surface the campaign tag so a caller seeing /status output knows
    # this run is part of a closed-loop campaign without separately
    # querying `campaign list` / `campaign status`.
    if updated.campaign_id:
        data["campaign_id"] = updated.campaign_id

    # A-M1: surface preempted-task counts directly on /status so a
    # caller polling a partially-bumped run sees them without first
    # having to call /failures. The campus user's harness can branch
    # on "X of N tasks got preempted" while the run is still in
    # flight, instead of waiting for the whole array to fail before
    # noticing scheduler pressure. Sourced from the per-task sidecar
    # ``preempt`` block written by dispatch.py's SIGTERM handler.
    preempt_summary = _preempted_summary_from_sidecar(args.experiment_dir, args.run_id)
    if preempt_summary is not None:
        count, ids = preempt_summary
        data["preempted_count"] = count
        data["preempted_task_ids"] = ids

    _ok(data, name="poll-run-status")
    return EXIT_OK


def _preempted_summary_from_sidecar(
    experiment_dir: Any, run_id: str
) -> tuple[int, list[int]] | None:
    """Return (preempted_count, preempted_task_ids_sorted) or None.

    Walks the per-task ``tasks`` block of the run sidecar and collects
    every task_id whose entry carries a ``preempt`` block (set by
    dispatch.py's SIGTERM handler when the cluster bumps the campus
    user's low-priority job). Returns None when there are no preempted
    tasks or when the sidecar can't be read — callers should treat
    None as "no preempt info to surface", not an error.
    """
    try:
        from claude_hpc.orchestrator.runs import (
            read_run_sidecar as _read_sidecar_for_status,
        )

        sidecar = _read_sidecar_for_status(Path(experiment_dir), run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(sidecar, dict):
        return None
    tasks_block = sidecar.get("tasks") or {}
    if not isinstance(tasks_block, dict):
        return None
    preempted_ids: list[int] = []
    for tid_str, entry in tasks_block.items():
        if not isinstance(entry, dict):
            continue
        if "preempt" in entry:
            try:
                preempted_ids.append(int(tid_str))
            except (TypeError, ValueError):
                continue
    if not preempted_ids:
        return None
    return len(preempted_ids), sorted(preempted_ids)


def _overlay_meta_on_spec(spec: dict[str, Any], experiment_dir: Path) -> dict[str, Any]:
    """Overlay missing ``profile`` / ``job_name`` from ``meta.json``.

    Uses ``setdefault`` semantics — never overwrites a caller-supplied
    value, silent no-op when ``meta.json`` is absent or has no
    ``experiment_id``. Mutates and returns *spec* for clarity.
    """
    meta = read_meta_json(experiment_dir)
    if meta is None:
        return spec
    experiment_id = meta.get("experiment_id")
    if not experiment_id:
        return spec
    spec.setdefault("profile", experiment_id)
    spec.setdefault("job_name", experiment_id)
    return spec


# ─── subcommand: submit ────────────────────────────────────────────────────


def cmd_submit(args: argparse.Namespace) -> int:
    # Load without schema validation so ``--from-meta`` can fill missing
    # required fields (profile/job_name) before the schema check rejects
    # an otherwise-partial spec.
    spec = _load_spec(args.spec, schema_name=None)
    if getattr(args, "from_meta", False):
        spec = _overlay_meta_on_spec(spec, args.experiment_dir)
    _validate_against_schema(spec, "submit")
    required = (
        "profile",
        "cluster",
        "ssh_target",
        "remote_path",
        "job_name",
        "run_id",
        "job_ids",
        "total_tasks",
    )
    missing = [k for k in required if k not in spec]
    if missing:
        raise errors.SpecInvalid(
            f"--spec missing required fields: {missing}. See docs/reference/cli-spec.md."
        )

    if args.dry_run:
        _ok(
            {
                "would_launch": int(spec["total_tasks"]),
                "profile": spec["profile"],
                "cluster": spec["cluster"],
                "run_id": spec["run_id"],
                "dry_run": True,
            },
            name="submit-spec",
        )
        return EXIT_OK

    record, deduped = runner.submit_and_record(
        args.experiment_dir,
        profile=spec["profile"],
        cluster=spec["cluster"],
        ssh_target=spec["ssh_target"],
        remote_path=spec["remote_path"],
        job_name=spec["job_name"],
        job_ids=list(spec["job_ids"]),
        total_tasks=int(spec["total_tasks"]),
        run_id=spec["run_id"],
        campaign_id=spec.get("campaign_id") or "",
    )
    _ok(
        {
            "run_id": record.run_id,
            "job_ids": record.job_ids,
            "total_tasks": record.total_tasks,
            "deduped": deduped,
        },
        name="submit-spec",  # honest now that submit_and_record dedups
    )
    return EXIT_OK


# ─── subcommand: submit-flow ───────────────────────────────────────────────


def cmd_submit_flow(args: argparse.Namespace) -> int:
    """Workflow atom — pre-flight + rsync + deploy + qsub + record in one shot.

    See ``claude_hpc/job/submit_flow.py`` for the pipeline contract
    and ``schemas/submit_flow.{input,output}.json`` for the envelope
    shapes. Idempotent on ``run_id`` via the same dedup mechanism as
    ``submit``.
    """
    from claude_hpc.orchestrator.submit_flow import submit_flow

    spec = _load_spec(args.spec, schema_name=None)
    # Surface --partial-ok at the CLI in addition to spec.partial_ok so a
    # caller can opt in via either path. Flag wins over spec when both
    # are set (CLI is the more explicit override).
    if getattr(args, "partial_ok", False):
        spec = dict(spec)
        spec["partial_ok"] = True
    _validate_against_schema(spec, "submit_flow")

    if args.dry_run:
        _ok(
            {
                "would_launch": int(spec["total_tasks"]),
                "profile": spec["profile"],
                "cluster": spec["cluster"],
                "run_id": spec["run_id"],
                "canary": bool(spec.get("canary", True)),
                "dry_run": True,
            },
            name="submit-flow",
        )
        return EXIT_OK

    result = submit_flow(
        experiment_dir=args.experiment_dir,
        profile=spec["profile"],
        cluster=spec["cluster"],
        ssh_target=spec["ssh_target"],
        remote_path=spec["remote_path"],
        job_name=spec["job_name"],
        run_id=spec["run_id"],
        total_tasks=int(spec["total_tasks"]),
        backend=spec["backend"],
        script=spec["script"],
        job_env=dict(spec["job_env"]),
        pass_env_keys=spec.get("pass_env_keys"),
        canary=bool(spec.get("canary", True)),
        campaign_id=spec.get("campaign_id") or "",
        runtime=spec.get("runtime"),
        rsync_excludes=spec.get("rsync_excludes"),
        skip_preflight=bool(spec.get("skip_preflight", False)),
        slurm_account=spec.get("slurm_account"),
        slurm_cluster=spec.get("slurm_cluster"),
        partial_ok=bool(spec.get("partial_ok", False)),
    )
    _ok(result.to_envelope_data(), name="submit-flow")
    return EXIT_OK


# ─── subcommand: monitor-flow ──────────────────────────────────────────────


def cmd_monitor_flow(args: argparse.Namespace) -> int:
    """Workflow atom — poll a run to terminal-or-budget; auto-combine waves.

    See ``claude_hpc/job/monitor_flow.py`` for the loop contract and
    ``schemas/monitor_flow.{input,output}.json`` for the envelope shapes.
    Internal poll loop runs to terminal lifecycle, wall-clock budget,
    or escalation; emits one envelope at the end. Pairs with
    ``submit-flow`` for the campaign composition pattern
    ``submit-flow → monitor-flow → next iteration``.
    """
    from claude_hpc.orchestrator.monitor_flow import monitor_flow

    spec = _load_spec(args.spec, schema_name=None)
    _validate_against_schema(spec, "monitor_flow")

    if args.dry_run:
        _ok(
            {
                "run_id": spec["run_id"],
                "poll_interval_seconds": spec.get("poll_interval_seconds", 60),
                "wall_clock_budget_seconds": spec.get("wall_clock_budget_seconds", 86400),
                "auto_combine_waves": spec.get("auto_combine_waves", True),
                "dry_run": True,
            },
            name="monitor-flow",
        )
        return EXIT_OK

    result = monitor_flow(
        experiment_dir=args.experiment_dir,
        run_id=spec["run_id"],
        poll_interval_seconds=float(spec.get("poll_interval_seconds", 60.0)),
        wall_clock_budget_seconds=float(spec.get("wall_clock_budget_seconds", 86400.0)),
        auto_combine_waves=bool(spec.get("auto_combine_waves", True)),
        combiner_max_retries=int(spec.get("combiner_max_retries", 1)),
        file_glob=spec.get("file_glob", "*"),
    )
    _ok(result.to_envelope_data(), name="monitor-flow")
    return EXIT_OK


# ─── subcommand: aggregate-flow ────────────────────────────────────────────


def cmd_aggregate_flow(args: argparse.Namespace) -> int:
    """Workflow atom — ensure all waves combined, pull partials, reduce locally.

    See ``claude_hpc/job/aggregate_flow.py`` for the pipeline contract
    and ``schemas/aggregate_flow.{input,output}.json`` for the envelope
    shapes. Pairs with submit-flow + monitor-flow as the third workflow
    atom — the campaign loop's per-iteration tail is
    ``submit-flow → monitor-flow → aggregate-flow → next iter``.
    """
    from claude_hpc.orchestrator.aggregate_flow import aggregate_flow

    spec = _load_spec(args.spec, schema_name=None)
    _validate_against_schema(spec, "aggregate_flow")

    if args.dry_run:
        _ok(
            {
                "run_id": spec["run_id"],
                "ensure_all_combined": spec.get("ensure_all_combined", True),
                "pull_summaries": spec.get("pull_summaries", False),
                "output_dir": spec.get("output_dir"),
                "dry_run": True,
            },
            name="aggregate-flow",
        )
        return EXIT_OK

    result = aggregate_flow(
        experiment_dir=args.experiment_dir,
        run_id=spec["run_id"],
        output_dir=spec.get("output_dir"),
        ensure_all_combined=bool(spec.get("ensure_all_combined", True)),
        combiner_max_retries=int(spec.get("combiner_max_retries", 1)),
        pull_summaries=bool(spec.get("pull_summaries", False)),
        summary_glob=spec.get("summary_glob"),
        results_subdir=spec.get("results_subdir", "results"),
    )
    _ok(result.to_envelope_data(), name="aggregate-flow")
    return EXIT_OK


# ─── subcommand: aggregate ─────────────────────────────────────────────────


# Re-exported from claude_hpc.atoms.failures for back-compat with the
# auto-retry resolver test suite, which imports the helper directly.
from claude_hpc.atoms.failures import _resolve_auto_retry  # noqa: E402,F401


def _sidecar_aggregate_defaults(experiment_dir: Path, run_id: str) -> dict[str, str]:
    """Read ``aggregate_defaults.{require_outputs,expect_output}`` from the run sidecar.

    Returns an empty dict when the sidecar is missing, malformed, or has
    no ``aggregate_defaults`` block. Silent failure is intentional —
    config validity is enforced by ``/submit``, not the aggregate path.
    """
    try:
        from claude_hpc.orchestrator.runs import read_run_sidecar
    except ImportError:
        return {}
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    block = sidecar.get("aggregate_defaults") or {}
    if not isinstance(block, dict):
        return {}
    return {
        k: block[k] for k in ("require_outputs", "expect_output") if isinstance(block.get(k), str)
    }


def cmd_aggregate(args: argparse.Namespace) -> int:
    # The aggregation pipeline is driven by claude_hpc.orchestrator.runner.combine_wave
    # plus the user-supplied combiner script on the cluster. The CLI wraps it
    # with three optional, framework-agnostic guarantees:
    #   --require-outputs <template>  : every per-task output exists before
    #                                   the combiner runs (precondition)
    #   --expect-output <path>        : the combiner produced a parseable
    #                                   artifact at <path> (postcondition)
    #   provenance                    : metadata block in envelope.data and
    #                                   sidecar file when --expect-output set
    # Defaults for require/expect can be set per-run in the sidecar's
    # ``aggregate_defaults`` block, populated by /submit. CLI flags win.
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for run_id {args.run_id!r}")
    if args.wave is None:
        raise errors.SpecInvalid("aggregate requires --wave <int>")

    # Resolve aggregate flags: explicit CLI > sidecar aggregate_defaults > none.
    # ``getattr`` keeps in-process callers (tests, slash-command wrappers)
    # working even when they hand-build a Namespace without these keys.
    defaults = _sidecar_aggregate_defaults(args.experiment_dir, args.run_id)
    require_outputs = getattr(args, "require_outputs", None) or defaults.get("require_outputs")
    expect_output = getattr(args, "expect_output", None) or defaults.get("expect_output")

    # Precondition: every per-task output must exist before we combine.
    if require_outputs:
        missing = runner.verify_per_task_outputs(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            run_id=args.run_id,
            wave=int(args.wave),
            template=require_outputs,
        )
        if missing:
            preview = missing[:10]
            ellipsis = "..." if len(missing) > 10 else ""
            return _err_from_hpc(
                errors.OutputsMissing(
                    f"{len(missing)} per-task output(s) missing for wave "
                    f"{args.wave}: {preview}{ellipsis}",
                )
            )

    ok, stdout, stderr = runner.combine_wave(
        args.experiment_dir,
        args.run_id,
        wave=int(args.wave),
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        force=args.force,
    )
    if ok:
        # Postcondition: the combiner must have produced the declared file.
        if expect_output:
            artifact_ok, detail = runner.verify_combiner_artifact(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                expect_output=expect_output,
            )
            if not artifact_ok:
                return _err_from_hpc(
                    errors.CombinerFailed(
                        f"combiner returned 0 but expected output {expect_output!r} {detail}",
                    )
                )

        provenance = runner.build_provenance(record, wave=int(args.wave))
        sidecar_path: str | None = None
        if expect_output:
            try:
                sidecar_path = runner.write_remote_provenance(
                    ssh_target=record.ssh_target,
                    remote_path=record.remote_path,
                    expect_output=expect_output,
                    provenance=provenance,
                )
            except errors.RemoteCommandFailed:
                # Best-effort — envelope still carries provenance.
                sidecar_path = None

        data: dict[str, Any] = {
            "run_id": args.run_id,
            "wave": int(args.wave),
            "combined": True,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
            "provenance": provenance,
        }
        if sidecar_path is not None:
            data["provenance_sidecar"] = sidecar_path
        _ok(data, idempotent=True)
        return EXIT_OK
    # Combiner returned non-zero — surface as a typed error so the
    # envelope's ``ok`` field and the exit code stay in sync.  Tail of
    # stderr was already in the success payload; here we put it in the
    # human-readable message so the caller can grep it.
    return _err_from_hpc(
        errors.CombinerFailed(
            f"combiner returned non-zero for wave {args.wave}; stderr tail: {stderr[-500:]!r}"
        )
    )


# ─── subcommand: resubmit ──────────────────────────────────────────────────


# Canonical failure-category vocabulary. Must be the UNION of:
#   - the auto-classifier in claude_hpc.orchestrator.runner.cluster_failures_by_fingerprint
#     (gpu_oom, system_oom, walltime, node_failure, import_error,
#      file_not_found, permission_denied, disk_full, python_traceback)
#   - the human-supplied taxonomy here (segv, queue_stall, code_bug, unknown)
# A test in tests/test_resubmit.py asserts the classifier never emits a
# category outside this set.
# B2: derived from the canonical FailureCategory StrEnum.
# Pre-B2 this was a literal frozenset that drifted from the classifier
# emissions in claude_hpc.orchestrator.runner; A4 landed the union as a literal,
# B2 makes the literal redundant by sourcing from the StrEnum so the
# drift class cannot recur. test_lifecycle.py asserts the cross-set
# invariants (classifier emissions ⊆ accepted ⊆ FailureCategory).
from claude_hpc._internal.lifecycle import FailureCategory as _FailureCategory  # noqa: E402

_VALID_RESUBMIT_CATEGORIES = frozenset({fc.value for fc in _FailureCategory})


def cmd_resubmit(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec, schema_name="resubmit")
    failed = spec.get("failed_task_ids")
    category = spec.get("category")
    if not isinstance(failed, list) or not failed:
        raise errors.SpecInvalid("--spec.failed_task_ids must be a non-empty list")
    if not isinstance(category, str):
        raise errors.SpecInvalid("--spec.category must be a string")
    # Belt-and-braces: schema validation also enforces this enum, but
    # ``_validate_against_schema`` is a no-op when ``jsonschema`` is not
    # installed.  Keep the local check so the seven-category contract
    # holds either way.
    if category not in _VALID_RESUBMIT_CATEGORIES:
        raise errors.SpecInvalid(
            f"--spec.category must be one of {sorted(_VALID_RESUBMIT_CATEGORIES)}; got {category!r}"
        )

    # A-M3: surface preempted runs as a typed Preempted exception at
    # the envelope level. If every task_id the caller is trying to
    # resubmit carries a ``preempt`` marker in the per-task sidecar
    # (set by dispatch.py's SIGTERM handler), the campus user wasn't
    # the one who failed — they got bumped by higher-priority work.
    # Raising Preempted lets the agent harness branch on
    # ``error_code: preempted`` instead of seeing a successful resubmit
    # envelope and treating it like any other retry. The resubmit
    # itself still happens after the harness handles the signal; we
    # raise BEFORE doing the cluster-side work so the caller can
    # decide whether to throttle.
    if category == "preempted":
        from claude_hpc.orchestrator.runs import read_run_sidecar as _read_sidecar

        try:
            sidecar = _read_sidecar(Path(args.experiment_dir), args.run_id)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            sidecar = None
        if sidecar is not None:
            tasks_block = sidecar.get("tasks") or {}
            failed_ids_int = [int(t) for t in failed]
            all_preempted = bool(failed_ids_int) and all(
                isinstance(tasks_block.get(str(tid)), dict)
                and "preempt" in tasks_block.get(str(tid), {})
                for tid in failed_ids_int
            )
            if all_preempted:
                raise errors.Preempted(
                    f"all {len(failed_ids_int)} task ids in resubmit spec carry "
                    "preempt markers; the campus user got bumped by higher-priority "
                    "work, not failed. Resubmit when scheduler pressure abates."
                )

    record, deduped, request_id = runner.resubmit_failed(
        args.experiment_dir,
        args.run_id,
        failed_task_ids=[int(t) for t in failed],
        category=category,
        overrides=spec.get("overrides"),
        new_job_ids=spec.get("new_job_ids"),
        request_id=spec.get("request_id"),
    )
    _ok(
        {
            "run_id": record.run_id,
            "retries": record.retries,
            "job_ids": record.job_ids,
            "request_id": request_id,
            "deduped": deduped,
        },
        # Honest now that resubmit_failed dedups on request_id: a replay
        # with the same spec is a no-op, just like submit.
        name="resubmit-failed",
    )
    return EXIT_OK


# ─── subcommand: reconcile ─────────────────────────────────────────────────


@primitive(
    name="reconcile-journal",
    verb="mutate",
    side_effects=[
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock)",
        ),
        SideEffect("ssh", "<cluster>"),
    ],
    error_codes=[errors.SshUnreachable, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="run_id",
)
def cmd_reconcile(args: argparse.Namespace) -> int:
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = runner.reconcile(
        args.experiment_dir,
        args.run_id,
        scheduler=args.scheduler,
    )
    _ok(
        {
            "run_id": record.run_id,
            "lifecycle_state": record.status,
            "combined_waves": record.combined_waves,
            "failed_waves": record.failed_waves,
            "last_status": record.last_status,
        },
        name="reconcile-journal",
    )
    return EXIT_OK


# ─── subcommand: logs ──────────────────────────────────────────────────────


def cmd_logs(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.logs.

    Two ways to select tasks:
      --task-id 7,12,42   explicit list
      --all-failed        re-poll status, fetch logs for failed tasks
    """
    if (rc := _require_ssh_agent()) is not None:
        return rc

    from claude_hpc.atoms.logs import fetch_logs

    # Parse the user-facing comma-separated --task-id at the adapter
    # boundary; the atom takes a typed list[int].
    task_ids: list[int] | None = None
    if not getattr(args, "all_failed", False) and args.task_id:
        try:
            task_ids = [int(t.strip()) for t in args.task_id.split(",") if t.strip()]
        except ValueError as exc:
            raise errors.SpecInvalid(f"--task-id must be comma-separated integers: {exc}") from exc
        if not task_ids:
            raise errors.SpecInvalid("--task-id is empty")

    data = fetch_logs(
        experiment_dir=args.experiment_dir,
        run_id=args.run_id,
        task_ids=task_ids,
        all_failed=bool(getattr(args, "all_failed", False)),
        lines=int(getattr(args, "lines", 50) or 50),
    )
    _ok(data, name="logs")
    return EXIT_OK


# ─── subcommand: failures ──────────────────────────────────────────────────


def cmd_failures(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at claude_hpc.atoms.failures.

    Cluster failed tasks by stderr fingerprint so 40 failures with the
    same root cause show up as one cluster instead of 40 separate logs
    to read.
    """
    if (rc := _require_ssh_agent()) is not None:
        return rc

    from claude_hpc.atoms.failures import fetch_failures

    _ok(
        fetch_failures(
            experiment_dir=args.experiment_dir,
            run_id=args.run_id,
            lines=int(getattr(args, "lines", 30) or 30),
        ),
        name="failures",
    )
    return EXIT_OK


# ─── subcommand: campaign-health ───────────────────────────────────────────


def cmd_campaign_health(args: argparse.Namespace) -> int:
    """Aggregate run-history into a campaign-health payload (D2a).

    Thin CLI wrapper. The ``@primitive(name="campaign-health", ...)``
    decorator lives on ``claude_hpc.orchestrator.campaign_health.campaign_health``
    (the module-level implementation), matching the ``backed_by.python``
    pointer in ``docs/primitives/campaign-health.md``.
    """
    from claude_hpc.orchestrator.campaign_health import campaign_health

    try:
        data = campaign_health(
            args.experiment_dir,
            campaign_id=args.campaign_id,
            since_iso=args.since_iso,
            profile=args.profile,
            cluster=args.cluster,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort error envelope
        return _err(
            error_code="internal",
            message=f"campaign_health failed: {exc}",
            category="internal",
            retry_safe=False,
        )
    _ok(data, name="campaign-health")
    return EXIT_OK


# ─── subcommand: build-executor ────────────────────────────────────────────


@primitive(
    name="build-executor",
    verb="scaffold",
    side_effects=[
        SideEffect(
            "writes-file",
            "<output_dir>/<name>.py (refuses to overwrite without --force)",
        ),
    ],
    idempotent=False,
)
def cmd_build_executor(args: argparse.Namespace) -> int:
    starters = claude_hpc._PACKAGE_ROOT / "mapreduce" / "templates" / "starters"
    template_map = {
        "plain": starters / "executor_template.py",
    }
    if args.type not in template_map:
        raise errors.SpecInvalid(
            f"unknown --type {args.type!r}; choose from {sorted(template_map)}"
        )
    src = template_map[args.type]
    if not src.exists():
        raise errors.ConfigInvalid(f"template missing on disk: {src}")
    dest = (args.output_dir / args.name).with_suffix(".py")
    if dest.exists() and not args.force:
        raise errors.SpecInvalid(f"refusing to overwrite {dest}; pass --force to overwrite")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    _ok(
        {"path": str(dest.resolve()), "type": args.type, "source": str(src)},
        name="build-executor",
    )
    return EXIT_OK


# ─── parser ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hpc-mapreduce",
        description=(
            "Submit, track status of, and aggregate parameter-grid HPC experiments. "
            "Stdout is a single-line JSON envelope; stderr is JSON-per-line "
            "log records. See docs/reference/cli-spec.md for full schemas."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {claude_hpc.__version__}",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # capabilities
    p_cap = sub.add_parser(
        "capabilities",
        help="Machine-readable feature flags: subcommands, schedulers, schema dirs.",
    )
    p_cap.add_argument(
        "--full",
        action="store_true",
        help=(
            "Emit a plain-text llms-full dump (catalog + every primitive doc + "
            "schemas + envelope + boundary contract + cli-spec). Exception to the "
            "stdout-is-JSON contract; intended for one-shot LLM context loading."
        ),
    )
    p_cap.set_defaults(func=cmd_capabilities)

    # preflight
    p_pre = sub.add_parser(
        "preflight",
        help="Health check: SSH agent, ssh/rsync on PATH, clusters.yaml parses.",
    )
    p_pre.add_argument("--cluster", help="Optional cluster name to TCP-probe on :22.")
    p_pre.set_defaults(func=cmd_preflight)

    # interview
    p_iv = sub.add_parser(
        "interview",
        help=(
            "Validate an agent-written tasks.py against the structured intent "
            "from an interview, then persist intent + cmd_sha + dry-resolve "
            "preview to <campaign-dir>/interview.json."
        ),
    )
    p_iv.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="Path to interview.input.json conforming to schemas/interview.input.json.",
    )
    p_iv.add_argument(
        "--campaign-dir",
        required=True,
        help=(
            "Campaign workdir; must already contain a tasks.py written by the "
            "interview agent. interview.json (and optionally meta.json) is "
            "written into this directory."
        ),
    )
    p_iv.set_defaults(func=cmd_interview)

    # recall
    p_rc = sub.add_parser(
        "recall",
        help=(
            "Query past interview.json files under --root. Returns "
            "recency-sorted campaign summaries (goal, task_kind, "
            "task_count, operator, materialized_at, cmd_sha) for use as "
            "context in the next interview."
        ),
    )
    p_rc.add_argument(
        "--root",
        help=(
            "Filesystem directory to walk recursively for interview.json. "
            "When omitted, falls back to ~/.claude-hpc/config.json:"
            "experiment_roots; if neither is set, errors."
        ),
    )
    p_rc.add_argument(
        "--task-kind",
        help="Exact-match filter against intent.task_kind.",
    )
    p_rc.add_argument(
        "--operator",
        help="Exact-match filter against intent.produced_by.operator.",
    )
    p_rc.add_argument(
        "--since",
        help="ISO-8601 timestamp; only campaigns materialized at or after this point are returned.",
    )
    p_rc.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of summaries to return (default 20).",
    )
    p_rc.add_argument(
        "--include-runtime",
        action="store_true",
        help=(
            "Tier 2 rollup: walk each matched campaign's .hpc/runtimes/*.json "
            "and aggregate elapsed_sec / failure rate across all dispatched tasks."
        ),
    )
    p_rc.add_argument(
        "--include-generator-stats",
        action="store_true",
        help=(
            "Tier 3 rollup: bucket by task_generator.kind and report observed "
            "parameter envelopes. Most useful with --task-kind also set."
        ),
    )
    p_rc.set_defaults(func=cmd_recall)

    # discover
    p_disc = sub.add_parser(
        "discover",
        help="List executor scripts in --experiment-dir (CLIs with __main__).",
    )
    _add_experiment_dir(p_disc)
    p_disc.set_defaults(func=cmd_discover)

    # inspect-cluster
    p_ic = sub.add_parser(
        "inspect-cluster",
        help=(
            "Snapshot a cluster's per-node state (alloc mem, CPU load, "
            "co-tenants, drain). Read-only; output is the planner's input."
        ),
    )
    p_ic.add_argument("--cluster", required=True, help="Cluster name from clusters.yaml.")
    p_ic.add_argument(
        "--sacct-window-hours",
        type=int,
        default=24,
        help="Look-back window for co-tenant attribution (default 24h).",
    )
    p_ic.add_argument(
        "--stress-alloc-mem-pct",
        type=float,
        default=0.80,
        help="AllocMem fraction above which a node is flagged is_stressed.",
    )
    p_ic.add_argument(
        "--stress-cpu-load-frac",
        type=float,
        default=0.80,
        help="CPULoad/CPUTot fraction above which a node is flagged is_stressed.",
    )
    p_ic.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the 60s in-process cache and re-poll the cluster.",
    )
    p_ic.set_defaults(func=cmd_inspect_cluster)

    # plan-submit
    p_ps = sub.add_parser(
        "plan-submit",
        help=(
            "Score candidate constraints for a submit. Combines inspect-cluster "
            "and runtime priors. Output is JSON the slash command hands to "
            "Claude for cost-model judgment."
        ),
    )
    _add_experiment_dir(p_ps)
    _add_profile_cluster_cmdsha(p_ps)
    p_ps.add_argument(
        "--candidates",
        default=None,
        help=(
            "Optional comma-separated list of constraint expressions to evaluate "
            "(e.g. 'a100,a40|a100,a40|a100|v100'). Defaults to single-GPU + "
            "all-GPU-types from clusters.yaml."
        ),
    )
    p_ps.add_argument(
        "--no-adversarial",
        action="store_true",
        help=(
            "Disable the default backfill-attack mode. By default, plan-submit "
            "right-sizes the walltime ask from runtime priors and probes a "
            "(walltime × constraint) lattice via `sbatch --test-only` to find "
            "the variant SLURM predicts will start earliest. Pass this flag "
            "for debugging or on clusters that throttle --test-only. With "
            "<5 prior samples per GPU type the right-sizing falls back to "
            "the default walltime regardless."
        ),
    )
    p_ps.add_argument(
        "--walltime-safety-mult",
        type=float,
        default=1.30,
        help=(
            "Multiplier applied to the runtime prior's p95 to derive the "
            "right-sized walltime ask. Default 1.30 (30%% pad). Lower = more "
            "aggressive backfill targeting at higher risk of cliff-kill."
        ),
    )
    p_ps.add_argument(
        "--target-backfill-window-sec",
        type=int,
        default=None,
        help=(
            "Adversarial knob: if you've observed a typical backfill gap size "
            "on this cluster (e.g., 1800 for 30 minutes), pass it here. "
            "Triggers array-reshape and walltime-split recommendations sized "
            "to fit that window."
        ),
    )
    p_ps.add_argument(
        "--current-max-array-size",
        type=int,
        default=None,
        help=(
            "Adversarial array-reshape input: the cluster's currently "
            "configured max array size. When supplied (with optionally "
            "--target-backfill-window-sec and --est-per-task-sec), the "
            "report includes a `array_reshape` recommendation."
        ),
    )
    p_ps.add_argument(
        "--est-per-task-sec",
        type=int,
        default=None,
        help=(
            "Adversarial knob: estimated per-task runtime (typically the "
            "p95 from `runtime-prior`). Used by array-reshape and "
            "walltime-split recommendations."
        ),
    )
    p_ps.set_defaults(func=cmd_plan_submit)

    # runtime-prior
    p_rp = sub.add_parser(
        "runtime-prior",
        help="Quantile rollup of runtime samples for a (profile, cluster).",
    )
    _add_experiment_dir(p_rp)
    _add_profile_cluster_cmdsha(
        p_rp,
        cmd_sha_help=("Filter samples to one cmd_sha (recommended after .hpc/tasks.py changes)."),
    )
    p_rp.set_defaults(func=cmd_runtime_prior)

    # walltime-drift
    p_wd = sub.add_parser(
        "walltime-drift",
        help=(
            "Closed-loop calibration: measure cliff-kill rate from past "
            "samples and recommend an adjusted safety_mult per cluster."
        ),
    )
    _add_experiment_dir(p_wd)
    _add_profile_cluster_cmdsha(p_wd)
    p_wd.add_argument("--base-safety-mult", type=float, default=1.30)
    p_wd.set_defaults(func=cmd_walltime_drift)

    # house-edge
    p_he = sub.add_parser(
        "house-edge",
        help=(
            "Compare planner's --test-only predictions against observed "
            "Submit→Start deltas. Validates that the lattice probe is "
            "finding real backfill windows and surfaces miscalibration."
        ),
    )
    _add_experiment_dir(p_he)
    _add_profile_cluster_cmdsha(p_he)
    p_he.set_defaults(func=cmd_house_edge)

    # predict-queue-wait
    p_pqw = sub.add_parser(
        "predict-queue-wait",
        help=(
            "Forecast queue-wait seconds for a hypothetical submit. "
            "Backend 'auto' picks DES when a snapshot + user-profiles "
            "are available; falls back to the diurnal MA baseline."
        ),
    )
    _add_experiment_dir(p_pqw)
    p_pqw.add_argument("--profile", required=True)
    p_pqw.add_argument("--cluster", required=True)
    p_pqw.add_argument("--at-iso", default=None, help="reference timestamp (default: now)")
    p_pqw.add_argument("--backend", choices=["auto", "diurnal_ma", "des"], default="auto")
    p_pqw.add_argument(
        "--n-replications",
        type=int,
        default=64,
        help="DES replications (only used on the DES path)",
    )
    p_pqw.add_argument("--seed", type=int, default=None, help="seed for deterministic DES sampling")
    p_pqw.set_defaults(func=cmd_predict_queue_wait)

    # best-submit-window
    p_bsw = sub.add_parser(
        "best-submit-window",
        help=(
            "Sweep the diurnal queue-wait predictor over the next "
            "--within-hours hours and surface the top_k lowest-wait "
            "submit candidates. Used by /submit-hpc Step 4c to advise "
            "submit-now vs. wait-for-window."
        ),
    )
    _add_experiment_dir(p_bsw)
    p_bsw.add_argument("--profile", required=True)
    p_bsw.add_argument("--cluster", required=True)
    p_bsw.add_argument("--within-hours", type=int, default=24)
    p_bsw.add_argument("--top-k", type=int, default=5)
    p_bsw.set_defaults(func=cmd_best_submit_window)

    # clusters
    p_cl = sub.add_parser("clusters", help="Introspect available cluster definitions.")
    p_cl_sub = p_cl.add_subparsers(dest="clusters_cmd", required=True)
    p_cl_list = p_cl_sub.add_parser("list", help="List all clusters.")
    p_cl_list.set_defaults(func=cmd_clusters_list)
    p_cl_desc = p_cl_sub.add_parser("describe", help="Print one cluster's config.")
    p_cl_desc.add_argument("name")
    p_cl_desc.set_defaults(func=cmd_clusters_describe)

    # list-in-flight
    p_lif = sub.add_parser(
        "list-in-flight",
        help="List runs with status=in_flight in the journal (recovery path).",
    )
    _add_experiment_dir(p_lif)
    p_lif.set_defaults(func=cmd_list_in_flight)

    # campaign — closed-loop campaign read-only commands
    p_camp = sub.add_parser(
        "campaign",
        help="Closed-loop campaign read-only commands (status, list).",
    )
    p_camp_sub = p_camp.add_subparsers(dest="action", required=True)

    p_camp_st = p_camp_sub.add_parser(
        "status",
        help=(
            "Report per-iteration reduced metrics for one campaign. "
            "Walks every sidecar tagged with --campaign-id, runs "
            "reduce_metrics on each, and emits the history dict-list."
        ),
    )
    _add_experiment_dir(p_camp_st)
    p_camp_st.add_argument("--campaign-id", required=True)
    p_camp_st.set_defaults(func=cmd_campaign_status)

    p_camp_ls = p_camp_sub.add_parser(
        "list",
        help="List every campaign with at least one sidecar in this experiment.",
    )
    _add_experiment_dir(p_camp_ls)
    p_camp_ls.set_defaults(func=cmd_campaign_list)

    # status
    p_st = sub.add_parser(
        "status", help="Poll cluster status for a run_id; one-shot, returns snapshot."
    )
    _add_experiment_dir(p_st)
    _add_run_id(p_st)
    p_st.set_defaults(func=cmd_status)

    # submit
    p_sub = sub.add_parser(
        "submit",
        help=(
            "Record a submission in the journal. Idempotent on run_id: "
            "the bundled atomic-ops layer dedups so a retry on transient "
            "network errors does not double-submit."
        ),
    )
    _add_experiment_dir(p_sub)
    p_sub.add_argument("--spec", type=Path, required=True, help="JSON spec file")
    p_sub.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the spec and report what would be launched; no SSH/qsub.",
    )
    p_sub.add_argument(
        "--from-meta",
        action="store_true",
        help=(
            "Overlay missing 'profile' and 'job_name' on the spec from "
            "<experiment-dir>/meta.json `experiment_id`. setdefault "
            "semantics — never overwrites caller-supplied values; silent "
            "no-op when meta.json is absent."
        ),
    )
    p_sub.set_defaults(func=cmd_submit)

    # submit-flow
    p_sf = sub.add_parser(
        "submit-flow",
        help=(
            "Workflow atom: pre-flight + rsync + deploy + qsub + record in "
            "one shot. Lets higher-level workflows (campaigns, sweeps) "
            "compose the submit pipeline as a single CLI call instead of "
            "agent-driving /submit-hpc. Idempotent on run_id."
        ),
    )
    p_sf.add_argument(
        "--partial-ok",
        action="store_true",
        help=(
            "Tolerate per-task failures: when the wave finishes, classify "
            "as `complete` if at least one task succeeded; record failed "
            "task IDs in <run_id>.failed.json so aggregate-flow can skip "
            "them. Without this flag (the default), any failure aborts the "
            "wave with lifecycle_state=failed."
        ),
    )
    _add_experiment_dir(p_sf)
    _add_spec_and_dry_run(
        p_sf,
        schema_hint="schemas/submit_flow.input.json",
        dry_run_help="Validate the spec and report what would be launched; no SSH/rsync/qsub.",
    )
    p_sf.set_defaults(func=cmd_submit_flow)

    # monitor-flow
    p_mf = sub.add_parser(
        "monitor-flow",
        help=(
            "Workflow atom: poll a run to terminal lifecycle (or wall-clock "
            "budget); auto-combine waves as they finish; write the same "
            ".monitor.jsonl tick log /monitor-hpc writes. Pairs with "
            "submit-flow for the campaign loop composition. MVP does not "
            "auto-resubmit failed tasks."
        ),
    )
    _add_experiment_dir(p_mf)
    _add_spec_and_dry_run(
        p_mf,
        schema_hint="schemas/monitor_flow.input.json",
        dry_run_help="Validate the spec and report what would be polled; no SSH.",
    )
    p_mf.set_defaults(func=cmd_monitor_flow)

    # aggregate-flow
    p_af = sub.add_parser(
        "aggregate-flow",
        help=(
            "Workflow atom: ensure all waves combined on the cluster, "
            "rsync the _combiner/ partials locally, reduce_partials over "
            "them, optionally pull per-task summaries. Third atom in the "
            "submit-flow → monitor-flow → aggregate-flow campaign chain."
        ),
    )
    _add_experiment_dir(p_af)
    _add_spec_and_dry_run(
        p_af,
        schema_hint="schemas/aggregate_flow.input.json",
        dry_run_help="Validate the spec and report what would be aggregated; no SSH.",
    )
    p_af.set_defaults(func=cmd_aggregate_flow)

    # aggregate
    p_agg = sub.add_parser(
        "aggregate",
        help="Run the on-cluster combiner for one wave; records outcome to journal.",
    )
    _add_experiment_dir(p_agg)
    _add_run_id(p_agg)
    p_agg.add_argument("--wave", type=int, required=True)
    p_agg.add_argument(
        "--force",
        action="store_true",
        help="Re-run the combiner even if the wave appears combined.",
    )
    p_agg.add_argument(
        "--require-outputs",
        default=None,
        help=(
            "Path template (with {task_id}) checked on the cluster before "
            "the combiner runs. Refuses to combine if any task in this "
            "wave is missing its expected output. Default reads from the "
            "run sidecar's aggregate_defaults.require_outputs."
        ),
    )
    p_agg.add_argument(
        "--expect-output",
        default=None,
        help=(
            "Remote path (relative to remote_path) that the combiner must "
            "produce. Verified after the combiner exits 0; .json files "
            "are also checked for parseability. Default reads from the "
            "run sidecar's aggregate_defaults.expect_output."
        ),
    )
    p_agg.set_defaults(func=cmd_aggregate)

    # resubmit
    p_rs = sub.add_parser(
        "resubmit",
        help="Record a resubmission attempt in the journal (caller does the actual qsub).",
    )
    _add_experiment_dir(p_rs)
    _add_run_id(p_rs)
    p_rs.add_argument("--spec", type=Path, required=True)
    p_rs.set_defaults(func=cmd_resubmit)

    # reconcile
    p_rec = sub.add_parser(
        "reconcile",
        help="Re-derive ground truth from the cluster (status, waves, alive jobs).",
    )
    _add_experiment_dir(p_rec)
    _add_run_id(p_rec)
    p_rec.add_argument(
        "--scheduler",
        required=True,
        choices=["sge", "slurm"],
        help="Scheduler family — needed to query alive job IDs.",
    )
    p_rec.set_defaults(func=cmd_reconcile)

    # logs
    p_logs = sub.add_parser(
        "logs",
        help="Fetch per-task stderr logs from the cluster (requires --task-id or --all-failed).",
    )
    _add_experiment_dir(p_logs)
    _add_run_id(p_logs)
    p_logs.add_argument(
        "--task-id",
        default=None,
        help="Comma-separated task ids to fetch (e.g. '7,12,42').",
    )
    p_logs.add_argument(
        "--all-failed",
        action="store_true",
        help="Re-poll status and fetch logs for every task with status=failed.",
    )
    p_logs.add_argument(
        "--lines",
        type=int,
        default=50,
        help="Number of trailing lines to return per log (default 50).",
    )
    p_logs.set_defaults(func=cmd_logs)

    # failures
    p_fail = sub.add_parser(
        "failures",
        help="Cluster failed tasks by stderr fingerprint for triage.",
    )
    _add_experiment_dir(p_fail)
    _add_run_id(p_fail)
    p_fail.add_argument(
        "--lines",
        type=int,
        default=30,
        help="Per-task stderr tail length used for fingerprinting (default 30).",
    )
    p_fail.set_defaults(func=cmd_failures)

    # campaign-health (D2a)
    p_ch = sub.add_parser(
        "campaign-health",
        help=(
            "Structured run-history aggregation for an LLM agent. Returns "
            "walltime cliff rates, failure breakdown, GPU utilization, and "
            "a ready-to-feed-LLM suggested_prompt."
        ),
    )
    _add_experiment_dir(p_ch)
    p_ch.add_argument("--campaign-id", default=None)
    p_ch.add_argument("--since-iso", default=None)
    p_ch.add_argument("--profile", default=None)
    p_ch.add_argument("--cluster", default=None)
    p_ch.set_defaults(func=cmd_campaign_health)

    # build-executor
    p_be = sub.add_parser(
        "build-executor",
        help="Scaffold a new executor from a starter template.",
    )
    p_be.add_argument("--name", required=True, help="Output filename stem (no .py).")
    p_be.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="Where to write the new file (default: CWD).",
    )
    p_be.add_argument(
        "--type",
        default="plain",
        choices=["plain"],
        help=(
            "Which template to instantiate. The only template is 'plain' "
            "(a standard executor scaffold); per-task fan-out lives "
            "inline in .hpc/tasks.py, scaffolded by /submit Step 6."
        ),
    )
    p_be.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the destination file if it already exists.",
    )
    p_be.set_defaults(func=cmd_build_executor)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Populate the primitive registry once before any subcommand
    # dispatch — without this, get_registry() raises RuntimeError
    # (the previous auto-import path silently swallowed ImportError
    # and made missing-decorator bugs hard to diagnose).
    from claude_hpc._internal._primitive import register_primitives

    register_primitives()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc: int = args.func(args)
        return rc
    except errors.HpcError as exc:
        return _err_from_hpc(exc)
    except subprocess.TimeoutExpired as exc:
        # Backend ``qsub``/``sbatch``/``sacct`` exceeded its timeout.
        # Surface as a typed cluster-category error so callers know
        # to retry rather than treat it as user input invalid.
        return _err_from_hpc(
            errors.ClusterTimeout(
                f"scheduler subprocess timed out after {exc.timeout}s: {exc.cmd!r}"
            )
        )
    except ValueError as exc:
        # Route through the canonical errors enum rather than inlining
        # the "spec_invalid" string — keeps error_code values centralised
        # in slash_commands.errors.
        return _err_from_hpc(errors.SpecInvalid(str(exc)))
    except Exception as exc:  # noqa: BLE001 — last-resort envelope
        # The base HpcError carries error_code="internal" + category=
        # "internal" by default, which matches the previous inline
        # values. Wrap so callers see a uniform shape.
        return _err_from_hpc(errors.HpcError(f"{type(exc).__name__}: {exc}"))


if __name__ == "__main__":
    sys.exit(main())
