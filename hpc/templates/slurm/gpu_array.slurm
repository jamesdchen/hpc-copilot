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
#   $TOTAL_CHUNKS  — total number of array tasks (auto-detected from SLURM if unset)
#   $REPO_DIR      — repository root to cd into
#   $GPU_COUNT     — number of GPUs per task (default: 2)
#   $EXTRA_ARGS    — additional arguments passed through to $EXECUTOR
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
if [ -n "$SLURM_ARRAY_TASK_COUNT" ]; then
    TOTAL_CHUNKS="$SLURM_ARRAY_TASK_COUNT"
else
    TOTAL_CHUNKS="${TOTAL_CHUNKS:-1}"
fi
GPU_COUNT="${GPU_COUNT:-2}"
RESULT_DIR="${RESULT_DIR:-.}"
REPO_DIR="${REPO_DIR:-.}"

# Convert 1-based SLURM_ARRAY_TASK_ID to 0-based chunk ID
CHUNK_ID=$((SLURM_ARRAY_TASK_ID - 1))

# --- Diagnostics ---
echo "============================================"
echo "Job ID:       $SLURM_JOB_ID"
echo "Array Task:   $SLURM_ARRAY_TASK_ID"
echo "Hostname:     $(hostname)"
echo "GPUs:         $GPU_COUNT"
echo "CPUs:         $SLURM_CPUS_PER_TASK"
echo "Chunk:        $CHUNK_ID / $TOTAL_CHUNKS"
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

# --- GPU Setup ---
# CUDA_VISIBLE_DEVICES is set by SLURM via --gres allocation
if [ -z "$CUDA_VISIBLE_DEVICES" ]; then
    echo "WARNING: CUDA_VISIBLE_DEVICES not set by scheduler"
fi
echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"

# Bind CPU threads to allocated cores
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
export MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK

# CUDA memory optimization
export PYTORCH_CUDA_ALLOC_CONF="max_split_size_mb:128"

# --- Prepare Output ---
mkdir -p "$RESULT_DIR"

echo "Result dir:   $RESULT_DIR"
echo "Executor:     $EXECUTOR"
echo "============================================"

# --- Execute ---
export CHUNK_ID TOTAL_CHUNKS RESULT_DIR GPU_COUNT
time $EXECUTOR ${EXTRA_ARGS:-}

echo "Job finished."
