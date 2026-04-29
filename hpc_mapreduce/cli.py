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
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

import hpc_mapreduce
from hpc_mapreduce.infra.clusters import load_clusters_config
from hpc_mapreduce.job.discover import (
    detect_mars_tier,
    discover_executors,
    read_meta_json,
)
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


def _load_spec(
    spec_path: Path | None, *, schema_name: str | None = None
) -> dict[str, Any]:
    """Load and (optionally) JSON-Schema-validate ``--spec`` input.

    Validation is opt-in via *schema_name* so callers without a matching
    schema (e.g. ad-hoc dicts) still work, but every CLI subcommand that
    has one in ``hpc_mapreduce/schemas/<name>.input.json`` should pass
    it.  Validation failures map to ``ManifestInvalid`` with the schema
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
        raise errors.ConfigInvalid(
            f"--spec is not valid JSON ({spec_path}): {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise errors.ConfigInvalid(
            f"--spec must be a JSON object; got {type(loaded).__name__}"
        )
    if schema_name is not None:
        _validate_against_schema(loaded, schema_name)
    return loaded


def _validate_against_schema(payload: dict[str, Any], schema_name: str) -> None:
    """Validate *payload* against ``hpc_mapreduce/schemas/<schema_name>.input.json``.

    Raises :class:`errors.ManifestInvalid` on schema mismatch.  When the
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
        raise errors.ManifestInvalid(
            f"--spec failed schema {schema_name}.input.json at {path}: {exc.message}"
        ) from exc


# ─── subcommand: capabilities ──────────────────────────────────────────────


_MARS_SKILL_NAMES = (
    "hpc-submit",
    "hpc-status",
    "hpc-preflight",
    "hpc-aggregate",
    "hpc-build-executor",
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
                "logs",
                "failures",
            ],
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


# ─── subcommand: expand-grid ───────────────────────────────────────────────


_WALLTIME_RE = __import__("re").compile(
    r"^(?:(?P<h>\d+):)?(?P<m>\d+):(?P<s>\d+)$|^(?P<bare_hours>\d+(?:\.\d+)?)h$",
    __import__("re").IGNORECASE,
)


def _parse_walltime_to_seconds(value: str) -> int:
    """Accept ``HH:MM:SS``, ``MM:SS``, or ``<float>h``; return seconds.

    Raises :class:`errors.ManifestInvalid` on unparseable input.
    """
    m = _WALLTIME_RE.match(value.strip())
    if not m:
        raise errors.ManifestInvalid(
            f"--per-task-walltime must be HH:MM:SS, MM:SS, or <float>h; got {value!r}"
        )
    if m.group("bare_hours"):
        return int(round(float(m.group("bare_hours")) * 3600))
    h = int(m.group("h") or 0)
    minutes = int(m.group("m"))
    s = int(m.group("s"))
    return h * 3600 + minutes * 60 + s


def cmd_expand_grid(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec, schema_name="expand_grid")
    grid = spec.get("grid")
    if not isinstance(grid, dict):
        raise errors.ManifestInvalid(
            "--spec must contain a top-level 'grid' object mapping name → values."
        )
    points = expand_grid(grid)

    data: dict[str, Any] = {"points": points, "total": len(points)}

    walltime_str = getattr(args, "per_task_walltime", None)
    if walltime_str:
        seconds = _parse_walltime_to_seconds(walltime_str)
        cpus = max(1, int(getattr(args, "per_task_cpus", 1) or 1))
        gpus = max(0, int(getattr(args, "per_task_gpus", 0) or 0))
        max_concurrent = getattr(args, "max_concurrent_tasks", None)
        if max_concurrent is not None:
            try:
                max_concurrent = max(1, int(max_concurrent))
            except (TypeError, ValueError):
                raise errors.ManifestInvalid(
                    f"--max-concurrent-tasks must be a positive integer; "
                    f"got {max_concurrent!r}"
                )

        total_task_seconds = seconds * len(points)
        cost = {
            "per_task_walltime_seconds": seconds,
            "per_task_cpus": cpus,
            "per_task_gpus": gpus,
            "total_tasks": len(points),
            "total_cpu_hours": round(total_task_seconds * cpus / 3600.0, 2),
            "total_gpu_hours": round(total_task_seconds * gpus / 3600.0, 2),
        }
        if max_concurrent:
            # Wall-clock estimate: ceil(total_tasks / max_concurrent) * walltime.
            from math import ceil
            waves = ceil(len(points) / max_concurrent)
            cost["estimated_walltime_seconds"] = waves * seconds
            cost["estimated_walltime_hours"] = round(waves * seconds / 3600.0, 2)
            cost["max_concurrent_tasks"] = max_concurrent
        data["cost_estimate"] = cost

    _ok(data, idempotent=True)
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
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return max(0, int(delta.total_seconds()))
    except (ValueError, TypeError):
        return None


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
                    "last_status_age_seconds": _last_status_age_seconds(r.last_status),
                }
                for r in records
            ]
        },
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
        manifest_filename=record.manifest,
        job_ids=record.job_ids,
        job_name=record.job_name,
    )
    _ok(
        {
            "run_id": updated.run_id,
            "lifecycle_state": updated.status,
            "last_status": updated.last_status,
            "last_status_age_seconds": _last_status_age_seconds(updated.last_status),
            "combined_waves": updated.combined_waves,
            "failed_waves": updated.failed_waves,
        },
        idempotent=True,
    )
    return EXIT_OK


