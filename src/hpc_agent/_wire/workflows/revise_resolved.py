"""Pydantic models for the ``revise-resolved`` workflow primitive.

``revise-resolved`` is proving-run #5 wave 5.1 (the ROOT fix,
``docs/design/history/proving-run-5-hardening.md`` §3/§4): it removes the last
hand-authoring surface in the submit loop — the nudge. Instead of the LLM
folding a spec-changing nudge into a hand-written spec JSON (where ``job_env``
got dropped, finding 13; ``EXECUTOR`` mangled, finding 17; ``scope_id``
improvised, finding 4; ``supersedes`` deleted, finding 10), the LLM names a
**field delta** ``{field: value}`` and this verb applies it and RE-RESOLVES,
re-deriving everything the delta invalidates (``job_env``/activation from the
new cluster, ``run_id``/``cmd_sha``, the ``EXECUTOR`` dispatcher, the sidecar).

The LLM cannot drop ``job_env`` because it never touches ``job_env``; it names
one INPUT field and code recomputes every DERIVED field. This is the
determinism boundary applied to its last hold-out: judgment in the LLM (the
delta), mechanism in the verb (the re-resolve).

I/O contracts:

* Input: ``schemas/revise_resolved.input.json`` (from ``ReviseResolvedInput``).
* Output: ``schemas/revise_resolved.output.json`` (from ``ReviseResolvedResult``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReviseResolvedInput(BaseModel):
    """Inputs to ``revise-resolved``: a scope + a resolver-owned field delta.

    ``scope_kind`` / ``scope_id`` name the run whose LATEST committed greenlit
    ``resolved`` is being amended (``scope_id`` is the run_id for a run-scoped
    decision). ``patch`` is the field-level delta the human's nudge expressed
    — e.g. ``{"cluster": "hoffman2"}`` for "use hoffman2 instead".

    The ``patch`` may name ONLY resolver-owned / caller-authored INPUT fields
    (``cluster``, ``goal``, ``task_generator``, ``walltime_sec``, ``gpu_type``,
    ``partition``, ``mpi_pe``, ``entry_point``, ``homogeneous_axes`` …). A key
    naming a CODE-DERIVED field (``job_env``, ``run_id``, ``cmd_sha``,
    ``executor``, ``ssh_target``, ``backend``, ``remote_path``, ``total_tasks``,
    the sidecar) is REFUSED with ``SpecInvalid`` — those are re-derived from the
    input delta and must never be hand-set (the whole point: hand-authoring a
    derived value is structurally impossible because the only thing the LLM can
    express is a delta on an input field).
    """

    model_config = ConfigDict(extra="forbid", title="revise-resolved input spec")

    scope_kind: str = Field(
        description="Decision scope: 'run' (a run's decision journal) or 'campaign'.",
    )
    scope_id: str = Field(
        min_length=1,
        description=(
            "The scope id — the run_id for a run-scoped decision. Its LATEST "
            "committed (response=='y') decision's resolved is the base the patch "
            "amends."
        ),
    )
    patch: dict[str, Any] = Field(
        description=(
            "The field-level delta {field: value} the nudge expressed. Keys must "
            "be resolver-owned / caller-authored INPUT fields; a key naming a "
            "code-derived field is refused (SpecInvalid) — the verb re-derives "
            "job_env/executor/run_id/etc. from the input delta."
        ),
    )


class ReviseResolvedResult(BaseModel):
    """The amended decision brief — mirrors the S1 ``SubmitBlockResult`` brief.

    ``stage_reached`` is the resolver outcome the amended brief stops at
    (``resolved`` / ``prior_run_found`` / ``needs_scaffold_interview``);
    ``needs_decision`` is True in every case (the human re-``y``s the amended
    brief through the EXISTING append-decision path, so the human-authorship +
    brief-provenance gates still run on the re-commit — this verb produces the
    brief, it does NOT bypass the gates). ``brief`` carries the re-derived
    ``resolved`` values + the fresh ``resolve`` output (``submit_spec`` with
    ``job_env`` re-derived from the patched cluster, ``run_id``, ``cmd_sha``,
    ``sidecar_path``). ``applied_patch`` records exactly what the delta changed
    (the audit trail shows *what the human changed*, design §4 point 2).
    """

    model_config = ConfigDict(extra="forbid", title="revise-resolved output data")

    stage_reached: str = Field(
        description="The resolver stage the amended brief stopped at.",
    )
    needs_decision: bool = Field(
        description="Always True — the human re-y's the amended brief through append-decision.",
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the re-resolution outcome.",
    )
    run_id: str | None = Field(
        default=None,
        description="The re-computed run_id (the patch may move it via cmd_sha).",
    )
    brief: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The amended S1-shaped brief: re-derived resolved values + the fresh "
            "resolve output (submit_spec, sidecar_path). The LLM relays it and "
            "takes the human's re-y; it never authored the derived fields."
        ),
    )
    applied_patch: dict[str, Any] = Field(
        default_factory=dict,
        description="The delta actually applied {field: value} — the audit of what changed.",
    )
