"""Tests for the ``recall`` primitive and its CLI adapter.

Recall walks one or more roots for ``interview.json`` files, projects
each into a structured per-campaign summary, and aggregates across all
matched campaigns into a tiered rollup. The tests pin:

- Per-campaign projection (Fix A — broadened to include budget,
  abort_if, task_generator, cluster_target).
- Tier 1 rollup (always-on; counts, histograms, task_count quantiles,
  materialized_at envelope).
- Tier 2 rollup (opt-in; walks .hpc/runtimes/*.json for walltime +
  failure-rate aggregation).
- Tier 3 rollup (opt-in; per-generator-kind parameter envelopes).
- Multi-root support and ``~/.hpc-agent/config.json:experiment_roots``
  fallback when --root is omitted.
- Filter semantics and CLI envelope shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.recall import RecallSpec
from hpc_agent.ops.memory import recall as recall_mod
from hpc_agent.ops.memory.recall import recall_campaigns, resolve_roots

if TYPE_CHECKING:
    from pathlib import Path


# ─── fixtures ─────────────────────────────────────────────────────────────


def _write_interview(
    campaign_dir: Path,
    *,
    goal: str = "test campaign",
    task_kind: str | None = None,
    task_count: int = 3,
    operator: str | None = None,
    materialized_at: str = "2026-05-04T12:00:00+00:00",
    cmd_sha: str = "deadbeef" * 8,
    budget: dict[str, Any] | None = None,
    abort_if: dict[str, Any] | None = None,
    cluster_target: dict[str, Any] | None = None,
    task_generator: dict[str, Any] | None = None,
) -> None:
    campaign_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "goal": goal,
        "task_count": task_count,
        "produced_by": {"kind": "human", "operator": operator},
        "_materialized": {
            "at": materialized_at,
            "cmd_sha": cmd_sha,
            "total_tasks": task_count,
        },
    }
    for key, val in (
        ("task_kind", task_kind),
        ("budget", budget),
        ("abort_if", abort_if),
        ("cluster_target", cluster_target),
        ("task_generator", task_generator),
    ):
        if val is not None:
            doc[key] = val
    (campaign_dir / "interview.json").write_text(json.dumps(doc, indent=2))


def _write_runtime_samples(
    campaign_dir: Path,
    *,
    profile: str = "gpu1",
    cluster: str = "slurm-a100",
    samples: list[dict[str, Any]] | None = None,
) -> None:
    """Drop a .hpc/runtimes/<profile>__<cluster>.json file with given samples."""
    runtimes = campaign_dir / ".hpc" / "runtimes"
    runtimes.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": 1, "profile": profile, "cluster": cluster, "samples": samples or []}
    (runtimes / f"{profile}__{cluster}.json").write_text(json.dumps(doc))


# ─── Fix A: per-campaign projection includes prior-decision fields ───────


def test_summary_includes_budget_abort_if_cluster_task_generator(tmp_path: Path) -> None:
    _write_interview(
        tmp_path / "c",
        budget={"gpu_hours": 200, "wall_clock_max_h": 12},
        abort_if={"metric": "val_loss", "above": 5.0, "after_tasks": 5},
        cluster_target={"cluster": "slurm-a100", "profile": "gpu1"},
        task_generator={
            "kind": "numeric_logspace",
            "params": {"param": "lr", "low": 1e-5, "high": 1e-1, "n": 30},
        },
    )
    data = recall_campaigns([tmp_path])
    s = data["campaigns"][0]
    assert s["budget"] == {"gpu_hours": 200, "wall_clock_max_h": 12}
    assert s["abort_if"]["metric"] == "val_loss"
    assert s["cluster_target"]["cluster"] == "slurm-a100"
    assert s["task_generator"]["kind"] == "numeric_logspace"
    assert s["task_generator"]["params"]["n"] == 30


def test_summary_fields_default_to_null_when_intent_omits(tmp_path: Path) -> None:
    """Minimal interview without optional fields shouldn't surface KeyErrors."""
    _write_interview(tmp_path / "c")
    s = recall_campaigns([tmp_path])["campaigns"][0]
    assert s["budget"] is None
    assert s["abort_if"] is None
    assert s["cluster_target"] is None
    assert s["task_generator"] is None


