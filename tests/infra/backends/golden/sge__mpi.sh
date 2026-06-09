#!/bin/bash
# Fail loudly: -e exits on error, -o pipefail propagates pipeline
# failures. -u is intentionally NOT set (the sourced preamble guards
# optional vars with `if [ -n "$VAR" ]`). The EXECUTOR guard catches
# the "ran `time` with no command and exited 0 silently" failure mode.
set -eo pipefail
: "${EXECUTOR:?EXECUTOR is not set; refusing to dispatch (would run \`time\` with no command and exit 0 silently — see hpc-agent #191/#192)}"

# ==============================================================
# SGE MPI (multi-rank) Job Template (hpc-agent, #293)
#
# A single multi-rank job is ONE unit of work: the launcher fans the
# executor out across $HPC_MPI_RANKS ranks and the whole job records
# as task 0. Resource sizing (ranks / nodes / topology) comes from the
# submit spec's `mpi` block via resource_flags, not a sweep axis.
#
# Extra env vars (beyond the array templates' set):
#   $HPC_MPI_RANKS            — total ranks to launch (default 2)
#   $HPC_MPI_LAUNCHER         — srun | mpirun | aprun (default srun)
#   $HPC_MPI_THREADS_PER_RANK — OpenMP threads per rank (default 1)
# ==============================================================

# --- SGE directives (defaults; resource_flags overrides) ---
#$ -cwd
#$ -j y
#$ -o logs/
#$ -pe mpi 2

mkdir -p logs
exec >"logs/${JOB_NAME}.o${JOB_ID}.1" 2>&1

# --- Defaults ---
RESULT_DIR="${RESULT_DIR:-.}"
REPO_DIR="${REPO_DIR:-.}"

# Single multi-rank unit of work — no scheduler array index. The
# dispatcher keys per-task identity off HPC_TASK_ID; an MPI job is task 0.
HPC_TASK_ID="${HPC_TASK_ID:-0}"
TASK_ID="$HPC_TASK_ID"

# --- Diagnostics ---
echo "============================================"
echo "Hostname:     $(hostname)"
echo "MPI ranks:    ${HPC_MPI_RANKS:-2} (launcher=${HPC_MPI_LAUNCHER:-srun})"
echo "Run ID:       ${HPC_RUN_ID:-<unset>}"
echo "============================================"

# --- Shared preamble (modules + conda + PYTHONPATH + uv sync) ---
# See hpc_agent/execution/mapreduce/templates/common/hpc_preamble.sh — deployed
# alongside this template at .hpc/templates/common/hpc_preamble.sh by deploy_runtime.
source "$REPO_DIR/.hpc/templates/common/hpc_preamble.sh"

# Hybrid MPI+OpenMP: give each rank its OpenMP thread budget. The
# preamble defaults OMP/MKL threads to 1; a hybrid job raises them to
# threads-per-rank so each rank's BLAS/OpenMP uses its allocated cores.
export OMP_NUM_THREADS="${HPC_MPI_THREADS_PER_RANK:-${OMP_NUM_THREADS:-1}}"
export MKL_NUM_THREADS="$OMP_NUM_THREADS"

# --- Prepare Output ---
mkdir -p "$RESULT_DIR"
echo "Executor:     $EXECUTOR"
echo "============================================"

# --- Execute ---
# Run the dispatcher once; it prefixes the per-task command with the
# launcher (HPC_MPI_LAUNCHER + HPC_MPI_RANKS) so a single bookkeeping
# process fans the compute out to N ranks. hpc_run_with_retry keeps the
# bounded retry + terminal-failure marker, identical to the array path.
export TASK_ID HPC_TASK_ID HPC_RUN_ID HPC_CAMPAIGN_ID RESULT_DIR HPC_MPI_RANKS HPC_MPI_LAUNCHER
hpc_run_with_retry

echo "Job finished."
