#!/bin/bash
set -e

# ==============================================================
# SGE CPU Array Job Template (claude-hpc)
#
# Environment variables (injected by claude-hpc from clusters.yaml
# and project.yaml before submission):
#
#   $CONDA_SOURCE  — path to conda.sh (e.g. /u/local/apps/anaconda3/.../conda.sh)
#   $CONDA_ENV     — conda environment name
#   $MODULES       — space-separated modules to load (e.g. "python gcc")
#   $EXECUTOR      — python command to run (e.g. "python3 -m myproject.cli.run")
#   $RESULT_DIR    — output directory for results
#   $REPO_DIR      — repository root to cd into
#   $EXTRA_ARGS    — additional arguments passed through to $EXECUTOR
#
# Submit with:
#   qsub -t 1-100 -v CONDA_SOURCE=...,CONDA_ENV=...,EXECUTOR=...,... cpu_array.sh
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

# --- Prepare Output ---
mkdir -p "$RESULT_DIR"

# Convert 1-based SGE_TASK_ID to 0-based task ID
TASK_ID=$((SGE_TASK_ID - 1))

echo "Task:         $TASK_ID"
echo "Result dir:   $RESULT_DIR"
echo "Executor:     $EXECUTOR"
echo "============================================"

# --- Execute ---
export TASK_ID RESULT_DIR
time $EXECUTOR ${EXTRA_ARGS:-}

echo "Job finished."
