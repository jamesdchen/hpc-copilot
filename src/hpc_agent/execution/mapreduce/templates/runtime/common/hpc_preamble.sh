#!/bin/bash
# ==============================================================
# hpc-agent shared preamble
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
#   $HPC_WALLTIME_SEC
#                   the job's walltime in seconds (stamped by build_submit_spec
#                   when the submit set one). Used to export
#                   $HPC_WALLTIME_END_EPOCH for checkpoint-aware executors.
#   $HPC_OMP_NUM_THREADS / $HPC_MKL_NUM_THREADS / $HPC_OPENBLAS_NUM_THREADS /
#     $HPC_NUMEXPR_NUM_THREADS / $HPC_VECLIB_NUM_THREADS
#                   per-library thread cap overrides; default 1 each
#   $HPC_PYTHONUNBUFFERED / $HPC_PYTHONHASHSEED /
#     $HPC_PYTHONDONTWRITEBYTECODE / $HPC_PYTHONIOENCODING /
#     $HPC_LC_ALL / $HPC_LANG
#                   reproducibility env overrides; defaults pin Python's
#                   hash seed, disable bytecode writes, force UTF-8, and
#                   unbuffer stdout so a parallel array's outputs match
#                   what a serial run would produce. Set any to "" to
#                   leave the corresponding variable unset.
#   $HPC_NFS_DATA_DIR
#                   optional NFS path to stage into node-local SSD before
#                   the executor runs. When set, the preamble exports
#                   $LOCAL_DATA_DIR for user code to read from instead.
#
# This file is scp'd to the cluster as .hpc/templates/common/hpc_preamble.sh
# alongside the per-scheduler templates by deploy_runtime().
# ==============================================================

# --- Walltime deadline (checkpoint-aware executors, #294) ---
# Capture the job start NOW — before the (possibly slow) module / conda / uv
# steps below — so the deadline reflects the real walltime clock, not the time
# left after a long preamble. Checkpoint-aware executors read
# $HPC_WALLTIME_END_EPOCH via should_checkpoint(strategy="walltime_margin") /
# run_iterations and checkpoint with margin to spare before the scheduler's
# walltime kill. Only when the submit set a walltime (build_submit_spec stamps
# $HPC_WALLTIME_SEC) and a deadline wasn't already provided.
if [ -n "${HPC_WALLTIME_SEC:-}" ] && [ -z "${HPC_WALLTIME_END_EPOCH:-}" ]; then
    export HPC_WALLTIME_END_EPOCH=$(( $(date +%s) + HPC_WALLTIME_SEC ))
fi

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
#
# #159 PYTHONPATH hygiene: an *inherited* PYTHONPATH can carry a stale or Py2
# ``hpc_agent`` (an ancient ``pip install --user``, a leftover deploy stub)
# that would shadow the conda env's install — the opaque ``bad magic number`` /
# wrong-version failure that killed the cluster-side reporter. Drop any
# inherited entry that itself contains an ``hpc_agent`` package; keep the
# user's other deps. REPO_DIR + REPO_DIR/.hpc stay first so the dispatcher and
# tasks.py still resolve. (PYTHONDONTWRITEBYTECODE, set below, stops fresh
# wrong-interpreter .pyc files accreting in the first place.)
_hpc_kept_pp=""
if [ -n "${PYTHONPATH:-}" ]; then
    _hpc_old_ifs="$IFS"
    IFS=":"
    for _hpc_pp_entry in $PYTHONPATH; do
        [ -z "$_hpc_pp_entry" ] && continue
        if [ -e "$_hpc_pp_entry/hpc_agent/__init__.py" ] \
            || [ -e "$_hpc_pp_entry/hpc_agent/__init__.pyc" ] \
            || [ -d "$_hpc_pp_entry/hpc_agent/__pycache__" ]; then
            echo "[hpc-agent] dropping inherited PYTHONPATH entry that would shadow hpc_agent: $_hpc_pp_entry" >&2
            continue
        fi
        if [ -z "$_hpc_kept_pp" ]; then
            _hpc_kept_pp="$_hpc_pp_entry"
        else
            _hpc_kept_pp="${_hpc_kept_pp}:${_hpc_pp_entry}"
        fi
    done
    IFS="$_hpc_old_ifs"
    unset _hpc_old_ifs _hpc_pp_entry
