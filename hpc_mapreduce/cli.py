"""Command-line interface — the agent surface.

Designed to be invoked by automation (MARs orchestrator agents via the
Bash tool, cron, scripts). Conventions:

- Stdout is exclusively a single-line JSON envelope.
- Stderr is JSON-per-line log records (debug for humans; agents may ignore).
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
from pathlib import Path
from typing import Any

import hpc_mapreduce
from hpc_mapreduce.infra.clusters import load_clusters_config
from hpc_mapreduce.job.discover import discover_executors
from hpc_mapreduce.job.grid import expand_grid
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


# ─── shared option helpers ─────────────────────────────────────────────────


def _add_experiment_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Path to the experiment repo (default: current working directory).",
    )


def _load_spec(spec_path: Path | None) -> dict[str, Any]:
    if spec_path is None:
        return {}
    try:
        loaded = json.loads(spec_path.read_text())
    except FileNotFoundError as exc:
        raise errors.ConfigInvalid(f"--spec file not found: {spec_path}") from exc
    except json.JSONDecodeError as exc:
        raise errors.ConfigInvalid(
            f"--spec is not valid JSON ({spec_path}): {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise errors.ConfigInvalid(
            f"--spec must be a JSON object; got {type(loaded).__name__}"
        )
    return loaded


# ─── subcommand: capabilities ──────────────────────────────────────────────


def cmd_capabilities(_args: argparse.Namespace) -> int:
    _ok(
        {
            "version": hpc_mapreduce.__version__,
            "subcommands": [
                "submit",
                "status",
                "aggregate",
                "reconcile",
                "resubmit",
                "preflight",
                "discover",
                "expand-grid",
                "list-in-flight",
                "clusters",
                "capabilities",
                "build-executor",
            ],
            "supported_schedulers": ["sge", "slurm"],
            "schemas_dir": str(hpc_mapreduce._PACKAGE_ROOT / "schemas"),
            "journal_dir": str(session.HPC_HOMEDIR),
            "ssh_multiplexing": os.environ.get("HPC_NO_SSH_MULTIPLEX") != "1",
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
            agent = subprocess.run(
                ["ssh-add", "-l"], capture_output=True, text=True, timeout=5
            )
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
        checks.append(
            _check("clusters_yaml_parses", True, f"{len(clusters)} clusters defined")
        )
    except (OSError, Exception) as exc:  # noqa: BLE001
        clusters = {}
        checks.append(_check("clusters_yaml_parses", False, str(exc)))

    # If --cluster passed, attempt a TCP probe on port 22.
    cluster_name = getattr(args, "cluster", None)
    if cluster_name:
        if cluster_name not in clusters:
            checks.append(
                _check("cluster_known", False, f"{cluster_name!r} not in clusters.yaml")
            )
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
    _ok(
        {
            "executors": [
                {
                    "name": i.name,
                    "path": str(i.path),
                    "cli_framework": i.cli_framework,
                    "has_main_guard": i.has_main_guard,
                }
                for i in infos
            ]
        },
        idempotent=True,
    )
    return EXIT_OK


# ─── subcommand: expand-grid ───────────────────────────────────────────────


def cmd_expand_grid(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    grid = spec.get("grid")
    if not isinstance(grid, dict):
        raise errors.ManifestInvalid(
            "--spec must contain a top-level 'grid' object mapping name → values."
        )
    points = expand_grid(grid)
    _ok({"points": points, "total": len(points)}, idempotent=True)
    return EXIT_OK


# ─── subcommand: clusters ──────────────────────────────────────────────────


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


def cmd_list_in_flight(args: argparse.Namespace) -> int:
    records = session.find_in_flight_runs(args.experiment_dir)
    _ok(
        {
            "runs": [
                {
                    "run_id": r.run_id,
                    "profile": r.profile,
                    "cluster": r.cluster,
                    "job_ids": r.job_ids,
                    "total_tasks": r.total_tasks,
                    "submitted_at": r.submitted_at,
                    "last_status": r.last_status,
                }
                for r in records
            ]
        },
        idempotent=True,
    )
    return EXIT_OK


# ─── subcommand: status ────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
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
        manifest_filename=record.manifest,
        job_ids=record.job_ids,
        job_name=record.job_name,
    )
    _ok(
        {
            "run_id": updated.run_id,
            "lifecycle_state": updated.status,
            "last_status": updated.last_status,
            "combined_waves": updated.combined_waves,
            "failed_waves": updated.failed_waves,
        },
        idempotent=True,
    )
    return EXIT_OK


# ─── subcommand: submit ────────────────────────────────────────────────────


def cmd_submit(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    required = ("profile", "cluster", "ssh_target", "remote_path", "job_name",
                "manifest_filename", "job_ids", "total_tasks")
    missing = [k for k in required if k not in spec]
    if missing:
        raise errors.ManifestInvalid(
            f"--spec missing required fields: {missing}. See docs/cli-spec.md."
        )

    if args.dry_run:
        _ok(
            {
                "would_launch": int(spec["total_tasks"]),
                "profile": spec["profile"],
                "cluster": spec["cluster"],
                "manifest": spec["manifest_filename"],
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
        manifest_filename=spec["manifest_filename"],
        job_ids=list(spec["job_ids"]),
        total_tasks=int(spec["total_tasks"]),
        run_id=spec.get("run_id"),
    )
    _ok(
        {
            "run_id": record.run_id,
            "job_ids": record.job_ids,
            "manifest": record.manifest,
            "total_tasks": record.total_tasks,
            "deduped": deduped,
        },
        idempotent=True,  # honest now that submit_and_record dedups
    )
    return EXIT_OK


# ─── subcommand: aggregate ─────────────────────────────────────────────────


def cmd_aggregate(args: argparse.Namespace) -> int:
    # The full aggregation pipeline is driven by slash_commands.runner.combine_wave
    # plus the user-supplied combiner script on the cluster. The CLI exposes the
    # journal-update half so a calling agent can record successful aggregation
    # outcomes; running an actual combiner from the CLI is deliberately out of
    # scope for this version (combiner choice + output dir layout is user-side).
    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(
            f"no journal record for run_id {args.run_id!r}"
        )
    if args.wave is None:
        raise errors.ManifestInvalid("aggregate requires --wave <int>")

    ok, stdout, stderr = runner.combine_wave(
        args.experiment_dir,
        args.run_id,
        wave=int(args.wave),
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        manifest_filename=record.manifest,
        force=args.force,
    )
    output_dir = args.output_dir or (args.experiment_dir / "_aggregated" / args.run_id)
    output_dir = Path(output_dir).resolve()
    _ok(
        {
            "run_id": args.run_id,
            "wave": int(args.wave),
            "combined": ok,
            "output_dir": str(output_dir),
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        },
        idempotent=ok,  # success is idempotent; failure is retry-safe per wave
    )
    return EXIT_OK if ok else EXIT_CLUSTER_ERROR


# ─── subcommand: resubmit ──────────────────────────────────────────────────


def cmd_resubmit(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    failed = spec.get("failed_task_ids")
    category = spec.get("category")
    if not isinstance(failed, list) or not failed:
        raise errors.ManifestInvalid("--spec.failed_task_ids must be a non-empty list")
    if not isinstance(category, str):
        raise errors.ManifestInvalid("--spec.category must be a string")

    record = runner.resubmit_failed(
        args.experiment_dir,
        args.run_id,
        failed_task_ids=[int(t) for t in failed],
        category=category,
        overrides=spec.get("overrides"),
        new_job_ids=spec.get("new_job_ids"),
    )
    _ok(
        {
            "run_id": record.run_id,
            "retries": record.retries,
            "job_ids": record.job_ids,
        },
        idempotent=False,  # each call increments retry counters
    )
    return EXIT_OK


# ─── subcommand: reconcile ─────────────────────────────────────────────────


def cmd_reconcile(args: argparse.Namespace) -> int:
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


# ─── subcommand: build-executor ────────────────────────────────────────────


def cmd_build_executor(args: argparse.Namespace) -> int:
    starters = hpc_mapreduce._PACKAGE_ROOT / "templates" / "starters"
    template_map = {
        "plain": starters / "executor_template.py",
        "chunked": starters / "chunking_shim.py",
        "date-window": starters / "date_window_shim.py",
        "shim": starters / "shim_template.py",
    }
    if args.type not in template_map:
        raise errors.ManifestInvalid(
            f"unknown --type {args.type!r}; choose from {sorted(template_map)}"
        )
    src = template_map[args.type]
    if not src.exists():
        raise errors.ConfigInvalid(f"template missing on disk: {src}")
    dest = (args.output_dir / args.name).with_suffix(".py")
    if dest.exists() and not args.force:
        raise errors.ManifestInvalid(
            f"refusing to overwrite {dest}; pass --force to overwrite"
        )
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
            "Submit, monitor, and aggregate parameter-grid HPC experiments. "
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

    # expand-grid
    p_eg = sub.add_parser(
        "expand-grid",
        help="Cartesian-product expand a grid spec; print all points.",
    )
    p_eg.add_argument("--spec", type=Path, required=True)
    _add_experiment_dir(p_eg)
    p_eg.set_defaults(func=cmd_expand_grid)

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

    # status
    p_st = sub.add_parser(
        "status", help="Poll cluster status for a run_id; one-shot, returns snapshot."
    )
    _add_experiment_dir(p_st)
    p_st.add_argument("--run-id", required=True)
    p_st.set_defaults(func=cmd_status)

    # submit
    p_sub = sub.add_parser(
        "submit",
        help=(
            "Record a submission in the journal. Idempotent on (profile, "
            "manifest sha): the bundled atomic-ops layer dedups by run_id, so "
            "a retry on transient network errors does not double-submit."
        ),
    )
    _add_experiment_dir(p_sub)
    p_sub.add_argument("--spec", type=Path, required=True, help="JSON spec file")
    p_sub.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the spec and report what would be launched; no SSH/qsub.",
    )
    p_sub.set_defaults(func=cmd_submit)

    # aggregate
    p_agg = sub.add_parser(
        "aggregate",
        help="Run the on-cluster combiner for one wave; records outcome to journal.",
    )
    _add_experiment_dir(p_agg)
    p_agg.add_argument("--run-id", required=True)
    p_agg.add_argument("--wave", type=int, required=True)
    p_agg.add_argument(
        "--output-dir",
        type=Path,
        help="Where pulled artifacts land (default: <experiment-dir>/_aggregated/<run_id>/).",
    )
    p_agg.add_argument(
        "--force",
        action="store_true",
        help="Re-run the combiner even if the wave appears combined.",
    )
    p_agg.set_defaults(func=cmd_aggregate)

    # resubmit
    p_rs = sub.add_parser(
        "resubmit",
        help="Record a resubmission attempt in the journal (caller does the actual qsub).",
    )
    _add_experiment_dir(p_rs)
    p_rs.add_argument("--run-id", required=True)
    p_rs.add_argument("--spec", type=Path, required=True)
    p_rs.set_defaults(func=cmd_resubmit)

    # reconcile
    p_rec = sub.add_parser(
        "reconcile",
        help="Re-derive ground truth from the cluster (status, waves, alive jobs).",
    )
    _add_experiment_dir(p_rec)
    p_rec.add_argument("--run-id", required=True)
    p_rec.add_argument(
        "--scheduler",
        required=True,
        choices=["sge", "slurm"],
        help="Scheduler family — needed to query alive job IDs.",
    )
    p_rec.set_defaults(func=cmd_reconcile)

    # build-executor
    p_be = sub.add_parser(
        "build-executor",
        help="Scaffold a new executor or shim from a starter template.",
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
        choices=["plain", "chunked", "date-window", "shim"],
        help=(
            "Which template to instantiate: "
            "plain = standard executor scaffold; "
            "chunked = one task per row-index range; "
            "date-window = one task per (start, end) date pair; "
            "shim = blank shim template for hand-written translations."
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
    except FileNotFoundError as exc:
        return _err(
            error_code="executor_not_found",
            message=str(exc),
            category="user",
            retry_safe=False,
        )
    except ValueError as exc:
        return _err(
            error_code="manifest_invalid",
            message=str(exc),
            category="user",
            retry_safe=False,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort envelope
        return _err(
            error_code="internal",
            message=f"{type(exc).__name__}: {exc}",
            category="internal",
            retry_safe=False,
        )


if __name__ == "__main__":
    sys.exit(main())
