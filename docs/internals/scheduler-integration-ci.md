# Scheduler-in-a-container integration CI

**Status: experimental, non-required.** A GitHub Actions job that runs the REAL
submit spine against a real Slurm inside a container, so the class of bug we
have historically only found live in a proving run is caught in CI instead.

- Workflow: [`.github/workflows/scheduler-integration.yml`](../../.github/workflows/scheduler-integration.yml)
- Container: [`ci/slurm/`](../../ci/slurm) (`Dockerfile`, `entrypoint.sh`, `slurm.conf`)
- Test: [`tests/integration/scheduler/`](../../tests/integration/scheduler) (`test_scheduler_smoke.py`, `conftest.py`, `README.md`)

## What it covers

The one smoke test drives the three workflow atoms end to end, with **no mocks
on the transport or scheduler seam**:

```
submit_flow  →  monitor_flow  →  aggregate_flow
```

Concretely, a green run has exercised — against a real `sbatch`/`squeue`/`sacct`
over real SSH:

| spine step | what actually runs |
| --- | --- |
| pre-flight probe | real `ssh true` to the container login node |
| stage | `infra.transport.rsync_push` (rsync on the ubuntu runner) pushes the experiment tree |
| deploy | `deploy_runtime` scp's the job templates + framework stubs into `<remote>/.hpc/` |
| canary auto-skip | `total_tasks=2 ≤ 4` → no canary (#263); the main array's own first tasks are the smoke |
| submit | `RemoteSlurmBackend` runs `sbatch --array=1-2` over SSH |
| dispatch | cluster-side `.hpc/_hpc_dispatch.py` resolves each task's kwargs from `.hpc/tasks.py`, runs the per-task executor, promotes `metrics.json` into `RESULT_DIR` |
| monitor | real status reporter (`python -m hpc_agent…reduce.status` → `sacct`/`squeue`) polled to terminal over SSH; asserts `lifecycle_state == "complete"` |
| aggregate | cluster-side combiner + local reduce; asserts `aggregated_metrics` non-empty and `_aggregated/<run_id>/metrics_aggregate.json` landed on disk |

The two-layer executor contract is exercised on purpose: the job-script
`EXECUTOR` is the dispatcher (`python3 .hpc/_hpc_dispatch.py`), while the per-run
sidecar's `executor` is the real per-task command (`python3 train.py`). This is
the exact seam that has bitten proving runs (bare-name executors, dispatcher
self-recursion, empty `EXECUTOR`) — now under a real round-trip.

## What it deliberately does NOT cover (known gaps)

- **SGE (Hoffman2's scheduler).** There is no maintained single-container SGE/UGE
  story comparable to the Slurm images; standing one up reliably in CI is a
  project of its own. Hoffman2 is where several live incidents originated, so
  this is a real gap. **Revisit trigger:** if a maintained SGE/OpenGridScheduler
  container appears, add an `sge` variant mirroring `ci/slurm/` and parametrize
  the smoke test over `backend`.
- **GPU / MPI paths.** The container is CPU-only; `gpu_array` / `mpi` templates
  are not exercised here.
- **Multi-wave (>cap) sweeps.** The smoke stays a single wave (2 tasks). The
  wave-planner has its own unit coverage; a >cap container run is a follow-up.
- **Real conda/module activation.** The container uses the system `python3`
  (activation is empty), so the `$MODULES` / `$CONDA_SOURCE` / `$CONDA_ENV`
  preamble branches are not driven. The activation-coherence logic is unit-tested
  elsewhere.
- **The native-Windows transport (tar-over-ssh fallback, named-pipe retry).**
  The runner is Linux with rsync, so the rsync path is what runs. The tar
  fallback that the Windows dev box actually uses is not exercised here.

## Container design (pragmatic v1)

One container runs **slurmctld + slurmd + munge + sshd** — the simplest thing
that yields working `sbatch`/`squeue`/`sacct` plus an sshd accepting key auth.
It is built on `ubuntu:22.04` with the distro `slurm-wlm` package (no custom
Slurm build, no accounting DB, no cgroups). See `ci/slurm/slurm.conf` for the
single-node config; the load-bearing choices:

- `TaskPlugin=task/none` + `ProctrackType=proctrack/linuxproc` — no cgroup
  hierarchy required (a default Docker container has none).
- CPUs/RealMemory are **under-reported** (CPUs=2, RealMemory=1500). Slurm only
  drains a node when configured resources *exceed* the detected hardware, so
  under-reporting is always safe on a small runner.
- The smoke test passes tiny `SubmitResources(mem_mb=256, cpus=1,
  walltime_sec=300)`; a command-line `--mem`/`--cpus-per-task`/`--time` beats the
  template's `#SBATCH` directives (16G/4-cpu/6h), so the job always fits.

The freshly-built wheel is **installed into the container's python at workflow
time** (not baked into the image), mirroring the real cluster-env convention
where `hpc_agent` lives in the login/job env — and keeping the image layer cache
stable across code changes. The image build itself is cached via
`docker/build-push-action` `type=gha`.

SSH wiring: the workflow generates a throwaway ed25519 keypair, bind-mounts the
public half at `/pubkey` (the entrypoint installs it as `hpcuser`'s
`authorized_keys`), maps container `22 → host 2222`, and writes a `~/.ssh/config`
`Host slurmci` alias (HostName 127.0.0.1, Port 2222, User hpcuser, the throwaway
key, `StrictHostKeyChecking no`). The test's `ssh_target` is `hpcuser@slurmci`.

## Local reproduction

You need Docker (this cannot run on the native-Windows dev box). From the repo
root:

```bash
# 1. Build the wheel and the container image.
python -m build --wheel --outdir dist
docker build -f ci/slurm/Dockerfile -t hpc-agent-slurm-ci:latest .

# 2. Throwaway keypair.
ssh-keygen -t ed25519 -N '' -f ./ci_key -C scheduler-integration

# 3. Start the container (pubkey bind-mounted; entrypoint installs it).
docker run -d --name slurmci -p 2222:22 -v "$PWD/ci_key.pub:/pubkey:ro" \
  hpc-agent-slurm-ci:latest

# 4. Install the wheel into the container python.
docker cp dist/*.whl slurmci:/tmp/
docker exec slurmci bash -lc 'pip3 install /tmp/*.whl'

# 5. Wait for the node to go idle.
docker exec slurmci sinfo

# 6. SSH config + clusters.yaml (see the workflow for the exact contents).
#    Then point the framework at them and run the smoke test:
export HPC_SCHEDULER_IT=1
export HPC_CLUSTERS_CONFIG="$PWD/ci_clusters.yaml"
export HPC_JOURNAL_DIR="$(mktemp -d)"
python -m pytest tests/integration/scheduler -q -m scheduler_integration

# 7. Tear down.
docker rm -f slurmci
```

## Flake posture / promotion

The job is a **separate workflow, not in branch-protection required checks**, so
a first-run flake cannot block main CI — iteration happens on GitHub. It does not
set `continue-on-error`, so a genuine regression still shows red on the PR; it
just is not a merge gate. Promote it to a required check only after it has proven
stable across a run of PRs. Branch protection is configured out-of-band (you
cannot change it from a workflow file), so promotion is a repo-settings change,
not a code change.

### First-run follow-up list

Things most likely to need a fix on the very first live run (author could not run
docker locally):

1. **Slurm config path** — `slurm-wlm` on 22.04 uses `/etc/slurm/`; the Dockerfile
   symlinks `/etc/slurm-llnl` too, but if the package version differs the daemons
   may look elsewhere. Check `docker logs slurmci` for "cannot find configuration".
2. **Node drains** — if `sinfo` shows `drain`/`down`, the configured CPUs/RealMemory
   likely exceed the runner; lower them in `ci/slurm/slurm.conf`.
3. **munge startup ordering** — if `sbatch` reports auth errors, `munged` may not
   have been up before the daemons; add a longer sleep / readiness check in
   `entrypoint.sh`.
4. **sshd host keys / perms** — the entrypoint runs `ssh-keygen -A`; if login
   fails, verify `/home/hpcuser/.ssh` perms and that `/pubkey` was mounted.
5. **`--export` env not reaching the job** — if tasks fail with `EXECUTOR is not
   set`, confirm the Slurm build honors `sbatch --export=ALL,…` (it should) and
   inspect a task log under `<remote>/logs/`.
6. **`sacct` empty** — accounting may be disabled without a slurmdbd; the reporter
   falls back to `squeue` for live state, but terminal detection can lag. If
   monitor times out with jobs already gone, check the reporter's job-state read
   and consider enabling minimal `sacct` (or asserting on result-file presence).
7. **PEP 668 / pip** — if `pip3 install` refuses on a newer base image, add
   `--break-system-packages` to the container install step.
