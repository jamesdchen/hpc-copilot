"""Pydantic models for the ``reproduce-run`` workflow primitive.

``reproduce-run`` is the reproduction-receipt wave, task T5 (the MINT half;
``docs/design/reproduction-receipt.md`` is the decision record). It re-runs a
finished experiment against a pinned identity so a later ``verify-reproduction``
can answer the one honest question — *did it reproduce?* — without the framework
ever judging whether two numbers are "close enough".

It borrows the ``retarget-run`` SHAPE (``ops/retarget_run.py``): a non-blocking
mint that re-resolves under a NEW run_name and hands off to ``submit-s2`` via
``next_block`` — returning in SECONDS. It NEVER runs the re-canary or the array
inline (S2's detach-by-contract worker owns the canary poll), which is what
makes it safe to expose as a curated MCP tool (the run-#8 wedge: an agent unable
to reach a blocking verb over MCP hand-ran a recovery against a throttled
cluster). Unlike ``retarget-run`` it supersedes NOTHING — a reproduction
*closes nothing*; the original stays valid, the second run is a one-directional
``reproduces`` provenance back-link, not a lineage replacement (decision record:
"``reproduces`` is a sidecar provenance field — NOT supersession").

Two guards stand before the mint (decision record, findings 3/4):

* **The drift guard, both dimensions.** ``cmd_sha`` is PARAMETER identity only
  (#207) — an executor-body edit keeps it — so a ``cmd_sha`` match alone would
  "reproduce" drifted code. ``reproduce-run`` refuses when the CURRENT tree's
  ``cmd_sha`` for the original's run_name differs from the recorded one (param
  drift, naming BOTH shas + the first differing task index via the sidecar's
  ``trial_params`` pre-image), AND when ``state.code_drift.detect_code_drift``
  finds the recorded ``executor`` / ``tasks_py_sha`` drifted from current (code
  drift). A moved/edited tree REFUSES with the evidence — v1 never
  reconstructs-and-pretends (tree-snapshot storage is out).
* **The disjoint remote_path.** The reproduction resolves under a
  ``<orig_remote_path>-repro`` root, never nested under or a sibling within the
  original's tree — the per-task fallback reduce scans ``remote_path``
  recursively, so a shared subtree would blend the repro's rows into the
  original's future mean (the run-#6 11-row-mean class).

I/O contracts:

* Input: ``schemas/reproduce_run.input.json`` (from ``ReproduceRunInput``).
* Output: ``schemas/reproduce_run.output.json`` (from ``ReproduceRunResult``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hpc_agent._wire._shared import RunIdStrict


class ReproduceRunInput(BaseModel):
    """Inputs to ``reproduce-run``: the ORIGINAL run + an optional repro name.

    ``original_run_id`` is the finished run being reproduced — its on-disk
    sidecar (``.hpc/runs/<original_run_id>.json``, the v2 config snapshot)
    supplies the run-owned resolve inputs to re-derive from, and it is the run
    the drift guard pins the CURRENT tree against. ``new_run_name`` is optional:
    when absent it is derived in CODE as ``<original_run_name>-repro`` so the LLM
    never authors a run name. A distinct run_name gives a distinct run_id, so the
    reproduction executes as its own run rather than re-attaching to the original
    (the ``reproduction_of`` dedup lever pierces the same-params dedup).
    """

    model_config = ConfigDict(extra="forbid", title="reproduce-run input spec")

    original_run_id: RunIdStrict = Field(
        description=(
            "The finished run's run_id being reproduced — its sidecar supplies "
            "the run-owned resolve inputs, and the drift guard pins the current "
            "tree's cmd_sha / executor / tasks_py_sha against its recorded "
            "identity. The reproduction records `reproduces: <this>` as a "
            "one-directional provenance link; the original is NEVER superseded."
        ),
    )
    new_run_name: str | None = Field(
        default=None,
        description=(
            "Optional explicit run name for the reproduction. When null the verb "
            "DERIVES it as <original_run_name>-repro so the LLM never authors a "
            "run name. Supply one to force a FRESH reproduction when a prior "
            "reproduction already occupies the derived run_id (the "
            "prior_repro_exists branch)."
        ),
    )
    task_sample: list[int] | Literal["derived"] | None = Field(
        default=None,
        description=(
            "PARTIAL reproduction subset (design center 5). Null reproduces the "
            "FULL task list. Either an explicit caller list of task indices (wins "
            'over the derived mode), OR the sentinel "derived" — the machinery '
            "then derives the subset MECHANICALLY from the axes: the canary task "
            "(task 0) plus one task per distinct axis value at that axis's "
            "row-major stride (a PURE function of axis structure, no "
            "representative/importance heuristic). The reproduction keeps the SAME "
            "task shape / trial_params / cmd_sha (a rebuilt smaller trial_params "
            "would move cmd_sha and be refused by the param-drift guard); the "
            "subset only restricts EXECUTION via HPC_TASK_INCLUDE (non-selected "
            "indices exit 0 immediately). The selected indices are recorded on the "
            "reproduction sidecar (extra.task_sample) so verify-reproduction "
            "compares per-task honestly."
        ),
    )

    @field_validator("task_sample")
    @classmethod
    def _task_sample_indices_are_clean(
        cls, value: list[int] | str | None
    ) -> list[int] | str | None:
        """A caller list must be non-empty non-negative ints (no bools, no dups).

        The derived sentinel and null pass through. A malformed caller list is
        refused at the wire so a bad subset never reaches the execution-restriction
        seam (where a silent all-skip would strand the reproduction).
        """
        if not isinstance(value, list):
            return value
        if not value:
            raise ValueError("task_sample list must be non-empty (or null for a full reproduction)")
        for idx in value:
            if isinstance(idx, bool) or not isinstance(idx, int):
                raise ValueError(f"task_sample indices must be ints, got {idx!r}")
            if idx < 0:
                raise ValueError(f"task_sample indices must be >= 0, got {idx}")
        if len(set(value)) != len(value):
            raise ValueError(f"task_sample indices must be unique, got {value}")
        return value


class ReproduceRunResult(BaseModel):
    """The reproduction outcome — a resolve brief + the S2 hand-off.

    ``stage_reached`` is what the human decides on:

    * ``repro_pending_canary`` — re-resolved against the pinned identity under a
      disjoint remote_path; the canary + array run in ``submit-s2``'s DETACHED
      worker after the greenlight (this verb never blocks on a canary poll,
      which is what makes it MCP-safe). ``next_block`` carries the
      ``{verb: submit-s2, ...}`` hand-off.
    * ``resolve_blocked`` — the fresh resolve surfaced its OWN decision (an
      UNRELATED live same-params prior, or a needed scaffold); nothing was
      minted and NOTHING was superseded. ``next_block`` is null (a human branch).
    * ``prior_repro_exists`` — a COMPLETE reproduction of the same original
      already occupies the derived run_id; the reason directs the human to
      ``verify-reproduction`` (compare the existing pair) or to pass an explicit
      ``new_run_name`` for a fresh reproduction. ``next_block`` is null.

    ``needs_decision`` is always True — the human re-``y``s the brief through the
    EXISTING ``append-decision`` path (this verb produces the brief, it does NOT
    bypass the gates; the canary + S3 greenlight still stand). ``reproduces`` is
    the original this run reproduces (the provenance link). ``next_block``'s
    presence on this model is also what derives ``reproduce-run`` into the
    curated MCP catalog.
    """

    model_config = ConfigDict(extra="forbid", title="reproduce-run output data")

    stage_reached: Literal[
        "repro_pending_canary",
        "resolve_blocked",
        "prior_repro_exists",
    ] = Field(description="The boundary the reproduction stops at for the human's re-y.")
    needs_decision: bool = Field(
        description="Always True — the human re-y's the reproduction brief through append-decision.",
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the reproduction outcome.",
    )
    run_id: str | None = Field(
        default=None,
        description=(
            "The reproduction run's run_id (<repro_run_name>-<cmd_sha[:8]>); the "
            "already-recorded prior reproduction's id on prior_repro_exists; None "
            "if the resolve did not mint one."
        ),
    )
    reproduces: str = Field(
        description="The ORIGINAL run this reproduction reproduces (the one-directional provenance link).",
    )
    brief: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The reproduction brief: the resolved values + the fresh resolve's "
            "submit_spec (what submit-s2 stages & canaries) + the disjoint "
            "remote_path + the pre-dispatch cost estimate. The LLM relays it and "
            "takes the human's re-y."
        ),
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The {verb, why, spec_hint} hand-off — submit-s2 on the "
            "pending-canary path (its detached worker owns the canary poll), null "
            "on resolve_blocked / prior_repro_exists (human branches). Presence of "
            "this field is also what derives reproduce-run into the curated MCP "
            "catalog."
        ),
    )
