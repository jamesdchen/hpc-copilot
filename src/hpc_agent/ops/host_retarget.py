"""``host-retarget`` — move an in-flight run to a new login node of the SAME cluster.

Run-12 finding 23 (``docs/design/history/run12-findings.md`` §23), the sanctioned
expression of RULING 1: ``resolve_ssh_target`` already resolves ``user@host`` from
the record's journaled ``cluster`` key at USE time, so re-pointing an in-flight run
at a healthy login node is a change of that ONE key — journaled as a decision,
never a hand-edit of the record JSON (the improvisation the relay agent had to do
live when discovery2 fork-exhausted and discovery1 was healthy).

**Not a retarget-run.** ``retarget-run`` mints a NEW run_id, supersedes the failed
attempt, and re-canaries — for a genuine cluster CHANGE where the jobs re-stage.
``host-retarget`` keeps the SAME run (jobs live), the SAME run_id, the SAME scratch
and scheduler, and moves ONLY the login node. It composes nothing: it validates the
new cluster serves the same scheduler + scratch, journals the failover as a
decision, and patches the record's ``cluster`` (+ its provenance ``ssh_target``)
through the sanctioned locked ``update_run_record`` callback.

**The load-bearing guards** (each CAN fire — engineering-principles "verify a guard
can actually fire"):

* the new cluster MUST be resolvable from ``clusters.yaml`` to a ``user@host``
  (:class:`errors.ClusterUnknown` otherwise — you cannot fail over onto a cluster
  the framework doesn't know);
* it MUST serve the SAME scheduler and SAME scratch as the run's current cluster
  (:class:`errors.SpecInvalid` otherwise, routed to ``retarget-run``) — a different
  scheduler/scratch would silently invalidate the run's ``backend`` / ``remote_path``
  / ``job_ids``, which is a cluster MOVE, not a login-node failover;
* the new cluster MUST differ from the run's current one (a same-key host change is
  a plain ``clusters.yaml`` edit — there is nothing per-run to journal).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.host_retarget import HostRetargetInput, HostRetargetResult
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.state.run_record import RunRecord

__all__ = ["host_retarget"]


def _cluster_scheduler_scratch(cfg: dict[str, object]) -> tuple[str, str]:
    """The ``(scheduler, scratch)`` pair from a raw ``clusters.yaml`` entry."""
    return (
        str(cfg.get("scheduler") or "").strip(),
        str(cfg.get("scratch") or "").strip(),
    )


@primitive(
    name="host-retarget",
    verb="mutate",
    side_effects=[
        SideEffect(
            "writes-journal",
            "<experiment>/.hpc/decisions/run/<run_id>.jsonl (the failover decision) + "
            "the run record's cluster/ssh_target (locked update_run_record)",
        ),
    ],
    error_codes=[errors.SpecInvalid, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Move an IN-FLIGHT run to a different login node of the SAME cluster "
            "(run-12 finding 23): patch the run's journaled `cluster` key so "
            "resolve_ssh_target derives the new user@host at use time — no journal "
            "surgery. The new cluster MUST serve the same scheduler & scratch (a "
            "login-node failover, not a cluster move); a different scheduler/scratch "
            "is REFUSED — use retarget-run for that. Journals the failover as a "
            "decision (the provenance trail the hand-edit lacked)."
        ),
        spec_arg=True,
        spec_model=HostRetargetInput,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="host_retarget"),
    ),
    agent_facing=True,
)
def host_retarget(experiment_dir: Path, *, spec: HostRetargetInput) -> HostRetargetResult:
    """Re-point an in-flight run at a new login node; journal the failover.

    1. Load the run record (refuse a missing run).
    2. Resolve the new cluster to a ``user@host`` from ``clusters.yaml`` (refuse an
       unknown / un-derivable cluster) and refuse a same-cluster no-op.
    3. **Guard** (load-bearing): the new cluster must serve the SAME scheduler and
       scratch as the run's current cluster — else refuse, routed to ``retarget-run``.
    4. Journal the failover as a decision (the provenance trail).
    5. Patch the record's ``cluster`` (and its provenance ``ssh_target``) through the
       sanctioned locked ``update_run_record`` — every transport consumer then
       resolves the new host at use time.
    """
    from hpc_agent.infra.clusters import ClusterConfig, load_clusters_config, resolve_ssh_target
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import load_run, update_run_record

    run_id = spec.run_id
    new_cluster = spec.cluster.strip()

    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.SpecInvalid(
            f"host-retarget: no run record for run_id={run_id!r} — this verb re-points "
            "an EXISTING run at a new login node. There is nothing to re-point for a "
            "run that was never submitted."
        )

    old_cluster = str(getattr(record, "cluster", "") or "").strip()
    if new_cluster == old_cluster:
        raise errors.SpecInvalid(
            f"host-retarget: the run is already on cluster {new_cluster!r} — a "
            "same-cluster host change is a plain clusters.yaml edit (change "
            f"clusters.yaml[{new_cluster!r}].host); there is nothing per-run to journal. "
            "host-retarget moves the run to a DIFFERENT cluster key serving the same "
            "scheduler & scratch."
        )

    clusters = load_clusters_config()
    new_cfg = clusters.get(new_cluster)
    if not new_cfg:
        raise errors.ClusterUnknown(
            f"host-retarget: cluster {new_cluster!r} is absent from clusters.yaml — "
            "add the healthy login node as a cluster entry (host + user + scheduler + "
            "scratch) before failing over to it."
        )
    try:
        new_ssh_target = ClusterConfig.model_validate(new_cfg).ssh_target
    except Exception as exc:  # noqa: BLE001 — a malformed entry is a loud refusal here
        raise errors.ClusterUnknown(
            f"host-retarget: clusters.yaml[{new_cluster!r}] failed validation ({exc}) — "
            "fix the entry before failing over to it."
        ) from exc
    if not new_ssh_target:
        raise errors.ClusterUnknown(
            f"host-retarget: clusters.yaml[{new_cluster!r}] yields no derivable user@host "
            "(missing user/host) — the failover target must resolve a login node."
        )

    # The load-bearing guard: same scheduler + scratch, else this is a cluster MOVE.
    new_sched, new_scratch = _cluster_scheduler_scratch(new_cfg)
    old_cfg = clusters.get(old_cluster) or {}
    old_sched, old_scratch = _cluster_scheduler_scratch(old_cfg)
    # Fall back to the record's own backend when the old cluster is ad-hoc / absent.
    effective_old_sched = old_sched or str(getattr(record, "backend", "") or "").strip()
    if new_sched and effective_old_sched and new_sched != effective_old_sched:
        raise errors.SpecInvalid(
            f"host-retarget: cluster {new_cluster!r} runs scheduler {new_sched!r} but the "
            f"run is on {effective_old_sched!r}. A scheduler change invalidates the run's "
            "backend and job_ids — that is a cluster MOVE, not a login-node failover. Use "
            "retarget-run (it re-stages and re-canaries) instead."
        )
    remote_path = str(getattr(record, "remote_path", "") or "").strip()
    # Scratch must match: either the two entries agree, or (old ad-hoc) the run's
    # remote_path already lives under the new scratch — else results would move.
    scratch_ok = (
        (old_scratch and new_scratch and old_scratch == new_scratch)
        or (not old_scratch and new_scratch and remote_path.startswith(new_scratch))
        or (not new_scratch)
    )
    if not scratch_ok:
        raise errors.SpecInvalid(
            f"host-retarget: cluster {new_cluster!r} has scratch {new_scratch!r} but the "
            f"run's scratch is {old_scratch or remote_path!r}. A scratch change moves the "
            "run's result tree — that is a cluster MOVE, not a login-node failover. Use "
            "retarget-run instead."
        )

    old_ssh_target = str(getattr(record, "ssh_target", "") or "") or resolve_ssh_target(record)

    # (a) Journal the failover as a DECISION — the provenance trail the hand-edit lacked.
    decision = append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=run_id,
        block="host-retarget",
        response="y",
        proposal=(spec.reason or f"login-node failover {old_cluster or '?'} → {new_cluster}"),
        resolved={"cluster": new_cluster},
        provenance={
            "directed": True,
            "kind": "host-retarget",
            "old_cluster": old_cluster,
            "new_cluster": new_cluster,
            "old_ssh_target": old_ssh_target,
            "new_ssh_target": new_ssh_target,
            "reason": spec.reason or "login-node failover",
        },
    )

    # (b) Patch the record's cluster key (+ its provenance ssh_target) — the locked
    #     sanctioned RMW; every transport consumer then resolves the new host at use time.
    def _mutate(rec: RunRecord) -> None:
        rec.cluster = new_cluster
        rec.ssh_target = new_ssh_target

    update_run_record(experiment_dir, run_id, _mutate)

    return HostRetargetResult(
        stage_reached="host_retargeted",
        run_id=run_id,
        old_cluster=old_cluster,
        new_cluster=new_cluster,
        old_ssh_target=old_ssh_target,
        new_ssh_target=new_ssh_target,
        decision_ts=str(decision.get("ts", "")),
        reason=(
            f"re-pointed {run_id!r} from {old_cluster or '?'} ({old_ssh_target}) to "
            f"{new_cluster} ({new_ssh_target}); jobs and identity unchanged."
        ),
    )
