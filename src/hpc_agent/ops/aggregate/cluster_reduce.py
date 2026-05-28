"""``cluster-reduce`` primitive — run the reducer on the cluster, pull only its output.

Eliminates the 1200-chunk rsync_pull failure mode where ``aggregate-flow``
with ``pull_summaries=True`` + a permissive ``summary_glob`` drags every
per-task output file to the local machine before reducing. Instead:

1. SSH into the cluster's ``remote_path``.
2. Run the user's reducer command (either ``aggregate_cmd`` from the run
   sidecar, or an auto-discovered reducer) — typically
   ``python -m <module> --run-id $HPC_RUN_ID``.
3. The reducer writes a single output file (defaulting to
   ``_aggregated/<run_id>.json`` under ``remote_path``).
4. rsync_pull just that one file (~KB, not GB).
5. Parse + return the JSON inline so the agent doesn't even need to
   read the local copy.

The reducer contract: any program that accepts ``$HPC_RUN_ID`` (or
``--run-id``) and writes to ``$HPC_AGGREGATED_OUTPUT`` (or the default
path). See ``docs/reference/reducer-contract.md``.

Pure cluster-side reduction. Per-task chunks stay on the cluster; only
the reduced JSON crosses the wire.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

# Default cluster-side output path under remote_path. The reducer is
# expected to write its single JSON output here unless overridden via
# the ``output_path`` arg.
_DEFAULT_OUTPUT_REL = "_aggregated/{run_id}.json"


def _format_output_rel(template: str, *, run_id: str) -> str:
    """Substitute ``{run_id}`` in *template*. Bare string replace so other
    literal braces (e.g. ``{date}``) in a user-supplied path don't raise
    ``KeyError`` from ``str.format``. Only ``{run_id}`` is recognised."""
    return template.replace("{run_id}", run_id)


def _resolve_aggregate_cmd(
    aggregate_cmd: str | None,
    *,
    experiment_dir: Path,
    run_id: str,
) -> str:
    """Argument > sidecar fallback, raising SpecInvalid when nothing's available."""
    if aggregate_cmd:
        return aggregate_cmd
    from hpc_agent.state.runs import read_run_sidecar

    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError):
        sidecar = {}
    agg_defaults = sidecar.get("aggregate_defaults") or {}
    resolved: str | None = agg_defaults.get("aggregate_cmd")
    if not resolved:
        raise errors.SpecInvalid(
            f"no aggregate_cmd available for run_id={run_id!r}; pass "
            "aggregate_cmd= or set aggregate_defaults.aggregate_cmd on "
            "the run sidecar (write_run_sidecar's aggregate_defaults arg)."
        )
    return resolved


def _build_remote_cmd(
    *,
    remote_path: str,
    output_rel: str,
    aggregate_cmd: str,
    run_id: str,
    extra_env: dict[str, str] | None,
) -> str:
    """Compose the single shell line that runs the reducer on the cluster."""
    env_parts: list[str] = [
        f"HPC_RUN_ID={shlex.quote(run_id)}",
        f"HPC_AGGREGATED_OUTPUT={shlex.quote(output_rel)}",
    ]
    if extra_env:
        for k, v in extra_env.items():
            env_parts.append(f"{k}={shlex.quote(str(v))}")
    env_setup = "export " + " ".join(env_parts)
    output_dir_rel = os.path.dirname(output_rel) or "."
    return (
        f"cd {shlex.quote(remote_path)} && "
        f"mkdir -p {shlex.quote(output_dir_rel)} && "
        f"{env_setup} && "
        f"{aggregate_cmd}"
    )