def _overlay_meta_on_spec(
    spec: dict[str, Any], experiment_dir: Path
) -> dict[str, Any]:
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
    required = ("profile", "cluster", "ssh_target", "remote_path", "job_name",
                "manifest_filename", "job_ids", "total_tasks")
    missing = [k for k in required if k not in spec]
    if missing:
        raise errors.ManifestInvalid(
            f"--spec missing required fields: {missing}. See docs/cli-spec.md."
        )

    # Pre-submit manifest sanity: opportunistic — if the manifest exists
    # locally at the conventional path, validate it before recording the
    # submission. Catches unresolved {placeholder}s, empty cmd fields, and
    # wave_map / tasks coverage drift before they crash the cluster job
    # mid-run. When the manifest is only on the cluster (rare), we skip.
    manifest_path = args.experiment_dir / spec["manifest_filename"]
    skip_check = getattr(args, "skip_manifest_check", False)
    if manifest_path.is_file() and not skip_check:
        runner.validate_manifest_file(manifest_path)

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


def _hpc_yaml_auto_retry(
    experiment_dir: Path, profile: str | None
) -> dict[str, dict[str, Any]]:
    """Read ``profiles[profile].auto_retry`` from hpc.yaml.

    Returns the raw nested dict (category -> policy fields). Empty when
    hpc.yaml is missing, malformed, or has no ``auto_retry`` block.
    """
    hpc_yaml = experiment_dir / "hpc.yaml"
    if not hpc_yaml.is_file():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(hpc_yaml.read_text()) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    profiles = data.get("profiles") or {}
    if profile and isinstance(profiles, dict) and profile in profiles:
        block = (profiles[profile] or {}).get("auto_retry") or {}
    elif not profiles:
        block = data.get("auto_retry") or {}
    else:
        return {}
    if not isinstance(block, dict):
        return {}
    return {
        cat: pol
        for cat, pol in block.items()
        if isinstance(cat, str) and isinstance(pol, dict)
    }


def _hpc_yaml_aggregate_defaults(
    experiment_dir: Path, profile: str | None
) -> dict[str, str]:
    """Read ``profiles[profile].results.{require_outputs,expect_output}``.

    Returns an empty dict when hpc.yaml is missing, malformed, or the
    profile has no aggregate defaults.  Silent failure is intentional —
    config validity is enforced by ``/submit``, not the aggregate path.
    """
    hpc_yaml = experiment_dir / "hpc.yaml"
    if not hpc_yaml.is_file():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(hpc_yaml.read_text()) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    profiles = data.get("profiles") or {}
    if profile and isinstance(profiles, dict) and profile in profiles:
        results_block = (profiles[profile] or {}).get("results") or {}
    elif not profiles:
        # Single-profile shorthand: results at top level
        results_block = data.get("results") or {}
    else:
        return {}
    if not isinstance(results_block, dict):
        return {}
    return {
        k: results_block[k]
        for k in ("require_outputs", "expect_output")
        if isinstance(results_block.get(k), str)
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
    # Defaults for require/expect can be set in hpc.yaml under
    # ``results.require_outputs`` / ``results.expect_output``.
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(
            f"no journal record for run_id {args.run_id!r}"
        )
    if args.wave is None:
        raise errors.ManifestInvalid("aggregate requires --wave <int>")

    # Resolve aggregate flags: explicit CLI > hpc.yaml > none.
    # ``getattr`` keeps in-process callers (tests, slash-command shims)
    # working even when they hand-build a Namespace without these keys.
    defaults = _hpc_yaml_aggregate_defaults(args.experiment_dir, record.profile)
    require_outputs = getattr(args, "require_outputs", None) or defaults.get("require_outputs")
    expect_output = getattr(args, "expect_output", None) or defaults.get("expect_output")

    # Precondition: every per-task output must exist before we combine.
    if require_outputs:
        missing = runner.verify_per_task_outputs(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            manifest_filename=record.manifest,
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
            f"combiner returned non-zero for wave {args.wave}; "
            f"stderr tail: {stderr[-500:]!r}"
        )
    )


