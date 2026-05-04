#!/bin/bash
set -e

# ==============================================================
# SLURM CPU Array Job Template (claude-hpc)
#
# Environment variables (injected by claude-hpc from clusters.yaml
# and project.yaml before submission):
#
#   $CONDA_SOURCE  — path to conda.sh (e.g. /apps/conda/miniforge3/.../conda.sh)
#   $CONDA_ENV     — conda environment name
#   $MODULES       — space-separated modules to load
#   $EXECUTOR      — python command to run (e.g. "python3 -m myproject.cli.run")
#   $RESULT_DIR    — output directory for results
#   $REPO_DIR      — repository root to cd into
#   $EXTRA_ARGS    — additional arguments passed through to $EXECUTOR
#   $HPC_RUNTIME   — optional, "uv" runs ``uv sync`` in $REPO_DIR before
#                    dispatch (honors MARs's #1 invariant: never bare pip)
#
# Submit with:
#   sbatch --array=1-100 --export=TASK_OFFSET=0,CONDA_SOURCE=...,CONDA_ENV=...,... cpu_array.slurm
# ==============================================================

# --- SLURM directives ---
#SBATCH --job-name=cpu_array
#SBATCH --output=logs/%x_%A_%a.out
#SBATCH --error=logs/%x_%A_%a.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=6:00:00

# --- Defaults ---
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
echo "CPUs:         $SLURM_CPUS_PER_TASK"
echo "Task:         $TASK_ID (offset=${TASK_OFFSET:-0})"
echo "Run ID:       ${HPC_RUN_ID:-<unset>}"
echo "============================================"

# --- Shared preamble (modules + conda + PYTHONPATH + uv sync) ---
# See claude_hpc/mapreduce/templates/common/hpc_preamble.sh — deployed alongside
# this template at .hpc/templates/common/hpc_preamble.sh by deploy_runtime.
source "$(dirname "$0")/common/hpc_preamble.sh"

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
# claude_hpc.mapreduce.reduce.history.prior() for the campaign's history.
export TASK_ID HPC_TASK_ID HPC_RUN_ID HPC_CAMPAIGN_ID RESULT_DIR
time $EXECUTOR ${EXTRA_ARGS:-}

echo "Job finished."
