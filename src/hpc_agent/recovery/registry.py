"""Central typed recovery registry.

One canonical place per failure ``kind`` listing ``{cli_command,
when_to_use, safety_rank}`` recovery options. Replaces the per-call-site
hand-rolled ``remediation`` strings and the SKILL.md inline recovery
menus that drifted independently (the empirical 0.10.5 case: an
``already_in_flight`` recovery menu landed in ``hpc-submit/SKILL.md``
only, while ``hpc-aggregate``'s symmetric path was unaware of it).

See ``docs/design/recovery-registry.md`` for design rationale.

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
    # Detached-worker TERMINAL causes (s2-readiness pillar 5, 2026-07-30). Each
    # is a class a live operator diagnosed by READING A WORKER LOG on the night
    # of 2026-07-30; the registry is what makes the diagnosis composed instead of
    # archaeological. Keyed by ``ops/recover/terminal_cause.py``'s classifier,
    # which names ONE of these on every terminal detached-worker death it can
    # discriminate.
    "dead_hop_route",
    "flap_exhausted_staging",
    "canary_reporter_unreachable",
    "zombie_submitting_record",
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
    references=("docs/design/recovery-registry.md",),
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


_GPU_OOM = RecoveryMenu(
    kind="gpu_oom",
    summary=(
        "A task exhausted GPU memory (CUDA OOM). The right fix depends on whether "
        "the model is already sharded across GPUs: if it is not, give it more "
        "memory per GPU; if it already spans multiple GPUs, more per-GPU memory "
        "will not clear the wall — widen the tensor-parallel shard instead."
    ),
    options=(
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: gpu_oom, submit_to_cluster: true, "
                "overrides: {mem_per_gpu: <~1.5x>}}>"
            ),
            when_to_use=(
                "The model is NOT already sharded across GPUs (tp_size == 1). The "
                "deterministic catalog fix is ~1.5x more memory per GPU "
                "(``infra/failure_signatures.py`` gpu_oom row; "
                "``ops/recover/resolve.py::_gpu_oom_action`` fall-through)."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: gpu_oom, submit_to_cluster: true, "
                "overrides: {tp_size: <2x>}}>"
            ),
            when_to_use=(
                "The model already spans multiple GPUs (tp_size > 1). More memory "
                "per GPU cannot fix a per-GPU-capacity wall, so double the "
                "tensor-parallel degree to reshard "
                "(``ops/recover/resolve.py::_gpu_oom_action`` sharded branch)."
            ),
            safety_rank=1,
        ),
    ),
    references=("infra/failure_signatures.py", "ops/recover/resolve.py"),
)


_SYSTEM_OOM = RecoveryMenu(
    kind="system_oom",
    summary=(
        "A task exhausted host (system RAM) memory and was killed by the kernel "
        "OOM-killer (exit 137). Unlike gpu_oom this is CPU-side memory; the "
        "deterministic fix is more host memory per task."
    ),
    options=(
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: system_oom, submit_to_cluster: true, "
                "overrides: {mem: <~1.5x>}}>"
            ),
            when_to_use=(
                "Give the failed tasks ~1.5x more host memory and rerun them "
                "(``infra/failure_signatures.py`` system_oom row; "
                "``ops/recover/resolve.py::_deterministic_fix`` increase-mem)."
            ),
            safety_rank=0,
        ),
    ),
    references=("infra/failure_signatures.py", "ops/recover/resolve.py"),
)


_COMBINER_FAILED = RecoveryMenu(
    kind="combiner_failed",
    summary=(
        "The per-wave combiner returned non-zero on the cluster — typically a "
        "task's metrics.json was missing or malformed, so the aggregation step "
        "could not complete."
    ),
    options=(
        RecoveryOption(
            cli_command="cat <experiment_dir>/.hpc/runs/<run_id>.json",
            when_to_use=(
                "Inspect the stderr_tail in the JSON payload to find which task's "
                "metrics.json was missing or malformed."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: combiner_failed, submit_to_cluster: true}> "
                "&& /aggregate"
            ),
            when_to_use=(
                "After identifying the offending tasks, resubmit just those and re-run /aggregate."
            ),
            safety_rank=1,
        ),
    ),
    references=("errors.py::CombinerFailed",),
)


_OUTPUTS_MISSING = RecoveryMenu(
    kind="outputs_missing",
    summary=(
        "Per-task output files declared by --require-outputs are absent, so "
        "aggregate refused to combine on partial data before the combiner ran."
    ),
    options=(
        RecoveryOption(
            cli_command=(
                "hpc-agent resubmit --run-id <run_id> --experiment-dir <experiment_dir> "
                "--spec <{failed_task_ids, category: outputs_missing, submit_to_cluster: true}> "
                "&& /aggregate"
            ),
            when_to_use=(
                "Resubmit the listed task ids and re-run aggregate — the primary "
                "fix when the declared outputs were simply never produced."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command="ssh <ssh_target> 'ls <remote_path>/logs/ && cat <remote_path>/logs/*.o*'",
            when_to_use=(
                "If the resubmit does not produce the expected output, inspect "
                "<remote_path>/logs/ for the per-task stderr that explains why "
                "the output is missing."
            ),
            safety_rank=1,
        ),
    ),
    references=("errors.py::OutputsMissing",),
)


_DEAD_HOP_ROUTE = RecoveryMenu(
    kind="dead_hop_route",
    summary=(
        "The configured path to the cluster runs through a ProxyJump hop and THAT "
        "HOP is down — the discriminated ``hop_down_direct_*`` readiness cause. The "
        "login node is not the fault: every sibling login node reached through the "
        "same hop inherits the same dead hop, so a host-retarget moves the target "
        "and keeps the break (the 2026-07-30 misdiagnosis this entry exists to "
        "prevent). The discriminator is the DIRECT route: whether the target "
        "answers with the jump bypassed."
    ),
    options=(
        RecoveryOption(
            cli_command="hpc-agent net-triage --spec <{probe_preamble: true}>",
            when_to_use=(
                "ALWAYS first. The triage rungs re-read the route chain per element "
                "(hop / direct / target) and report the named ``path_cause`` plus "
                "the breaker state, so the next step is chosen from evidence rather "
                "than from which host the error message happened to mention."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command="ssh -G <cluster_host>",
            when_to_use=(
                "The CONFIG discriminator: it prints the effective chain ssh will "
                "actually use (ProxyJump, HostName, User, Port) after every "
                "Host/Match block has applied. Run it when triage names a hop you "
                "did not expect — the dead hop is frequently inherited from a "
                "wildcard block rather than the cluster's own stanza."
            ),
            safety_rank=1,
        ),
        RecoveryOption(
            cli_command="hpc-agent cluster-readiness --spec <{host: <cluster_host>}>",
            when_to_use=(
                "The hop is back (VPN/tunnel restored) and you want the standing "
                "ledger's verdict per chain element before re-firing, rather than "
                "learning at fire time whether the path healed."
            ),
            safety_rank=2,
        ),
        RecoveryOption(
            cli_command="ssh -o ProxyJump=none <cluster_host> true",
            when_to_use=(
                "ONLY when triage reported ``hop_down_direct_ok`` — the target "
                "answers directly. This confirms the bypass by hand before you "
                "route the run without the jump. Do NOT run this as a generic "
                "workaround: on ``hop_down_direct_dead`` both routes are gone and "
                "the problem is local network / site-wide, not the login node."
            ),
            safety_rank=3,
        ),
    ),
    references=(
        "docs/design/s2-readiness.md",
        "infra/readiness_sensors.py::PathCause",
        "ops/path_gate.py",
    ),
)


_FLAP_EXHAUSTED_STAGING = RecoveryMenu(
    kind="flap_exhausted_staging",
    summary=(
        "Staging (the delta push / deploy) died after the flap-riding retry ladder "
        "exhausted its attempts: the transport kept dropping mid-command — a "
        "flapping tunnel or VPN — rather than the remote command failing. The "
        "delta push is idempotent and CONTENT-KEYED, so everything already shipped "
        "is banked: a re-fire RESUMES, it does not restart. The ladder stops early "
        "(rather than holding the worker) when the host's circuit breaker is "
        "cooling for longer than the ladder's patience — that is a fence, not a "
        "failure to try."
    ),
    options=(
        RecoveryOption(
            cli_command="hpc-agent net-triage",
            when_to_use=(
                "FIRST, to read the breaker state for the host. An OPEN circuit "
                "means the next attempts fail fast by design (ban-risk protection) "
                "and a re-fire before the cooldown lapses buys nothing — the "
                "remaining cooldown is the honest wait."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command="/submit-hpc <your original args>",
            when_to_use=(
                "The breaker is closed or half-open-eligible and the tunnel is back. "
                "Re-fire the SAME submit: staging converges on what is already "
                "there, so the second attempt ships only the residue. This is the "
                "primary path — 'attempt count' is not a concept here, only progress."
            ),
            safety_rank=1,
        ),
        RecoveryOption(
            cli_command="HPC_STAGE_RETRY_ATTEMPTS=<n> /submit-hpc <your original args>",
            when_to_use=(
                "The tunnel flaps on a period longer than the default ladder rides "
                "out. Widen the ladder for this fire only. Raising it does NOT "
                "bypass the breaker — a cooling circuit still stops the ladder."
            ),
            safety_rank=2,
        ),
    ),
    references=(
        "docs/design/s2-readiness.md",
        "ops/submit_flow.py::_stage_with_flap_retry",
        "infra/ssh_options.py::mark_transport_flap",
    ),
)


_CANARY_REPORTER_UNREACHABLE = RecoveryMenu(
    kind="canary_reporter_unreachable",
    summary=(
        "The canary's every status poll failed: the cluster-side reporter never "
        "returned a readable status, so the canary CANNOT be trusted as passed "
        "(never-pass-unverified). The scheduler may well have run the job. The "
        "ROUTE CLASS is the discriminator and it decides which side of the wire to "
        "look at: a last poll of rc=255 is the SSH transport itself failing — it "
        "says NOTHING about the cluster env — while any other shape keeps the "
        "cluster-side reading (wrong/absent conda env, module-load failure in the "
        "job preamble)."
    ),
    options=(
        RecoveryOption(
            cli_command="hpc-agent net-triage",
            when_to_use=(
                "The recorded last-poll return code is 255 (or the record names a "
                "transport cause). The dial died before any cluster-side read, so "
                "triage the route — including the local ssh resolution — BEFORE "
                "touching the remote env. Blaming the conda env here sends you to "
                "the wrong side of the wire."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command=(
                "ssh <ssh_target> '<activation> && python -c \"import hpc_agent, sys; "
                "print(hpc_agent.__version__, sys.executable)\"'"
            ),
            when_to_use=(
                "The route class is NOT transport (any last-poll shape other than "
                "rc=255). Reproduce the reporter's own precondition under the "
                "cluster's activation: an unimportable hpc-agent or a failed module "
                "load in the preamble is the ordinary cause."
            ),
            safety_rank=1,
        ),
        RecoveryOption(
            cli_command="hpc-agent worker-log-digest --path <log_path>",
            when_to_use=(
                "Neither of the above named it. The worker log is the FORENSIC tier "
                "— read it last, not first, and read it through the digest so the "
                "poll sequence is summarised rather than scrolled."
            ),
            safety_rank=2,
        ),
    ),
    references=(
        "docs/design/s2-readiness.md",
        "ops/verify_canary.py::_reporter_unreachable_envelope",
    ),
)


_ZOMBIE_SUBMITTING_RECORD = RecoveryMenu(
    kind="zombie_submitting_record",
    summary=(
        "A run record was left in ``submitting`` by a worker that died between "
        "minting the record and issuing the dispatch. This class is now AUTO-HEALED: "
        "the record carries its OWN durable dispatch evidence, and reconcile's rung "
        "0 reads ``dispatch_evidence.state == 'pending'`` — proof no qsub/sbatch was "
        "ever sent — and settles the record ``abandoned`` OFFLINE, with no cluster "
        "round-trip and no acceptance evidence to hunt for. The entry stays in the "
        "registry because the record is the human-visible artefact of the heal: it "
        "documents WHY the run went abandoned without anyone asking the scheduler, "
        "and it is the recovery menu for the residue when the evidence is UNKNOWN "
        "(a pre-fix record, which rung 0 correctly refuses to settle offline)."
    ),
    options=(
        RecoveryOption(
            cli_command="hpc-agent status-snapshot --experiment-dir <experiment_dir>",
            when_to_use=(
                "Confirm the auto-heal already happened: the run reads ``abandoned`` "
                "with verdict_reason ``submit_once_never_dispatched_safe_resubmit``. "
                "Nothing is owed — resubmitting is safe because nothing was ever "
                "sent."
            ),
            safety_rank=0,
        ),
        RecoveryOption(
            cli_command="/submit-hpc <your original args>",
            when_to_use=(
                "After confirming the never-dispatched settle. The submit-once "
                "dedup no longer blocks on the healed record, and no duplicate array "
                "can result — the evidence says the first dispatch never actuated."
            ),
            safety_rank=1,
        ),
        RecoveryOption(
            cli_command=(
                "hpc-agent reconcile --run-id <run_id> --scheduler <scheduler> "
                "--experiment-dir <experiment_dir>"
            ),
            when_to_use=(
                "The record predates dispatch evidence (``dispatch_evidence`` empty "
                "== UNKNOWN), so rung 0 cannot prove the dispatch never actuated and "
                "deliberately does NOT settle it offline. Reconcile asks the "
                "scheduler — the only remaining evidence class — before anything is "
                "declared abandoned."
            ),
            safety_rank=2,
        ),
    ),
    references=(
        "docs/design/s2-readiness.md",
        "ops/monitor/reconcile.py::_never_actuated_abandon",
        "_kernel/contract/vocabulary.py::DispatchState",
    ),
)


REGISTRY: dict[str, RecoveryMenu] = {
    _ALREADY_IN_FLIGHT.kind: _ALREADY_IN_FLIGHT,
    _SUBMISSION_INCOMPLETE.kind: _SUBMISSION_INCOMPLETE,
    _WALLTIME.kind: _WALLTIME,
    _NODE_FAILURE.kind: _NODE_FAILURE,
    _GPU_OOM.kind: _GPU_OOM,
    _SYSTEM_OOM.kind: _SYSTEM_OOM,
    _COMBINER_FAILED.kind: _COMBINER_FAILED,
    _OUTPUTS_MISSING.kind: _OUTPUTS_MISSING,
    _DEAD_HOP_ROUTE.kind: _DEAD_HOP_ROUTE,
    _FLAP_EXHAUSTED_STAGING.kind: _FLAP_EXHAUSTED_STAGING,
    _CANARY_REPORTER_UNREACHABLE.kind: _CANARY_REPORTER_UNREACHABLE,
    _ZOMBIE_SUBMITTING_RECORD.kind: _ZOMBIE_SUBMITTING_RECORD,
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
