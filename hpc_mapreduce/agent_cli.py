"""Command-line interface — the agent surface.

Designed to be invoked by automation (MARs orchestrator agents via the
Bash tool, cron, scripts). Conventions:

- Stdout is exclusively a single-line JSON envelope. Exception:
  ``capabilities --full`` emits a plain-text ``llms-full`` dump (one-shot
  LLM context loading, analogous to ``--help``). Every other invocation
  preserves the JSON-envelope contract.
- Stderr carries free-form diagnostic prose (e.g. ``[dispatch] ERROR: …``
  emitted by ``hpc_mapreduce.map.dispatch`` and ``…map.combiner``); it is
  intended for humans tailing logs. Do not parse it as JSON.
- Exit codes: 0 success, 1 user error, 2 cluster/network error, 3 internal.
- Every subcommand accepts ``--experiment-dir`` (defaults to CWD).
- Subcommands with non-trivial inputs accept ``--spec path/to/spec.json``.

The full schema for each subcommand is documented in ``docs/cli-spec.md``
and shipped as JSON Schema files under ``hpc_mapreduce/schemas/``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

import hpc_mapreduce
from hpc_mapreduce._primitive import SideEffect, primitive
from hpc_mapreduce.infra.clusters import load_clusters_config
from hpc_mapreduce.job.discover import (
    detect_mars_tier,
    discover_executors,
    read_meta_json,
)
from slash_commands import errors, runner, session

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_CLUSTER_ERROR = 2
EXIT_INTERNAL = 3

# error_code → exit code mapping. Stable contract; documented in docs/cli-spec.md.
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


def _ok(data: dict[str, Any], *, idempotent: bool) -> None:
    _emit({"ok": True, "idempotent": idempotent, "data": data})


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
                "See docs/mars-integration.md for the Bun.spawn env block."
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
    has one in ``hpc_mapreduce/schemas/<name>.input.json`` should pass
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
    """Validate *payload* against ``hpc_mapreduce/schemas/<schema_name>.input.json``.

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
            _resource_files("hpc_mapreduce.schemas") / f"{schema_name}.input.json"
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


_MARS_SKILL_NAMES = (
    "hpc-submit",
    "hpc-status",
    "hpc-preflight",
    "hpc-aggregate",
    "hpc-build-executor",
    "hpc-campaign",
)


def _mars_skill_paths() -> dict[str, str]:
    # Skills live one level up from the package (skills/ is a sibling of
    # hpc_mapreduce/ in the source tree). Wheel-only deploys won't ship
    # them — return only entries that resolve to an existing file so a
    # consumer can rely on every value being a real path.
    skills_root = hpc_mapreduce._PACKAGE_ROOT.parent / "skills"
    out: dict[str, str] = {}
    for name in _MARS_SKILL_NAMES:
        path = skills_root / name / "SKILL.md"
        if path.is_file():
            out[name] = str(path.resolve())
    return out


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
    from hpc_mapreduce.operations import operations_catalog, render_llms_full

    if getattr(args, "full", False):
        # Human/LLM-mode: emit a multi-section text blob (NOT the JSON
        # envelope) modeled on Modal\'s llms-full.txt pattern. Documented
        # exception to the stdout-is-JSON contract; analogous to --help.
        sys.stdout.write(render_llms_full())
        sys.stdout.flush()
        return EXIT_OK

    _ok(
        {
            "version": hpc_mapreduce.__version__,
            "subcommands": _live_subcommands(),
            "supported_schedulers": ["sge", "slurm"],
            "schemas_dir": str(hpc_mapreduce._PACKAGE_ROOT / "schemas"),
            "journal_dir": str(session.HPC_HOMEDIR),
            "ssh_multiplexing": os.environ.get("HPC_NO_SSH_MULTIPLEX") != "1",
            "mars_skill_paths": _mars_skill_paths(),
            "required_env": [
                "SSH_AUTH_SOCK",
                "HPC_JOURNAL_DIR",
                "HPC_CLUSTERS_CONFIG",
            ],
            "operations": operations_catalog(),
        },
        idempotent=True,
    )
    return EXIT_OK


