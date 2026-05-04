#!/bin/bash
# ==============================================================
# claude-hpc shared preamble
#
# Sourced by sge/cpu_array.sh, sge/gpu_array.sh, slurm/cpu_array.slurm,
# and slurm/gpu_array.slurm before they begin executing the user's
# command. Owns the steps every template needs identically:
#
#   1. Module setup (Hoffman2 UGE init + per-cluster $MODULES list)
#   2. Conda activation ($CONDA_SOURCE + $CONDA_ENV)
#   3. cd $REPO_DIR + PYTHONPATH export
#   4. Optional `uv sync` when HPC_RUNTIME=uv
#   5. Thread caps so BLAS/OpenMP libs don't oversubscribe the cgroup
#      and get the campus user's job killed by the OOM daemon
#   6. Optional NFS-staging copy from $HPC_NFS_DATA_DIR into local node
#      SSD ($SLURM_TMPDIR/$TMPDIR) so the array doesn't get throttled
#      by NFS when 200 tasks read the same files at once
#
# Reads from the surrounding job's environment:
#   $MODULES        space-separated module list
#   $CONDA_SOURCE   path to conda.sh (optional)
#   $CONDA_ENV      conda env to activate (optional)
#   $REPO_DIR       repo root to cd into (defaulted before sourcing)
#   $HPC_RUNTIME    "uv" to enable uv sync; anything else is no-op
#   $HPC_OMP_NUM_THREADS / $HPC_MKL_NUM_THREADS / $HPC_OPENBLAS_NUM_THREADS /
#     $HPC_NUMEXPR_NUM_THREADS / $HPC_VECLIB_NUM_THREADS
#                   per-library thread cap overrides; default 1 each
#   $HPC_NFS_DATA_DIR
#                   optional NFS path to stage into node-local SSD before
#                   the executor runs. When set, the preamble exports
#                   $LOCAL_DATA_DIR for user code to read from instead.
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

# --- Thread caps (survival) ---
# Survival: cap threads so the campus user's job doesn't get killed by
# the OOM daemon for oversubscribing the node it was honestly allocated.
# The scheduler gave us $SLURM_CPUS_PER_TASK / $NSLOTS cores; libraries
# like OpenBLAS, MKL, NumExpr and vecLib otherwise default to "all CPUs
# the kernel can see" and will spawn 16+ threads on a 1-core allocation,
# blowing past the cgroup CPU limit and pinning RSS until the OOM daemon
# kills the job. Default to 1 thread; user override per-experiment via
# $HPC_OMP_NUM_THREADS=N (or any of the per-library $HPC_*_NUM_THREADS)
# in the spec's ``job_env``. The CPU/GPU array templates re-export
# OMP_NUM_THREADS / MKL_NUM_THREADS to $SLURM_CPUS_PER_TASK / $NSLOTS
# *after* sourcing this preamble, so multi-threaded workloads still get
# all their allocated cores — these defaults exist for the much more
# common single-core, NumPy-via-OpenBLAS case.
export OMP_NUM_THREADS="${HPC_OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${HPC_MKL_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${HPC_OPENBLAS_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${HPC_NUMEXPR_NUM_THREADS:-1}"
export VECLIB_MAXIMUM_THREADS="${HPC_VECLIB_NUM_THREADS:-1}"

# --- NFS staging (survival) ---
# Survival: copy the read-only dataset to local node SSD before the
# executor runs, so the campus user's array doesn't trigger NFS
# throttling when 200 tasks read the same files simultaneously. NFS
# servers throttle hard under that pattern — at best, every task waits
# minutes on `open()`; at worst, the array gets blacklisted from the
# fileserver and tasks time out. Local SSD reads are ~100x faster and
# scale per-node, not per-cluster.
#
# Gated on $HPC_NFS_DATA_DIR being set; users without an NFS dataset
# pay nothing. SLURM exposes a per-job $SLURM_TMPDIR; SGE exposes
# $TMPDIR (Hoffman2 sets it to a per-job /work/<jobid>). Both default
# to /tmp so user code has a stable $LOCAL_DATA_DIR to read from. The
# variable name $LOCAL_DATA_DIR is the contract — user executors should
# prefer $LOCAL_DATA_DIR over the NFS path when set.
if [ -n "${HPC_NFS_DATA_DIR:-}" ]; then
    # Pick the per-job scratch dir; fall back to /tmp only if neither
    # the SLURM nor the SGE/Hoffman2 variant is exported. /tmp is a
    # known footgun on quota'd clusters (Hoffman2 enforces a per-user
    # /tmp cap; mid-stage failures look like data-corruption to the
    # campus user). Emit a one-line warning so the failure mode is
    # diagnosable without spelunking the scheduler env.
    _hpc_stage_root="${SLURM_TMPDIR:-${TMPDIR:-}}"
    if [ -z "$_hpc_stage_root" ]; then
        _hpc_stage_root="/tmp"
        echo "[claude-hpc] warning: \$SLURM_TMPDIR / \$TMPDIR not set; falling back to /tmp." >&2
        echo "[claude-hpc]   On clusters with /tmp quotas (e.g. Hoffman2), staging may fail mid-run." >&2
        echo "[claude-hpc]   Set HPC_NFS_DATA_DIR=\"\" to disable, or have your job export TMPDIR." >&2
    fi
    export LOCAL_DATA_DIR="${_hpc_stage_root}/claude_hpc_data"
    unset _hpc_stage_root
    mkdir -p "$LOCAL_DATA_DIR"
    # Race + diagnostic guards. 200 array tasks landing on the same node
    # would all rsync into the same $LOCAL_DATA_DIR and step on each
    # other — half-staged files visible to siblings, overlapping writes,
    # tempfile collisions. flock serialises the staging so the first
    # task on a node copies, subsequent siblings block briefly then
    # fast-skip via the .staged_ok sentinel. Steady-state cost is one
    # `test -f` per task. The `|| exit 2` outside the subshell propagates
    # rsync failure to the parent template (set -e doesn't cross subshell
    # boundaries on its own); without it, a failed staging would silently
    # let the executor run against missing data.
    if [ ! -f "$LOCAL_DATA_DIR/.staged_ok" ]; then
        (
            flock -x 9
            if [ ! -f "$LOCAL_DATA_DIR/.staged_ok" ]; then
                # Capture rsync's exit code with `if !` so $? isn't
                # clobbered by the echo trash before we read it (echo
                # always succeeds, overwriting $? to 0).
                if ! rsync -a "$HPC_NFS_DATA_DIR/" "$LOCAL_DATA_DIR/"; then
                    rc=$?
                    echo "[claude-hpc] NFS staging from \$HPC_NFS_DATA_DIR=$HPC_NFS_DATA_DIR" >&2
                    echo "[claude-hpc]   to \$LOCAL_DATA_DIR=$LOCAL_DATA_DIR failed (rsync exit $rc)." >&2
                    echo "[claude-hpc]   Check the source path exists, the destination has space," >&2
                    echo "[claude-hpc]   and the NFS server is reachable from this compute node." >&2
                    exit 2
                fi
                touch "$LOCAL_DATA_DIR/.staged_ok"
            fi
        ) 9>"$LOCAL_DATA_DIR/.staging.lock" || exit 2
    fi
fi
