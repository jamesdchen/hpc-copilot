"""Central typed recovery registry.

One canonical place per failure ``kind`` listing ``{cli_command,
when_to_use, safety_rank}`` recovery options. Replaces the per-call-site
hand-rolled ``remediation`` strings and the SKILL.md inline recovery
menus that drifted independently (the empirical 0.10.5 case: an
``already_in_flight`` recovery menu landed in ``hpc-submit/SKILL.md``
only, while ``hpc-aggregate``'s symmetric path was unaware of it).

See ``docs/proposals/recovery-registry.md`` for design rationale.

The kind vocabulary is deliberately broader than
:class:`hpc_agent._kernel.contract.vocabulary.FailureCategory` (classifier
output) and :data:`hpc_agent._wire._shared.ErrorCode` (envelope output)
combined — it also includes prose-only kinds like ``already_in_flight``
and ``submission_incomplete`` that slash-skill recovery menus address
but no Python code emits today. Each registry kind is documented inline
with its provenance.
"""

from __future__ import annotations

import re
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "RecoveryKind",
    "RecoveryOption",
    "RecoveryMenu",
    "REGISTRY",
    "PORTED_KINDS",
    "remediation_for",
    "menu_for",
    "all_kinds",
]


# Open vocabulary covering every recovery-keyable failure kind. Strictly
# broader than ``FailureCategory`` (classifier-emitted) and ``ErrorCode``
# (envelope-emitted): the registry also keys on prose-only kinds like
# ``already_in_flight``, ``submission_incomplete`` that slash-skill
# recovery menus address but no Python code emits today.
#
# When adding a new kind:
#   1. Append it to this Literal.
#   2. Add an entry to ``REGISTRY`` with at least one ``RecoveryOption``.
#   3. The contract test in ``tests/contracts/test_recovery_registry.py``
#      will fail until both happen.
RecoveryKind = Literal[
    # Classifier-emitted (subset that has a multi-option menu).
    "gpu_oom",
    "system_oom",
    "walltime",
    "node_failure",
    # Envelope-emitted (subset).
    "combiner_failed",
    "outputs_missing",
    "ssh_unreachable",
    # Prose-only / slash-skill-emitted kinds — the empirical drift cases.
    "already_in_flight",
    "submission_incomplete",
    "spawn_worker_died",
]


class RecoveryOption(BaseModel):
    """One concrete recovery path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cli_command: str = Field(
        description=(
            "Literal command string the operator runs. May contain "
            "``<placeholder>`` tokens (e.g. ``<run_id>``, ``<scheduler>``) "
            "the caller substitutes at emit time."
        ),
    )
    when_to_use: str = Field(
        description=(
            "One-sentence guidance on when this option is appropriate. "
            "Should distinguish itself from the other options in the "
            "menu (no two options should be applicable in the same case)."
        ),
    )
    safety_rank: int = Field(
        ge=0,
        description=(
            "Lower is safer / more reversible. Caller may sort by this "
            "when rendering the menu; the primary recommendation is "
            "``safety_rank=0``."
        ),
    )


_PLACEHOLDER_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")


class RecoveryMenu(BaseModel):
    """The complete recovery menu for one failure kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(description="The ``RecoveryKind`` literal value.")
    summary: str = Field(
        description=(
            "One-sentence description of what this failure kind means — "
            "the framework's diagnosis, separate from the per-call message."
        ),
    )
    options: tuple[RecoveryOption, ...] = Field(
        min_length=1,
        description="Ordered by ``safety_rank`` ascending.",
    )
    references: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Optional issue / commit refs that motivated each option, for "
            "audit when an option's wording drifts."
        ),
    )

    def remediation_text(
        self,
        *,
        placeholders: dict[str, str] | None = None,
    ) -> str:
        """Render the menu as the ``remediation`` string for the envelope.

        Format: ``'(a) <cmd1> — <when1>; (b) <cmd2> — <when2>; …'``.
        Stable across calls (no random ordering); placeholders substituted
        with the caller-supplied dict (e.g. ``{'run_id': 'foo-bar'}``).
        Unsubstituted ``<token>`` placeholders pass through verbatim so a
        downstream renderer can still substitute.
        """
        subs = placeholders or {}
        sorted_options = sorted(self.options, key=lambda o: o.safety_rank)
        parts: list[str] = [self.summary]
        for idx, opt in enumerate(sorted_options):
            label = chr(ord("a") + idx)
            cmd = _PLACEHOLDER_RE.sub(lambda m: subs.get(m.group(1), m.group(0)), opt.cli_command)
            parts.append(f"({label}) `{cmd}` — {opt.when_to_use}")
        return " ".join(parts)


