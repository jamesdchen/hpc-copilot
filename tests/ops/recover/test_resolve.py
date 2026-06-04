"""Tests for the context-keyed failure resolver (#234).

Pins the central claim: the SAME signature resolves to DIFFERENT fixes once
the failure_features context is supplied, and only the genuinely-ambiguous
residue escalates to judgement.
"""

from __future__ import annotations

from hpc_agent._wire.fixtures.escalation import EscalationCluster
from hpc_agent._wire.fixtures.failure_features import FailureFeatures
from hpc_agent.ops.recover.resolve import resolve, tally_decisions


def _features(**kw) -> FailureFeatures:
    return FailureFeatures(**kw)


# ── the OOM@tp_size vs OOM@width split — same error_class, opposite fix ──────


def test_gpu_oom_with_parallelism_reshards_not_more_memory():
    """OOM at tp_size=2: model already sharded → more per-GPU mem won't help."""
    r = resolve(_features(error_class="gpu_oom", resource_spec={"tp_size": 2}))
    assert r.decided_by == "code"
    assert r.action["action"] == "increase-parallelism"


def test_gpu_oom_with_large_width_first_attempt_shrinks():
    """OOM at a large batch width on the first attempt → shrink the width."""
    r = resolve(
        _features(
            error_class="gpu_oom",
            resource_spec={"batch_size": 512},
            temporal_context={"phase": "first_attempt"},
        )
    )
    assert r.decided_by == "code"
    assert r.action["action"] == "reduce-width"


def test_gpu_oom_without_discriminating_context_falls_back_to_catalog_fix():
    r = resolve(_features(error_class="gpu_oom", resource_spec={}))
    assert r.decided_by == "code"
    assert r.action["action"] == "increase-mem-per-gpu"


# ── walltime is keyed on temporal_context ───────────────────────────────────


def test_walltime_first_attempt_doubles():
    r = resolve(
        _features(error_class="walltime", temporal_context={"phase": "first_attempt"})
    )
    assert r.action["action"] == "increase-walltime"
    assert r.action["factor"] == 2.0


def test_walltime_after_progress_bumps_smaller():
    r = resolve(
        _features(
            error_class="walltime",
            temporal_context={"phase": "after_progress", "successful_units": 90},
        )
    )
    assert r.action["factor"] == 1.5


# ── deterministic classes resolve to code ───────────────────────────────────


def test_system_oom_and_node_failure_are_deterministic():
    assert resolve(_features(error_class="system_oom")).decided_by == "code"
    assert resolve(_features(error_class="node_failure")).decided_by == "code"


# ── the residue escalates to judgement, carrying evidence + cluster ─────────


def test_unknown_escalates_with_features_and_cluster():
    cluster = EscalationCluster(fingerprint="fp", task_ids=["t1", "t2"])
    r = resolve(_features(error_class="unknown"), cluster=cluster)
    assert r.decided_by == "judgement"
    assert r.action is None
    assert r.escalation is not None
    assert r.escalation.failure_features.error_class == "unknown"
    assert r.escalation.cluster.task_ids == ["t1", "t2"]


def test_code_bug_escalates_with_real_vs_transient_candidates():
    r = resolve(_features(error_class="code_bug"))
    assert r.decided_by == "judgement"
    actions = {c.action for c in r.escalation.candidate_actions}
    assert actions == {"retry", "user-debug"}


def test_segv_escalates():
    assert resolve(_features(error_class="segv")).decided_by == "judgement"


def test_none_error_class_treated_as_unknown_escalates():
    assert resolve(_features()).decided_by == "judgement"


# ── exhaustion fall-through (don't loop a fix that isn't working) ────────────


def test_exhausted_deterministic_strategy_escalates():
    """gpu_oom whose 'increase-mem-per-gpu' was already tried this episode →
    escalate instead of looping the same fix."""
    r = resolve(
        _features(
            error_class="gpu_oom",
            resource_spec={},
            attempts_this_episode={"count": 1, "strategies": ["increase-mem-per-gpu"]},
        ),
        max_code_attempts=1,
    )
    assert r.decided_by == "judgement"


def test_attempt_cap_spent_escalates():
    r = resolve(
        _features(error_class="walltime", attempts_this_episode={"count": 2}),
        max_code_attempts=1,
    )
    assert r.decided_by == "judgement"


# ── the decided_by health-signal tally ──────────────────────────────────────


def test_tally_decisions_counts_code_vs_judgement():
    res = [
        resolve(_features(error_class="gpu_oom", resource_spec={"tp_size": 2})),
        resolve(_features(error_class="system_oom")),
        resolve(_features(error_class="unknown")),
    ]
    assert tally_decisions(res) == {"code": 2, "judgement": 1}
