#!/bin/bash
set -e

# ==============================================================
# SLURM GPU Array Job Template (claude-hpc)
#
# Environment variables (injected by claude-hpc from clusters.yaml
# and project.yaml before submission):
#
#   $CONDA_SOURCE  — path to conda.sh (e.g. /apps/conda/miniforge3/.../conda.sh)
#   $CONDA_ENV     — conda environment name
#   $MODULES       — space-separated modules to load (e.g. "cuda/12.3")
#   $EXECUTOR      — python command to run (e.g. "python3 -m myproject.cli.gpu_run")
#   $RESULT_DIR    — output directory for results
#   $REPO_DIR      — repository root to cd into
#   $GPU_COUNT     — number of GPUs per task (default: 2)
#   $EXTRA_ARGS    — additional arguments passed through to $EXECUTOR
#   $HPC_RUNTIME   — optional, "uv" runs ``uv sync`` in $REPO_DIR before
#                    dispatch (honors MARs's #1 invariant: never bare pip)
#
# Submit with:
#   sbatch --array=1-10 --export=CONDA_SOURCE=...,CONDA_ENV=...,... gpu_array.slurm
#   sbatch --array=1-10 --gres=gpu:4 --export=GPU_COUNT=4,... gpu_array.slurm
# ==============================================================

# --- SLURM directives ---
#SBATCH --job-name=gpu_array
#SBATCH --output=logs/%x_%A_%a.out
#SBATCH --error=logs/%x_%A_%a.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --gres=gpu:2
#SBATCH --partition=gpu
#SBATCH --time=6:00:00

# --- Defaults ---
GPU_COUNT="${GPU_COUNT:-2}"
RESULT_DIR="${RESULT_DIR:-.}"
REPO_DIR="${REPO_DIR:-.}"

# Convert 1-based SLURM_ARRAY_TASK_ID to 0-based, add offset for batched submission
TASK_ID=$((SLURM_ARRAY_TASK_ID - 1 + ${TASK_OFFSET:-0}))
HPC_TASK_ID=$TASK_ID  # canonical name used by .hpc/_hpc_dispatch.py

# --- Diagnostics ---
echo "============================================"
echo "Job ID:       $SLURM_JOB_ID"
echo "Array Task:   $SLURM_ARRAY_TASK_ID"
echo "Hostname:     $(hostname)"
echo "GPUs:         $GPU_COUNT"
echo "CPUs:         $SLURM_CPUS_PER_TASK"
echo "Task:         $TASK_ID (offset=${TASK_OFFSET:-0})"
echo "Run ID:       ${HPC_RUN_ID:-<unset>}"
echo "============================================"

# --- Shared preamble (modules + conda + PYTHONPATH + uv sync) ---
source "$(dirname "$0")/common/hpc_preamble.sh"

# --- Shared GPU preamble (CUDA_VISIBLE_DEVICES warn + PYTORCH_CUDA_ALLOC_CONF) ---
source "$(dirname "$0")/common/gpu_preamble.sh"

# Bind CPU threads to allocated cores ($SLURM_CPUS_PER_TASK — SLURM-specific).
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
export MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK

# --- Prepare Output ---
mkdir -p "$RESULT_DIR"

echo "Result dir:   $RESULT_DIR"
echo "Executor:     $EXECUTOR"
echo "============================================"

# --- Execute ---
# HPC_RUN_ID arrives via sbatch --export from the submit-side env;
# re-exported here so the dispatcher inside $EXECUTOR sees it.
# HPC_CAMPAIGN_ID is optional — present when the run is part of a
# closed-loop campaign — and lets the user's tasks.py call
# hpc_mapreduce.reduce.history.prior() for the campaign's history.
export TASK_ID HPC_TASK_ID HPC_RUN_ID HPC_CAMPAIGN_ID RESULT_DIR GPU_COUNT
time $EXECUTOR ${EXTRA_ARGS:-}

echo "Job finished."
