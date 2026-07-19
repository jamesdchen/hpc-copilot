"""Unit tests for ``_run_scoped_results_subdir`` (run-#12 finding 19 leg B).

The per-task pulls must scope to the run's OWN results subtree — the static
prefix of its ``result_dir_template`` — because the scp fallback cannot
include-filter and pulling the whole ``results/`` root drags every prior
run's outputs through the transfer (the live 1800s timeout).

Run-15 finding (pull root-scoping): the prefix cut ran on the RAW template,
so the framework-default ``results/{run_id}/task_{task_id}`` collapsed to the
SHARED ``results`` root even though run_id is fully known at aggregate time —
the puller enumerated every run's dirs under the shared tree and the
cardinality gate refused the foreign rows the pull itself imported (the
mirror could never converge). The fix renders the aggregate-time-known
placeholders (``run_id``) BEFORE the static-prefix cut, scoping that
template to ``results/<run_id>``; the pins below cover the default template,
a shared-tree foreign-rows fixture (scope level AND fallback enumeration
level), the non-run-scoped degrade path, and the canary sibling.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from hpc_agent.ops import aggregate_flow as agg
from hpc_agent.ops.aggregate_flow import _run_scoped_results_subdir
from hpc_agent.ops.monitor.reconcile import sibling_run_ids

if TYPE_CHECKING:
    import pytest

_EXP = Path("unused")  # the record carries the template; no sidecar read needed

_DEFAULT_TEMPLATE = "results/{run_id}/task_{task_id}"  # the framework default


def _rec(template: str | None) -> SimpleNamespace:
    return SimpleNamespace(result_dir_template=template)


def test_sweep_template_scopes_to_static_prefix() -> None:
    scoped = _run_scoped_results_subdir(
        _EXP,
        "run-1",
        _rec("results/causal_tune_linear/{estimator}/{exog_bucket}/chunk_{chunk_start}"),
        "results",
    )
    assert scoped == "results/causal_tune_linear"


def test_placeholder_mid_component_trims_to_last_full_dir() -> None:
    scoped = _run_scoped_results_subdir(_EXP, "run-1", _rec("results/causal_{x}/y"), "results")
    assert scoped == "results"


def test_fixed_template_scopes_to_itself() -> None:
    scoped = _run_scoped_results_subdir(_EXP, "run-1", _rec("results/fixed_dir/"), "results")
    assert scoped == "results/fixed_dir"


def test_absent_template_falls_back_to_root() -> None:
    assert _run_scoped_results_subdir(_EXP, "run-1", _rec(None), "results") == "results"


def test_template_escaping_the_root_falls_back() -> None:
    # A template outside the configured root must never widen/redirect the pull.
    scoped = _run_scoped_results_subdir(_EXP, "run-1", _rec("elsewhere/{task_id}"), "results")
    assert scoped == "results"


def test_template_with_no_directory_falls_back() -> None:
    scoped = _run_scoped_results_subdir(_EXP, "run-1", _rec("{task_id}"), "results")
    assert scoped == "results"


# --- Run-15 pull root-scoping pins -------------------------------------------


def test_default_run_scoped_template_scopes_to_run_dir() -> None:
    """(a) The framework default scopes to ``results/<run_id>``, NOT ``results``."""
    scoped = _run_scoped_results_subdir(_EXP, "drill-abc123", _rec(_DEFAULT_TEMPLATE), "results")
    assert scoped == "results/drill-abc123"


def test_non_run_scoped_template_degrades_to_static_root() -> None:
    """(c) No ``{run_id}`` in the template: the render is a no-op and the
    static-prefix cut degrades to the pre-fix root scope (the cardinality
    gate remains the contamination backstop) — never worse than today."""
    scoped = _run_scoped_results_subdir(
        _EXP, "drill-abc123", _rec("results/{estimator}/task_{task_id}"), "results"
    )
    assert scoped == "results"


def test_run_id_mid_component_renders() -> None:
    """A mid-component ``{run_id}`` renders too — the run's own dir, not the root."""
    scoped = _run_scoped_results_subdir(
        _EXP, "drill-abc123", _rec("results/causal_{run_id}/{task_id}"), "results"
    )
    assert scoped == "results/causal_drill-abc123"