def _parse_local_output(local_output: Path, *, run_id: str) -> dict:
    """Read + JSON-parse the pulled reducer output, mapping errors to RemoteCommandFailed."""
    if not local_output.is_file():
        raise errors.RemoteCommandFailed(
            f"reducer for run_id={run_id!r} reported success but "
            f"{local_output} is missing locally — check rsync_pull "
            "include filter and the reducer's output path."
        )
    try:
        parsed: dict = json.loads(local_output.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise errors.RemoteCommandFailed(
            f"reducer output at {local_output} is not valid JSON: {exc}"
        ) from exc
    return parsed


def _cluster_reduce_arg_pre(ns: argparse.Namespace) -> dict[str, Any]:
    """Parse ``--extra-env "k=v,k=v"`` into a ``{k: v}`` dict.

    Returns ``{"extra_env": None}`` when the flag is unset or empty so
    the primitive sees its own default (no extra env), and an explicit
    dict otherwise. Tokens without ``=`` are silently dropped — matches
    the pre-migration cmd_cluster_reduce behaviour.
    """
    extra_env: dict[str, str] | None = None
    if getattr(ns, "extra_env", None):
        extra_env = {}
        for tok in str(ns.extra_env).split(","):
            if "=" in tok:
                k, _, v = tok.partition("=")
                extra_env[k.strip()] = v.strip()
    return {"extra_env": extra_env}


@primitive(
    name="cluster-reduce",
    verb="mutate",
    side_effects=[
        SideEffect("ssh", "<cluster> (run reducer)"),
        SideEffect("sync-pull", "<remote_path>/<output_rel> → <local_dir>"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Run the user's reducer on the cluster, pull only its single "
            "output JSON. Eliminates the bulk per-task rsync_pull failure "
            "mode at /aggregate-hpc + campaign-loop time."
        ),
        experiment_dir_arg=True,
        requires_ssh=True,
        args=(
            CliArg(
                "--run-id",
                type=str,
                required=True,
                help="Run identifier (matches .hpc/runs/<run_id>.json).",
            ),
            CliArg(
                "--aggregate-cmd",
                type=str,
                default=None,
                help=(
                    "Shell command to run on the cluster. Defaults to the run "
                    "sidecar's aggregate_defaults.aggregate_cmd."
                ),
            ),
            CliArg(
                "--output-path",
                type=str,
                default=None,
                help=(
                    "Cluster-side path the reducer writes its single JSON output. "
                    "Defaults to '_aggregated/<run_id>.json' under remote_path. "
                    "Threaded as $HPC_AGGREGATED_OUTPUT to the reducer."
                ),
            ),
            CliArg(
                "--local-dir",
                type=str,
                default=None,
                help="Local destination dir; defaults to <experiment>/_aggregated/<run_id>/.",
            ),
            CliArg(
                "--extra-env",
                type=str,
                default=None,
                help=(
                    "Comma-separated KEY=VALUE pairs forwarded to the reducer "
                    "(in addition to HPC_RUN_ID / HPC_AGGREGATED_OUTPUT)."
                ),
            ),
            CliArg(
                "--timeout-sec",
                type=int,
                default=1800,
                help="Reducer timeout in seconds (default 1800 = 30 min).",
            ),
        ),
        arg_pre=_cluster_reduce_arg_pre,
    ),
    agent_facing=True,
)
def cluster_reduce(
    experiment_dir: Path,
    *,
    run_id: str,
    aggregate_cmd: str | None = None,
    output_path: str | None = None,
    local_dir: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout_sec: int = 1800,
) -> dict[str, Any]:
    """Run the cluster-side reducer for *run_id* and pull its single output.

    Parameters
    ----------
    experiment_dir:
        Repo root. The journal record at
        ``~/.claude/hpc/<repo_hash>/runs/<run_id>.json`` carries the
        ``ssh_target`` + ``remote_path``.
    run_id:
        Run identifier — stamped into ``$HPC_RUN_ID`` for the reducer.
    aggregate_cmd:
        Shell command to run on the cluster. When None, falls back to
        the run sidecar's ``aggregate_defaults.aggregate_cmd``. When
        both are absent, raises :class:`errors.SpecInvalid` — there's
        nothing to run.
    output_path:
        Path on the cluster (relative to ``remote_path`` or absolute)
        the reducer writes its single JSON output to. Defaults to
        ``_aggregated/<run_id>.json``. Threaded through to the reducer
        as ``$HPC_AGGREGATED_OUTPUT`` so contract-compliant reducers
        don't have to hard-code it.
    local_dir:
        Local directory to rsync_pull the output into. Defaults to
        ``<experiment_dir>/_aggregated/<run_id>/``.
    extra_env:
        Additional env vars forwarded to the reducer (in addition to
        ``HPC_RUN_ID`` / ``HPC_AGGREGATED_OUTPUT``).
    timeout_sec:
        SSH timeout for the reducer subprocess. Default 1800s (30 min).

    Returns
    -------
    ``{ok, run_id, output_path_remote, output_path_local, reduced,
    exit_code, stderr_tail}``:

    * ``ok=True`` iff the reducer exited 0 and the output file was
      pulled + parsed as JSON.
    * ``reduced`` is the parsed JSON dict (so callers don't need to
      re-read the local file).

    Raises
    ------
    :class:`errors.SpecInvalid`
        Empty *run_id*, or no *aggregate_cmd* available.
    :class:`errors.SshUnreachable`
        Pre-flight SSH probe fails.
    :class:`errors.RemoteCommandFailed`
        Reducer exited non-zero, output file missing on cluster, or
        rsync_pull failed.
    """
    if not run_id:
        raise errors.SpecInvalid("run_id is required")

    from pathlib import Path as _Path

    from hpc_agent.infra.remote import ssh_run
    from hpc_agent.infra.transport import rsync_pull
    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for run_id={run_id!r}")

    aggregate_cmd = _resolve_aggregate_cmd(
        aggregate_cmd, experiment_dir=experiment_dir, run_id=run_id
    )
    output_rel = _format_output_rel(output_path or _DEFAULT_OUTPUT_REL, run_id=run_id)
    local_dir_path = (
        _Path(local_dir)
        if local_dir is not None
        else _Path(experiment_dir) / "_aggregated" / run_id
    )

    proc = ssh_run(
        _build_remote_cmd(
            remote_path=record.remote_path,
            output_rel=output_rel,
            aggregate_cmd=aggregate_cmd,
            run_id=run_id,
            extra_env=extra_env,
        ),
        ssh_target=record.ssh_target,
        timeout=float(timeout_sec),
    )
    stderr_tail = (proc.stderr or "")[-2000:]
    if proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"reducer for run_id={run_id!r} exited {proc.returncode}: {stderr_tail.strip()[:500]}"
        )

    local_dir_path.mkdir(parents=True, exist_ok=True)
    output_basename = os.path.basename(output_rel)
    if output_rel.startswith("/"):
        # Absolute remote path: rsync_pull joins remote_path + remote_subdir
        # via path-stripping that mangles absolute inputs (`record.remote_path`
        # ends up prepended to the absolute target). Use the absolute dirname
        # as the rsync source directly, with an empty project-relative base.
        abs_dirname = os.path.dirname(output_rel)
        if abs_dirname in ("", "/"):
            # Files at filesystem root (``/foo.json``) — `validate_remote_path`
            # rejects an empty string, and an absolute-root path is almost
            # certainly a misconfiguration. Refuse with a clear message
            # rather than crash with the validator's generic error.
            raise errors.SpecInvalid(
                f"cluster_reduce output_path {output_rel!r} resolves to a "
                "filesystem-root location; choose an output under remote_path "
                "or a non-root absolute directory."
            )
        pull_proc = rsync_pull(
            ssh_target=record.ssh_target,
            remote_path=abs_dirname,
            remote_subdir="",
            local_dir=str(local_dir_path),
            include=[output_basename],
            timeout=float(timeout_sec),
        )
    else:
        pull_proc = rsync_pull(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            remote_subdir=os.path.dirname(output_rel) or ".",
            local_dir=str(local_dir_path),
            include=[output_basename],
            timeout=float(timeout_sec),
        )
    if pull_proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"rsync_pull of {output_rel!r} failed (exit "
            f"{pull_proc.returncode}): {(pull_proc.stderr or '').strip()[:300]}"
        )

    local_output = local_dir_path / output_basename
    reduced = _parse_local_output(local_output, run_id=run_id)

    return {
        "ok": True,
        "run_id": run_id,
        "output_path_remote": output_rel,
        "output_path_local": str(local_output),
        "reduced": reduced,
        "exit_code": int(proc.returncode),
        "stderr_tail": stderr_tail,
    }


__all__ = ["cluster_reduce"]


# Suppress "unused import" warnings for the lazy-imported tempfile
# usage; kept at module scope so the type checker has the right import
# graph even when the import isn't exercised by the typical happy path.
_ = tempfile  # noqa: F841 — placeholder for future tempdir-pull mode