# ─── subcommand: preflight ─────────────────────────────────────────────────


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def cmd_preflight(args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []

    # SSH agent
    sock = os.environ.get("SSH_AUTH_SOCK")
    if not sock:
        checks.append(_check("ssh_auth_sock", False, "SSH_AUTH_SOCK is not set"))
    else:
        try:
            agent = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=5)
            has_keys = agent.returncode == 0 and bool(agent.stdout.strip())
            checks.append(
                _check(
                    "ssh_auth_sock",
                    has_keys,
                    "ssh-agent has no keys" if not has_keys else f"agent at {sock}",
                )
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            checks.append(_check("ssh_auth_sock", False, f"ssh-add failed: {exc}"))

    # Binaries on PATH
    for binary in ("ssh", "rsync"):
        path = shutil.which(binary)
        checks.append(_check(f"{binary}_on_path", path is not None, path or "not found"))

    # Clusters config parseable
    try:
        clusters = load_clusters_config()
        checks.append(_check("clusters_yaml_parses", True, f"{len(clusters)} clusters defined"))
    except (OSError, Exception) as exc:  # noqa: BLE001
        clusters = {}
        checks.append(_check("clusters_yaml_parses", False, str(exc)))

    # If --cluster passed, attempt a TCP probe on port 22.
    cluster_name = getattr(args, "cluster", None)
    if cluster_name:
        if cluster_name not in clusters:
            checks.append(_check("cluster_known", False, f"{cluster_name!r} not in clusters.yaml"))
        else:
            host = clusters[cluster_name].get("host")
            try:
                with socket.create_connection((host, 22), timeout=3):
                    checks.append(_check("cluster_tcp_22", True, f"{host}:22 open"))
            except OSError as exc:
                checks.append(_check("cluster_tcp_22", False, f"{host}:22 — {exc}"))

    all_ok = all(c["ok"] for c in checks)
    _ok({"all_ok": all_ok, "checks": checks}, idempotent=True)
    return EXIT_OK if all_ok else EXIT_CLUSTER_ERROR


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
    _ok(data, idempotent=True)
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
    from hpc_mapreduce.infra.inspect import inspect_cluster

    snap = inspect_cluster(
        args.cluster,
        sacct_window_hours=args.sacct_window_hours,
        stress_alloc_mem_pct=args.stress_alloc_mem_pct,
        stress_cpu_load_frac=args.stress_cpu_load_frac,
        use_cache=not args.no_cache,
    )
    _ok(snap.to_dict(), idempotent=True)
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
    from hpc_mapreduce.job.runtime_prior import roll_up_quantiles

    out = roll_up_quantiles(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        cmd_sha=args.cmd_sha,
    )
    _ok(out, idempotent=True)
    return EXIT_OK


# ─── subcommand: walltime-drift / house-edge (calibration) ────────────────


@primitive(
    name="walltime-drift",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
)
def cmd_walltime_drift(args: argparse.Namespace) -> int:
    from hpc_mapreduce.job.calibration import (
        compute_walltime_drift,
        recommend_safety_mult_adjustment,
    )
    from hpc_mapreduce.job.runtime_prior import read_samples

    samples = read_samples(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        cmd_sha=args.cmd_sha,
        only_successful=False,
    )
    drift = compute_walltime_drift(samples)
    adjusted, rationale = recommend_safety_mult_adjustment(
        drift, base_safety_mult=float(args.base_safety_mult)
    )
    _ok(
        {
            "n_recent": drift.n_recent,
            "n_cliff_events": drift.n_cliff_events,
            "n_near_misses": drift.n_near_misses,
            "weighted_cliff_rate": drift.weighted_cliff_rate,
            "median_utilization": drift.median_utilization,
            "base_safety_mult": float(args.base_safety_mult),
            "adjusted_safety_mult": adjusted,
            "rationale": rationale,
        },
        idempotent=True,
    )
    return EXIT_OK


@primitive(
    name="house-edge",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
)
def cmd_house_edge(args: argparse.Namespace) -> int:
    from hpc_mapreduce.job.calibration import compute_house_edge
    from hpc_mapreduce.job.runtime_prior import read_samples

    samples = read_samples(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        cmd_sha=args.cmd_sha,
        only_successful=True,
    )
    edge = compute_house_edge(samples)
    _ok(
        {
            "n_with_prediction": edge.n_with_prediction,
            "mean_delta_sec": edge.mean_delta_sec,
            "median_delta_sec": edge.median_delta_sec,
            "p95_delta_sec": edge.p95_delta_sec,
            "calibration_ratio": edge.calibration_ratio,
        },
        idempotent=True,
    )
    return EXIT_OK


# ─── subcommand: plan-submit ───────────────────────────────────────────────


def cmd_plan_submit(args: argparse.Namespace) -> int:
    if (rc := _require_ssh_agent()) is not None:
        return rc
    from hpc_mapreduce.job.planner import plan_submit

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
    _ok(out, idempotent=True)
    return EXIT_OK


def cmd_clusters_list(_args: argparse.Namespace) -> int:
    clusters = load_clusters_config()
    _ok(
        {
            "clusters": [
                {"name": name, "host": cfg.get("host"), "scheduler": cfg.get("scheduler")}
                for name, cfg in clusters.items()
            ]
        },
        idempotent=True,
    )
    return EXIT_OK


def cmd_clusters_describe(args: argparse.Namespace) -> int:
    clusters = load_clusters_config()
    if args.name not in clusters:
        raise errors.ClusterUnknown(
            f"unknown cluster {args.name!r}; run `hpc-mapreduce clusters list`"
        )
    _ok({"name": args.name, "config": clusters[args.name]}, idempotent=True)
    return EXIT_OK


# ─── subcommand: list-in-flight ────────────────────────────────────────────


def _last_status_age_seconds(last_status: dict[str, Any] | None) -> int | None:
    """Return age in seconds of ``last_status.checked_at``, or None.

    Returns ``None`` when ``last_status`` is empty, has no ``checked_at``,
    or the timestamp is unparseable.  Callers use this to surface
    staleness to humans without changing the freshness contract of any
    SSH-mutating subcommand.
    """
    if not isinstance(last_status, dict):
        return None
    iso = last_status.get("checked_at")
    if not isinstance(iso, str):
        return None
    from hpc_mapreduce._time import parse_iso_utc_or_none, utcnow

    ts = parse_iso_utc_or_none(iso)
    if ts is None:
        return None
    delta = utcnow() - ts
    return max(0, int(delta.total_seconds()))


def cmd_list_in_flight(args: argparse.Namespace) -> int:
    records = session.find_in_flight_runs(args.experiment_dir)

    def _row(r: session.RunRecord) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": r.run_id,
            "profile": r.profile,
            "cluster": r.cluster,
            "job_ids": r.job_ids,
            "total_tasks": r.total_tasks,
            "submitted_at": r.submitted_at,
            "last_status": r.last_status,
            "last_status_age_seconds": _last_status_age_seconds(r.last_status),
        }
        if r.campaign_id:
            d["campaign_id"] = r.campaign_id
        return d

    _ok({"runs": [_row(r) for r in records]}, idempotent=True)
    return EXIT_OK


