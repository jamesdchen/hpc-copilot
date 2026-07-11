"""Unit tests for the per-cluster ``failure_features`` glue (#240).

Pins the PURE mapping ``fetch_failures`` cluster + RunRecord + sidecar → a
:class:`FailureFeatures`: the three resolver-keyed fields (error_class,
resource_spec, temporal_context) plus attempts_this_episode, and the
:class:`EscalationCluster` provenance shape.
"""

from __future__ import annotations

from typing import Any

from hpc_agent.ops.recover.features_glue import (
    build_escalation_cluster,
    build_failure_features,
)
from hpc_agent.state.pack_declarations import FailurePatternsDecl, PackEcho
from hpc_agent.state.run_record import RunRecord


def _record(**overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": "20260606-120000-aaa",
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "myjob",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-06-06T12:00:00+00:00",
        "experiment_dir": "/exp",
    }
    base.update(overrides)
    return RunRecord(**base)


def _cluster(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "error_class": "gpu_oom",
        "category": "gpu_oom",
        "fingerprint": "fp-oom",
        "task_ids": [0, 1],
    }
    base.update(overrides)
    return base


# ── error_class passthrough ──────────────────────────────────────────────────


def test_error_class_from_cluster() -> None:
    f = build_failure_features(_cluster(error_class="walltime"), record=_record(), sidecar=None)
    assert f.error_class == "walltime"


def test_none_error_class_passes_through_as_none() -> None:
    f = build_failure_features(_cluster(error_class=None), record=_record(), sidecar=None)
    assert f.error_class is None


def test_fine_grained_catalog_classes_do_not_crash_the_seam() -> None:
    """bug-sweep #2: the fingerprint classifier stamps fine-grained catalog
    classes (import_error, python_traceback, mpi_*, ...) that the narrow wire
    Literal used to reject — build_failure_features -> FailureFeatures
    validation raised and killed the monitor's terminal-FAILED tick. Every
    class the catalog emits must now pass through unchanged."""
    from hpc_agent.infra.failure_signatures import CLASSIFIER_CATEGORIES

    for cls in sorted(CLASSIFIER_CATEGORIES):
        f = build_failure_features(_cluster(error_class=cls), record=_record(), sidecar=None)
        assert f.error_class == cls


# ── resource_spec sourcing ───────────────────────────────────────────────────


def test_resource_spec_from_sidecar_resources_block() -> None:
    sidecar = {"resources": {"gpus": 2, "mem": "32G"}}
    f = build_failure_features(_cluster(), record=_record(), sidecar=sidecar)
    assert f.resource_spec == {"gpus": 2, "mem": "32G"}


def test_resource_spec_merges_extra_spec_kwargs_over_resources() -> None:
    """The free-form ``extra.spec_kwargs`` pocket carries task-level sweep kwargs
    (tp_size / batch_size / n) and wins on a key collision."""
    sidecar = {
        "resources": {"gpus": 2, "tp_size": 1},
        "extra": {"spec_kwargs": {"tp_size": 2, "batch_size": 512}},
    }
    f = build_failure_features(_cluster(), record=_record(), sidecar=sidecar)
    assert f.resource_spec == {"gpus": 2, "tp_size": 2, "batch_size": 512}


def test_resource_spec_passes_values_through_as_written() -> None:
    """No int normalization — a stringified value is preserved verbatim (the
    resolver's _degree coerces int-like strings downstream)."""
    sidecar = {"extra": {"spec_kwargs": {"tp_size": "2"}}}
    f = build_failure_features(_cluster(), record=_record(), sidecar=sidecar)
    assert f.resource_spec == {"tp_size": "2"}


def test_resource_spec_none_when_no_sidecar() -> None:
    f = build_failure_features(_cluster(), record=_record(), sidecar=None)
    assert f.resource_spec is None


def test_resource_spec_none_when_empty() -> None:
    f = build_failure_features(_cluster(), record=_record(), sidecar={"resources": {}})
    assert f.resource_spec is None


# ── temporal_context.phase ───────────────────────────────────────────────────


