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
#
# Submit with:
#   sbatch --array=1-100 --export=CONDA_SOURCE=...,CONDA_ENV=...,... cpu_array.slurm
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

# Convert 1-based SLURM_ARRAY_TASK_ID to 0-based task ID
TASK_ID=$((SLURM_ARRAY_TASK_ID - 1))

# --- Diagnostics ---
echo "============================================"
echo "Job ID:       $SLURM_JOB_ID"
echo "Array Task:   $SLURM_ARRAY_TASK_ID"
echo "Hostname:     $(hostname)"
echo "CPUs:         $SLURM_CPUS_PER_TASK"
echo "Task:         $TASK_ID"
echo "============================================"

# --- Module Setup ---
module purge

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

# Bind CPU threads to allocated cores
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
export MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK

# --- Prepare Output ---
mkdir -p "$RESULT_DIR"

echo "Result dir:   $RESULT_DIR"
echo "Executor:     $EXECUTOR"
echo "============================================"

# --- Execute ---
export TASK_ID RESULT_DIR
time $EXECUTOR ${EXTRA_ARGS:-}

echo "Job finished."