# ─── subcommand: campaign status / list ────────────────────────────────────


def cmd_campaign_status(args: argparse.Namespace) -> int:
    """Read-only summary of a closed-loop campaign.

    Walks every sidecar tagged with ``--campaign-id`` and reports the
    per-iteration reduced metrics dicts (``history.prior``) plus an
    in-flight count (sidecars whose journal status is still
    ``in_flight``). No SSH, no scheduler — pure local filesystem read.
    """
    from hpc_mapreduce.reduce.history import find_sidecars_by_campaign, prior

    sidecars = find_sidecars_by_campaign(args.experiment_dir, args.campaign_id)
    history = prior(args.experiment_dir, args.campaign_id)
    in_flight_records = session.find_runs_by_campaign(args.experiment_dir, args.campaign_id)
    in_flight = sum(1 for r in in_flight_records if r.status == "in_flight")
    _ok(
        {
            "campaign_id": args.campaign_id,
            "iterations": len(sidecars),
            "in_flight": in_flight,
            "history": history,
            "run_ids": [s["run_id"] for s in sidecars],
        },
        idempotent=True,
    )
    return EXIT_OK


def cmd_campaign_list(args: argparse.Namespace) -> int:
    """List every campaign with at least one sidecar in this experiment."""
    from collections import Counter

    from hpc_mapreduce.job.runs import find_existing_runs, read_run_sidecar

    counts: Counter[str] = Counter()
    for path in find_existing_runs(args.experiment_dir):
        try:
            data = read_run_sidecar(args.experiment_dir, path.stem)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        cid = data.get("campaign_id")
        if isinstance(cid, str) and cid:
            counts[cid] += 1
    _ok(
        {"campaigns": [{"campaign_id": cid, "iterations": n} for cid, n in sorted(counts.items())]},
        idempotent=True,
    )
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
    _ok(data, idempotent=True)
    return EXIT_OK


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
            f"--spec missing required fields: {missing}. See docs/cli-spec.md."
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
            idempotent=True,
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
        idempotent=True,  # honest now that submit_and_record dedups
    )
    return EXIT_OK