fi
export PYTHONPATH="$REPO_DIR:$REPO_DIR/.hpc${_hpc_kept_pp:+:$_hpc_kept_pp}"
unset _hpc_kept_pp

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

# --- glibc malloc arena cap (vmem survival) ---
# glibc malloc creates up to 8*ncores per-thread arenas, each reserving
# ~64MB of VIRTUAL memory. On vmem-enforced schedulers (UGE/SGE h_data
# kills on vmem, silently, with no traceback) a multi-threaded task
# (xgboost/OpenMP + arrow buffers) inflates vmem 3-5x over RSS from
# arena reservations alone — two run-14 canaries died exactly so ~1min
# into data prep at h_data=8000M. Capping arenas at 4 removes most of
# that inflation at effectively zero throughput cost. Override via
# $HPC_MALLOC_ARENA_MAX in the spec's job_env; set "" to leave unset.
if [ "${HPC_MALLOC_ARENA_MAX-4}" != "" ]; then
    export MALLOC_ARENA_MAX="${HPC_MALLOC_ARENA_MAX:-4}"
fi

# --- Reproducibility env (fidelity vs. serial) ---
# These defaults narrow the gap between an array task on a compute node
# and the same task run serially on a workstation. They cost ~nothing
# for code that doesn't depend on them and close real divergence sources
# for code that does.
#
#   PYTHONUNBUFFERED=1
#     Force unbuffered stdout/stderr so the scheduler's per-task log
#     captures every print() in order, the way an interactive run would.
#     Without it, a crashed task can leave its last few prints in the
#     stdio buffer and the campus user sees a truncated log.
#
#   PYTHONHASHSEED=0
#     Pin the hash randomization seed. CPython's default is "random per
#     interpreter," which makes set/dict iteration order vary across
#     runs. Most code doesn't depend on iteration order, but code that
#     does (e.g. building a list from a set, then doing a stable
#     reduction) becomes silently nondeterministic across the array.
#
#   PYTHONDONTWRITEBYTECODE=1
#     Don't write .pyc files. When 200 array tasks land on the same
#     node simultaneously and all import the same module, they race to
#     write the same .pyc — corruption is rare but real, and the .pyc
#     cache is per-node-shared so a corruption affects every subsequent
#     task on that node. Cheap to disable; cost is one re-parse per
#     import per task.
#
#   PYTHONIOENCODING=utf-8 / LC_ALL=C.UTF-8 / LANG=C.UTF-8
#     Pin locale and stdio encoding. Locale affects float-string parsing
#     ("1,5" vs "1.5"), date parsing, and string sort order. Different
#     compute nodes can have different default locales — pinning makes
#     the executor's behavior independent of which node it lands on.
#
# Override any of these by exporting HPC_<NAME> in the spec's job_env;
# the empty string leaves the corresponding variable unset.
if [ "${HPC_PYTHONUNBUFFERED-1}" != "" ]; then
    export PYTHONUNBUFFERED="${HPC_PYTHONUNBUFFERED:-1}"
fi
if [ "${HPC_PYTHONHASHSEED-0}" != "" ]; then
    export PYTHONHASHSEED="${HPC_PYTHONHASHSEED:-0}"
fi
if [ "${HPC_PYTHONDONTWRITEBYTECODE-1}" != "" ]; then
    export PYTHONDONTWRITEBYTECODE="${HPC_PYTHONDONTWRITEBYTECODE:-1}"
fi
if [ "${HPC_PYTHONIOENCODING-utf-8}" != "" ]; then
    export PYTHONIOENCODING="${HPC_PYTHONIOENCODING:-utf-8}"
fi
if [ "${HPC_LC_ALL-C.UTF-8}" != "" ]; then
    export LC_ALL="${HPC_LC_ALL:-C.UTF-8}"
fi
if [ "${HPC_LANG-C.UTF-8}" != "" ]; then
    export LANG="${HPC_LANG:-C.UTF-8}"
