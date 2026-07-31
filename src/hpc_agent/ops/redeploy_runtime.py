"""``redeploy-runtime``: restore the framework artifacts a run depends on.

The repair verb the 2026-07-30 incident did not have. The deployed combiner
(``.hpc/_hpc_combiner.py``) went missing from the cluster; nothing noticed
until the cross-wave reduce failed at aggregate time, and the only way back was
a human hand-launching the reduce over ssh at 22:00. There was no command to
name in a remediation, because every path that ships ``deploy_runtime``'s
artifacts was welded to a *submit* — and a run being aggregated must not be
resubmitted to get its combiner back.

So: one verb that re-ships the framework runtime for an EXISTING run and
submits nothing.

* Resolves ``ssh_target`` / ``remote_path`` from the run's own journal record —
  no cluster flag to get wrong, no path for the caller to compose.
* Re-ships to the base ``remote_path`` **and** to the run's content-addressed
  code tree when it has one (``job_env["REPO_DIR"]``, §10.S4). Those are two
  distinct deploy roots and a job reads its framework files from the tree, so a
  repair that fixed only one of them would fix the wrong half half the time.
* Bypasses the content-hash deploy cache by default (``use_cache=False``).
  That is the whole point: the cache is what made the dropout permanent — it
  records what a past deploy *wrote*, never what is on disk *now*, so a
  cache-hit on a deleted file re-affirms itself forever
  (:mod:`hpc_agent.execution.mapreduce.deployed_artifact`, dropout D2).
* Verifies the result with a presence+sha probe folded into the same ssh that
  reads it back, and returns that verdict. A repair that reports success
  without positive evidence is the failure mode this verb exists to end.

Safe against a live run: ``deploy_runtime``'s transfer is temp-then-rename (not
``--inplace``), so replacing ``.hpc/_hpc_dispatch.py`` under a running array
does not tear it — the same property that lets every re-submit re-deploy today.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.execution.mapreduce.deployed_artifact import (
    COMBINER_REL,
    CombinerProbe,
    combiner_probe_snippet,
    split_combiner_probe,
)

__all__ = ["redeploy_runtime"]


def _scheduler_for_cluster(cluster: str) -> str | None:
    """The cluster's scheduler family from ``clusters.yaml``, or ``None``.

    ``None`` is a fine answer, not a failure: ``deploy_runtime`` then ships the
    sge + slurm template families instead of one, which for a REPAIR is the
    more conservative choice — a superset of what the run needs.
    """
    if not cluster:
        return None
    try:
        from hpc_agent.infra.clusters import load_clusters_config

        cfg = load_clusters_config().get(cluster) or {}
    except Exception:  # noqa: BLE001 — an unreadable config must not block a repair
        return None
    sched = cfg.get("scheduler") if isinstance(cfg, dict) else None
    return str(sched) if sched else None


def _probe_roots(*, ssh_target: str, roots: list[str]) -> dict[str, CombinerProbe | None]:
    """Read back each root's deployed-combiner presence+sha in ONE ssh.

    The verification leg. Every root's probe line rides a single exec — the
    snippets are concatenated and the lines demultiplexed in order — so
    confirming a two-root repair still costs one round-trip, not two.
    """
    from hpc_agent.infra import remote

    script = "".join(combiner_probe_snippet(root=r) for r in roots)
    proc = remote.ssh_run(script, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"redeploy-runtime verification probe failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()[:200]}"
        )
    out = proc.stdout or ""
    verdicts: dict[str, CombinerProbe | None] = {}
    for root in roots:
        probe, out = split_combiner_probe(out)
        verdicts[root] = probe
    return verdicts


@primitive(
    name="redeploy-runtime",
    verb="mutate",
    side_effects=[
        SideEffect("ssh", "<cluster> (re-ship framework runtime files, then verify)"),
        SideEffect(
            "writes-cluster",
            "<remote_path>/.hpc/{_hpc_combiner.py,_hpc_dispatch.py,templates/} + hpc_agent/",
        ),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.RemoteCommandFailed],
    idempotent=True,
    idempotency_key="(run_id, deploy_root)",
    cli=CliShape(
        verb="redeploy-runtime",
        help=(
            "Re-ship the framework runtime files (combiner, dispatcher, job "
            "templates, importable stubs) for an existing run, to its base "
            "remote_path AND its code tree, with the deploy cache bypassed. "
            "Submits nothing. The repair for a combiner/dispatcher that went "
            "missing on the cluster."
        ),
        experiment_dir_arg=True,
        requires_ssh=True,
        args=(
            CliArg(
                "--run-id",
                type=str,
                required=True,
                help="Run identifier — resolves ssh_target + remote_path from the journal.",
            ),
            CliArg(
                "--use-cache",
                action="store_true",
                help=(
                    "Honour the content-hash deploy cache instead of bypassing it. "
                    "Off by default: a cache HIT on a file that is no longer on the "
                    "cluster is exactly the dropout this verb repairs."
                ),
            ),
        ),
    ),
    agent_facing=True,
)
def redeploy_runtime(
    experiment_dir: Path,
    *,
    run_id: str,
    use_cache: bool = False,
) -> dict[str, Any]:
    """Re-ship the framework runtime for *run_id*; verify and report.

    Returns ``{ok, run_id, ssh_target, deploy_roots, combiner_rel, verified}``
    where ``verified`` maps each deploy root to
    ``{"state": "present"|"stale"|"absent"|"unknown", "sha": <str|None>}``.
    ``ok`` is True iff EVERY root reads back ``present`` — a root whose probe
    line never arrived reads ``unknown`` and does NOT green the verb (absence
    of evidence is not evidence of repair).

    Raises :class:`errors.SpecInvalid` when the run has no journal record or no
    ``remote_path`` (a pure-API backend has no tree to deploy into), and
    :class:`errors.RemoteCommandFailed` when the transfer or the verification
    probe fails.
    """
    from hpc_agent.infra.clusters import resolve_ssh_target
    from hpc_agent.infra.code_tree import repo_dir_for_run
    from hpc_agent.infra.transport import deploy_runtime as _deploy_runtime
    from hpc_agent.state.journal import load_run

    if not run_id:
        raise errors.SpecInvalid("run_id is required")
    exp = Path(experiment_dir)
    record = load_run(exp, run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for run_id={run_id!r} under {exp}")
    if not record.remote_path:
        raise errors.SpecInvalid(
            f"run {run_id!r} has no remote_path recorded (a pure-API backend, or a run "
            "that never deployed to a cluster) — there is no deploy root to repair."
        )

    ssh_target = resolve_ssh_target(record)
    base = record.remote_path
    # The run's code tree is a SECOND deploy root when §10.S4 pinned one: the
    # job reads its framework files from there, the aggregate's combine leg
    # from the base. Repair both, de-duplicated and base-first so the shared
    # anchor is restored even if the tree leg fails.
    tree = repo_dir_for_run(base, getattr(record, "job_env", None))
    roots = [base] if tree == base else [base, tree]

    scheduler = _scheduler_for_cluster(getattr(record, "cluster", "") or "")
    for root in roots:
        _deploy_runtime(
            ssh_target=ssh_target,
            remote_path=root,
            scheduler=scheduler,
            use_cache=use_cache,
        )

    verdicts = _probe_roots(ssh_target=ssh_target, roots=roots)
    verified = {
        root: {
            "state": probe.state if probe is not None else "unknown",
            "sha": probe.sha if probe is not None else None,
        }
        for root, probe in verdicts.items()
    }
    return {
        "ok": all(v["state"] == "present" for v in verified.values()),
        "run_id": run_id,
        "ssh_target": ssh_target,
        "deploy_roots": roots,
        "combiner_rel": COMBINER_REL,
        "verified": verified,
    }
