# Crowd-compute executor example

A minimal, **stdlib-only** executor packaged as a container image. It
demonstrates that the dispatcher env contract (`HPC_TASK_ID`,
`RESULT_DIR`, `HPC_KW_*` — see `docs/integrations/CONTRACT.md`) is the
portability boundary: the same image runs

- under a cluster dispatcher (Apptainer/Singularity on SLURM/SGE), and
- on a crowd-compute platform (Vast.ai / SaladCloud / Akash), where the
  platform-side launcher sets the env vars and ships `RESULT_DIR` out
  after exit.

## Smoke test (no container)

```bash
HPC_TASK_ID=0 RESULT_DIR=/tmp/crowd-out HPC_KW_N_SAMPLES=50000 \
    python examples/crowd-compute-executor/executor.py
cat /tmp/crowd-out/result.json /tmp/crowd-out/metrics.json
```

## Build and run the image

```bash
docker build -t crowd-executor examples/crowd-compute-executor
docker run -e HPC_TASK_ID=0 -e RESULT_DIR=/out -e HPC_KW_N_SAMPLES=50000 \
    -v "$PWD/out:/out" crowd-executor
```

## Adapting it

Replace `compute()` with your work; keep everything else. The rules
that make an executor crowd-portable:

1. **All inputs via `HPC_KW_*`** (JSON-decoded), never positional argv
   or hardcoded paths. Data files travel via URLs/object-store keys in
   kwargs — there is no shared filesystem on a crowd platform.
2. **All outputs into `RESULT_DIR`**, plus an atomic `metrics.json`
   sidecar (scalar summaries + `n_samples`) so the combiner can
   aggregate per grid point.
3. **Seed from `HPC_TASK_ID`** so any node — trusted or not — produces
   the same numbers for the same task, which is also what makes
   redundant-execution validation of untrusted nodes possible.
4. **No hpc-agent import.** The two helper functions mirror
   `metrics_io` semantics by design (same duplication-by-design rule
   as the shipped standalone templates).