# ── Registry entries ───────────────────────────────────────────────────────


_ALREADY_IN_FLIGHT = RecoveryMenu(
    kind="already_in_flight",
    summary=(
        "A prior run for this cmd_sha is recorded as in_flight in the journal "
        "and reconcile has confirmed the cluster agrees it is still running."
    ),
    options=(
        RecoveryOption(
            cli_command="/monitor-hpc",
            when_to_use=(
                "The prior submit really is still running — drive it to a "
                "terminal state, then resubmit."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command=(
                "hpc-agent reconcile --run-id <run_id> "
                "--scheduler <scheduler> --experiment-dir <experiment_dir>"
            ),
            when_to_use=(
                "The cluster state is gone (scratch wiped, manual qdel, "
                "cluster bounce). Reconcile polls the cluster, sees the dir "
                "is missing, marks the journal abandoned, unblocks the next "
                "submit."
            ),
            safety_rank=1,
        ),
        RecoveryOption(
            cli_command="--no-canary",
            when_to_use=(
                "Only when the prior run's canary is the in-flight one AND "
                "the operator has independently confirmed it succeeded. NOT "
                "a generic workaround for a journal-cluster mismatch — use "
                "reconcile for that."
            ),
            safety_rank=2,
        ),
    ),
    references=("#257", "8986cf5c"),
)


_SUBMISSION_INCOMPLETE = RecoveryMenu(
    kind="submission_incomplete",
    summary=(
        "The qsub/sbatch call structurally succeeded but cluster-side init "
        "crashed before the sidecar got fully populated — the run record has "
        "no job_ids, so scheduler state cannot be polled. Distinct from "
        "'abandoned' (where job_ids existed but no longer live on the "
        "scheduler) and from 'submission complete, scheduler lost track'."
    ),
    options=(
        RecoveryOption(
            cli_command=(
                "cat <experiment_dir>/.hpc/runs/<run_id>.json && ls <experiment_dir>/.hpc/runs/"
            ),
            when_to_use=(
                "Inspect the run sidecar to find which cluster-side init "
                "step crashed before writing job_ids — typically a "
                "deploy_runtime, env-activation, or qsub stderr capture issue."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command=(
                "ssh <ssh_target> 'ls <remote_path>/logs/ && cat <remote_path>/logs/*.o*'"
            ),
            when_to_use=(
                "Check cluster-side logs (when remote_path was populated) — "
                "the qsub stderr or the first task's stderr usually names "
                "the precise crash. Substitute ssh_target and remote_path "
                "from the run sidecar."
            ),
            safety_rank=1,
        ),
        RecoveryOption(
            cli_command=(
                "rm <experiment_dir>/.hpc/runs/<run_id>.json && /submit-hpc <your original args>"
            ),
            when_to_use=(
                "After diagnosing and fixing the underlying init failure "
                "(e.g. broken conda_env in clusters.yaml, missing "
                "remote_path), clear the broken sidecar locally and "
                "resubmit. Required because submit dedup against a broken "
                "sidecar otherwise blocks the next attempt by cmd_sha."
            ),
            safety_rank=2,
        ),
    ),
    references=("SESSION_HANDOFF.md:179",),
)


_SPAWN_WORKER_DIED = RecoveryMenu(
    kind="spawn_worker_died",
    summary=(
        "The spawned ``claude -p --bare`` worker exited 1 before emitting a "
        "valid report — typically a credential or quota failure that the "
        "worker hit but the parent session does not (e.g. workspace API key "
        "over quota while the operator's OAuth session is fine)."
    ),
    options=(
        RecoveryOption(
            cli_command=("HPC_AGENT_INVOKER=inline /submit-hpc <your original args>"),
            when_to_use=(
                "The framework's malformed-report remediation hints inline "
                "as the fallback. Set the env var explicitly (the operator "
                "opt-in form) and the orchestrator runs the procedure in its "
                "own context instead of spawning a fresh worker. The "
                "preemptive ``--inline`` flag is still refused under #155 — "
                "this is the post-failure recovery form."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command=(
                "$env:ANTHROPIC_API_KEY = '<fresh-key>'; /submit-hpc <your original args>"
            ),
            when_to_use=(
                "When the worker dies from a quota / auth failure on the "
                "current API key. Set a fresh key in the parent shell and "
                "retry — the spawn path will inherit it."
            ),
            safety_rank=1,
        ),
    ),
    references=("29fbac9f", "88a3869a"),
)


_WALLTIME = RecoveryMenu(
    kind="walltime",
    summary=(
        "The scheduler killed the job at its walltime limit — the dispatcher "
        "trapped SIGTERM and exited 130, marking the in-flight tasks preempted. "
        "The unfinished tasks need to continue, not the whole array rerun."
    ),
    options=(
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: walltime, from_checkpoint: true, "
                "submit_to_cluster: true}>"
            ),
            when_to_use=(
                "Resume the unfinished tasks from their last checkpoint (#294) — "
                "the privileged path for a long solve that checkpoints. A task "
                "with no checkpoint simply restarts fresh, so this is safe to "
                "prefer."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: walltime, submit_to_cluster: true, "
                "overrides: {walltime_sec: <larger>}}>"
            ),
            when_to_use=(
                "When the executor does NOT checkpoint and the work genuinely "
                "needs longer — bump the walltime and rerun the unfinished tasks "
                "from scratch."
            ),
            safety_rank=1,
        ),
    ),
    references=("#294",),
)


_NODE_FAILURE = RecoveryMenu(
    kind="node_failure",
    summary=(
        "A compute node died mid-run (hardware / scheduler fault), killing its "
        "tasks through no fault of the job. The affected tasks need to re-run on "
        "healthy nodes."
    ),
    options=(
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: node_failure, from_checkpoint: true, "
                "submit_to_cluster: true}>"
            ),
            when_to_use=(
                "Resume the affected tasks from their last checkpoint (#294); "
                "they land on healthy nodes. A task with no checkpoint restarts "
                "fresh."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: node_failure, submit_to_cluster: true}>"
            ),
            when_to_use=(
                "When the executor does NOT checkpoint — node failure is "
                "transient, so a from-scratch rerun on healthy nodes is the fix."
            ),
            safety_rank=1,
        ),
    ),
    references=("#294",),
)


