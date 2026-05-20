#!/bin/bash
# Fail loudly: -e exits on error, -o pipefail propagates failures
# through `time $EXECUTOR | ...`. -u is intentionally NOT set because
# the sourced preambles use the `if [ -n "$VAR" ]` pattern on
# optionally-set vars; switching them en masse to `${VAR:-}` is a
# separate change. Explicit guards on the critical scheduler vars
# below catch the "task -1" dispatch failure mode.
set -eo pipefail
: "${SGE_TASK_ID:?SGE_TASK_ID is not set; refusing to dispatch task -1}"

# ==============================================================
# SGE CPU Array Job Template (hpc-agent)
#
# Environment variables (injected by hpc-agent from clusters.yaml
# and project.yaml before submission):
#
#   $CONDA_SOURCE  — path to conda.sh (e.g. /u/local/apps/anaconda3/.../conda.sh)
#   $CONDA_ENV     — conda environment name
#   $MODULES       — space-separated modules to load (e.g. "python gcc")
#   $EXECUTOR      — python command to run (e.g. "python3 -m myproject.cli.run")
#   $RESULT_DIR    — output directory for results
#   $REPO_DIR      — repository root to cd into
#   $EXTRA_ARGS    — additional arguments passed through to $EXECUTOR
#   $HPC_RUNTIME   — optional, "uv" runs ``uv sync`` in $REPO_DIR before
#                    dispatch (honors the "no bare pip" invariant common to uv-first integrators)
#
# Submit with:
#   qsub -t 1-100 -v TASK_OFFSET=0,CONDA_SOURCE=...,CONDA_ENV=...,EXECUTOR=...,... cpu_array.sh
# ==============================================================

# --- SGE directives ---
#$ -cwd
#$ -j y
#$ -l h_data=16G

# --- Diagnostics ---
echo "============================================"
echo "Job ID:       $JOB_ID"
echo "Array Task:   $SGE_TASK_ID"
echo "Hostname:     $(hostname)"
echo "============================================"

# --- Defaults ---
RESULT_DIR="${RESULT_DIR:-.}"
REPO_DIR="${REPO_DIR:-.}"

# --- Shared preamble (modules + conda + PYTHONPATH + uv sync) ---
# See hpc_agent/mapreduce/templates/common/hpc_preamble.sh — deployed alongside
# this template at .hpc/templates/common/hpc_preamble.sh by deploy_runtime.
source "$REPO_DIR/.hpc/templates/common/hpc_preamble.sh"

# --- Prepare Output ---
mkdir -p "$RESULT_DIR"

# Convert 1-based SGE_TASK_ID to 0-based, add offset for batched submission
TASK_ID=$((SGE_TASK_ID - 1 + ${TASK_OFFSET:-0}))
HPC_TASK_ID=$TASK_ID  # canonical name used by .hpc/_hpc_dispatch.py

echo "Task:         $TASK_ID (offset=${TASK_OFFSET:-0})"
echo "Run ID:       ${HPC_RUN_ID:-<unset>}"
echo "Result dir:   $RESULT_DIR"
echo "Executor:     $EXECUTOR"
echo "============================================"

# --- Execute ---
# HPC_RUN_ID arrives via qsub -v from the submit-side env; re-exported here
# so the dispatcher inside $EXECUTOR sees it. HPC_CAMPAIGN_ID is optional —
# present when the run is part of a closed-loop campaign — and lets the
# user's tasks.py call hpc_agent.mapreduce.reduce.history.prior() to learn what
# prior iterations of the same campaign produced.
export TASK_ID HPC_TASK_ID HPC_RUN_ID HPC_CAMPAIGN_ID RESULT_DIR
time $EXECUTOR ${EXTRA_ARGS:-}

echo "Job finished."
