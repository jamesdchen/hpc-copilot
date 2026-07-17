"""Conformance kit — capability 6 (the scheduler-write fence).

Asserts a harness's pre-execution command FENCE (``run_scheduler_fence``) BLOCKS a
mutating scheduler verb the agent is about to run — in command position, including
wrapped / transport forms (``bash -c 'qsub …'``, ``ssh host qdel``, a wrapper, a
command substitution) — while PASSING a mere mention (``grep qsub``), a read-only
probe (``qstat``/``squeue``), and the ``hpc-agent`` CLI itself (whose command line
carries no fenced token — the blocks dial the scheduler remotely inside Python).
This is contract capability 6 (``docs/internals/harness-contract.md``, "Capability
6 — the scheduler-write fence"), conduct rule 7 mechanized: "consequences are
gated, curiosity isn't."

The seam is outcome-shaped (``EnforcementOutcome``: ``blocked`` + a ``reason``
naming the fenced verb), never mechanism-shaped — a PreToolUse hook and any other
pre-execution interceptor certify through the same seam (the D-K3 outcome-not-
mechanism rule).

Standalone / reference (the K2 pattern): with no ``--harness-adapter`` — OR an
adapter that does not declare capability 6 (a FOREIGN proof is owed, Wave C) — the
built-in REFERENCE fence, hpc-agent's own
``scheduler_write_fence.fenced_in_command`` core driven IN-PROCESS, is the
candidate, so this module certifies the vectors against our own implementation
(the behaved-for-the-reference-adapter leg). When an adapter DECLARES capability 6,
its ``run_scheduler_fence`` is the candidate instead. It never SKIPs: capability 6
is not part of the three-capability ``conforming: harness contract v1`` verdict, so
the module always certifies *something* (the reference core as the baseline).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance.adapter import (
    CAP_SCHEDULER_FENCE,
    EnforcementOutcome,
    declared_capabilities,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# --- the vectors -------------------------------------------------------------
#
# BLOCK: a fenced verb that can actually EXECUTE — bare, wrapped, transported, or
# inside a command substitution. Each MUST be refused (the reason names the verb).
_BLOCK_VECTORS: tuple[tuple[str, str], ...] = (
    ("bare_qsub", "qsub job.sh"),
    ("bare_sbatch", "sbatch --array=1-4 run.slurm"),
    ("bare_qdel", "qdel 12345"),
    ("bare_scancel", "scancel 999"),
    ("bash_c_qsub", "bash -c 'qsub job.sh'"),  # the wrapped form the fake misses
    ("bash_lc_sbatch", 'bash -lc "sbatch run.slurm"'),
    ("ssh_transport_qdel", "ssh hoffman2 qdel 555"),
    ("wrapper_nohup_qsub", "nohup qsub job.sh &"),
    ("wrapper_timeout_qsub", "timeout -k 5 60 qsub job.sh"),
    ("command_substitution_qsub", "echo $(qsub job.sh)"),
    ("second_segment_qmod", "cd /scratch && qmod -c all.q"),
)

# PASS: a fenced verb only MENTIONED (never in command position), a read-only
# probe, or the hpc-agent CLI itself — none of which EXECUTE a mutation.
_PASS_VECTORS: tuple[tuple[str, str], ...] = (
    ("mention_grep_qsub", "grep qsub /var/log/sched.log"),
    ("mention_echo_qdel", "echo qdel is how you cancel"),
    ("probe_qstat", "qstat -u $USER"),
    ("probe_squeue", "squeue --me"),
    ("probe_ssh_qstat", "ssh hoffman2 qstat -u me"),
    ("cli_submit_s2", "hpc-agent submit-s2 --run-id pi-train-demo"),
    ("cli_kill", "hpc-agent kill --run-id pi-train-demo"),
    ("cli_describe_mentions_qsub", "hpc-agent describe submit-flow"),
)


# --- the fence candidate seam ------------------------------------------------


@dataclass(frozen=True)
class FenceCandidate:
    """A scheduler-write fence under test — the reference core or an adapter."""

    name: str
    run: Callable[[str], EnforcementOutcome]


def _builtin_reference() -> FenceCandidate:
    """hpc-agent's own fence core driven in-process (the reference provider).

    Maps ``scheduler_write_fence.fenced_in_command`` onto an
    ``EnforcementOutcome``: a returned fenced verb is a BLOCK (the reason names
    it); ``None`` is a PASS. This is the exact command-position analysis a live
    ``PreToolUse`` hook runs (exit 2 → block), with no subprocess.
    """
    from hpc_agent._kernel.hooks.scheduler_write_fence import fenced_in_command

    def run(command: str) -> EnforcementOutcome:
        verb = fenced_in_command(command)
        return EnforcementOutcome(blocked=verb is not None, reason=verb)

    return FenceCandidate(name="hpc-agent (scheduler_write_fence)", run=run)


@pytest.fixture
def scheduler_fence_candidate(request: pytest.FixtureRequest) -> FenceCandidate:
    """The fence seam to certify — the adapter's when declared, else the reference.

    With ``--harness-adapter`` AND a declared capability 6, the adapter's
    ``run_scheduler_fence`` is the candidate. Otherwise the built-in reference core
    runs (no SKIP — capability 6 is not a ``conforming: harness contract v1``
    verdict capability; a FOREIGN proof is the Wave-C follow-on).
    """
    spec = request.config.getoption("--harness-adapter", default=None)
    if spec:
        adapter = request.getfixturevalue("harness_adapter")
        if CAP_SCHEDULER_FENCE in declared_capabilities(adapter):
            return FenceCandidate(
                name=getattr(adapter, "name", "<adapter>"), run=adapter.run_scheduler_fence
            )
    return _builtin_reference()


# --- assertions (mirror-drivable: first arg is the candidate) ----------------


def check_blocks_fenced_verbs(candidate: FenceCandidate) -> None:
    """Every command that would EXECUTE a fenced verb is refused, with a reason.

    Includes the wrapped/transport forms (``bash -c 'qsub …'``, ``ssh host qdel``,
    a wrapper, a command substitution) — a fence that only substring-matches or
    only checks the local first token is FAILED here (guard-can-fire).
    """
    for name, cmd in _BLOCK_VECTORS:
        outcome = candidate.run(cmd)
        assert outcome.blocked is True, (
            f"[{candidate.name}] {name}: {cmd!r} EXECUTES a fenced scheduler verb "
            "in command position — a conforming fence MUST block it"
        )
        assert outcome.reason, f"[{candidate.name}] {name}: a block must name the fenced verb"


def check_passes_mentions_probes_and_cli(candidate: FenceCandidate) -> None:
    """A mere mention, a read-only probe, and the hpc-agent CLI itself all pass.

    The fence blocks EXECUTION, not mention: ``grep qsub`` / ``echo qdel`` name a
    fenced verb as an argument, ``qstat``/``squeue`` are read-only, and
    ``hpc-agent <verb>`` carries no fenced token (the blocks dial the scheduler
    remotely inside Python). A fence that refuses any of these is FAILED here.
    """
    for name, cmd in _PASS_VECTORS:
        outcome = candidate.run(cmd)
        assert outcome.blocked is False, (
            f"[{candidate.name}] {name}: {cmd!r} only MENTIONS a fenced verb / is a "
            "read-only probe / is the hpc-agent CLI — a conforming fence MUST pass it"
        )


def test_fence_blocks_fenced_verbs_in_command_position(
    scheduler_fence_candidate: FenceCandidate,
) -> None:
    """Capability 6 behaved leg: fenced verbs (incl. wrapped/transport) block."""
    check_blocks_fenced_verbs(scheduler_fence_candidate)


def test_fence_passes_mentions_probes_and_cli(
    scheduler_fence_candidate: FenceCandidate,
) -> None:
    """Capability 6 behaved leg: mentions, probes, and the hpc-agent CLI pass."""
    check_passes_mentions_probes_and_cli(scheduler_fence_candidate)
