"""``inspect-deployment``: read-only, throttled probe of a deployed tree.

The framework verb an agent reaches for when it needs to *see what was
deployed* for an experiment — ``ls`` / ``find`` under ``REPO_DIR`` (or an
explicit scratch path) on the cluster — instead of issuing **raw ``ssh``**
(``ssh usc-discovery "ls /scratch1/..."``). Raw ssh bypasses the #346
connection-storm hardening (``ConnectTimeout`` / ``IdentitiesOnly`` / the
per-host ``safe_interval`` throttle in :mod:`hpc_agent.infra.ssh_throttle`),
which only protects the cluster if **all** SSH goes through
:func:`hpc_agent.infra.remote.ssh_run`. This verb is that single throttled
seam, broadened from S5's single-file
:func:`hpc_agent.infra.backends._remote_base.preflight_executor_exists`
existence check to a depth-bounded listing.

Hard constraints (see the issue this lands; ``docs/internals/engineering-
principles.md`` "The determinism boundary"):

* **Not a general remote exec.** It runs a *bounded, read-only* probe
  (``test -e`` + ``find -maxdepth N``); there is **no caller-supplied
  command string** (that would just be raw ssh with extra steps).
* **One connection per call**, through :func:`infra.remote.ssh_run` — so it
  respects ``safe_interval`` / ``ConnectTimeout`` like every other verb.
* **Scratch-confined.** The probed path MUST resolve strictly under the
  cluster's scratch root, reusing
  :func:`hpc_agent.infra.ssh_validation.validate_remote_path_under_scratch`
  (the same guard ``build_submit_spec`` uses) — no arbitrary traversal.

I/O contracts:

* Input: see ``hpc_agent/schemas/inspect_deployment.input.json``.
* Output: a ``dict`` matching ``schemas/inspect_deployment.output.json``.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = ["inspect_deployment"]

# A path that does not exist is reported as ``exists=False`` rather than as a
# transport error; the probe echoes this sentinel so a missing target is
# distinguishable from an empty directory in the same single SSH round-trip.
_MISSING_SENTINEL = "__HPC_INSPECT_MISSING__"

# Bounds on the probe so a single call can never become a traversal storm or a
# multi-megabyte envelope. ``depth`` is operator-supplied but clamped; the line
# cap is applied cluster-side via ``head`` so the wire never carries more.
_MAX_DEPTH = 4
_LINE_CAP = 1000


def _resolve_cluster(cluster: str) -> tuple[str, str]:
    """Return ``(ssh_target, scratch)`` for *cluster* from ``clusters.yaml``.

    Raises :class:`errors.ClusterUnknown` if the cluster is absent, and
    :class:`errors.SpecInvalid` if it carries no derivable ``ssh_target``
    (no ``host``/``user`` — a local-only or half-configured entry that this
    SSH-only verb cannot reach).

    Reads ``ssh_target``/``scratch`` from the raw config dict (mirroring
    ``ClusterConfig.ssh_target``: an explicit key wins, else ``user@host``).
    A read-only inspection must not be blocked by an unrelated missing field
    (e.g. ``scheduler``), so it deliberately does NOT force full
    ``ClusterConfig`` validation.
    """
    from hpc_agent.infra.clusters import load_clusters_config

    clusters = load_clusters_config()
    if cluster not in clusters:
        raise errors.ClusterUnknown(f"unknown cluster {cluster!r}; run `hpc-agent clusters list`")
    cfg = clusters[cluster] or {}
    if not isinstance(cfg, dict):
        raise errors.SpecInvalid(
            f"cluster {cluster!r} entry in clusters.yaml must be a mapping, got "
            f"{type(cfg).__name__}"
        )
    ssh_target = cfg.get("ssh_target")
    if not ssh_target:
        host, user = cfg.get("host"), cfg.get("user")
        ssh_target = f"{user}@{host}" if host and user else None
    if not ssh_target:
        raise errors.SpecInvalid(
            f"cluster {cluster!r} has no derivable ssh_target (host/user unset); "
            "inspect-deployment needs an SSH-reachable cluster."
        )
    return str(ssh_target), str(cfg.get("scratch") or "")


def _resolve_target_path(
    *, path: str | None, run_id: str | None, experiment_dir: Path
) -> tuple[str, str | None, str | None]:
    """Return ``(target_path, repo_dir, resolved_run_id)``.

    Exactly one of *path* / *run_id* selects the target:

    * ``--path`` is probed verbatim (after the scratch confinement check).
    * ``--run-id`` derives ``REPO_DIR`` from the run's journaled
      ``remote_path`` via :func:`deploy_target_for` — the SAME single
      derivation ``build_submit_spec`` and ``preflight_executor_exists`` use,
      so "where the agent inspects" can't drift from "where rsync deployed".

    Raises :class:`errors.SpecInvalid` when neither or both are given, or
    when ``--run-id`` names a run with no journal record / no ``remote_path``.
    """
    from hpc_agent.infra.backends._remote_base import deploy_target_for
    from hpc_agent.state.journal import load_run

    if bool(path) == bool(run_id):
        raise errors.SpecInvalid(
            "provide exactly one of --path or --run-id to select the target "
            "(--path probes a scratch path verbatim; --run-id derives REPO_DIR "
            "from the run's journaled remote_path)."
        )
    if path:
        return path, None, None
    record = load_run(experiment_dir, run_id or "")
    if record is None:
        raise errors.SpecInvalid(
            f"no journal record for run_id {run_id!r} under {experiment_dir}; "
            "pass --path to inspect an explicit scratch path instead."
        )
    if not record.remote_path:
        raise errors.SpecInvalid(
            f"run {run_id!r} has no remote_path recorded (pure-API backend, or a "
            "run that never deployed to a cluster); pass --path instead."
        )
    repo_dir = deploy_target_for(record.remote_path)
    return repo_dir, repo_dir, run_id


@primitive(
    name="inspect-deployment",
    verb="query",
    side_effects=[
        SideEffect("ssh", "<cluster> (one read-only depth-bounded listing probe)"),
    ],
    error_codes=[errors.RemoteCommandFailed, errors.SpecInvalid, errors.ClusterUnknown],
    idempotent=True,
    cli=CliShape(
        verb="inspect-deployment",
        help=(
            "Inspect a deployed experiment tree on the cluster (read-only, "
            "throttled): ls/find under REPO_DIR (--run-id) or an explicit scratch "
            "path (--path), through the connection-storm-safe ssh seam. Replaces "
            'raw `ssh <host> "ls ..."`, which bypasses the safe_interval / '
            "ConnectTimeout guards. Scratch-confined; no caller command string."
        ),
        experiment_dir_arg=True,
        requires_ssh=True,
        args=(
            CliArg(
                flag="--cluster",
                required=True,
                help="Cluster key from clusters.yaml — resolves ssh_target + scratch.",
            ),
            CliArg(
                flag="--run-id",
                help="Derive REPO_DIR from this run's journaled remote_path.",
            ),
            CliArg(
                flag="--path",
                help="Explicit absolute path to probe (must be strictly under scratch).",
            ),
            CliArg(
                flag="--depth",
                type=int,
                default=1,
                help=f"find -maxdepth (1..{_MAX_DEPTH}, default 1).",
            ),
        ),
    ),
    agent_facing=True,
)
def inspect_deployment(
    *,
    experiment_dir: str | Path,
    cluster: str,
    run_id: str | None = None,
    path: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """Probe a deployed tree on *cluster*, read-only and throttled.

    Returns a dict matching ``schemas/inspect_deployment.output.json``; the
    CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir* accepts
    both ``str`` (the CLI path) and ``Path``.

    The probe is a single ``ssh_run`` call (one throttled connection):
    ``test -e`` then ``find <path> -maxdepth <depth>`` over the resolved
    target. A non-existent target returns ``exists=False`` (not an error);
    a transport failure (ssh rc != 0) raises
    :class:`errors.RemoteCommandFailed`. Output is capped cluster-side at
    ``_LINE_CAP`` entries (``truncated`` flags when the cap was hit).

    Raises :class:`errors.SpecInvalid` for a bad depth, a path outside
    scratch, or an unresolvable target; :class:`errors.ClusterUnknown` for
    an unknown cluster.
    """
    from hpc_agent.infra import remote
    from hpc_agent.infra.ssh_validation import validate_remote_path_under_scratch

    exp = Path(experiment_dir)
    if not isinstance(depth, int) or depth < 1 or depth > _MAX_DEPTH:
        raise errors.SpecInvalid(f"--depth must be an integer in 1..{_MAX_DEPTH}, got {depth!r}")

    ssh_target, scratch = _resolve_cluster(cluster)
    target_path, repo_dir, resolved_run_id = _resolve_target_path(
        path=path, run_id=run_id, experiment_dir=exp
    )

    # A caller-supplied ``--path`` can only be confined when the cluster
    # declares a scratch root: ``validate_remote_path_under_scratch`` is a no-op
    # on an empty scratch, which would let ``--path`` probe anywhere. Refuse
    # rather than list unconfined. (``--run-id`` is exempt: its target is the
    # run's OWN journaled deploy path, not an arbitrary caller string.)
    if path and not scratch:
        raise errors.SpecInvalid(
            f"cluster {cluster!r} declares no scratch root, so an explicit --path "
            "cannot be confined; use --run-id to inspect a known deployment instead."
        )

    # Confine the probe to the cluster's scratch root (#184 reuse): rejects the
    # scratch root itself and anything not strictly below it. Also runs the
    # shape check (no shell metachars / leading dash) so the value is safe to
    # interpolate. A path outside scratch is a SpecInvalid — never probed.
    validate_remote_path_under_scratch(target_path, scratch)

    quoted = shlex.quote(target_path)
    # One read-only, depth-bounded probe. NO caller-supplied command string:
    # the only interpolated value is the scratch-confined, shape-validated,
    # shell-quoted path. ``find ... 2>/dev/null`` swallows per-entry permission
    # noise; ``head`` caps the listing cluster-side so the wire is bounded.
    probe = (
        f"if [ ! -e {quoted} ]; then printf '%s\\n' {shlex.quote(_MISSING_SENTINEL)}; "
        f"else find {quoted} -maxdepth {depth} 2>/dev/null | LC_ALL=C sort | "
        f"head -n {_LINE_CAP}; fi"
    )
    proc = remote.ssh_run(probe, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"inspect-deployment probe to {ssh_target} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )

    lines = [ln for ln in proc.stdout.splitlines() if ln != ""]
    exists = not (len(lines) == 1 and lines[0] == _MISSING_SENTINEL)
    entries = lines if exists else []
    return {
        "cluster": cluster,
        "ssh_target": ssh_target,
        "path": target_path,
        "repo_dir": repo_dir,
        "run_id": resolved_run_id,
        "depth": depth,
        "exists": exists,
        "entries": entries,
        "entry_count": len(entries),
        "truncated": len(entries) >= _LINE_CAP,
    }