# ─── subcommand: submit-flow ───────────────────────────────────────────────


def cmd_submit_flow(args: argparse.Namespace) -> int:
    """Workflow atom — pre-flight + rsync + deploy + qsub + record in one shot.

    See ``hpc_mapreduce/job/submit_flow.py`` for the pipeline contract
    and ``schemas/submit_flow.{input,output}.json`` for the envelope
    shapes. Idempotent on ``run_id`` via the same dedup mechanism as
    ``submit``.
    """
    from hpc_mapreduce.job.submit_flow import submit_flow

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
            idempotent=True,
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
    _ok(result.to_envelope_data(), idempotent=True)
    return EXIT_OK


# ─── subcommand: monitor-flow ──────────────────────────────────────────────


def cmd_monitor_flow(args: argparse.Namespace) -> int:
    """Workflow atom — poll a run to terminal-or-budget; auto-combine waves.

    See ``hpc_mapreduce/job/monitor_flow.py`` for the loop contract and
    ``schemas/monitor_flow.{input,output}.json`` for the envelope shapes.
    Internal poll loop runs to terminal lifecycle, wall-clock budget,
    or escalation; emits one envelope at the end. Pairs with
    ``submit-flow`` for the campaign composition pattern
    ``submit-flow → monitor-flow → next iteration``.
    """
    from hpc_mapreduce.job.monitor_flow import monitor_flow

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
            idempotent=True,
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
    _ok(result.to_envelope_data(), idempotent=True)
    return EXIT_OK


# ─── subcommand: aggregate-flow ────────────────────────────────────────────


def cmd_aggregate_flow(args: argparse.Namespace) -> int:
    """Workflow atom — ensure all waves combined, pull partials, reduce locally.

    See ``hpc_mapreduce/job/aggregate_flow.py`` for the pipeline contract
    and ``schemas/aggregate_flow.{input,output}.json`` for the envelope
    shapes. Pairs with submit-flow + monitor-flow as the third workflow
    atom — the campaign loop's per-iteration tail is
    ``submit-flow → monitor-flow → aggregate-flow → next iter``.
    """
    from hpc_mapreduce.job.aggregate_flow import aggregate_flow

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
            idempotent=True,
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
    _ok(result.to_envelope_data(), idempotent=True)
    return EXIT_OK


# ─── subcommand: aggregate ─────────────────────────────────────────────────