def test_phase_unknown_without_a_progress_signal() -> None:
    # The recover seam has no progress signal; phase must NOT be asserted from
    # the retry count. "unknown" is resolve()'s conservative not-first_attempt read.
    f = build_failure_features(_cluster(), record=_record(), sidecar=None)
    assert f.temporal_context.phase == "unknown"


def test_phase_stays_unknown_even_when_a_cluster_task_was_retried() -> None:
    # A retry is not progress: a retried task may still have failed before any
    # unit of work succeeded. phase stays "unknown" (the retry count lands in
    # attempts_this_episode, not in phase).
    rec = _record(retries={"0": {"attempts": 1, "category": "gpu_oom", "overrides": {}}})
    f = build_failure_features(_cluster(task_ids=[0, 1]), record=rec, sidecar=None)
    assert f.temporal_context.phase == "unknown"


def test_phase_unknown_regardless_of_unrelated_task_retries() -> None:
    rec = _record(retries={"7": {"attempts": 2, "category": "walltime", "overrides": {}}})
    f = build_failure_features(_cluster(task_ids=[0, 1]), record=rec, sidecar=None)
    assert f.temporal_context.phase == "unknown"


# ── attempts_this_episode ────────────────────────────────────────────────────


def test_attempts_count_is_max_among_cluster_tasks() -> None:
    rec = _record(
        retries={
            "0": {"attempts": 1, "category": "gpu_oom", "overrides": {}},
            "1": {"attempts": 3, "category": "gpu_oom", "overrides": {}},
        }
    )
    f = build_failure_features(_cluster(task_ids=[0, 1]), record=rec, sidecar=None)
    assert f.attempts_this_episode.count == 3


def test_strategies_collect_category_and_override_action_deduped() -> None:
    rec = _record(
        retries={
            "0": {
                "attempts": 1,
                "category": "gpu_oom",
                "overrides": {"action": "increase-mem-per-gpu", "factor": 1.5},
            },
            "1": {
                "attempts": 1,
                "category": "gpu_oom",  # duplicate category — deduped
                "overrides": {"action": "reduce-width"},
            },
        }
    )
    f = build_failure_features(_cluster(task_ids=[0, 1]), record=rec, sidecar=None)
    assert f.attempts_this_episode.strategies == [
        "gpu_oom",
        "increase-mem-per-gpu",
        "reduce-width",
    ]


def test_strategies_none_when_no_retries() -> None:
    f = build_failure_features(_cluster(), record=_record(), sidecar=None)
    assert f.attempts_this_episode.count == 0
    assert f.attempts_this_episode.strategies is None


# ── EscalationCluster provenance ─────────────────────────────────────────────


def test_escalation_cluster_carries_fingerprint_run_id_and_str_task_ids() -> None:
    ec = build_escalation_cluster(_cluster(task_ids=[0, 2]), run_id="run-1")
    assert ec.fingerprint == "fp-oom"
    assert ec.run_id == "run-1"
    assert ec.task_ids == ["0", "2"]  # model field is list[str]


# ── S2 domain-pack failure_patterns (T9b) ────────────────────────────────────
#
# Core COMPILES + COUNTS caller-opaque regexes over the cluster stderr and records
# the HIT ids as evidence + the pack echo. MATCH-AND-RECORD ONLY: never a
# category/action/retry. Toy patterns only (`widget-jam`).


def _decl(patterns: dict[str, str], *, pack: str, sha: str, version: str | None = "v1") -> Any:
    return FailurePatternsDecl(
        patterns=patterns, echo=PackEcho(pack=pack, version=version, sha=sha)
    )


def test_no_patterns_is_byte_identical_to_pre_packs_vector() -> None:
    # None and [] both leave the vector byte-identical to the pre-packs shape:
    # the new fields are None and drop out of the dumped envelope.
    cluster = _cluster(fingerprint="widget-jam at rotor", sample="widget-jam trace")
    baseline = build_failure_features(cluster, record=_record(), sidecar=None)
    with_none = build_failure_features(
        cluster, record=_record(), sidecar=None, failure_patterns=None
    )
    with_empty = build_failure_features(
        cluster, record=_record(), sidecar=None, failure_patterns=[]
    )
    dump = baseline.model_dump(exclude_none=True)
    assert with_none.model_dump(exclude_none=True) == dump
    assert with_empty.model_dump(exclude_none=True) == dump
    assert baseline.pack_pattern_ids is None
    assert baseline.pack_pattern_echoes is None


