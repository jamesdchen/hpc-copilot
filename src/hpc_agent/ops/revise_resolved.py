"""``revise-resolved`` — the nudge becomes a field delta; code re-derives the rest.

Proving-run #5 wave 5.1, the ROOT fix (``docs/design/history/proving-run-5-hardening.md``
§1 thesis, §3, §4). The block-drive loop had one builder affordance left — **the
nudge**. The skill told the agent to "fold the nudge into the block's inputs,"
which in practice meant hand-writing a spec JSON, and that is where ``job_env``
got dropped (finding 13), ``scope_id`` improvised (finding 4), ``supersedes``
deleted (finding 10), ``EXECUTOR`` mangled (finding 17).

``revise-resolved`` removes the surface: the LLM names a **field delta**
``{field: value}`` ("use hoffman2 instead" IS ``{"cluster": "hoffman2"}``); this
verb applies it and RE-RESOLVES, re-deriving every field the delta invalidates.
The LLM cannot drop ``job_env``/``scope_id``/``executor`` because it never
touches them — it names one INPUT field and code recomputes the rest.

Mechanism (verified structures):

* the LATEST committed greenlight for ``(scope_kind, scope_id)`` supplies the
  base ``resolved`` walk-values (goal / task_generator / cluster) for the brief;
* the run's on-disk **sidecar** (``.hpc/runs/<run_id>.json`` — the v2
  config-snapshot, designed to "rebuild full context without consulting any
  external config") supplies the run-owned resolve inputs (executor,
  result_dir_template, task_count, profile, remote_path, resources) LOSSLESSLY;
* the cluster-owned fields (``ssh_target`` / ``backend`` / activation) are
  RE-DERIVED from ``clusters.yaml`` for the *patched* cluster — activation by
  ``build-submit-spec``'s own cluster fallback (the wave-4 fix), ``ssh_target`` /
  ``backend`` here (``build-submit-spec`` only *cross-checks* them, so a retarget
  that kept the old values would be REFUSED by the finding-18/19 gate);
* :func:`hpc_agent.ops.resolve_submit_inputs.resolve_submit_inputs` re-runs to
  re-derive ``job_env`` / ``run_id`` / ``cmd_sha`` / ``EXECUTOR`` / the sidecar.

**The load-bearing guard** (:func:`_assert_patch_targets_input_fields`) is the
whole point — a guard that CAN fire: the ``patch`` may target ONLY resolver-owned
/ caller-authored INPUT fields; a key naming a DERIVED field (``job_env``,
``run_id``, ``cmd_sha``, ``executor``, ``ssh_target``, ``backend``,
``remote_path``, ``total_tasks``, the sidecar, the activation triple) is refused
with :class:`~hpc_agent.errors.SpecInvalid`. That makes hand-authoring a derived
value structurally impossible: the only thing the LLM can express is a delta on
an input field.

**It does NOT bypass the gates.** The amended brief is committed by the human's
re-``y`` through the EXISTING ``append-decision`` path, so the human-authorship
gate (a ``task_generator`` / ``goal`` delta still needs a human utterance) and
the brief-provenance gate still run on the re-commit. ``revise-resolved`` only
produces the brief.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec
from hpc_agent._wire.workflows.revise_resolved import (
    ReviseResolvedInput,
    ReviseResolvedResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

# The resolver-owned / caller-authored INPUT partition — the SINGLE source of
# truth, imported (never redefined) so the allowlist can't drift from the walk's
# own field vocabulary (walk_submit_ambiguities + field_partition).
from hpc_agent.ops.submit.field_partition import (
    AUTO_RESOLVABLE_FIELDS,
    CODE_DERIVED_FIELDS,
    REQUIRED_CALLER_FIELDS,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["revise_resolved"]

# The fields a ``patch`` MAY name: caller-authored intent (goal /
# task_generator) + the deterministically-resolvable walk inputs (cluster,
# resources, entry_point, homogeneous_axes, …). Enumerated from
# WalkSubmitAmbiguitiesInput's own field vocabulary via the field partition, so
# the two never drift. ``mpi_ranks`` / ``user_preferred_partition`` are the two
# resource INPUT knobs the walk carries that the partition doesn't enumerate.
_REVISABLE_INPUT_FIELDS: frozenset[str] = (
    REQUIRED_CALLER_FIELDS
    | AUTO_RESOLVABLE_FIELDS
    | frozenset({"mpi_ranks", "user_preferred_partition"})
)

# CODE-DERIVED fields the LLM must NEVER set — each is recomputed from the input
# delta. Naming one in a ``patch`` is exactly the hand-authoring bug this verb
# removes (findings 13/4/10/17), so it earns a TARGETED refusal message. This is
# not the allow/deny mechanism (that is the allowlist above); it only makes the
# error name the field's real owner. BOUND (never copied) from the field
# partition — the single source of truth since run #6 F1 promoted the list to a
# third partition class shared with append-decision's resolved refusal.
_DERIVED_FIELDS: frozenset[str] = CODE_DERIVED_FIELDS


def _assert_patch_targets_input_fields(patch: dict[str, Any]) -> None:
    """The load-bearing guard: refuse a ``patch`` that touches a DERIVED field.

    A ``patch`` key must be a resolver-owned / caller-authored INPUT field
    (:data:`_REVISABLE_INPUT_FIELDS`). A key that names a code-derived field —
    ``job_env`` / ``run_id`` / ``executor`` / ``ssh_target`` / … — is refused
    with :class:`errors.SpecInvalid`, because those are re-derived from the input
    delta and hand-setting one is exactly the finding-13/4/10/17 bug. An
    unknown key (a typo, or a field outside the walk's vocabulary) is refused
    too — the allowlist is exhaustive over the resolvable input surface, so an
    unattributed key is never silently threaded.

    This is a guard that CAN fire (engineering-principles: "verify a guard can
    actually fire"): the only structured-state a caller can express here is a
    delta on an input field, which is what makes hand-authoring a derived value
    structurally impossible.
    """
    if not isinstance(patch, dict) or not patch:
        raise errors.SpecInvalid(
            "revise-resolved patch must be a non-empty {field: value} delta naming "
            "the resolver-owned input field(s) the nudge changes (e.g. "
            '{"cluster": "hoffman2"}).'
        )
    derived = sorted(k for k in patch if k in _DERIVED_FIELDS)
    unknown = sorted(
        k for k in patch if k not in _REVISABLE_INPUT_FIELDS and k not in _DERIVED_FIELDS
    )
    if derived:
        raise errors.SpecInvalid(
            f"revise-resolved: patch field(s) {derived} are CODE-DERIVED, not "
            "caller-authored — the verb re-derives them from the input delta "
            "(job_env/activation from the cluster, run_id/cmd_sha from the task "
            "list, executor from the interview, ssh_target/backend/remote_path "
            "from clusters.yaml). Naming one is the hand-authored-spec bug this "
            "verb removes (proving-run-5 findings 13/4/10/17): a hand-edit "
            "silently drops the rest. Name the INPUT field that changed instead "
            f"(one of {sorted(_REVISABLE_INPUT_FIELDS)}) — e.g. to move clusters "
            'use {"cluster": "<name>"}, not {"job_env": …} / {"ssh_target": …}.'
        )
    if unknown:
        raise errors.SpecInvalid(
            f"revise-resolved: patch field(s) {unknown} are not resolver-owned "
            f"input fields. A patch may name only {sorted(_REVISABLE_INPUT_FIELDS)} "
            "(the walk's caller-authored + auto-resolvable inputs). Fix the field "
            "name, or — if it is a derived output — do not set it (the verb "
            "re-derives it)."
        )


def _latest_committed_resolved(
    experiment_dir: Path, scope_kind: str, scope_id: str
) -> dict[str, Any]:
    """The base ``resolved`` from the scope's latest committed (``y``) decision.

    Mirrors ``block_drive._latest_committed_resolved`` (the commit-is-approval
    sentinel): the most recent ``response=='y'`` record's ``resolved`` is the
    approved plan the patch amends. Returns ``{}`` when nothing is committed yet
    — the brief's ``resolved`` is then rebuilt from the sidecar + patch alone.
    """
    from hpc_agent.state.decision_journal import read_decisions

    records = read_decisions(experiment_dir, scope_kind, scope_id)
    for record in reversed(records):
        if record.get("response") == "y":
            resolved = record.get("resolved")
            return dict(resolved) if isinstance(resolved, dict) else {}
    return {}


def _cluster_owned_fields(
    experiment_dir: Path, *, cluster: str, sidecar: dict[str, Any], cluster_changed: bool
) -> tuple[str, str, str]:
    """Re-derive ``(ssh_target, backend, remote_path)`` for the effective cluster.

    ``build-submit-spec`` re-derives ACTIVATION from the cluster (the wave-4
    fix), but only CROSS-CHECKS ``ssh_target`` / ``backend`` — so a retarget that
    kept the old cluster's values would be refused by the finding-18/19 gate.
    This re-derives them from ``clusters.yaml`` for the (possibly-patched)
    cluster, so the delta stays a single field the LLM named:

    * ``ssh_target`` — the cluster entry's ``user@host`` (``ClusterConfig``);
    * ``backend`` — the cluster entry's ``scheduler`` family;
    * ``remote_path`` — re-anchored under the cluster's ``scratch`` on a retarget
      (keeping the experiment's leaf dir), else the sidecar's prior path.

    On a cluster CHANGE the cluster MUST be resolvable from clusters.yaml (host +
    user + scheduler) — else refuse loudly (you cannot retarget onto a cluster
    the framework doesn't know). When the cluster is UNCHANGED and its entry is
    ad-hoc / absent (empty config), fall back to the prior run's journaled
    ``RunRecord`` values (an unchanged cluster's identity did not move).
    """
    from hpc_agent.infra.clusters import ClusterConfig, load_clusters_config

    cfg = load_clusters_config().get(cluster) or {}
    derived_ssh: str | None = None
    if cfg:
        try:
            derived_ssh = ClusterConfig.model_validate(cfg).ssh_target
        except Exception:  # noqa: BLE001 — a malformed entry is another guard's concern
            derived_ssh = None
    derived_backend = (str(cfg.get("scheduler") or "").strip()) or None
    scratch = str(cfg.get("scratch") or "").strip()
    prior_remote = str(sidecar.get("remote_path") or "").strip()

    # remote_path: re-anchor under the new scratch on a retarget (not cross-
    # checked, so keeping the leaf dir is safe), else keep the prior path.
    if cluster_changed and scratch and prior_remote:
        leaf = prior_remote.rstrip("/").rsplit("/", 1)[-1]
        remote_path: str | None = scratch.rstrip("/") + "/" + leaf
    else:
        remote_path = prior_remote or None

    ssh_target: str | None = derived_ssh
    backend: str | None = derived_backend
    if ssh_target is None or backend is None:
        if cluster_changed:
            raise errors.SpecInvalid(
                f"revise-resolved: cluster {cluster!r} is not resolvable from "
                "clusters.yaml (needs host + user + scheduler) — a retarget "
                "re-derives ssh_target/backend/activation from the cluster entry, "
                "so the target cluster must be onboarded. Run `hpc-agent setup "
                f"--cluster {cluster}` (or fix the name), then re-nudge."
            )
        # Unchanged ad-hoc cluster: the identity did not move — reuse the prior
        # attempt's journaled RunRecord values.
        from hpc_agent.state.journal import load_run

        rec = load_run(experiment_dir, sidecar.get("run_id") or "")
        ssh_target = ssh_target or (rec.ssh_target if rec else None)
        backend = backend or (rec.backend if rec and rec.backend else None)
        remote_path = remote_path or (rec.remote_path if rec else None)

    if not ssh_target or not backend or not remote_path:
        raise errors.SpecInvalid(
            f"revise-resolved: could not re-derive the cluster-owned fields for "
            f"{cluster!r} (ssh_target={ssh_target!r}, backend={backend!r}, "
            f"remote_path={remote_path!r}). The cluster entry must carry host + "
            "user + scheduler + scratch, or the prior run's sidecar/record must "
            "supply them for an unchanged cluster."
        )
    return ssh_target, backend, remote_path


def _reconstruct_resolve_spec(
    experiment_dir: Path, *, run_id: str, sidecar: dict[str, Any], patch: dict[str, Any]
) -> ResolveSubmitInputsSpec:
    """Rebuild a ``ResolveSubmitInputsSpec`` from the on-disk sidecar + the patch.

    Run-owned inputs (executor, result_dir_template, task_count, profile,
    resources, runtime, campaign_id) come from the sidecar VERBATIM — it stores
    them as the config snapshot. Cluster-owned inputs (ssh_target / backend /
    remote_path) are re-derived from clusters.yaml; activation (modules /
    conda_source / conda_env) is DROPPED so ``build-submit-spec`` re-derives it
    from the patched cluster. ``run_id`` / ``cmd_sha`` are placeholders
    ``resolve-submit-inputs`` overrides with ``compute-run-id``'s values.
    """
    prior_cluster = str(sidecar.get("cluster") or "").strip()
    cluster = str(patch.get("cluster", prior_cluster)).strip()
    if not cluster:
        raise errors.SpecInvalid(
            "revise-resolved: no cluster on the prior sidecar and none in the "
            "patch — cannot re-resolve without a cluster."
        )
    cluster_changed = "cluster" in patch and str(patch["cluster"]).strip() != prior_cluster
    ssh_target, backend, remote_path = _cluster_owned_fields(
        experiment_dir, cluster=cluster, sidecar=sidecar, cluster_changed=cluster_changed
    )

    resources: dict[str, Any] = dict(sidecar.get("resources") or {})
    if "walltime_sec" in patch:
        resources["walltime_sec"] = patch["walltime_sec"]

    def _res(*keys: str) -> Any:
        for key in keys:
            val = resources.get(key)
            if val is not None:
                return val
        return None

    run_name = run_id.rsplit("-", 1)[0] or run_id
    placeholder_run_id = run_id  # a valid RunIdStrict; compute-run-id overrides it
    placeholder_cmd_sha = str(sidecar.get("cmd_sha") or ("0" * 8))
    task_count = int(sidecar.get("task_count") or 1)
    result_dir_template = (
        str(sidecar.get("result_dir_template") or "") or "results/{run_id}/task_{task_id}"
    )

    submit = BuildSubmitSpecInput(
        profile=str(sidecar.get("profile") or run_name),
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        run_id=placeholder_run_id,
        cmd_sha=placeholder_cmd_sha,
        total_tasks=task_count,
        backend=backend,  # type: ignore[arg-type]  # validated against BackendName at the boundary
        result_dir_template=result_dir_template,
        walltime_sec=_res("walltime_sec", "walltime"),
        mem_mb=_res("mem_mb", "mem"),
        cpus=_res("cpus"),
        runtime=sidecar.get("runtime"),
        campaign_id=sidecar.get("campaign_id"),
        # modules / conda_source / conda_env DROPPED → build-submit-spec re-derives
        # the activation from (cluster, clusters.yaml) — the wave-4 fix.
    )
    sidecar_input = WriteRunSidecarInput(
        run_id=placeholder_run_id,
        cmd_sha=placeholder_cmd_sha,
        # The per-task command is the interview's materialized value; the sidecar
        # stored it verbatim, and resolve-submit-inputs re-applies the interview's
        # value on top when present (so it never rides on the LLM).
        executor=str(sidecar.get("executor") or "python3 .hpc/_hpc_dispatch.py"),
        result_dir_template=result_dir_template,
        task_count=task_count,
        cluster=cluster,
        profile=sidecar.get("profile"),
        remote_path=remote_path,
        resources=resources or None,
        runtime=sidecar.get("runtime"),
        campaign_id=sidecar.get("campaign_id"),
        # env DROPPED — the activation is re-derived from the cluster.
    )
    return ResolveSubmitInputsSpec(run_name=run_name, submit=submit, sidecar=sidecar_input)


@primitive(
    name="revise-resolved",
    verb="workflow",
    composes=["resolve-submit-inputs"],
    side_effects=[
        SideEffect(
            "writes-sidecar",
            "<experiment>/.hpc/runs/<run_id>.json (the re-resolved sidecar)",
        ),
    ],
    error_codes=[errors.SpecInvalid, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="scope_id",
    cli=CliShape(
        help=(
            "Apply a resolver-owned FIELD DELTA {field: value} to a run's latest "
            "greenlit resolved and RE-RESOLVE, re-deriving job_env/executor/"
            "run_id/cmd_sha/sidecar from the delta (proving-run-5 wave 5.1). The "
            "patch may name only INPUT fields (cluster, goal, task_generator, "
            "walltime_sec, entry_point, …); a derived field (job_env, executor, "
            "ssh_target, …) is REFUSED. Returns the amended brief for the human's "
            "re-y — it does NOT bypass the append-decision gates."
        ),
        spec_arg=True,
        spec_model=ReviseResolvedInput,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="revise_resolved"),
    ),
    agent_facing=True,
)
def revise_resolved(experiment_dir: Path, *, spec: ReviseResolvedInput) -> ReviseResolvedResult:
    """Apply the field delta and re-resolve; return the amended brief.

    1. **Guard** (load-bearing): the patch may target ONLY resolver-owned input
       fields — a derived field is refused (:func:`_assert_patch_targets_input_fields`).
    2. Load the base ``resolved`` from the scope's latest committed greenlight.
    3. Read the run's on-disk sidecar for the run-owned resolve inputs; rebuild a
       ``ResolveSubmitInputsSpec``, applying the patch and re-deriving the
       cluster-owned fields (:func:`_reconstruct_resolve_spec`).
    4. Re-run ``resolve-submit-inputs`` → ``job_env`` / ``run_id`` / ``cmd_sha`` /
       ``EXECUTOR`` / the sidecar all re-derived from the delta.
    5. Return the amended S1-shaped brief; the human re-``y``s it through the
       EXISTING append-decision path (the authorship + provenance gates still run
       there — this verb does NOT bypass them).

    Raises :class:`errors.SpecInvalid` on a derived-field patch, a scope with no
    resolved run to amend (no sidecar), or an unresolvable target cluster.
    """
    _assert_patch_targets_input_fields(spec.patch)

    from hpc_agent.state.runs import read_run_sidecar

    base_resolved = _latest_committed_resolved(experiment_dir, spec.scope_kind, spec.scope_id)
    run_id = spec.scope_id

    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"revise-resolved: no resolved-run sidecar for scope_id={run_id!r} — "
            "this verb amends a RESOLVED prior (its per-run sidecar carries the "
            "run-owned resolve inputs to re-derive from). The pre-resolve S1 "
            "boundary has no run yet: resolve it first (submit-s1), then a nudge "
            "revises it. (A cluster retarget under a live canary IS a resolved "
            "prior — its sidecar exists.)"
        ) from exc

    resolve_spec = _reconstruct_resolve_spec(
        experiment_dir, run_id=run_id, sidecar=sidecar, patch=spec.patch
    )
    rr = resolve_submit_inputs(experiment_dir, spec=resolve_spec)

    # The amended brief mirrors submit-s1's brief shape: the re-derived resolved
    # values (base + the patch reflected, so a goal/task_generator delta is
    # visible to the authorship gate + the audit at re-commit) + the fresh
    # resolve output (submit_spec with job_env re-derived, run_id, sidecar_path).
    resolved: dict[str, Any] = {k: v for k, v in base_resolved.items() if k != "next_block"}
    resolved.update(spec.patch)
    brief: dict[str, Any] = {
        "resolved": resolved,
        "resolve": {
            "stage_reached": rr.stage_reached,
            "reason": rr.reason,
            "run_id": rr.run_id,
            "submit_spec": rr.submit_spec,
            "sidecar_path": rr.sidecar_path,
            "prior_run_id": rr.prior_run_id,
            "prior_status": rr.prior_status,
            "prior_cluster": rr.prior_cluster,
        },
        "patch": dict(spec.patch),
    }
    return ReviseResolvedResult(
        stage_reached=rr.stage_reached,
        needs_decision=True,
        reason=rr.reason,
        run_id=rr.run_id,
        brief=brief,
        applied_patch=dict(spec.patch),
    )