def _resolve_auto_retry(experiment_dir: Path, run_id: str) -> dict[str, dict[str, Any]]:
    """Resolve the auto-retry policy for a run.

    Precedence: per-run sidecar override (``auto_retry`` field, populated
    by /submit when the user supplies a custom policy) > framework
    defaults (:data:`runner.DEFAULT_AUTO_RETRY_POLICY`).

    Always returns a non-empty dict so callers can rely on advice being
    computed for every run.
    """
    try:
        from hpc_mapreduce.job.runs import read_run_sidecar
    except ImportError:
        return dict(runner.DEFAULT_AUTO_RETRY_POLICY)
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return dict(runner.DEFAULT_AUTO_RETRY_POLICY)
    user_policy = sidecar.get("auto_retry")
    if not isinstance(user_policy, dict):
        return dict(runner.DEFAULT_AUTO_RETRY_POLICY)
    valid = {
        cat: pol
        for cat, pol in user_policy.items()
        if isinstance(cat, str) and isinstance(pol, dict)
    }
    return valid or dict(runner.DEFAULT_AUTO_RETRY_POLICY)


def _sidecar_aggregate_defaults(experiment_dir: Path, run_id: str) -> dict[str, str]:
    """Read ``aggregate_defaults.{require_outputs,expect_output}`` from the run sidecar.

    Returns an empty dict when the sidecar is missing, malformed, or has
    no ``aggregate_defaults`` block. Silent failure is intentional —
    config validity is enforced by ``/submit``, not the aggregate path.
    """
    try:
        from hpc_mapreduce.job.runs import read_run_sidecar
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
    # The aggregation pipeline is driven by slash_commands.runner.combine_wave
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
#   - the auto-classifier in slash_commands.runner.cluster_failures_by_fingerprint
#     (gpu_oom, system_oom, walltime, node_failure, import_error,
#      file_not_found, permission_denied, disk_full, python_traceback)
#   - the human-supplied taxonomy here (segv, queue_stall, code_bug, unknown)
# A test in tests/test_resubmit.py asserts the classifier never emits a
# category outside this set.
_VALID_RESUBMIT_CATEGORIES = frozenset(
    {
        # Human-supplied taxonomy.
        "gpu_oom",
        "system_oom",
        "segv",
        "walltime",
        "node_failure",
        "queue_stall",
        "code_bug",
        "unknown",
        # Auto-classifier emits these too — accept them so an agent can
        # round-trip the classifier's output back into a resubmit spec.
        "import_error",
        "file_not_found",
        "permission_denied",
        "disk_full",
        "python_traceback",
    }
)


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
        idempotent=True,
    )
    return EXIT_OK


# ─── subcommand: reconcile ─────────────────────────────────────────────────


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
        idempotent=True,
    )
    return EXIT_OK


# ─── subcommand: logs ──────────────────────────────────────────────────────


def cmd_logs(args: argparse.Namespace) -> int:
    """Fetch per-task stderr logs from the cluster.

    Two ways to select tasks:
      --task-id 7,12,42   explicit list
      --all-failed        re-poll status, fetch logs for failed tasks
    """
    if (rc := _require_ssh_agent()) is not None:
        return rc

    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for run_id {args.run_id!r}")

    # Resolve task ids.
    task_ids: list[int] = []
    note: str | None = None
    if getattr(args, "all_failed", False):
        # Fresh status poll to enumerate failed tasks.
        try:
            report = runner._ssh_status_report(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                run_id=args.run_id,
                job_ids=record.job_ids,
                job_name=record.job_name,
            )
        except errors.HpcError:
            raise
        for tid_str, info in (report.get("tasks") or {}).items():
            if isinstance(info, dict) and info.get("status") == "failed":
                try:
                    task_ids.append(int(tid_str))
                except (TypeError, ValueError):
                    continue
        if not task_ids:
            note = "no failed tasks in current status report"
    elif args.task_id:
        try:
            task_ids = [int(t.strip()) for t in args.task_id.split(",") if t.strip()]
        except ValueError as exc:
            raise errors.SpecInvalid(f"--task-id must be comma-separated integers: {exc}") from exc
        if not task_ids:
            raise errors.SpecInvalid("--task-id is empty")
    else:
        raise errors.SpecInvalid("logs requires --task-id <ids> or --all-failed")

    # Cluster-side scheduler.
    try:
        clusters = load_clusters_config()
    except Exception:  # noqa: BLE001 — config errors fall through to user-error path
        clusters = {}
    scheduler = (clusters.get(record.cluster) or {}).get("scheduler") or "slurm"

    logs: list[dict[str, Any]] = []
    if task_ids:
        logs = runner.fetch_task_logs(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            job_name=record.job_name,
            job_ids=record.job_ids,
            scheduler=scheduler,
            task_ids=task_ids,
            lines=int(getattr(args, "lines", 50) or 50),
        )

    data: dict[str, Any] = {
        "run_id": args.run_id,
        "scheduler": scheduler,
        "logs": logs,
    }
    if note is not None:
        data["note"] = note
    _ok(data, idempotent=True)
    return EXIT_OK


