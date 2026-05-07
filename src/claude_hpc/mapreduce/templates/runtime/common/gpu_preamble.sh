#!/bin/bash
# ==============================================================
# claude-hpc shared GPU preamble
#
# Sourced by sge/gpu_array.sh and slurm/gpu_array.slurm AFTER
# hpc_preamble.sh has set up the conda env. Owns the GPU-specific
# environment knobs that are identical between SGE and SLURM:
#
#   1. Warn if CUDA_VISIBLE_DEVICES wasn't set by the scheduler.
#   2. Configure PyTorch CUDA allocator.
#   3. Export HPC_GPU_TYPE for the runtime-prior pipeline (the
#      cluster-side dispatcher reads this and stamps it onto the
#      per-task _runtime.json so the local-side rollup can group
#      samples by GPU type).
#
# OMP_NUM_THREADS / MKL_NUM_THREADS are NOT set here because the two
# schedulers expose the per-task CPU count differently ($NSLOTS on SGE,
# $SLURM_CPUS_PER_TASK on SLURM). Each per-template body sets those.
# ==============================================================

# CUDA_VISIBLE_DEVICES is set by the scheduler via -l gpu (SGE) or
# --gres=gpu:N (SLURM). Warn if absent; do not abort — some clusters
# only expose this via runtime detection.
if [ -z "$CUDA_VISIBLE_DEVICES" ]; then
    echo "WARNING: CUDA_VISIBLE_DEVICES not set by scheduler"
fi
echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"

# CUDA memory optimization — splits large allocations into 128MB
# blocks so PyTorch fragments less under heavy mixed-batch workloads.
export PYTORCH_CUDA_ALLOC_CONF="max_split_size_mb:128"

# Detect GPU model from nvidia-smi when the submit-side didn't already
# export HPC_GPU_TYPE (e.g. via qsub -v / sbatch --export). Used by
# claude-hpc's runtime-prior pipeline to tag per-task samples so the
# warm-axis-picker / GPU-type quantile rollups don't bucket every GPU
# under "" (empty string). Best-effort: a missing nvidia-smi or an
# unrecognized model leaves HPC_GPU_TYPE unset, and the dispatcher
# falls back to $SLURM_JOB_PARTITION → "".
if [ -z "$HPC_GPU_TYPE" ] && command -v nvidia-smi >/dev/null 2>&1; then
    _detected=$(nvidia-smi -L 2>/dev/null | head -1 \
        | grep -ioE '(h200|h100|a100|a6000|a40|v100|p100|k80|t4|rtx ?2080 ?ti|rtx ?3090|rtx ?4090|l40s|l4)' \
        | head -1 \
        | tr -d ' ' \
        | tr '[:upper:]' '[:lower:]')
    if [ -n "$_detected" ]; then
        export HPC_GPU_TYPE="$_detected"
    fi
    unset _detected
fi
echo "HPC_GPU_TYPE=${HPC_GPU_TYPE:-<unknown>}"
