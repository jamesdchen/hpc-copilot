"""Capability 6 (scheduler-write fence) — mirror unit test (core CI).

Drives the SHIPPED kit assertions
(``hpc_agent.conformance.test_capability_scheduler_fence``) against the REFERENCE
fence core (green — the behaved-for-the-reference-adapter leg) AND against planted
NON-conforming fakes that the kit correctly FAILS (guard-can-fire):

* a fence that only checks the LOCAL first token — so it MISSES ``bash -c 'qsub …'``
  (and every other wrapped / transport form) — trips the block battery;
* a fence that refuses a mere MENTION (``grep qsub``) trips the pass battery.

Plus: an adapter that IMPLEMENTS ``run_scheduler_fence`` DECLARES capability 6 (the
adapter seam a foreign provider uses in Wave C).
"""

from __future__ import annotations

import shlex

import pytest

from hpc_agent.conformance import test_capability_scheduler_fence as kit
from hpc_agent.conformance.adapter import (
    CAP_SCHEDULER_FENCE,
    EnforcementOutcome,
    declared_capabilities,
)

_FENCED = frozenset({"qsub", "sbatch", "qdel", "scancel", "qmod", "qalter"})


# ─── the reference core passes both shipped batteries ────────────────────────


def test_reference_core_blocks_fenced_and_passes_mentions() -> None:
    """The SHIPPED batteries pass driven by hpc-agent's own fence core."""
    candidate = kit._builtin_reference()
    kit.check_blocks_fenced_verbs(candidate)
    kit.check_passes_mentions_probes_and_cli(candidate)


def test_reference_blocks_bash_c_and_ssh_transport_and_passes_cli() -> None:
    """Spot-checks the load-bearing discriminations directly on the reference core."""
    run = kit._builtin_reference().run
    assert run("bash -c 'qsub job.sh'").blocked is True  # wrapped execution
    assert run("ssh hoffman2 qdel 555").blocked is True  # transport execution
    assert run("grep qsub /var/log/x").blocked is False  # mention, not execution
    assert run("hpc-agent submit-s2 --run-id r-1").blocked is False  # the CLI itself


# ─── guard-can-fire: a non-conforming fake is FAILED by the kit ──────────────


def _naive_first_token_fence(command: str) -> EnforcementOutcome:
    """A DELIBERATELY WEAK fence: blocks only when the LOCAL first token is fenced.

    Misses every wrapped / transport form — ``bash -c 'qsub …'`` has head ``bash``,
    ``ssh host qdel`` has head ``ssh`` — the exact hole the kit's block battery
    exists to catch.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    head = tokens[0].rsplit("/", 1)[-1] if tokens else ""
    blocked = head in _FENCED
    return EnforcementOutcome(blocked=blocked, reason=head if blocked else None)


def _mention_refusing_fence(command: str) -> EnforcementOutcome:
    """A fence that BLOCKS on any substring mention — refuses ``grep qsub`` too."""
    hit = next((verb for verb in _FENCED if verb in command), None)
    return EnforcementOutcome(blocked=hit is not None, reason=hit)


def test_fake_fence_missing_bash_c_is_failed() -> None:
    """The planted fence that misses ``bash -c 'qsub …'`` FAILS the block battery."""
    fake = kit.FenceCandidate(name="fake-first-token-only", run=_naive_first_token_fence)
    with pytest.raises(AssertionError, match="bash_c_qsub"):
        kit.check_blocks_fenced_verbs(fake)


def test_fake_fence_refusing_a_mention_is_failed() -> None:
    """A fence that refuses a mere mention (``grep qsub``) FAILS the pass battery."""
    fake = kit.FenceCandidate(name="fake-substring", run=_mention_refusing_fence)
    with pytest.raises(AssertionError, match="mention_grep_qsub"):
        kit.check_passes_mentions_probes_and_cli(fake)


# ─── the adapter seam: implementing the method DECLARES capability 6 ─────────


class _FenceAdapter:
    """A minimal harness declaring ONLY capability 6 (the Wave-C adapter shape)."""

    name = "fence-only"

    def run_scheduler_fence(self, command: str) -> EnforcementOutcome:
        return kit._builtin_reference().run(command)


def test_adapter_implementing_run_scheduler_fence_declares_capability_6() -> None:
    assert CAP_SCHEDULER_FENCE in declared_capabilities(_FenceAdapter())


def test_adapter_declaring_capability_6_passes_the_battery() -> None:
    """A declared capability-6 adapter certifies through the SAME shipped battery."""
    candidate = kit.FenceCandidate(name="fence-only", run=_FenceAdapter().run_scheduler_fence)
    kit.check_blocks_fenced_verbs(candidate)
    kit.check_passes_mentions_probes_and_cli(candidate)
