#!/bin/bash
# ==============================================================
# claude-hpc shared preamble
#
# Sourced by sge/cpu_array.sh, sge/gpu_array.sh, slurm/cpu_array.slurm,
# and slurm/gpu_array.slurm before they begin executing the user's
# command. Owns the three steps that every template needs identically:
#
#   1. Module setup (Hoffman2 UGE init + per-cluster $MODULES list)
#   2. Conda activation ($CONDA_SOURCE + $CONDA_ENV)
#   3. cd $REPO_DIR + PYTHONPATH export
#   4. Optional `uv sync` when HPC_RUNTIME=uv
#
# Reads from the surrounding job's environment:
#   $MODULES        space-separated module list
#   $CONDA_SOURCE   path to conda.sh (optional)
#   $CONDA_ENV      conda env to activate (optional)
#   $REPO_DIR       repo root to cd into (defaulted before sourcing)
#   $HPC_RUNTIME    "uv" to enable uv sync; anything else is no-op
#
# This file is scp'd to the cluster as .hpc/templates/common/hpc_preamble.sh
# alongside the per-scheduler templates by deploy_runtime().
# ==============================================================

# --- Module Setup ---
# Hoffman2 needs the UGE path; other clusters may not.
if [ -f /u/local/Modules/default/init/modules.sh ]; then
    source /u/local/Modules/default/init/modules.sh
fi

if [ -n "$MODULES" ]; then
    for mod in $MODULES; do
        module load "$mod"
    done
fi

# --- Conda ---
if [ -n "$CONDA_SOURCE" ]; then
    source "$CONDA_SOURCE"
fi
if [ -n "$CONDA_ENV" ]; then
    conda activate "$CONDA_ENV"
fi

# --- Working Directory ---
cd "$REPO_DIR"
# .hpc/ on PYTHONPATH so `python -m cli` resolves the dispatcher
# generated at .hpc/cli.py by /submit-hpc Step 6.
export PYTHONPATH="$REPO_DIR:$REPO_DIR/.hpc:${PYTHONPATH:-}"

# --- Runtime (uv) ---
# Opt-in via HPC_RUNTIME=uv. Sync the project's uv-managed venv before
# any task runs so the dispatcher's ``uv run python`` resolves to the
# right interpreter. Fail fast if uv is missing — this is much clearer
# than running tasks with the wrong Python.
if [ "${HPC_RUNTIME:-}" = "uv" ]; then
    if ! command -v uv >/dev/null 2>&1; then
        echo "[template] HPC_RUNTIME=uv but 'uv' not on PATH" >&2
        exit 2
    fi
    uv sync || { echo "[template] uv sync failed" >&2; exit 2; }
fi