REGISTRY: dict[str, RecoveryMenu] = {
    _ALREADY_IN_FLIGHT.kind: _ALREADY_IN_FLIGHT,
    _SUBMISSION_INCOMPLETE.kind: _SUBMISSION_INCOMPLETE,
    _SPAWN_WORKER_DIED.kind: _SPAWN_WORKER_DIED,
    _WALLTIME.kind: _WALLTIME,
    _NODE_FAILURE.kind: _NODE_FAILURE,
}


# Exposed so the contract test and the un-ported migration plan can name
# what is currently shipped vs what is on the punch list.
PORTED_KINDS: frozenset[str] = frozenset(REGISTRY)


def all_kinds() -> tuple[str, ...]:
    """Return every value the :data:`RecoveryKind` ``Literal`` admits.

    Use this (not ``REGISTRY.keys()``) when iterating "every kind that
    *should* exist" — the difference between the two is the migration
    punch list.
    """
    return tuple(get_args(RecoveryKind))


def menu_for(kind: str) -> RecoveryMenu:
    """Return the :class:`RecoveryMenu` for *kind*.

    Raises ``KeyError`` if *kind* is unknown — callers should pass a
    :data:`RecoveryKind` value, not an arbitrary string.
    """
    return REGISTRY[kind]


def remediation_for(
    kind: str,
    *,
    placeholders: dict[str, str] | None = None,
) -> str:
    """Render the canonical ``remediation`` string for *kind*.

    Single chokepoint every ``ErrorEnvelope`` consumer should call so the
    rendered prose stays byte-stable across emit sites. Raises ``KeyError``
    for an un-ported kind so the failure is loud — silently falling back
    to a generic string would reintroduce the drift the registry exists
    to eliminate.
    """
    return REGISTRY[kind].remediation_text(placeholders=placeholders)