fi

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
        echo "[hpc-agent] warning: \$SLURM_TMPDIR / \$TMPDIR not set; falling back to /tmp." >&2
        echo "[hpc-agent]   On clusters with /tmp quotas (e.g. Hoffman2), staging may fail mid-run." >&2
        echo "[hpc-agent]   Set HPC_NFS_DATA_DIR=\"\" to disable, or have your job export TMPDIR." >&2
    fi
    # B-M2: include a hash of $HPC_NFS_DATA_DIR in the path suffix so
    # two concurrent campaigns (different datasets) on the same node
    # don't rsync into the same directory and step on each other. Without
    # the disambiguator, the first campaign's flock-protected rsync
    # publishes .staged_ok; the second campaign's tasks then read the
    # WRONG dataset and silently produce garbage. md5sum is universally
    # available on HPC clusters; 8 hex chars is plenty of entropy for
    # campus-scale dataset paths.
    _hpc_data_tag="$(printf '%s' "${HPC_NFS_DATA_DIR}" | md5sum | awk '{print $1}' | cut -c1-8)"
    export LOCAL_DATA_DIR="${_hpc_stage_root}/hpc_agent_data_${_hpc_data_tag}"
    unset _hpc_stage_root _hpc_data_tag
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
                # Capture rsync's exit code BEFORE any other command so
                # `$?` reflects rsync's status. The previous form
                # `if ! rsync ...; then rc=$?` inverted the status via
                # `!`, so `rc` was always 0 inside the failure branch
                # and the operator only ever saw "rsync exit 0".
                rsync -a "$HPC_NFS_DATA_DIR/" "$LOCAL_DATA_DIR/"
                rc=$?
                if [ "$rc" -ne 0 ]; then
                    echo "[hpc-agent] NFS staging from \$HPC_NFS_DATA_DIR=${HPC_NFS_DATA_DIR}" >&2
                    echo "[hpc-agent]   to \$LOCAL_DATA_DIR=$LOCAL_DATA_DIR failed (rsync exit $rc)." >&2
                    echo "[hpc-agent]   Check the source path exists, the destination has space," >&2
                    echo "[hpc-agent]   and the NFS server is reachable from this compute node." >&2
                    exit 2
                fi
                touch "$LOCAL_DATA_DIR/.staged_ok"
            fi
        ) 9>"$LOCAL_DATA_DIR/.staging.lock" || exit 2
    fi
fi

# --- Bounded dispatch retry + backoff (survival) ---
# #161: the array path used to run the per-task command exactly once and
# let the scheduler (or a wrapper) relaunch a hard failure in a tight,
# uncapped loop — one task retried ~8,647 times in 12 min, burning 8
# nodes and flooding scratch with empty `_wip_*_failed_*` dirs. This
# helper bounds that blast radius:
#
#   * Cap at $HPC_MAX_ATTEMPTS attempts (default 3) with EXPONENTIAL
#     backoff between tries ($HPC_RETRY_BACKOFF_SEC * 2^(n-1), default
#     base 2s). A deterministic instant failure therefore aborts in a
#     few seconds, NOT at walltime.
#   * After the cap, write a TERMINAL failure marker under
#     $RESULT_DIR/.hpc_failed/<run>.<task>.failed and exit non-zero so
#     the task records as `failed` rather than looping.
#   * Honor that marker on entry: a scheduler relaunch of an
#     already-given-up (run, task) refuses to re-run — the cross-
#     invocation half of the cap, so even an external resubmit loop is
#     bounded. A DELIBERATE resubmit (resubmit_flow, ops/recover_flow.py)
#     clears the markers for exactly the task ids it re-submits before
#     the new array lands, so the adjusted-resources recovery path is
#     not refused by a marker from the exhausted prior attempt.
#   * Treat dispatcher exit code 3 (no per-task runner resolved — the
#     `.hpc/cli.py`-missing self-recursion guard, #162) as TERMINAL: it
#     is a deterministic scaffold error retrying cannot fix, so fail
#     immediately without burning the remaining attempts.
#
# Overridable per-experiment via $HPC_MAX_ATTEMPTS / $HPC_RETRY_BACKOFF_SEC
# in the spec's job_env. Set $HPC_MAX_ATTEMPTS=1 to disable retries.
#
# Runs ``$EXECUTOR ${EXTRA_ARGS:-}`` (the same command the templates ran
# inline before) under ``time`` so the per-attempt wall-clock still lands
# in the job log.

