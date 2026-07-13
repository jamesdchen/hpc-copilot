"""``update-run-constraints`` primitive — mutate cluster-side Features.

Lesson 9: ``scontrol update jobid=N Features=X`` works without losing
age priority. The framework's pre-existing path was "cancel + re-
submit," which loses every minute of accumulated age priority
(typically the difference between "shipped now" and "shipped in 6h").

This primitive exposes ``scontrol update`` as a first-class operation:

1. Read the run's sidecar; pull job_ids + ssh_target.
2. For each job_id, run ``scontrol update jobid=<id> Features=<expr>``
   over SSH.
3. Update the sidecar's recorded features so subsequent observers see
   the new set.

Idempotent on (run_id, target features set): re-running with the
same final feature set produces the same on-cluster state.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.update_run_constraints import (
    UpdateRunConstraintsResult,
    UpdateRunConstraintsSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.clusters import resolve_ssh_target
from hpc_agent.infra.io import atomic_locked_update

if TYPE_CHECKING:
    from pathlib import Path

# SLURM uses ``|`` for OR, ``&`` for AND. We don't infer; the user
# supplies the final feature list and we join with ``|`` as the
# common case (any-of). A future spec might add ``operator: 'and'|'or'``
# but that's a YAGNI for the lesson-9 use case.
_FEATURE_JOIN = "|"

# Feature names: alphanumerics, underscores, hyphens, periods. Reject
# anything else as a defence against shell injection through the
# scontrol command.
_FEATURE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_feature(feat: str) -> str:
    if not _FEATURE_RE.fullmatch(feat):
        raise errors.SpecInvalid(
            f"feature name {feat!r} contains characters outside [A-Za-z0-9._-]"
        )
    return feat


@primitive(
    name="update-run-constraints",
    verb="mutate",
    side_effects=[
        SideEffect("ssh", "<cluster> (scontrol update jobid=<id> Features=<expr>)"),
        SideEffect(
            "writes-sidecar",
            "<experiment>/.hpc/runs/<run_id>.json (constraints.features mirror)",
        ),
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
            "Mutate a run's cluster-side SLURM Features in place via "
            "`scontrol update jobid=<id> Features=<expr>` — retargets constraints "
            "WITHOUT cancel+resubmit, so the jobs keep their accumulated age "
            "priority; mirrors the new feature set onto the run sidecar."
        ),
        spec_arg=True,
        spec_model=UpdateRunConstraintsSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="update_run_constraints"),
    ),
    agent_facing=True,
)
def update_run_constraints(
    experiment_dir: Path,
    *,
    spec: UpdateRunConstraintsSpec,
) -> UpdateRunConstraintsResult:
    """Run ``scontrol update jobid=<id> Features=<expr>`` for each job
    in the run's sidecar; update the sidecar's recorded Features.

    Either ``spec.set_features`` (replace) or ``spec.add_features``
    (extend) must be set. Both is rejected — ambiguous semantics.
    """
    if spec.set_features is not None and spec.add_features:
        raise errors.SpecInvalid(
            "Pass exactly one of `set_features` (replace) or `add_features` (extend)"
        )
    if spec.set_features is None and not spec.add_features:
        raise errors.SpecInvalid("Pass at least one of `set_features` or `add_features`")

    from hpc_agent.infra.remote import ssh_run
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar, run_sidecar_path

    sidecar = read_run_sidecar(experiment_dir, spec.run_id)
    job_ids = list(sidecar.get("job_ids") or [])
    if not job_ids:
        raise errors.SpecInvalid(
            f"sidecar for run_id={spec.run_id!r} has no job_ids; nothing to update"
        )

    # Resolve the SSH target from the journal RunRecord. ssh_target is
    # NOT a v2 sidecar field (see _V2_CONFIG_FIELDS in state/runs.py) —
    # the journal record at ~/.claude/hpc/<repo_hash>/runs/<run_id>.json
    # is the canonical store.
    record = load_run(experiment_dir, spec.run_id)
    ssh_target = resolve_ssh_target(record) if record is not None else None
    if not ssh_target:
        raise errors.SpecInvalid(
            f"no journal record for run_id={spec.run_id!r}, or the record "
            "is missing ssh_target; scontrol update requires explicit "
            "cluster routing"
        )

    # Compute the new Features expression.
    constraints = sidecar.get("constraints") or {}
    existing = list(constraints.get("features") or [])
    if spec.set_features is not None:
        new_features = [_validate_feature(f) for f in spec.set_features]
    else:
        added = [_validate_feature(f) for f in spec.add_features]
        new_features = list(dict.fromkeys([*existing, *added]))

    if not new_features:
        raise errors.SpecInvalid("computed feature set is empty")

    feature_expr = _FEATURE_JOIN.join(new_features)

    updated: list[str] = []
    failed: list[str] = []
    for jid in job_ids:
        if not _FEATURE_RE.fullmatch(jid):  # job-id is also command-substituted
            failed.append(jid)
            continue
        cmd = f"scontrol update jobid={jid} Features={shlex.quote(feature_expr)}"
        try:
            cp = ssh_run(cmd, ssh_target=ssh_target)
        except (errors.SshUnreachable, TimeoutError, OSError):
            failed.append(jid)
            continue
        if cp.returncode == 0:
            updated.append(jid)
        else:
            failed.append(jid)

    # Persist the new feature set on the sidecar (best-effort; the
    # cluster-side update succeeded for `updated` jobs regardless of
    # whether the local mirror lands). ``atomic_locked_update`` re-reads
    # the sidecar under flock, applies only the constraints diff, and
    # writes atomically. The earlier hand-rolled read/mutate/tempfile
    # path lost concurrent updates from monitor_flow / status writes
    # because it locked nothing.
    if updated:
        target = run_sidecar_path(experiment_dir, spec.run_id)

        def _apply(doc: dict | None) -> dict:
            base = dict(doc) if isinstance(doc, dict) else dict(sidecar)
            cstr = base.get("constraints")
            if not isinstance(cstr, dict):
                cstr = {}
            cstr["features"] = new_features
            base["constraints"] = cstr
            return base

        atomic_locked_update(target, _apply)

    return UpdateRunConstraintsResult(
        run_id=spec.run_id,
        job_ids_updated=updated,
        job_ids_failed=failed,
        new_features=new_features,
    )