def test_unknown_placeholder_with_format_spec_survives_the_render() -> None:
    """The partial render re-emits unknown fields verbatim (spec included), so
    the prefix cut still trims the per-task component exactly as before."""
    scoped = _run_scoped_results_subdir(
        _EXP, "drill-abc123", _rec("results/{run_id}/task_{task_id:03d}"), "results"
    )
    assert scoped == "results/drill-abc123"


def test_shared_tree_foreign_runs_never_under_scope() -> None:
    """(b, scope level) Foreign runs' dirs under the shared tree are outside
    the computed scope — the puller cannot enumerate them."""
    scoped = _run_scoped_results_subdir(_EXP, "drill-abc123", _rec(_DEFAULT_TEMPLATE), "results")
    for foreign in ("drill-foreign", "drill-zzz999", "20260701-000000-apex"):
        foreign_dir = f"results/{foreign}"
        assert foreign_dir != scoped
        assert not foreign_dir.startswith(scoped + "/")


def test_canary_sibling_is_not_under_scope() -> None:
    """(d) The ``<run_id>-canary`` FAMILY is excluded structurally — directory-root
    scoping, no prefix-glob over-match (``results/<id>-canary`` shares the string
    prefix ``results/<id>`` but is not UNDER the scoped dir)."""
    scoped = _run_scoped_results_subdir(_EXP, "drill-abc123", _rec(_DEFAULT_TEMPLATE), "results")
    canaries = sibling_run_ids("drill-abc123")
    assert canaries  # the pin means nothing if the family definition regresses to []
    for canary in canaries:
        canary_dir = f"results/{canary}"
        assert canary_dir != scoped
        assert not canary_dir.startswith(scoped + "/")


def test_malformed_template_degrades_to_raw_prefix_cut() -> None:
    """An unparseable template (unbalanced brace) skips the render entirely —
    scoping is an optimization, never a gate: the raw static-prefix cut applies."""
    scoped = _run_scoped_results_subdir(_EXP, "drill-abc123", _rec("results/{run_id"), "results")
    assert scoped == "results"


def test_per_task_fallback_never_enumerates_foreign_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b, enumeration level) The fallback pull asks the transport for the run's
    OWN subtree — the foreign rows sharing the results root are never mirrored.

    The fake transport models a shared remote ``results/`` tree interleaving
    the run's tasks, its canary sibling, and a foreign run; it mirrors exactly
    the requested ``remote_subdir``. Pre-fix the request was the shared ROOT
    (``results``) and the cardinality gate refused the imported foreign rows;
    post-fix it is ``results/<run_id>`` and the reduce converges on the run's
    own rows alone.
    """
    run_id = "drill-abc123"
    remote_tree = {
        f"results/{run_id}/task_0/metrics.json": {"metric": 1.0, "n_samples": 1},
        f"results/{run_id}/task_1/metrics.json": {"metric": 3.0, "n_samples": 1},
        f"results/{run_id}-canary/task_0/metrics.json": {"metric": 99.0, "n_samples": 1},
        "results/drill-foreign/task_0/metrics.json": {"metric": 50.0, "n_samples": 1},
        "results/drill-foreign/task_1/metrics.json": {"metric": 60.0, "n_samples": 1},
    }
    asked: list[str] = []

    def _fake_pull(
        *, remote_subdir: str, local_dir: str, include: list[str] | None = None, **_kw: Any
    ) -> SimpleNamespace:
        asked.append(remote_subdir)
        prefix = remote_subdir.rstrip("/") + "/"
        for name, payload in remote_tree.items():
            if not name.startswith(prefix):
                continue
            if include and PurePosixPath(name).name not in include:
                continue
            dest = Path(local_dir) / PurePosixPath(name).relative_to(prefix)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(agg, "rsync_pull", _fake_pull)
    record = SimpleNamespace(
        ssh_target="u@h",
        remote_path="/remote",
        total_tasks=2,
        result_dir_template=_DEFAULT_TEMPLATE,
    )

    result = agg._per_task_metrics_reduce(
        tmp_path,
        run_id,
        record=record,
        out=tmp_path / "out",
        results_subdir="results",
        summary_name="metrics.json",
    )

    # Every pull asked for the run's OWN subtree — never the shared root.
    assert asked
    assert all(a == f"results/{run_id}" for a in asked)
    # And the reduce converged on the run's two rows (weighted mean of 1 and 3);
    # pre-fix this REFUSED on the cardinality gate with 5 dirs > 2 tasks.
    assert result == {run_id: {"metric": 2.0, "n_samples": 2}}