def test_transcript_and_notes_are_NOT_in_summary(tmp_path: Path) -> None:
    """Verbose fields stay out of the summary; caller re-reads interview.json."""
    campaign = tmp_path / "c"
    _write_interview(campaign)
    # Add transcript/notes manually
    doc = json.loads((campaign / "interview.json").read_text())
    doc["transcript"] = [{"role": "agent", "text": "...long..."}]
    doc["notes"] = "long-form notes" * 100
    (campaign / "interview.json").write_text(json.dumps(doc))

    s = recall_campaigns([tmp_path])["campaigns"][0]
    assert "transcript" not in s
    assert "notes" not in s


# ─── discovery + recency sort + multi-root ────────────────────────────────


def test_walks_nested_directories(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a" / "exp1")
    _write_interview(tmp_path / "b" / "deeper" / "exp2")
    _write_interview(tmp_path / "exp3")
    data = recall_campaigns([tmp_path])
    assert data["total_matching"] == 3


def test_multiple_roots_are_unioned(tmp_path: Path) -> None:
    a = tmp_path / "rootA"
    b = tmp_path / "rootB"
    _write_interview(a / "c1")
    _write_interview(a / "c2")
    _write_interview(b / "c3")
    data = recall_campaigns([a, b])
    assert data["total_matching"] == 3


def test_recency_sort_descending(tmp_path: Path) -> None:
    _write_interview(tmp_path / "old", materialized_at="2026-01-01T00:00:00+00:00")
    _write_interview(tmp_path / "new", materialized_at="2026-05-01T00:00:00+00:00")
    _write_interview(tmp_path / "mid", materialized_at="2026-03-01T00:00:00+00:00")
    ats = [c["materialized_at"] for c in recall_campaigns([tmp_path])["campaigns"]]
    assert ats == sorted(ats, reverse=True)


# ─── filters ──────────────────────────────────────────────────────────────


def test_filter_task_kind_exact_match(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", task_kind="ml-hparam-sweep")
    _write_interview(tmp_path / "b", task_kind="rl-rollout")
    _write_interview(tmp_path / "c", task_kind="ml-hparam-sweep")
    data = recall_campaigns([tmp_path], spec=RecallSpec(task_kind="ml-hparam-sweep"))
    assert data["total_matching"] == 2


def test_filter_operator_exact_match(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", operator="james")
    _write_interview(tmp_path / "b", operator="alex")
    data = recall_campaigns([tmp_path], spec=RecallSpec(operator="james"))
    assert data["total_matching"] == 1


def test_filter_since_iso_compare(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", materialized_at="2026-01-15T10:00:00+00:00")
    _write_interview(tmp_path / "b", materialized_at="2026-04-15T10:00:00+00:00")
    _write_interview(tmp_path / "c", materialized_at="2026-06-15T10:00:00+00:00")
    data = recall_campaigns([tmp_path], spec=RecallSpec(since="2026-04-01T00:00:00+00:00"))
    assert data["total_matching"] == 2


def test_combined_filters_are_anded(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", task_kind="ml", operator="james")
    _write_interview(tmp_path / "b", task_kind="rl", operator="james")
    _write_interview(tmp_path / "c", task_kind="ml", operator="alex")
    data = recall_campaigns([tmp_path], spec=RecallSpec(task_kind="ml", operator="james"))
    assert data["total_matching"] == 1


def test_limit_truncates_and_reports_total(tmp_path: Path) -> None:
    for i in range(5):
        _write_interview(tmp_path / f"c{i}", materialized_at=f"2026-05-0{i + 1}T00:00:00+00:00")
    data = recall_campaigns([tmp_path], spec=RecallSpec(limit=3))
    assert data["total_matching"] == 5
    assert data["showing"] == 3
    assert len(data["campaigns"]) == 3


# ─── Tier 1 rollup (always-on) ────────────────────────────────────────────


def test_tier1_rollup_count_and_histograms(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", task_kind="ml", operator="james", task_count=10)
    _write_interview(tmp_path / "b", task_kind="ml", operator="james", task_count=20)
    _write_interview(tmp_path / "c", task_kind="rl", operator="alex", task_count=30)
    rollup = recall_campaigns([tmp_path])["rollup"]
    assert rollup["count"] == 3
    assert rollup["task_kind_distribution"] == {"ml": 2, "rl": 1}
    assert rollup["operators"] == {"james": 2, "alex": 1}
    assert rollup["produced_by_kinds"] == {"human": 3}
    assert rollup["task_count"]["min"] == 10
    assert rollup["task_count"]["max"] == 30


def test_tier1_rollup_quantiles_interpolate(tmp_path: Path) -> None:
    """Linear-interp percentile so 1, 2, 3 → p50=2."""
    for i, count in enumerate([1, 2, 3]):
        _write_interview(tmp_path / f"c{i}", task_count=count)
    tc = recall_campaigns([tmp_path])["rollup"]["task_count"]
    assert tc["p50"] == 2.0


def test_tier1_rollup_histograms_drop_none(tmp_path: Path) -> None:
    """Campaigns missing task_kind / cluster_target shouldn't show as 'None' bucket."""
    _write_interview(tmp_path / "a", task_kind="ml")
    _write_interview(tmp_path / "b")  # no task_kind, no cluster_target
    rollup = recall_campaigns([tmp_path])["rollup"]
    assert rollup["task_kind_distribution"] == {"ml": 1}
    assert rollup["clusters"] == {}


def test_tier1_rollup_clusters_pulls_from_cluster_target(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", cluster_target={"cluster": "slurm-a100", "profile": "g1"})
    _write_interview(tmp_path / "b", cluster_target={"cluster": "slurm-a100", "profile": "g1"})
    _write_interview(tmp_path / "c", cluster_target={"cluster": "sge-cpu", "profile": "c1"})
    assert recall_campaigns([tmp_path])["rollup"]["clusters"] == {"slurm-a100": 2, "sge-cpu": 1}


def test_tier1_rollup_empty_when_no_matches(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", task_kind="ml")
    rollup = recall_campaigns([tmp_path], spec=RecallSpec(task_kind="zzz"))["rollup"]
    assert rollup["count"] == 0
    assert rollup["task_count"] is None


# ─── Tier 2 rollup (opt-in) ───────────────────────────────────────────────


def test_tier2_runtime_rollup_aggregates_walltime(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_interview(a)
    _write_interview(b)
    _write_runtime_samples(
        a, samples=[{"elapsed_sec": 100, "exit_code": 0}, {"elapsed_sec": 200, "exit_code": 0}]
    )
    _write_runtime_samples(
        b, samples=[{"elapsed_sec": 300, "exit_code": 1}, {"elapsed_sec": 400, "exit_code": 0}]
    )
    rollup = recall_campaigns([tmp_path], spec=RecallSpec(include_runtime=True))["rollup"]
    rt = rollup["runtime_rollup"]
    assert rt["walltime_per_task_sec"]["min"] == 100
    assert rt["walltime_per_task_sec"]["max"] == 400
    assert rt["walltime_per_task_sec"]["n_samples"] == 4
    assert rt["total_task_samples"] == 4
    assert rt["failure_rate"] == pytest.approx(0.25)
    assert rt["campaigns_with_no_runtime"] == 0


def test_tier2_counts_campaigns_without_runtime_files(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a")  # no .hpc/runtimes/
    _write_interview(tmp_path / "b")
    _write_runtime_samples(tmp_path / "b", samples=[{"elapsed_sec": 50, "exit_code": 0}])
    rt = recall_campaigns([tmp_path], spec=RecallSpec(include_runtime=True))["rollup"][
        "runtime_rollup"
    ]  # noqa: E501
    assert rt["campaigns_with_no_runtime"] == 1
    assert rt["total_task_samples"] == 1


def test_tier2_skips_malformed_samples_instead_of_crashing(tmp_path: Path) -> None:
    """One poisoned runtimes file must not sink the whole rollup (the module's
    tolerant-read contract): non-dict samples are skipped, a non-numeric
    exit_code counts the sample without counting it failed, and the healthy
    campaign's numbers still aggregate."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_interview(a)
    _write_interview(b)
    _write_runtime_samples(
        a,
        samples=[
            "not-a-dict",
            None,
            [{"elapsed_sec": 5}],
            {"elapsed_sec": "fast", "exit_code": "boom"},
            {"elapsed_sec": 100, "exit_code": 1},
        ],
    )
    _write_runtime_samples(b, samples=[{"elapsed_sec": 200, "exit_code": 0}])
    rt = recall_campaigns([tmp_path], spec=RecallSpec(include_runtime=True))["rollup"][
        "runtime_rollup"
    ]  # noqa: E501
    # The three non-dict entries are skipped; the two dict samples (one with
    # unusable values) plus b's healthy sample are counted.
    assert rt["total_task_samples"] == 3
    assert rt["walltime_per_task_sec"]["n_samples"] == 2
    assert rt["walltime_per_task_sec"]["min"] == 100
    assert rt["walltime_per_task_sec"]["max"] == 200
    assert rt["failure_rate"] == pytest.approx(1 / 3)
    assert rt["campaigns_with_no_runtime"] == 0


def test_tier2_absent_when_not_requested(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a")
    _write_runtime_samples(tmp_path / "a", samples=[{"elapsed_sec": 60, "exit_code": 0}])
    rollup = recall_campaigns([tmp_path])["rollup"]
    assert "runtime_rollup" not in rollup


def test_tier2_handles_no_samples_at_all(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a")
    rt = recall_campaigns([tmp_path], spec=RecallSpec(include_runtime=True))["rollup"][
        "runtime_rollup"
    ]  # noqa: E501
    assert rt["walltime_per_task_sec"] is None
    assert rt["failure_rate"] is None
    assert rt["campaigns_with_no_runtime"] == 1


# ─── Tier 3 rollup (opt-in, generator-aware) ──────────────────────────────


def test_tier3_logspace_param_envelope_across_campaigns(tmp_path: Path) -> None:
    for low, high, n in [(1e-5, 1e-2, 30), (1e-6, 1e-1, 50), (1e-4, 1e-1, 20)]:
        _write_interview(
            tmp_path / f"c-{n}",
            task_generator={
                "kind": "numeric_logspace",
                "params": {"param": "lr", "low": low, "high": high, "n": n},
            },
        )
    rollup = recall_campaigns([tmp_path], spec=RecallSpec(include_generator_stats=True))["rollup"]
    env = rollup["generator_rollup"]["by_kind"]["numeric_logspace"]["param_envelopes"]["lr"]
    assert env["low"] == [1e-6, 1e-4]
    assert env["high"] == [1e-2, 1e-1]
    assert env["n"] == [20, 50]


def test_tier3_cartesian_axis_value_unions(tmp_path: Path) -> None:
    _write_interview(
        tmp_path / "a",
        task_generator={
            "kind": "cartesian_product",
            "params": {"axes": {"lr": [1e-3, 1e-2], "bs": [16, 32]}},
        },
        task_count=4,
    )
    _write_interview(
        tmp_path / "b",
        task_generator={
            "kind": "cartesian_product",
            "params": {"axes": {"lr": [1e-2, 1e-1], "bs": [32, 64]}},
        },
        task_count=4,
    )
    rollup = recall_campaigns([tmp_path], spec=RecallSpec(include_generator_stats=True))["rollup"]
    union = rollup["generator_rollup"]["by_kind"]["cartesian_product"]["axis_value_unions"]
    assert sorted(union["lr"]) == [1e-3, 1e-2, 1e-1]
    assert sorted(union["bs"]) == [16, 32, 64]


def test_tier3_buckets_by_kind(tmp_path: Path) -> None:
    _write_interview(
        tmp_path / "a",
        task_generator={
            "kind": "numeric_logspace",
            "params": {"param": "lr", "low": 1e-5, "high": 1e-1, "n": 5},
        },
    )
    _write_interview(
        tmp_path / "b",
        task_generator={"kind": "enumerated", "params": {"items": [{"x": 1}]}},
        task_count=1,
    )
    rollup = recall_campaigns([tmp_path], spec=RecallSpec(include_generator_stats=True))["rollup"]
    by_kind = rollup["generator_rollup"]["by_kind"]
    assert set(by_kind) == {"numeric_logspace", "enumerated"}
    assert by_kind["enumerated"]["count"] == 1
    assert "param_envelopes" not in by_kind["enumerated"]


def test_tier3_skips_campaigns_without_task_generator(tmp_path: Path) -> None:
    """Hand-written tasks.py campaigns shouldn't pollute the bucket list."""
    _write_interview(tmp_path / "hand", task_generator=None)
    _write_interview(
        tmp_path / "gen",
        task_generator={"kind": "enumerated", "params": {"items": [{"x": 1}]}},
        task_count=1,
    )
    by_kind = recall_campaigns([tmp_path], spec=RecallSpec(include_generator_stats=True))["rollup"][
        "generator_rollup"
    ]["by_kind"]
    assert set(by_kind) == {"enumerated"}


# ─── resilience ───────────────────────────────────────────────────────────


def test_malformed_interview_json_is_skipped(tmp_path: Path) -> None:
    _write_interview(tmp_path / "good")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "interview.json").write_text("{not json")
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "interview.json").write_text(json.dumps({"goal": "old"}))

    data = recall_campaigns([tmp_path])
    assert data["total_matching"] == 1


def test_invalid_root_raises(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="not a directory"):
        recall_campaigns([tmp_path / "does-not-exist"])


def test_empty_roots_list_raises() -> None:
    with pytest.raises(errors.SpecInvalid, match="no roots to walk"):
        recall_campaigns([])


# ─── resolve_roots: config-driven default ─────────────────────────────────


def test_resolve_roots_explicit_wins() -> None:
    assert resolve_roots("/explicit/path") == [recall_mod.Path("/explicit/path")]


def test_resolve_roots_reads_config(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"experiment_roots": ["/exp1", "/exp2"]}))
    monkeypatch.setattr(recall_mod, "_USER_CONFIG", config)
    roots = resolve_roots(None)
    assert roots == [recall_mod.Path("/exp1"), recall_mod.Path("/exp2")]


def test_resolve_roots_returns_empty_when_no_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(recall_mod, "_USER_CONFIG", tmp_path / "no-config.json")
    assert resolve_roots(None) == []


def test_resolve_roots_ignores_malformed_config(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text("{not json")
    monkeypatch.setattr(recall_mod, "_USER_CONFIG", config)
    assert resolve_roots(None) == []


def test_resolve_roots_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"experiment_roots": ["~/experiments"]}))
    monkeypatch.setattr(recall_mod, "_USER_CONFIG", config)
    roots = resolve_roots(None)
    assert roots[0].is_absolute()
    assert "~" not in str(roots[0])


# ─── CLI surface ──────────────────────────────────────────────────────────


def _run_cli(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_help_lists_recall() -> None:
    rc, out, _ = _run_cli("--help")
    assert rc == 0
    assert "recall" in out


def test_cli_emits_envelope_with_rollup(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", task_kind="ml-hparam-sweep")
    _write_interview(tmp_path / "b", task_kind="rl-rollout")
    rc, out, err = _run_cli("recall", "--root", str(tmp_path), "--task-kind", "ml-hparam-sweep")
    assert rc == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["data"]["total_matching"] == 1
    assert "rollup" in payload["data"]
    assert payload["data"]["rollup"]["count"] == 1


def test_cli_no_root_no_config_errors(tmp_path: Path, monkeypatch) -> None:
    """When neither --root nor config is set, the CLI errors with spec_invalid."""
    # Force an empty HOME so the test doesn't pick up the developer's config.
    env = {**__import__("os").environ, "HOME": str(tmp_path)}
    rc, out, _ = _run_cli("recall", env=env)
    assert rc == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "no roots" in payload["message"].lower()


def test_cli_invalid_root_maps_to_user_error(tmp_path: Path) -> None:
    rc, out, _ = _run_cli("recall", "--root", str(tmp_path / "nonexistent"))
    assert rc == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error_code"] == "spec_invalid"