# ─── subcommand: resubmit ──────────────────────────────────────────────────


_VALID_RESUBMIT_CATEGORIES = frozenset(
    {
        "gpu_oom",
        "system_oom",
        "walltime",
        "node_failure",
        "queue_stall",
        "code_bug",
        "unknown",
    }
)


def cmd_resubmit(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec, schema_name="resubmit")
    failed = spec.get("failed_task_ids")
    category = spec.get("category")
    if not isinstance(failed, list) or not failed:
        raise errors.ManifestInvalid("--spec.failed_task_ids must be a non-empty list")
    if not isinstance(category, str):
        raise errors.ManifestInvalid("--spec.category must be a string")
    # Belt-and-braces: schema validation also enforces this enum, but
    # ``_validate_against_schema`` is a no-op when ``jsonschema`` is not
    # installed.  Keep the local check so the seven-category contract
    # holds either way.
    if category not in _VALID_RESUBMIT_CATEGORIES:
        raise errors.ManifestInvalid(
            f"--spec.category must be one of {sorted(_VALID_RESUBMIT_CATEGORIES)}; "
            f"got {category!r}"
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
        raise errors.JournalCorrupt(
            f"no journal record for run_id {args.run_id!r}"
        )

    # Resolve task ids.
    task_ids: list[int] = []
    note: str | None = None
    if getattr(args, "all_failed", False):
        # Fresh status poll to enumerate failed tasks.
        try:
            report = runner._ssh_status_report(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                manifest_filename=record.manifest,
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
            raise errors.ManifestInvalid(
                f"--task-id must be comma-separated integers: {exc}"
            ) from exc
        if not task_ids:
            raise errors.ManifestInvalid("--task-id is empty")
    else:
        raise errors.ManifestInvalid(
            "logs requires --task-id <ids> or --all-failed"
        )

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
        raise errors.JournalCorrupt(
            f"no journal record for run_id {args.run_id!r}"
        )

    # Fresh poll: enumerate failed tasks.
    report = runner._ssh_status_report(
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        manifest_filename=record.manifest,
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

    # Auto-retry policy from hpc.yaml: annotate each cluster with which
    # task ids are still eligible for an automated retry per the
    # configured per-category max_attempts.  Purely advisory — the actual
    # resubmit remains the caller's job (matches existing /resubmit
    # semantics).
    auto_retry = _hpc_yaml_auto_retry(args.experiment_dir, record.profile)
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
    p_eg.add_argument(
        "--per-task-walltime",
        default=None,
        help=(
            "Per-task walltime as HH:MM:SS, MM:SS, or '4h'. When set, the "
            "envelope's data block adds a `cost_estimate` with total CPU- "
            "and GPU-hours."
        ),
    )
    p_eg.add_argument(
        "--per-task-cpus",
        type=int,
        default=1,
        help="CPUs per task (default 1); used for total CPU-hour estimate.",
    )
    p_eg.add_argument(
        "--per-task-gpus",
        type=int,
        default=0,
        help="GPUs per task (default 0); used for total GPU-hour estimate.",
    )
    p_eg.add_argument(
        "--max-concurrent-tasks",
        type=int,
        default=None,
        help=(
            "Optional concurrency cap; when set, the cost estimate also "
            "reports an estimated wall-clock time as "
            "ceil(total/concurrent) * walltime."
        ),
    )
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
    p_sub.add_argument(
        "--skip-manifest-check",
        action="store_true",
        help=(
            "Skip pre-submit manifest sanity. Use only when the manifest "
            "is built directly on the cluster and not present locally."
        ),
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

    # aggregate
    p_agg = sub.add_parser(
        "aggregate",
        help="Run the on-cluster combiner for one wave; records outcome to journal.",
    )
    _add_experiment_dir(p_agg)
    p_agg.add_argument("--run-id", required=True)
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
            "wave is missing its expected output. Default reads from "
            "hpc.yaml's results.require_outputs."
        ),
    )
    p_agg.add_argument(
        "--expect-output",
        default=None,
        help=(
            "Remote path (relative to remote_path) that the combiner must "
            "produce. Verified after the combiner exits 0; .json files "
            "are also checked for parseability. Default reads from "
            "hpc.yaml's results.expect_output."
        ),
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

    # logs
    p_logs = sub.add_parser(
        "logs",
        help="Fetch per-task stderr logs from the cluster (requires --task-id or --all-failed).",
    )
    _add_experiment_dir(p_logs)
    p_logs.add_argument("--run-id", required=True)
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
    p_fail.add_argument("--run-id", required=True)
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