def test_hit_ids_recorded_sorted_deduped_with_echo() -> None:
    cluster = _cluster(fingerprint="widget-jam detected", sample="rotor stalled: widget-jam")
    decl = _decl(
        {"wj": r"widget-jam", "rotor": r"rotor stalled", "miss": r"never-here"},
        pack="toybox",
        sha="sha-abc",
    )
    f = build_failure_features(cluster, record=_record(), sidecar=None, failure_patterns=[decl])
    # sorted + deduped union of hit ids (the miss id is absent).
    assert f.pack_pattern_ids == ["rotor", "wj"]
    assert len(f.pack_pattern_echoes) == 1
    echo = f.pack_pattern_echoes[0]
    assert echo.pack == "toybox"
    assert echo.version == "v1"
    assert echo.sha == "sha-abc"
    assert echo.pattern_ids == ["rotor", "wj"]


def test_miss_leaves_pack_fields_empty() -> None:
    cluster = _cluster(fingerprint="clean exit", sample="all good")
    decl = _decl({"wj": r"widget-jam"}, pack="toybox", sha="sha-abc")
    f = build_failure_features(cluster, record=_record(), sidecar=None, failure_patterns=[decl])
    assert f.pack_pattern_ids is None
    assert f.pack_pattern_echoes is None


def test_multiple_packs_united_with_per_pack_echoes() -> None:
    cluster = _cluster(fingerprint="widget-jam", sample="belt-slip on rail")
    a = _decl({"wj": r"widget-jam"}, pack="alpha", sha="sha-a")
    b = _decl({"belt": r"belt-slip", "rail": r"rail"}, pack="beta", sha="sha-b", version=None)
    f = build_failure_features(cluster, record=_record(), sidecar=None, failure_patterns=[a, b])
    # United, sorted, deduped across both packs.
    assert f.pack_pattern_ids == ["belt", "rail", "wj"]
    # One echo per pack that hit, each carrying only its OWN hit ids + its echo.
    by_pack = {e.pack: e for e in f.pack_pattern_echoes}
    assert set(by_pack) == {"alpha", "beta"}
    assert by_pack["alpha"].pattern_ids == ["wj"]
    assert by_pack["alpha"].sha == "sha-a"
    assert by_pack["beta"].pattern_ids == ["belt", "rail"]
    assert by_pack["beta"].version is None


def test_pack_hits_never_touch_the_category_legs() -> None:
    # MATCH-AND-RECORD ONLY: a hit is evidence, never a category/action/retry.
    # The error_class / temporal / attempts legs are identical with and without a hit.
    cluster = _cluster(error_class="gpu_oom", fingerprint="widget-jam", sample="widget-jam")
    rec = _record(retries={"0": {"attempts": 2, "category": "gpu_oom", "overrides": {}}})
    without = build_failure_features(cluster, record=rec, sidecar={"resources": {"gpus": 2}})
    decl = _decl({"wj": r"widget-jam"}, pack="toybox", sha="sha-abc")
    with_hit = build_failure_features(
        cluster, record=rec, sidecar={"resources": {"gpus": 2}}, failure_patterns=[decl]
    )
    assert with_hit.pack_pattern_ids == ["wj"]  # the hit landed…
    # …but every non-pack leg is byte-identical.
    keys = {
        "error_class",
        "error_class_raw",
        "resource_spec",
        "temporal_context",
        "attempts_this_episode",
    }
    assert without.model_dump(include=keys) == with_hit.model_dump(include=keys)


def test_non_compiling_regex_is_skipped_not_raised() -> None:
    # Belt-and-braces: shape validation already refuses a bad regex at load; if one
    # slips through, matching skips it rather than crashing the recover flow.
    cluster = _cluster(fingerprint="widget-jam", sample="widget-jam")
    decl = _decl({"bad": r"([unterminated", "good": r"widget-jam"}, pack="toybox", sha="sha-abc")
    f = build_failure_features(cluster, record=_record(), sidecar=None, failure_patterns=[decl])
    assert f.pack_pattern_ids == ["good"]
