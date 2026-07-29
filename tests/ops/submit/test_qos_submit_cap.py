"""The QoS submit-cap gate (lesson-6 self-DOS class).

Covers both layers: the pure arithmetic (``check_qos_submit_cap`` — refuse at
the cap, disclose proximity, pass below) and the composed gate
(``_qos_submit_cap_gate`` — config-absent no-op, same-cluster in-flight
counting, canary counting, refusal before any transport).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops import submit_flow as sf


def _spec(run_id: str, total_tasks: int, cluster: str = "clusterA") -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="p",
        cluster=cluster,
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        run_id=run_id,
        total_tasks=total_tasks,
        backend="slurm",
        script="run.sh",
        job_env={},
    )


class _Record:
    def __init__(self, cluster: str, total_tasks: int) -> None:
        self.cluster = cluster
        self.total_tasks = total_tasks


# ── the pure arithmetic ──────────────────────────────────────────────────────


def test_pure_refuses_at_cap_with_split_guidance() -> None:
    verdict = sf.check_qos_submit_cap(cap=100, known_in_flight_tasks=20, new_tasks=80)
    assert verdict is not None
    kind, message = verdict
    assert kind == "refuse"
    # Names the numbers, the config key, and the split headroom (cap-20-1=79).
    assert "100" in message and "max_submit_jobs_per_user" in message
    assert "<= 79 tasks" in message
    # The undercount disclosure: external jobs are invisible to the journal.
    assert "NOT counted" in message


def test_pure_warns_near_cap_and_passes_below() -> None:
    warn = sf.check_qos_submit_cap(cap=100, known_in_flight_tasks=0, new_tasks=70)
    assert warn is not None and warn[0] == "warn"
    assert sf.check_qos_submit_cap(cap=100, known_in_flight_tasks=0, new_tasks=69) is None


def test_pure_zero_headroom_names_wait() -> None:
    verdict = sf.check_qos_submit_cap(cap=10, known_in_flight_tasks=10, new_tasks=5)
    assert verdict is not None and verdict[0] == "refuse"
    assert "<= 0 tasks" in verdict[1]


# ── the composed gate ────────────────────────────────────────────────────────


def _patch_config(monkeypatch: pytest.MonkeyPatch, config: dict) -> None:
    from hpc_agent.infra import clusters

    monkeypatch.setattr(clusters, "load_clusters_config", lambda path=None: config)


def test_gate_noop_when_cluster_declares_no_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch, {"clusterA": {}})
    sf._qos_submit_cap_gate(
        tmp_path, [_spec("r1", 10_000)], [0], canary_decisions={0: (False, None)}
    )


def test_gate_refuses_oversized_array_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch, {"clusterA": {"max_submit_jobs_per_user": 100}})
    monkeypatch.setattr(sf, "find_in_flight_runs", lambda _dir: [])
    with pytest.raises(errors.SpecInvalid, match="qos submit-cap"):
        sf._qos_submit_cap_gate(
            tmp_path, [_spec("r1", 200)], [0], canary_decisions={0: (False, None)}
        )


def test_gate_counts_same_cluster_in_flight_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch, {"clusterA": {"max_submit_jobs_per_user": 100}})
    # 60 in flight on clusterA + 50 new >= 100 → refuse; the 500 on clusterB
    # consume a different queue and must not count.
    monkeypatch.setattr(
        sf,
        "find_in_flight_runs",
        lambda _dir: [_Record("clusterA", 60), _Record("clusterB", 500)],
    )
    with pytest.raises(errors.SpecInvalid):
        sf._qos_submit_cap_gate(
            tmp_path, [_spec("r1", 50)], [0], canary_decisions={0: (False, None)}
        )
    # Same submit with clusterB's noise removed from the equation: 60+39 < 70%
    # of 100 is false (99 >= 70 warns) — use a clearly-passing size instead.
    sf._qos_submit_cap_gate(tmp_path, [_spec("r2", 5)], [0], canary_decisions={0: (False, None)})


def test_gate_counts_the_canary_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"clusterA": {"max_submit_jobs_per_user": 100}})
    monkeypatch.setattr(sf, "find_in_flight_runs", lambda _dir: [])
    # 99 tasks + 1 canary == 100 == cap → refuse; without the canary it passes
    # (99 < 100), proving the canary task is counted.
    with pytest.raises(errors.SpecInvalid):
        sf._qos_submit_cap_gate(
            tmp_path, [_spec("r1", 99)], [0], canary_decisions={0: (True, None)}
        )


def test_gate_warns_without_refusing_near_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_config(monkeypatch, {"clusterA": {"max_submit_jobs_per_user": 100}})
    monkeypatch.setattr(sf, "find_in_flight_runs", lambda _dir: [])
    sf._qos_submit_cap_gate(tmp_path, [_spec("r1", 80)], [0], canary_decisions={0: (False, None)})
    assert "qos submit-cap proximity" in capsys.readouterr().err


def test_gate_degrades_to_array_only_on_unreadable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch, {"clusterA": {"max_submit_jobs_per_user": 100}})

    def _boom(_dir: Path) -> list:
        raise OSError("index unreadable")

    monkeypatch.setattr(sf, "find_in_flight_runs", _boom)
    # Journal trouble must not block a small legal submit...
    sf._qos_submit_cap_gate(tmp_path, [_spec("r1", 5)], [0], canary_decisions={0: (False, None)})
    # ...while the array-size check itself still fires.
    with pytest.raises(errors.SpecInvalid):
        sf._qos_submit_cap_gate(
            tmp_path, [_spec("r2", 200)], [0], canary_decisions={0: (False, None)}
        )


def test_gate_noop_with_no_fresh_specs(tmp_path: Path) -> None:
    sf._qos_submit_cap_gate(tmp_path, [_spec("r1", 999_999)], [], canary_decisions={})