# Dispatcher exit code signalling "no per-task runner resolved" — kept in
# lock-step with ``_EXIT_NO_RUNNER`` in hpc_agent/execution/mapreduce/dispatch.py.
HPC_DISPATCH_EXIT_NO_RUNNER=3

# Dispatcher exit code signalling "executor exited 0 but produced no output"
# (empty WIP result dir) — kept in lock-step with ``_EXIT_NO_OUTPUT`` in
# hpc_agent/execution/mapreduce/dispatch.py. Like the no-runner code it is a
# DETERMINISTIC scaffold error (an executor whose __main__ bypasses the
# framework's result-writer): retrying cannot make it write output, so treat
# it as TERMINAL and fail immediately instead of burning the remaining
# attempts on a run that will never produce a result.
HPC_DISPATCH_EXIT_NO_OUTPUT=4

hpc_run_with_retry() {
    local max_attempts="${HPC_MAX_ATTEMPTS:-3}"
    local backoff_base="${HPC_RETRY_BACKOFF_SEC:-2}"
    local task="${TASK_ID:-${HPC_TASK_ID:-0}}"
    local run="${HPC_RUN_ID:-run}"
    local fail_dir="${RESULT_DIR:-.}/.hpc_failed"
    local fail_marker="${fail_dir}/${run}.${task}.failed"
    local attempt=1
    local rc=0
    local sleep_for

    # Cross-invocation cap: a prior invocation already gave up on this
    # (run, task). Refuse to re-run so a scheduler relaunch can't loop.
    if [ -f "$fail_marker" ]; then
        echo "[hpc-agent] task ${task} already marked terminally failed; refusing to re-run" >&2
        cat "$fail_marker" >&2 2>/dev/null || true
        return 1
    fi

    while : ; do
        echo "[hpc-agent] attempt ${attempt}/${max_attempts}: $EXECUTOR"
        rc=0
        # ``|| rc=$?`` neutralises ``set -e`` so a failed attempt routes
        # into the retry logic instead of aborting the whole template.
        time $EXECUTOR ${EXTRA_ARGS:-} || rc=$?
        if [ "$rc" -eq 0 ]; then
            return 0
        fi
        if [ "$rc" -eq "$HPC_DISPATCH_EXIT_NO_RUNNER" ]; then
            echo "[hpc-agent] terminal error (exit ${rc}: no per-task runner resolved); not retrying" >&2
            break
        fi
        if [ "$rc" -eq "$HPC_DISPATCH_EXIT_NO_OUTPUT" ]; then
            echo "[hpc-agent] terminal error (exit ${rc}: executor exited 0 but produced no output); not retrying" >&2
            break
        fi
        if [ "$attempt" -ge "$max_attempts" ]; then
            break
        fi
        # Exponential backoff: base * 2^(attempt-1). bash arithmetic.
        sleep_for=$(( backoff_base * (2 ** (attempt - 1)) ))
        echo "[hpc-agent] attempt ${attempt} failed (exit ${rc}); backing off ${sleep_for}s before retry" >&2
        sleep "$sleep_for"
        attempt=$(( attempt + 1 ))
    done

    # Cap reached (or terminal error) — record a terminal marker and fail.
    mkdir -p "$fail_dir" 2>/dev/null || true
    {
        echo "run_id=${HPC_RUN_ID:-}"
        echo "task_id=${task}"
        echo "attempts=${attempt}"
        echo "last_exit=${rc}"
        echo "executor=${EXECUTOR}"
        echo "host=$(hostname 2>/dev/null || echo unknown)"
        echo "when=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
    } > "$fail_marker" 2>/dev/null || true
    echo "[hpc-agent] task ${task} failed after ${attempt} attempt(s) (exit ${rc}); wrote terminal marker ${fail_marker}" >&2
    return "$rc"
}
