"""Pydantic models for the ``retarget-run`` workflow primitive.

``retarget-run`` is proving-run #5 wave 5.2 (the retarget RECOVERY ARM,
``docs/design/proving-run-5-hardening.md`` §3 wave 5.2, §4.1). The block-drive
anomaly terminators (``submit-s2``/``canary_failed``,
``submit-s3``/``watching_anomaly``) name recovery ACTIONS, but cluster-retarget
was the one action with no verb — so the agent freelanced ~5 steps (close out →
re-resolve → re-mint → supersede → re-canary) and fumbled three of them (proving
run #4/#5, findings 9/10/13).

This verb SEQUENCES those steps in CODE, not in the model, composing pieces that
already exist:

1. ``supersede(old_run_id)`` — mark/kill the failed attempt (wave-2 supersession);
2. a fresh ``resolve`` under a NEW run_name + the NEW cluster (re-derive
   ``job_env`` / ``ssh_target`` / ``backend`` / activation / the sidecar for the
   target cluster — the ``revise-resolved`` sidecar-reconstruction, re-pointed);
3. a re-canary (``submit-and-verify`` with ``stop_after_canary=True``).

The NEW run_name is the point: a run_id keys on parameters + run_name only (#207),
so a retarget that KEPT the run_name would mint the SAME run_id on the new cluster
and RE-ATTACH to the failed attempt instead of superseding it. A distinct
run_name gives a distinct run_id, so wave-2 supersession closes the old attempt
cleanly. The LLM names only the field delta (``{"cluster": "hoffman2"}``); the
new run_name is CODE-DERIVED (``<old_run_name>-<new_cluster>``), never authored.

**It does NOT bypass the gates.** ``retarget-run`` re-canaries (the #160 gate:
cheap, sandboxed, verified before any main array) and hands back an S2-shaped
brief with ``needs_decision=True``; the human re-``y``s it through the EXISTING
``append-decision`` path (so the authorship + brief-provenance gates still run on
the re-commit), and the main array stays behind the S3 greenlight gate.

I/O contracts:

* Input: ``schemas/retarget_run.input.json`` (from ``RetargetRunInput``).
* Output: ``schemas/retarget_run.output.json`` (from ``RetargetRunResult``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RetargetRunInput(BaseModel):
    """Inputs to ``retarget-run``: the failed attempt + the cluster-retarget delta.

    ``old_run_id`` is the attempt being retargeted — its on-disk sidecar
    (``.hpc/runs/<old_run_id>.json``, the v2 config snapshot) supplies the
    run-owned resolve inputs, and it is the run wave-2 supersession closes.
    ``patch`` is the field delta the nudge expressed — it MUST name a NEW
    ``cluster`` (a same-cluster delta is a plain revision → ``revise-resolved``,
    not a retarget). ``new_run_name`` is optional: when absent it is derived in
    CODE as ``<old_run_name>-<cluster>`` so the LLM never authors a run name.

    Like ``revise-resolved``, the ``patch`` may name ONLY resolver-owned /
    caller-authored INPUT fields; a key naming a CODE-DERIVED field (``job_env``,
    ``run_id``, ``cmd_sha``, ``executor``, ``ssh_target``, ``backend``,
    ``remote_path``, the sidecar) is REFUSED with ``SpecInvalid`` — the verb
    re-derives those from the new cluster.
    """

    model_config = ConfigDict(extra="forbid", title="retarget-run input spec")

    old_run_id: str = Field(
        min_length=1,
        description=(
            "The failed attempt's run_id — its sidecar supplies the run-owned "
            "resolve inputs, and wave-2 supersession closes it (and its -canary "
            "pairing). This is the run being retargeted OFF its cluster."
        ),
    )
    patch: dict[str, Any] = Field(
        description=(
            "The field-level delta {field: value} the retarget nudge expressed. "
            "MUST name a NEW `cluster` different from the failed attempt's (a "
            "same-cluster delta is a plain revision — use revise-resolved). Keys "
            "must be resolver-owned INPUT fields; a code-derived field (job_env, "
            "executor, ssh_target, backend, …) is refused (SpecInvalid) — the verb "
            "re-derives them for the target cluster."
        ),
    )
    new_run_name: str | None = Field(
        default=None,
        description=(
            "Optional explicit run name for the retargeted attempt. When null the "
            "verb DERIVES it as <old_run_name>-<cluster> so the LLM never authors "
            "a run name. Must differ from the old run_name so the new run_id does "
            "not collide with (and re-attach to) the superseded attempt (#207)."
        ),
    )


class RetargetRunResult(BaseModel):
    """The retarget outcome — an S2-shaped brief + the supersession audit.

    ``stage_reached`` is the re-canary outcome the human decides on:
    ``retargeted_canary_verified`` (green on the new cluster — greenlight S3),
    ``retargeted_canary_failed`` (failed AGAIN — the human picks the next
    recovery), or ``resolve_blocked`` (the fresh resolve surfaced its own
    decision, e.g. a live sibling of the NEW run_id from a prior retarget).
    ``needs_decision`` is always True: the human re-``y``s the brief through the
    EXISTING ``append-decision`` path — this verb produces the brief, it does NOT
    bypass the gates. ``superseded_run_id`` / ``applied_patch`` are the audit of
    what the retarget closed and changed.
    """

    model_config = ConfigDict(extra="forbid", title="retarget-run output data")

    stage_reached: Literal[
        "retargeted_canary_verified",
        "retargeted_canary_failed",
        "resolve_blocked",
    ] = Field(description="The re-canary outcome the amended brief stops at.")
    needs_decision: bool = Field(
        description="Always True — the human re-y's the retarget brief through append-decision.",
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the retarget outcome.",
    )
    superseded_run_id: str = Field(
        description="The failed attempt closed by wave-2 supersession (old→new link stamped).",
    )
    run_id: str | None = Field(
        default=None,
        description="The retargeted run's run_id (<new_run_name>-<cmd_sha[:8]>); None if unresolved.",
    )
    verified: bool = Field(
        default=False,
        description="True iff the re-canary verified green on the NEW cluster (#160 gate).",
    )
    failure_kind: str | None = Field(
        default=None,
        description="The canary failure kind when the re-canary failed on the new cluster; else None.",
    )
    brief: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The S2-shaped brief: the retargeted resolved values + the re-canary "
            "result (run_id, cluster, verified, est core-hours) + the supersession "
            "summary. The LLM relays it and takes the human's re-y."
        ),
    )
    applied_patch: dict[str, Any] = Field(
        default_factory=dict,
        description="The delta actually applied {field: value} — the audit of what the retarget changed.",
    )
