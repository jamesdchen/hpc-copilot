"""Wire model for the ``dry-run-local`` atom — the local pre-flight EXECUTION gate.

Every other pre-submit validator is STATIC: it introspects ``tasks.py`` /
the executor signature / the dataset / numeric limits, but none of them
EXECUTES the user's executor command even once before the submit pipeline
touches the cluster. The earliest a runtime error (bad import, mis-wired
``HPC_KW_*`` arg, a ``result_dir_template`` that references a key the
kwargs don't carry) surfaces today is the cluster-side canary —
*after* rsync + deploy + sbatch/qsub. ``dry-run-local`` closes that gap
locally (#205).

Two layers, deliberately split so the cheap one is default-on and the
expensive one is opt-in:

1. **Template-render check (DEFAULT-ON, near-free).** Re-uses the same
   ``resolve(i)`` sampler ``validate-executor-signatures`` / ``compute_cmd_sha``
   already walk: for the first ``sample_n_tasks`` ids it renders
   ``result_dir_template`` exactly as the cluster dispatcher's
   ``_format_result_dir`` will (``str.format`` over ``task_id`` + ``run_id``
   + kwargs) and flags (a) an unfilled ``{field}`` the kwargs don't supply
   and (b) two distinct ids that collapse to the SAME result dir (a
   silent overwrite bug — wave N clobbers wave M's metrics.json).

2. **Executor smoke-exec (OPT-IN, ``smoke=true``).** Actually runs the
   executor command for ONE sampled grid point locally, mirroring
   ``models/mapreduce/dispatch.py`` semantics (set ``HPC_KW_*`` / bare-upper
   env, run the command under a shell), to catch import errors and
   arg-binding bugs BEFORE any cluster cost. Local exec can't stand in
   for the cluster (modules, GPUs, scale), so the default command is an
   import / ``--help``-level probe — "catch broken code, not broken
   cluster"; this COMPLEMENTS the canary, never replaces it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire.workflows.validate_campaign import (
    ValidatorFinding,  # noqa: TC001 — Pydantic resolves the annotation at runtime
)


class DryRunLocalSpec(BaseModel):
    """Input spec for the local pre-flight execution gate.

    ``executor`` is the REAL per-task command (the same string the
    cluster dispatcher resolves from the run sidecar — e.g.
    ``python train.py --seed $SEED``), NOT the job-script's dispatcher
    command. It is only consulted when ``smoke`` is set; the default-on
    template-render layer needs ``result_dir_template`` + ``tasks.py``
    alone.
    """

    model_config = ConfigDict(extra="forbid")

    result_dir_template: str = Field(
        min_length=1,
        description=(
            "The per-run result-directory template the cluster dispatcher "
            "renders with str.format over {task_id}/{run_id}/<kwargs>. "
            "Checked for unfilled placeholder fields and cross-id collisions."
        ),
    )
    tasks_py_path: str = Field(
        default=".hpc/tasks.py",
        description="Path to the campaign's tasks.py (relative to experiment_dir).",
    )
    run_id: str = Field(
        default="dry-run-local",
        description=(
            "run_id fed into the template render + the smoke run's HPC_RUN_ID. "
            "A placeholder is fine — the gate never touches the journal; it "
            "only needs a value so a template referencing {run_id} resolves."
        ),
    )
    sample_n_tasks: int = Field(
        default=8,
        ge=1,
        description=(
            "Number of task ids to sample from tasks.resolve(i) for the "
            "template-render / collision check. Sampling (not an exhaustive "
            "walk) keeps the gate fast for large grids; collisions are still "
            "caught across the sampled window, which is where the bug class lives."
        ),
    )

    # --- Opt-in executor smoke-exec ---
    smoke: bool = Field(
        default=False,
        description=(
            "Opt in to actually executing the executor locally for ONE sampled "
            "grid point (default OFF). Runs `executor` (or `smoke_command` when "
            "given) under a shell with the same HPC_KW_*/bare-upper env the "
            "cluster dispatcher exports, to catch import errors + arg-binding "
            "bugs before any SSH. Scoped to 'broken code, not broken cluster'."
        ),
    )
    executor: str | None = Field(
        default=None,
        description=(
            "The real per-task command (e.g. `python train.py --seed $SEED`). "
            "Required when smoke=true. Must not be the dispatcher command "
            "itself (the #162 self-recursion footgun) — the gate refuses it."
        ),
    )
    smoke_command: str | None = Field(
        default=None,
        description=(
            "Override the smoke-exec command with an import/--help-level probe "
            "the executor opts into (e.g. `python -c 'import train'` or "
            "`python train.py --help`). When omitted, the gate runs `executor` "
            "verbatim — appropriate only for executors that no-op cheaply."
        ),
    )
    smoke_task_id: int = Field(
        default=0,
        ge=0,
        description="Which sampled task id supplies the kwargs/env for the smoke run.",
    )
    smoke_timeout_sec: int = Field(
        default=60,
        ge=1,
        description=(
            "Hard wall-clock cap on the local smoke run. A local probe that "
            "outlives this is killed and reported as smoke_timeout — the gate "
            "is meant to be fast (import/--help), not a real training run."
        ),
    )


class DryRunLocalResult(BaseModel):
    """Output — the standard ``findings`` envelope every validator shares.

    Empty ``findings`` == pass. Findings carry the failing ``task_id``
    (and, for the smoke layer, the captured ``stderr`` tail) in
    ``evidence`` so the /submit-hpc cascade can surface the raw error
    verbatim, exactly as it does for verify-canary.
    """

    model_config = ConfigDict(extra="forbid")

    findings: list[ValidatorFinding] = Field(default_factory=list)
