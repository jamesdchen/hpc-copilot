"""Pydantic models for the ``host-retarget`` primitive.

``host-retarget`` is run-12 finding 23 (``docs/design/history/run12-findings.md``
§23), the sanctioned expression of RULING 1 (upstream-fixes-2026-07.md): the
journal records the CLUSTER key — *history*, what the human approved — and
``user@host`` is *config*, resolved fresh from ``clusters.yaml`` at USE time by
:func:`hpc_agent.infra.clusters.resolve_ssh_target`. So moving an IN-FLIGHT run
to a different login node of the SAME cluster is a change of the record's ONE
``cluster`` key, not journal surgery.

Live origin (2026-07-11): discovery2 fork-exhausted and self-service
unrecoverable, discovery1 healthy — editing ``clusters.yaml`` did nothing for the
in-flight run because every consumer read the FROZEN ``record.ssh_target`` minted
at submit; the relay agent had to hand-edit the journal record JSON
(discovery2→discovery1). That is the exact improvisation class the block-drive
papercut work removes.

**Not a retarget-run.** ``retarget-run`` (proving-run-5 wave 5.2) mints a NEW
run_id, SUPERSEDES the failed attempt, and re-canaries — the right machinery for
a genuine cluster CHANGE where the jobs must be re-staged. ``host-retarget`` is
the opposite: the SAME run, its jobs still LIVE, only the login node it is talked
to THROUGH moves. So it keeps the run_id, the job_ids, the scratch, and the
scheduler — and it REFUSES any new cluster that does not serve the SAME scheduler
and scratch (that would invalidate ``remote_path`` / ``backend`` / ``job_ids``;
use ``retarget-run`` for a real move).

I/O contracts:

* Input: ``schemas/host_retarget.input.json`` (from ``HostRetargetInput``).
* Output: ``schemas/host_retarget.output.json`` (from ``HostRetargetResult``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HostRetargetInput(BaseModel):
    """Inputs to ``host-retarget``: the in-flight run + the new cluster KEY.

    ``run_id`` is the run to re-point — its record's ``cluster`` key is patched
    and every transport consumer picks up the new ``user@host`` at use time (no
    journal surgery). ``cluster`` is the NEW cluster KEY (a ``clusters.yaml``
    entry pointing at the healthy login node); it MUST serve the SAME scheduler
    and SAME scratch as the run's current cluster — a login-node failover, not a
    cluster move. ``reason`` is the optional human rationale journaled with the
    decision (e.g. "discovery2 fork-exhausted; failover to discovery1").
    """

    model_config = ConfigDict(extra="forbid", title="host-retarget input spec")

    run_id: str = Field(
        min_length=1,
        description=(
            "The in-flight run to re-point. Its record's `cluster` key is patched; "
            "resolve_ssh_target then derives the new user@host at use time."
        ),
    )
    cluster: str = Field(
        min_length=1,
        description=(
            "The NEW cluster KEY — a clusters.yaml entry for the healthy login node. "
            "MUST serve the SAME scheduler and scratch as the run's current cluster "
            "(a login-node failover). A different scheduler/scratch is refused "
            "(SpecInvalid) — use retarget-run for a genuine cluster move."
        ),
    )
    reason: str = Field(
        default="",
        description="Optional human rationale journaled with the host-retarget decision.",
    )


class HostRetargetResult(BaseModel):
    """The host-retarget outcome — the journaled login-node failover.

    ``stage_reached`` is ``host_retargeted``. ``old_cluster`` / ``new_cluster``
    and ``old_ssh_target`` / ``new_ssh_target`` are the audit of what moved (the
    run identity and its jobs did NOT move). ``decision_ts`` is the journaled
    decision record's timestamp — the provenance trail finding 23 said the
    hand-edit lacked.
    """

    model_config = ConfigDict(extra="forbid", title="host-retarget output data")

    stage_reached: Literal["host_retargeted"] = Field(
        description="The boundary reached — the cluster key was patched and journaled.",
    )
    run_id: str = Field(description="The re-pointed run (its identity and jobs are unchanged).")
    old_cluster: str = Field(description="The run's cluster key before the failover.")
    new_cluster: str = Field(description="The run's cluster key after the failover.")
    old_ssh_target: str = Field(description="The user@host the run was talked to through before.")
    new_ssh_target: str = Field(
        description="The user@host resolved from the new cluster key (now live for every consumer)."
    )
    decision_ts: str = Field(
        description="Timestamp of the journaled host-retarget decision (the provenance trail).",
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the failover.",
    )
