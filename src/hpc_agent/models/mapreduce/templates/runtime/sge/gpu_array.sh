#!/bin/bash
# Fail loudly: -e exits on error, -o pipefail propagates failures
# through `time $EXECUTOR | ...`. -u is intentionally NOT set because
# the sourced preambles use the `if [ -n "$VAR" ]` pattern on
# optionally-set vars. Explicit guards on the critical scheduler vars
# below catch the "task -1" dispatch failure mode.
set -eo pipefail
: "${SGE_TASK_ID:?SGE_TASK_ID is not set; refusing to dispatch task -1}"

# ==============================================================
# SGE GPU Array Job Template (hpc-agent)
#
# Environment variables (injected by hpc-agent from clusters.yaml
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
#                    dispatch (honors the "no bare pip" invariant common to uv-first integrators)
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

# --- Shared preamble (modules + conda + PYTHONPATH + uv sync) ---
source "$REPO_DIR/.hpc/templates/common/hpc_preamble.sh"

# --- Shared GPU preamble (CUDA_VISIBLE_DEVICES warn + PYTORCH_CUDA_ALLOC_CONF) ---
source "$REPO_DIR/.hpc/templates/common/gpu_preamble.sh"

# Bind CPU threads to allocated cores ($NSLOTS — SGE-specific).
# Honors the campus user's HPC_OMP_NUM_THREADS / HPC_MKL_NUM_THREADS env
# override before falling back to the scheduler-allocated core count;
# without this precedence, a user's HPC_OMP_NUM_THREADS=4 would be
# silently overridden by NSLOTS on multi-threaded array jobs and the
# run would oversubscribe its cgroup until OOM-killed.
export OMP_NUM_THREADS="${HPC_OMP_NUM_THREADS:-${NSLOTS:-8}}"
export MKL_NUM_THREADS="${HPC_MKL_NUM_THREADS:-${NSLOTS:-8}}"

# --- Prepare Output ---
mkdir -p "$RESULT_DIR"

echo "Result dir:   $RESULT_DIR"
echo "Executor:     $EXECUTOR"
echo "============================================"

# --- Execute ---
# HPC_RUN_ID arrives via qsub -v from the submit-side env; re-exported here
# so the dispatcher inside $EXECUTOR sees it. HPC_CAMPAIGN_ID is optional —
# present when the run is part of a closed-loop campaign — and lets the
# user's tasks.py call hpc_agent.models.mapreduce.reduce.history.prior() to learn what
# prior iterations of the same campaign produced.
export TASK_ID HPC_TASK_ID HPC_RUN_ID HPC_CAMPAIGN_ID RESULT_DIR GPU_COUNT HPC_GPU_TYPE
time $EXECUTOR ${EXTRA_ARGS:-}

echo "Job finished."
