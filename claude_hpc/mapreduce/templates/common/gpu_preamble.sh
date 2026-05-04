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