# ─── subcommand: failures ──────────────────────────────────────────────────


def cmd_failures(args: argparse.Namespace) -> int:
    """Cluster failed tasks by stderr fingerprint for triage.

    Re-polls status, fetches stderr for every failed task, and groups
    them by fingerprint so 40 failures with the same root cause show up
    as one cluster instead of 40 separate logs to read.
    """
    if (rc := _require_ssh_agent()) is not None:
        return rc

    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for run_id {args.run_id!r}")

    # Fresh poll: enumerate failed tasks.
    report = runner._ssh_status_report(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        run_id=args.run_id,
        job_ids=record.job_ids,
        job_name=record.job_name,
    )
    failed_ids: list[int] = []
    for tid_str, info in (report.get("tasks") or {}).items():
        if isinstance(info, dict) and info.get("status") == "failed":
            try:
                failed_ids.append(int(tid_str))
            except (TypeError, ValueError):
                continue

    if not failed_ids:
        _ok(
            {
                "run_id": args.run_id,
                "failed_count": 0,
                "clusters": [],
                "note": "no failed tasks in current status report",
            },
            idempotent=True,
        )
        return EXIT_OK

    # Cluster scheduler.
    try:
        clusters_cfg = load_clusters_config()
    except Exception:  # noqa: BLE001
        clusters_cfg = {}
    scheduler = (clusters_cfg.get(record.cluster) or {}).get("scheduler") or "slurm"

    logs = runner.fetch_task_logs(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        job_name=record.job_name,
        job_ids=record.job_ids,
        scheduler=scheduler,
        task_ids=failed_ids,
        lines=int(getattr(args, "lines", 30) or 30),
    )
    clusters = runner.cluster_failures_by_fingerprint(logs)

    # Auto-retry policy: resolve per-run sidecar override + framework
    # defaults (runner.DEFAULT_AUTO_RETRY_POLICY). Annotate each cluster
    # with which task ids are still eligible for an automated retry per
    # the per-category max_attempts. Purely advisory — the actual
    # resubmit remains the caller's job (matches existing /resubmit
    # semantics).
    auto_retry = _resolve_auto_retry(args.experiment_dir, args.run_id)
    if auto_retry:
        clusters = runner.annotate_clusters_with_retry_advice(
            clusters,
            auto_retry_policy=auto_retry,
            record=record,
        )

    data: dict[str, Any] = {
        "run_id": args.run_id,
        "failed_count": len(failed_ids),
        "clusters": clusters,
        "scheduler": scheduler,
    }
    if auto_retry:
        data["auto_retry_policy"] = auto_retry
    _ok(data, idempotent=True)
    return EXIT_OK


# ─── subcommand: build-executor ────────────────────────────────────────────


def cmd_build_executor(args: argparse.Namespace) -> int:
    starters = hpc_mapreduce._PACKAGE_ROOT / "templates" / "starters"
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
        idempotent=False,
    )
    return EXIT_OK


# ─── parser ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hpc-mapreduce",
        description=(
            "Submit, track status of, and aggregate parameter-grid HPC experiments. "
            "Stdout is a single-line JSON envelope; stderr is JSON-per-line "
            "log records. See docs/cli-spec.md for full schemas."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {hpc_mapreduce.__version__}",
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
            "Score candidate constraints for a submit. Combines inspect-cluster, "
            "runtime priors, and the SEGV blacklist. Output is JSON the slash "
            "command hands to Claude for cost-model judgment."
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
        cmd_sha_help=(
            "Filter samples to one cmd_sha (recommended after .hpc/tasks.py changes)."
        ),
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
