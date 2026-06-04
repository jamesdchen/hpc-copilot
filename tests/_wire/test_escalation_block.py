"""Contract tests for the unified escalation block (#231).

The escalation block is *decision-as-data*: one typed Pydantic model that
rides on the existing binary envelope as an optional field, present on
EITHER outcome. These tests pin the three properties the resolved design
promises:

1. it attaches to a success AND a failure envelope (orthogonal to ``ok``);
2. a router inspects every envelope for it and never silently drops one;
3. it subsumes the three escalation shapes it replaces.
"""

from __future__ import annotations

import json
from importlib.resources import files as resource_files

import pytest
from pydantic import ValidationError

from hpc_agent._wire.fixtures.envelope import EnvelopeAdapter
from hpc_agent._wire.fixtures.escalation import (
    CandidateAction,
    Escalation,
    EscalationCluster,
    escalation_of,
)


def _success(**escalation: object) -> dict:
    env: dict = {"ok": True, "idempotent": True, "data": {}}
    if escalation:
        env["escalation"] = escalation
    return env


def _failure(**escalation: object) -> dict:
    env: dict = {
        "ok": False,
        "error_code": "combiner_failed",
        "message": "boom",
        "category": "cluster",
        "retry_safe": True,
    }
    if escalation:
        env["escalation"] = escalation
    return env


# 1 ── orthogonal to ok: rides on either outcome ────────────────────────────


def test_escalation_rides_on_success_envelope():
    """A succeeded-but-needs-a-decision case (e.g. campaign-advance)."""
    env = _success(
        decided_by="code",
        reason="plateau reached",
        candidate_actions=[{"action": "stop_converged", "source": "policy"}],
    )
    parsed = EnvelopeAdapter.validate_python(env)
    assert parsed.ok is True
    assert parsed.escalation is not None
    assert parsed.escalation.decided_by == "code"


def test_escalation_rides_on_error_envelope():
    """A failure the deterministic resolver could not resolve."""
    env = _failure(
        decided_by="judgement",
        failure_features={"error_class": "unknown"},
        cluster={"fingerprint": "fp1", "task_ids": ["t1", "t2"]},
    )
    parsed = EnvelopeAdapter.validate_python(env)
    assert parsed.ok is False
    assert parsed.escalation is not None
    assert parsed.escalation.cluster is not None
    assert parsed.escalation.cluster.task_ids == ["t1", "t2"]


# 2 ── the router contract: inspect EVERY envelope, never drop ──────────────


@pytest.mark.parametrize(
    "env",
    [
        _success(decided_by="code", candidate_actions=[{"action": "continue"}]),
        _failure(decided_by="judgement", failure_features={"error_class": "unknown"}),
    ],
)
def test_router_extracts_on_both_outcomes(env):
    block = escalation_of(env)
    assert isinstance(block, Escalation)


@pytest.mark.parametrize("env", [_success(), _failure()])
def test_router_returns_none_when_no_decision_needed(env):
    """An ordinary envelope (no escalation) yields None — the router
    handles absence rather than requiring the key."""
    assert escalation_of(env) is None


# 3 ── subsumes the three shapes it replaces ────────────────────────────────


def test_subsumes_campaign_advance_decision():
    """campaign-advance's {decision, reason} dict maps onto an Escalation:
    a success that needs a decision, decided_by=code (computed), with the
    decision surfaced as the recommended candidate action."""
    block = Escalation(
        decided_by="code",
        reason="42 iteration(s) complete, plateau",
        candidate_actions=[CandidateAction(action="stop_converged", source="policy")],
    )
    assert block.candidate_actions[0].action == "stop_converged"


def test_subsumes_error_envelope_failure():
    """An ErrorEnvelope failure carries its #230 evidence inside the block."""
    block = Escalation(
        decided_by="judgement",
        failure_features={"error_class": "gpu_oom", "resource_spec": {"tp_size": 2}},
    )
    assert block.failure_features is not None
    assert block.failure_features.error_class == "gpu_oom"


def test_cluster_keeps_per_task_refs_for_fanout():
    """Cluster-to-decide-once must NOT dedup away the per-task refs."""
    cluster = EscalationCluster(fingerprint="oom@tp2", task_ids=["a", "b", "c"], wave=3)
    block = Escalation(decided_by="judgement", cluster=cluster)
    assert block.cluster.task_ids == ["a", "b", "c"]
    assert block.cluster.wave == 3


# 4 ── strictness + round-trip + schema presence ────────────────────────────


def test_extra_keys_rejected():
    with pytest.raises(ValidationError):
        Escalation(decided_by="code", bogus="x")  # type: ignore[call-arg]


def test_round_trips_through_dump_and_validate():
    block = Escalation(
        decided_by="judgement",
        reason="novel failure",
        failure_features={"error_class": "unknown"},
        candidate_actions=[{"action": "user-debug", "source": "catalog"}],
        cluster={"fingerprint": "fp", "task_ids": ["t1"]},
    )
    assert Escalation.model_validate(block.model_dump()) == block


def test_escalation_schema_file_emitted():
    """The standalone escalation.json is generated for external consumers."""
    text = (resource_files("hpc_agent.schemas") / "escalation.json").read_text(encoding="utf-8")
    schema = json.loads(text)
    assert schema["$id"].endswith("escalation.json")
    assert "decided_by" in schema["properties"]


def test_envelope_schema_inlines_escalation():
    """envelope.json carries the escalation field on both variants."""
    text = (resource_files("hpc_agent.schemas") / "envelope.json").read_text(encoding="utf-8")
    assert "escalation" in text
