"""``update-run-constraints`` primitive — mutate cluster-side Features.

Lesson 9: ``scontrol update jobid=N Features=X`` works without losing
age priority. The framework's pre-existing path was "cancel + re-
submit," which loses every minute of accumulated age priority
(typically the difference between "shipped now" and "shipped in 6h").

This primitive exposes ``scontrol update`` as a first-class operation:

1. Read the run's sidecar; pull job_ids + ssh_target.
2. Batch EVERY job's ``scontrol update jobid=<id> Features=<expr>`` into
   ONE login-shell round-trip: one ``scontrol update … ; echo
   "<ack> <id> $?"`` segment per id, ``;``-joined, run inside a single
   ``bash -lc`` (the old path paid a full cold SSH round-trip PER job id —
   an N-job run serialised N ``scontrol update`` calls). Per-id outcomes
   are recovered by parsing the per-segment ack echoes: an ack rc of 0 is
   the ONLY success signal; a non-zero rc OR a missing ack (UNKNOWN — the
   channel was truncated / killed mid-batch) is a failure, never assumed ok.
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

# Per-segment ack marker for the batched update (see ``_parse_upd_acks``). Each
# ``scontrol update`` segment in the fused login-shell command echoes
# ``<prefix> <job_id> <rc>`` carrying that command's OWN exit status, so per-id
# outcomes survive the collapse from N SSH calls into ONE. Mirrors the
# scheduler sentinel-ack idiom (_engine._SCHED_ACK_PREFIX): PRESENCE of the ack
# is the affirmative proof the segment ran; its ABSENCE is UNKNOWN, never "ok".
# MIRROR: hpc_agent/infra/backends/_engine.py::_SCHED_ACK_PREFIX (the sentinel-ack idiom) pinned-by tests/ops/monitor/test_update_run_constraints.py::test_sentinel_ack_idiom_lockstep_with_scheduler_ack  # noqa: E501
_UPD_ACK_PREFIX = "__HPC_UPD__"

# Batch-level sentinel-ack prefix for the WHOLE fused login-shell command, via
# the canonical :func:`wrap_with_ack` / :func:`split_ack` helpers: the trailing
# echo's PRESENCE proves the batch shell ran to completion. Its ABSENCE — a
# severed channel (NAT idle-drop, expired remote deadline) delivering rc 0 with
# truncated stdout — fails the WHOLE batch closed: every id reads UNKNOWN,
# never a settled per-id outcome off a partial read (run-12 finding 24).
_BATCH_ACK_PREFIX = "__HPC_UPD_BATCH__="


def _validate_feature(feat: str) -> str:
    if not _FEATURE_RE.fullmatch(feat):
        raise errors.SpecInvalid(
            f"feature name {feat!r} contains characters outside [A-Za-z0-9._-]"
        )
    return feat


def _parse_upd_acks(stdout: str) -> dict[str, int]:
    """Parse ``<prefix> <job_id> <rc>`` ack lines into ``{job_id: rc}``.

    Each batched ``scontrol update`` segment echoes exactly one ack carrying the
    per-command exit status. An id with NO ack line is simply absent from the
    map — the caller reads that as UNKNOWN (never success): the remote shell
    never reached that segment's echo (a truncated channel, or the batch killed
    mid-flight). A malformed / non-integer rc token is likewise skipped, so a
    corrupt line can never be mistaken for a successful rc 0.
    """
    acks: dict[str, int] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0] != _UPD_ACK_PREFIX:
            continue
        try:
            acks[parts[1]] = int(parts[2])
        except ValueError:
            continue
    return acks


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
    from hpc_agent.infra.ssh_validation import split_ack, wrap_with_ack
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

    # Validate EVERY job id BEFORE composing the fused command — a job id is
    # command-substituted into the remote shell string, so an id failing the
    # injection guard is dropped to ``failed`` and must NEVER reach the composed
    # command (the same defence the per-feature guard gives above).
    valid_ids: list[str] = []
    failed: list[str] = []
    for jid in job_ids:
        if _FEATURE_RE.fullmatch(str(jid)):
            valid_ids.append(jid)
        else:
            failed.append(jid)

    updated: list[str] = []
    if valid_ids:
        # Batch the per-job ``scontrol update`` calls into ONE login-shell
        # round-trip (the latency fix). Old path: one cold SSH ``scontrol
        # update`` PER job id, serialised. New: one ``scontrol update … ; echo
        # "<ack> <id> $?"`` segment per id, ``;``-joined — NOT ``&&``, so an
        # early failure never skips a later id's update (the SGE range-cancel
        # path precedents the same ``;`` rule) — all inside a single
        # ``bash -lc`` so ``scontrol`` resolves off the cluster's login PATH
        # (many clusters expose the scheduler binaries only via the profile
        # chain — see infra/backends/_remote_base._execute_command). A batch of
        # one is byte-identically the same shape, so the single-job path is
        # unchanged in behaviour. ``shlex.quote`` fires twice — once on the
        # ``|``-bearing Features expr, once on the whole ``inner`` — so no
        # feature/job token can break out of the login-shell string.
        quoted_expr = shlex.quote(feature_expr)
        segments = [
            f"scontrol update jobid={jid} Features={quoted_expr} ; "
            f'echo "{_UPD_ACK_PREFIX} {jid} $?"'
            for jid in valid_ids
        ]
        inner = " ; ".join(segments)
        remote_cmd = f"bash -lc {shlex.quote(inner)}"
        try:
            # wrap_with_ack suffixes the batch with a trailing sentinel echo;
            # its ABSENCE is positive proof the batch shell never ran to
            # completion (a severed channel returns rc 0 + truncated stdout).
            cp = ssh_run(wrap_with_ack(remote_cmd, _BATCH_ACK_PREFIX), ssh_target=ssh_target)
        except (errors.SshUnreachable, TimeoutError, OSError):
            # The transport died before ANY ack could return: every id is
            # UNKNOWN, so none may be reported updated — a failed batch must
            # never read as full success.
            failed.extend(valid_ids)
        else:
            clean, batch_rc = split_ack(cp.stdout or "", _BATCH_ACK_PREFIX)
            if batch_rc is None:
                # No batch ack: the login shell never provably ran to
                # completion, so a truncated read cannot be distinguished from
                # a partial batch — fail the WHOLE batch closed (every id
                # UNKNOWN, never assumed updated). The update is idempotent on
                # the target feature set, so a re-run converges.
                failed.extend(valid_ids)
            else:
                acks = _parse_upd_acks(clean)
                for jid in valid_ids:
                    if acks.get(str(jid)) == 0:
                        updated.append(jid)
                    else:
                        # Non-zero rc (scontrol failed) OR a missing ack (UNKNOWN —
                        # the remote shell never reached that segment's echo): both
                        # are failures, never assumed ok.
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
