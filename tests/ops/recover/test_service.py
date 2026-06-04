"""Tests for the service-dependency Tier 1 contract (#231).

Two halves: address passthrough (inject_service_env) and the
escalation-on-failure contract (service_failure_escalation), including the
silent-rot case that motivates separating liveness from correctness.
"""

from __future__ import annotations

from hpc_agent._wire.fixtures.escalation import Escalation, EscalationCluster
from hpc_agent.ops.recover.service import (
    SERVICE_ENV_NAMESPACE,
    inject_service_env,
    service_failure_escalation,
    service_failure_features,
)

# ── passthrough ─────────────────────────────────────────────────────────────


def test_inject_namespaces_each_var():
    env: dict[str, str] = {"PATH": "/usr/bin"}
    out = inject_service_env(env, {"addr": "http://node7:8000", "token": "abc"})
    assert out["HPC_SERVICE_ADDR"] == "http://node7:8000"
    assert out["HPC_SERVICE_TOKEN"] == "abc"
    # never clobbers existing process env
    assert out["PATH"] == "/usr/bin"
    assert all(k.startswith(SERVICE_ENV_NAMESPACE) or k == "PATH" for k in out)


def test_inject_none_is_noop():
    env = {"PATH": "/usr/bin"}
    assert inject_service_env(env, None) == {"PATH": "/usr/bin"}
    assert inject_service_env(env, {}) == {"PATH": "/usr/bin"}


def test_inject_coerces_values_to_str():
    out = inject_service_env({}, {"port": 8000})
    assert out["HPC_SERVICE_PORT"] == "8000"


# ── escalation: evidence ────────────────────────────────────────────────────


def test_features_carry_liveness_vs_correctness():
    f = service_failure_features(liveness="pass", correctness="fail", detail="bad tensor")
    assert f.error_class == "unknown"  # escape hatch — a decision, not a known fix
    assert f.liveness_vs_correctness.liveness == "pass"
    assert f.liveness_vs_correctness.correctness == "fail"
    assert f.liveness_vs_correctness.detail == "bad tensor"


# ── escalation: routing + candidates differ by signal ───────────────────────


def test_down_service_offers_restart():
    e = service_failure_escalation(liveness="fail", correctness="unknown")
    assert isinstance(e, Escalation)
    assert e.decided_by == "judgement"
    assert [c.action for c in e.candidate_actions] == ["restart-service"]


def test_silent_rot_offers_restart_and_human():
    """liveness=pass, correctness=fail — 'up but not ready', the case a port
    ping misses; restart OR escalate to a human."""
    e = service_failure_escalation(liveness="pass", correctness="fail")
    assert "silent rot" in e.reason
    assert {c.action for c in e.candidate_actions} == {"restart-service", "user-debug"}
    assert e.failure_features.liveness_vs_correctness.correctness == "fail"


def test_escalation_carries_cluster_for_fanout():
    cluster = EscalationCluster(fingerprint="svc", task_ids=["t1", "t2"])
    e = service_failure_escalation(liveness="fail", correctness="unknown", cluster=cluster)
    assert e.cluster.task_ids == ["t1", "t2"]


# ── sidecar field round-trips ───────────────────────────────────────────────


def test_service_env_sidecar_round_trips(tmp_path):
    from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id="r1",
        cmd_sha="sha",
        hpc_agent_version="0.10.0",
        submitted_at="2026-06-04T00:00:00Z",
        executor="python run.py",
        result_dir_template="out/{task_id}",
        task_count=1,
        tasks_py_sha="t",
        service_env={"addr": "http://node7:8000"},
    )
    data = read_run_sidecar(tmp_path, "r1")
    assert data["service_env"] == {"addr": "http://node7:8000"}


def test_service_env_absent_backfills_to_none(tmp_path):
    from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id="r2",
        cmd_sha="sha",
        hpc_agent_version="0.10.0",
        submitted_at="2026-06-04T00:00:00Z",
        executor="python run.py",
        result_dir_template="out/{task_id}",
        task_count=1,
        tasks_py_sha="t",
    )
    assert read_run_sidecar(tmp_path, "r2")["service_env"] is None
