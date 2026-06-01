"""The declarative behavioral-eval corpus: NL request → expected submit spec.

Each :class:`EvalCase` pairs a natural-language request (in two registers,
the lara pattern) with the fixture repo it runs against and the
**structural** expectation for the resolved ``submit`` spec — cluster,
grid cardinality, per-axis values, wave plan, resources. The expectation is
graded by :mod:`tests.eval.recursive_compare`: exact where it must be
(``cluster``, ``grid_points``), tolerant where it should be (resources,
walltime band).

Two registers, one ground truth
--------------------------------
``request_eval`` is the precise, "evaluation-style" phrasing; ``request_user``
is the casual, "how a researcher actually types it" phrasing. Both must
resolve to the SAME spec — that is the regression signal: a prompt edit that
makes the agent over-fit the precise phrasing and fumble the casual one shows
up as a divergence between the two. (The offline tier resolves deterministically
from the parsed axes and so exercises one path; the LLM tier is where the two
registers genuinely diverge — it runs both and grades each.)

The offline / deterministic split
----------------------------------
Resolving a free-text request into ``(executor_id, axis_values)`` tuples is
the LLM's job (the ``/submit-hpc`` Step 2 intent parse). To keep the DEFAULT
tier offline + free, each case ALSO records ``parsed_axes`` — the tuples an
upstream parser would emit — so the offline resolver can drive the genuinely
deterministic remainder of the decision (grid expansion + cluster lookup +
``plan-throughput`` wave planning + resource defaulting) without an API call.
The LLM tier ignores ``parsed_axes`` and lets the model parse the request
itself, then grades the resulting envelope against the same ``expect``.

Adding a case: append an :class:`EvalCase` to :data:`CASES`. Point
``fixture_repo`` at a directory under ``tests/eval/fixtures/`` that carries a
``clusters.yaml`` (+ executors/data as needed). Run
``HPC_EVAL_REGEN=1 pytest -q tests/eval`` to snapshot its gold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.eval.recursive_compare import Range, Tol

# Root of the version-controlled fixture repos the cases point at.
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

# Where ``--regen`` writes the snapshot of each case's resolved spec.
GOLD_DIR = Path(__file__).resolve().parent.parent / "gold"


@dataclass(frozen=True)
class EvalCase:
    """One behavioral eval: a request + fixture repo + structural expectation.

    Attributes
    ----------
    id:
        Stable slug — names the gold file (``gold/<id>.yaml``) and the
        parametrized test. Never reuse an id for a different case.
    request_eval / request_user:
        The same intent in two registers (precise vs casual). See the
        module docstring for why both exist.
    fixture_repo:
        Directory under :data:`FIXTURES_DIR` holding ``clusters.yaml`` and
        (optionally) executors + data. The resolver points
        ``HPC_CLUSTERS_CONFIG`` at ``<fixture_repo>/clusters.yaml``.
    cluster:
        The cluster the request targets (named in the request; the resolver
        confirms it exists in the fixture's ``clusters.yaml``).
    parsed_axes:
        ``{axis_name: [values...]}`` an upstream intent parser would emit
        from the request — ``executor`` is the special axis listing the
        executor ids. The Cartesian product of these is the task grid. Used
        only by the OFFLINE resolver (the LLM tier re-parses the request).
    expect:
        The gold structural spec graded with ``recursive_compare``: at least
        ``cluster``, ``grid_points``, ``axes``; optionally ``resources`` and
        ``wave_plan``. Subset-matched — extra fields on the candidate are OK.
    tolerant:
        Per-leaf-key tolerance for ``expect`` (see ``recursive_compare``):
        ``walltime_sec`` / ``mem_mb`` go here; ``cluster`` / ``grid_points``
        never do.
    est_task_duration_s:
        Optional per-task wall-seconds estimate fed to ``plan-throughput``
        so its walltime-feasibility + total-time estimate engage.
    """

    id: str
    request_eval: str
    request_user: str
    fixture_repo: str
    cluster: str
    parsed_axes: dict[str, list[Any]]
    expect: dict[str, Any]
    tolerant: dict[str, Tol | Range] = field(default_factory=dict)
    est_task_duration_s: int | None = None

    @property
    def fixture_path(self) -> Path:
        return FIXTURES_DIR / self.fixture_repo

    @property
    def clusters_yaml(self) -> Path:
        return self.fixture_path / "clusters.yaml"

    @property
    def gold_path(self) -> Path:
        return GOLD_DIR / f"{self.id}.yaml"


# ── the corpus ────────────────────────────────────────────────────────────
#
# Six cases across submit + campaign, deliberately spanning the decisions that
# matter: cluster selection (SGE vs SLURM), single- vs multi-axis grids,
# small grids that fit one wave vs large grids that fan out into multiple
# concurrency-bounded waves, GPU vs CPU resource defaulting. A handful of
# high-signal cases beats a large flaky suite (issue #204 scope).

CASES: list[EvalCase] = [
    # 1. The canonical issue-#204 example: two executors × a 3-value horizon
    #    on the SGE cluster. Grid = 6, one wave (max_concurrent=2 absorbs it).
    EvalCase(
        id="forecasting_ridge_xgb_horizon",
        request_eval=(
            "Run executors ml_ridge and ml_xgboost over horizon in {1, 5, 25} "
            "on the hoffman2 cluster."
        ),
        request_user="run ridge and xgboost with horizon=[1,5,25] on hoffman2",
        fixture_repo="forecasting_repo",
        cluster="hoffman2",
        parsed_axes={"executor": ["ml_ridge", "ml_xgboost"], "horizon": [1, 5, 25]},
        expect={
            "cluster": "hoffman2",
            "backend": "sge",
            "grid_points": 6,
            "axes": {"executor": ["ml_ridge", "ml_xgboost"], "horizon": [1, 5, 25]},
            "resources": {"cpus": 1, "mem_mb": 16384},
            "wave_plan": {"total_batches": 1, "n_waves": 1, "max_concurrent": 2},
        },
        # mem_mb is a resolved default (16G) — allow the band, don't pin bytes.
        tolerant={"mem_mb": Tol(rel=0.10)},
    ),
    # 2. Single executor, single axis, small grid — the simplest submit. Same
    #    SGE cluster; proves a degenerate 1-axis grid still resolves cleanly.
    EvalCase(
        id="forecasting_ridge_alpha_sweep",
        request_eval="Sweep ml_ridge over alpha in {0.1, 1.0, 10.0} on hoffman2.",
        request_user="try ridge with alpha 0.1, 1, and 10 on hoffman2",
        fixture_repo="forecasting_repo",
        cluster="hoffman2",
        parsed_axes={"executor": ["ml_ridge"], "alpha": [0.1, 1.0, 10.0]},
        expect={
            "cluster": "hoffman2",
            "backend": "sge",
            "grid_points": 3,
            "axes": {"executor": ["ml_ridge"], "alpha": [0.1, 1.0, 10.0]},
            "resources": {"cpus": 1, "mem_mb": 16384},
            "wave_plan": {"total_batches": 1, "n_waves": 1},
        },
        tolerant={"mem_mb": Tol(rel=0.10)},
    ),
    # 3. The SLURM cluster, to prove backend selection follows the cluster's
    #    scheduler (discovery is slurm, hoffman2 is sge). Two executors only.
    EvalCase(
        id="forecasting_two_models_on_slurm",
        request_eval="Run ml_ridge and ml_xgboost on the discovery cluster.",
        request_user="run both ridge and xgboost on discovery",
        fixture_repo="forecasting_repo",
        cluster="discovery",
        parsed_axes={"executor": ["ml_ridge", "ml_xgboost"]},
        expect={
            "cluster": "discovery",
            "backend": "slurm",
            "grid_points": 2,
            "axes": {"executor": ["ml_ridge", "ml_xgboost"]},
            "resources": {"cpus": 1, "mem_mb": 16384},
            # discovery allows 4 concurrent — still one wave for 2 tasks.
            "wave_plan": {"total_batches": 1, "n_waves": 1, "max_concurrent": 4},
        },
        tolerant={"mem_mb": Tol(rel=0.10)},
    ),
    # 4. A LARGE grid that must fan out into multiple concurrency-bounded
    #    waves on SGE (max_array_size=100, max_concurrent=2). This is the
    #    decision that silently regresses if planning breaks: 300 tasks must
    #    NOT land as a single 300-wide array on a cluster capped at 100.
    EvalCase(
        id="forecasting_large_grid_waves",
        request_eval=("Run ml_xgboost over seed in 0..99 and horizon in {1, 5, 25} on hoffman2."),
        request_user="xgboost, 100 seeds, horizons 1/5/25, hoffman2",
        fixture_repo="forecasting_repo",
        cluster="hoffman2",
        parsed_axes={
            "executor": ["ml_xgboost"],
            "seed": list(range(100)),
            "horizon": [1, 5, 25],
        },
        expect={
            "cluster": "hoffman2",
            "backend": "sge",
            "grid_points": 300,
            # The grid must be packed across multiple arrays/waves — not one
            # oversized array. Exact batch/wave counts are pinned (a wrong
            # packing is a wrong decision); see plan-throughput for the math.
            "wave_plan": {"total_batches": 3, "n_waves": 2, "max_concurrent": 2},
        },
    ),
    # 5. A GPU request: the DL cluster + a torch executor. Proves resource
    #    defaulting flips to the GPU profile (gpus≥1, larger walltime band)
    #    rather than the CPU/ML default. The executor's imports drive this.
    EvalCase(
        id="vision_gpu_resnet_lr_sweep",
        request_eval="Train dl_resnet over lr in {1e-3, 1e-2} on the discovery GPU cluster.",
        request_user="train the resnet at lr 0.001 and 0.01 on discovery gpus",
        fixture_repo="vision_repo",
        cluster="discovery",
        parsed_axes={"executor": ["dl_resnet"], "lr": [0.001, 0.01]},
        est_task_duration_s=3600,
        expect={
            "cluster": "discovery",
            "backend": "slurm",
            "grid_points": 2,
            "axes": {"executor": ["dl_resnet"], "lr": [0.001, 0.01]},
            # GPU/DL default profile (the documented /submit-hpc Step 4 ask:
            # 4 cpus × 16G × 6h × 2 gpus). gpus/cpus are exact — flipping to
            # the CPU profile (cpus=1, no gpus) would be a wrong decision.
            # walltime is a band (the planner trims it); mem is tolerant.
            "resources": {"gpus": 2, "cpus": 4},
            "wave_plan": {"n_waves": 1},
        },
        tolerant={
            "mem_mb": Tol(rel=0.20),
            "walltime_sec": Range(3600, 6 * 3600),
        },
    ),
    # 6. A CAMPAIGN (not a one-shot submit): an Optuna-style tuning loop. The
    #    decision graded here is the campaign SHAPE — strategy-driven path, a
    #    campaign_id tag, a bounded per-iteration grid — not absolute results.
    EvalCase(
        id="campaign_optuna_ridge_tuning",
        request_eval=(
            "Run an Optuna hyperparameter-tuning campaign for ml_ridge on hoffman2, "
            "8 trials per iteration, campaign id ridge-tune."
        ),
        request_user="tune ridge with optuna on hoffman2, 8 trials at a time, call it ridge-tune",
        fixture_repo="forecasting_repo",
        cluster="hoffman2",
        parsed_axes={"executor": ["ml_ridge"], "trial": list(range(8))},
        expect={
            "cluster": "hoffman2",
            "backend": "sge",
            "workflow": "campaign",
            "campaign_id": "ridge-tune",
            "grid_points": 8,
            "wave_plan": {"total_batches": 1, "n_waves": 1, "max_concurrent": 2},
        },
    ),
]


def case_by_id(case_id: str) -> EvalCase:
    """Look up a case by its id (used by ``regen.py`` for a single case)."""
    for case in CASES:
        if case.id == case_id:
            return case
    raise KeyError(f"no eval case with id {case_id!r}; known: {[c.id for c in CASES]}")
