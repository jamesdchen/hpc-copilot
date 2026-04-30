#!/bin/bash
set -e

# ==============================================================
# SGE GPU Array Job Template (claude-hpc)
#
# Environment variables (injected by claude-hpc from clusters.yaml
# and project.yaml before submission):
#
#   $CONDA_SOURCE  — path to conda.sh (e.g. /u/local/apps/anaconda3/.../conda.sh)
#   $CONDA_ENV     — conda environment name
#   $MODULES       — space-separated modules to load (e.g. "conda cuda/12.3")
#   $EXECUTOR      — python command to run (e.g. "python3 -m myproject.cli.gpu_run")
#   $RESULT_DIR    — output directory for results
#   $REPO_DIR      — repository root to cd into
#   $GPU_COUNT     — number of GPUs per task (default: 2)
#   $EXTRA_ARGS    — additional arguments passed through to $EXECUTOR
#   $HPC_RUNTIME   — optional, "uv" runs ``uv sync`` in $REPO_DIR before
#                    dispatch (honors MARs's #1 invariant: never bare pip)
#
# Submit with:
#   qsub -t 1-10 -l gpu,A100,cuda=2 -v CONDA_SOURCE=...,CONDA_ENV=...,... gpu_array.sh
#
# Supported GPUs (Hoffman2): H200, H100, A100, A6000, V100, RTX2080Ti
# ==============================================================

# --- SGE directives ---
#$ -cwd
#$ -j y
#$ -l gpu,A100,cuda=2
#$ -l h_data=16G,h_rt=21600
#$ -pe shared 8

# --- Defaults ---
GPU_COUNT="${GPU_COUNT:-2}"
RESULT_DIR="${RESULT_DIR:-.}"
REPO_DIR="${REPO_DIR:-.}"

# Convert 1-based SGE_TASK_ID to 0-based, add offset for batched submission
TASK_ID=$((SGE_TASK_ID - 1 + ${TASK_OFFSET:-0}))
HPC_TASK_ID=$TASK_ID  # canonical name used by .hpc/_hpc_dispatch.py

# --- Diagnostics ---
echo "============================================"
echo "Job ID:       $JOB_ID"
echo "Array Task:   $SGE_TASK_ID"
echo "Hostname:     $(hostname)"
echo "GPUs:         $GPU_COUNT"
echo "Task:         $TASK_ID (offset=${TASK_OFFSET:-0})"
echo "Run ID:       ${HPC_RUN_ID:-<unset>}"
echo "============================================"

# --- Module Setup ---
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
export PYTHONPATH="$REPO_DIR:${PYTHONPATH:-}"

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

# --- GPU Setup ---
# CUDA_VISIBLE_DEVICES is typically set by SGE, but enforce if needed
if [ -z "$CUDA_VISIBLE_DEVICES" ]; then
    echo "WARNING: CUDA_VISIBLE_DEVICES not set by scheduler"
fi
echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"

# Bind CPU threads to allocated cores
export OMP_NUM_THREADS=${NSLOTS:-8}
export MKL_NUM_THREADS=${NSLOTS:-8}

# CUDA memory optimization
export PYTORCH_CUDA_ALLOC_CONF="max_split_size_mb:128"

# --- Prepare Output ---
mkdir -p "$RESULT_DIR"

echo "Result dir:   $RESULT_DIR"
echo "Executor:     $EXECUTOR"
echo "============================================"

# --- Execute ---
# HPC_RUN_ID arrives via qsub -v from the submit-side env; re-exported here
# so the dispatcher inside $EXECUTOR sees it.
export TASK_ID HPC_TASK_ID HPC_RUN_ID RESULT_DIR GPU_COUNT
time $EXECUTOR ${EXTRA_ARGS:-}

echo "Job finished."
