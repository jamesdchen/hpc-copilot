# Proving run #15 — runsheet (the submit-once live-fire, hard-gated)

**Status: READY TO RUN.** DOCS-ONLY plan; no code lands here. Base = green
`main` (latest: `bfdf2677` U-HW1). **User ruling #5 (hard gate):** *"make sure we
actually get to test `submit_once` on the next proving run."* This runsheet exists
so the next submit window **cannot miss it**. Follow it top-to-bottom; every
command is copy-pasteable and every expected-evidence check is a file, a status,
or a grep — never a vibe.

## What this run first-exercises against a real cluster

| Capability | State on `main` | This run is the FIRST live fire |
|---|---|---|
| **submit-once** (`HPC_SUBMIT_ONCE=1`) | mint→promote wired live (`c73f5cfe`); jobmap marker + correlation token + reconcile recovery ladder all mechanized behind the flag; drilled hermetically only (`tests/faultinject/test_submit_once.py`) | **YES** — never run against a real scheduler. The lone remaining gate before the default flip. |
| **U-ENV1** env-lock snapshot (`env_lock_sha`) | `83e47735` + signed-manifest `6d12cdac` | YES — first live `pip freeze` capture in the canary's real activation |
| **U-HW1** hardware facts (`hw_sha`) | `bfdf2677` | YES — first live `_runtime.json` placement pull |
| **S1 reducibility disclosure** | `c8bd0e7e` | YES — first live irreducible-plan surface at the S1 walk boundary |
| **canary reducer-check** (rung 2) | `amortized-reduction-check` | YES — first live custom-reducer exec against the canary row (only fires if the demo declares a custom reducer) |
| **campaign breaker vs submitting orphan** | `7c0622ec` (F1/F2) | Only if the run is driven under a campaign |

The demo (`monte_carlo_pi.py`) uses the **built-in mean reducer**, so the
reducer-check will report `skipped` unless you deliberately declare a custom
`aggregate_cmd` (see §3).

---

## 0. Facts this runsheet keys on (verify, don't trust)

- **Demo repo:** `C:\Users\james\demo-hpc` — venv `.venv`, executor
  `executors\monte_carlo_pi.py`, `clusters.yaml` with real creds (gitignored),
  live snapshot in `C:\Users\james\demo-hpc\SESSION_HANDOFF.md`.
- **Clusters (BOTH must be refreshed):**
  - **hoffman2** — SGE/UGE family, env `~/.conda/envs/hpc-pi` (Python **3.11**),
    home `/u/home/j/jamesdc1`, `qsub` / `qstat`.
  - **discovery (CARC)** — Slurm family, env `~/.conda/envs/hpc-pi`
    (Python **3.12**), home `/home1/jc_905`, `sbatch` / `squeue`.
- **Native ssh** (agent-aware, named-pipe): `C:\Windows\System32\OpenSSH\ssh.exe`.
  Git Bash's bundled ssh is agent-blind — use the native binary.
- **Wheel is pure-python** (`py3-none-any`): ONE Windows-built wheel installs on
  both clusters (3.11/3.12) unchanged.
- **The submit-once flag reads the env LIVE on every call**
  (`infra/jobmap.py::submit_once_enabled`, `os.environ.get(...).strip()=="1"`) —
  no re-import needed; the operator flips it per-window.
- **Local run sidecar:** `<experiment_dir>\.hpc\runs\<run_id>.json` — carries
  `env_lock_sha` / `env_lock_status` / `hw_facts` / `hw_sha` / `hw_status`.
- **Cluster jobmap markers:** `<remote_path>/.hpc/submit/<run_id>.jobmap`
  (pending, pre-dispatch) and `<run_id>.jobmap.wave-0.id` (`"<rc> <raw stdout>"`,
  post-qsub).
- **Correlation token:** `HPC_TOKEN=<run_id>#<attempt>`, carried in Slurm
  `--comment` / SGE `-ac HPC_TOKEN=`; read back via `squeue -o "%k"` /
  `qstat -j <jobid>`.

**MSYS path traps (cost real installs before):** through Git Bash → native ssh,
use `~`-relative REMOTE paths; for any absolute remote path passed as an argument
prefix the command with `MSYS_NO_PATHCONV=1` (or `MSYS2_ARG_CONV_EXCL="*"`).

---

## 1. PRE-FLIGHT — build once, deploy everywhere, verify by import (never version string)

The submit-once machinery is **client-side Python on every reader of the journal
home** (premortem C1/Δ3: an old co-resident wheel's `prune_terminal_runs` would
GC a `submitting` orphan). So EVERY env that touches this journal — the demo
venv, the hook env, and BOTH cluster envs (they run the reporter/combiner and
also read `.hpc/`) — must carry the new wheel BEFORE the flag flips.

### 1.1 Build the wheel from green main

Run the `release` skill (it bumps `pyproject`, runs creds+regen+lint+**full
pytest** gates, commits the bump so the sha stamps clean, builds the wheel,
installs into the local/hook envs, and STOPS before anything outward). Do NOT
push or tag for a proving run.

```powershell
# From the hpc-agent repo (NOT the worktree). Confirm clean green main first:
cd 'C:\Users\james\CC Allowed\hpc-agent'
git remote -v                        # confirm hpc-copilot
git status --porcelain               # must be clean
git log --oneline -1                 # record the sha you are shipping
```

Invoke `/release` (the skill). When it stops, record:
- the built wheel path (`dist\hpc_agent-<ver>-py3-none-any.whl`), and
- its **sha256** — this is the deployment fingerprint, NOT the version string
  (the version is static `0.11.0`; the sha travels in `_build_info.py`).

```powershell
$whl = (Get-ChildItem 'C:\Users\james\CC Allowed\hpc-agent\dist\*.whl' |
        Sort-Object LastWriteTime | Select-Object -Last 1).FullName
(Get-FileHash $whl -Algorithm SHA256).Hash
```

> **Creds hazard (`release_clusters_yaml_hazard`):** if you edited the repo's
> `src/hpc_agent/config/clusters.yaml` with real creds, stash it OUT of the tree
> before building — it ships as package-data. The `release` skill's placeholder
> gate catches this, but confirm.

### 1.2 Install into the demo venv + hook env

The `release` skill already refreshes local envs. Confirm the demo venv:

```powershell
& 'C:\Users\james\demo-hpc\.venv\Scripts\python.exe' -m pip install --force-reinstall $whl
```

### 1.3 Refresh BOTH cluster envs (stage in ~, NEVER /tmp)

Hoffman2 `/tmp` is **per-login-node** — a stale same-named wheel silently
installs old code. Stage in `~` (shared FS). `conda run` is BLIND in
non-interactive SSH on hoffman2 — use the **direct env-python path**.

```bash
# Git Bash. Use native ssh/scp; ~-relative remote paths only.
SSH='C:/Windows/System32/OpenSSH/ssh.exe'
SCP='C:/Windows/System32/OpenSSH/scp.exe'
WHL='C:/Users/james/CC Allowed/hpc-agent/dist/hpc_agent-0.11.0-py3-none-any.whl'

# --- hoffman2 (SGE, py3.11) ---
"$SCP" -o ControlMaster=no -o ControlPath=none -o BatchMode=yes "$WHL" hoffman2:~/
"$SSH" -o ControlMaster=no -o ControlPath=none -o BatchMode=yes hoffman2 \
  '~/.conda/envs/hpc-pi/bin/python -m pip install --force-reinstall ~/hpc_agent-0.11.0-py3-none-any.whl'

# --- discovery/CARC (Slurm, py3.12) — conda run DOES work here ---
"$SCP" -o ControlMaster=no -o ControlPath=none -o BatchMode=yes "$WHL" discovery:~/
"$SSH" -o ControlMaster=no -o ControlPath=none -o BatchMode=yes discovery \
  'conda run -n hpc-pi python -m pip install --force-reinstall ~/hpc_agent-0.11.0-py3-none-any.whl'
```

### 1.4 Verify versions end-to-end — the IMPORT-CANONICAL check (never a version string)

The version string is static and lies across a stale install. Prove the NEW code
is present by (a) sha256 of the staged wheel matching §1.1, and (b) importing the
**submit-once functions and inspecting their source** on each env. This is the
gate: if any env cannot import `mint_submitting_record` / `promote_submitting_record`
/ `_recover_submitting`, or its `prune_terminal_runs` predates the Δ3 guard, STOP.

```bash
# The one-liner probe, run against EACH env (demo venv, hoffman2, discovery).
PROBE='import inspect, hpc_agent
from hpc_agent.infra.jobmap import submit_once_enabled, SUBMIT_ONCE_FLAG
from hpc_agent.ops.submit.runner import mint_submitting_record, promote_submitting_record
from hpc_agent.ops.monitor.reconcile import _recover_submitting
from hpc_agent.state.index import prune_terminal_runs
src = inspect.getsource(prune_terminal_runs)
assert "TERMINAL_STATUSES" in src, "OLD prune guard — Δ3 skew hazard, DO NOT FLIP"
print("OK submit_once wired; flag=", SUBMIT_ONCE_FLAG, "prune-guard=delta3")'

# hoffman2
"$SSH" ... hoffman2 "~/.conda/envs/hpc-pi/bin/python -c '$PROBE'"
# discovery
"$SSH" ... discovery "conda run -n hpc-pi python -c '$PROBE'"
```

```powershell
# demo venv
& 'C:\Users\james\demo-hpc\.venv\Scripts\python.exe' -c "import inspect; from hpc_agent.state.index import prune_terminal_runs; assert 'TERMINAL_STATUSES' in inspect.getsource(prune_terminal_runs); from hpc_agent.ops.submit.runner import mint_submitting_record, promote_submitting_record; from hpc_agent.ops.monitor.reconcile import _recover_submitting; print('OK')"
```

**Expected evidence (all three envs):** each prints `OK` and the assert does NOT
fire. A fired assert = an old wheel is still resident on that env → the orphan
would be garbage-collected → **DO NOT proceed to the flag flip**.

### 1.5 Restart the demo session with the flag DEFAULT-OFF

Restart the Claude Code demo session so it picks up the new wheel (PATH-prepend
launch, not `Activate.ps1`). Leave `HPC_SUBMIT_ONCE` **unset** for now — the flip
is per-window in §2.

```powershell
$env:PATH = "C:\Users\james\demo-hpc\.venv\Scripts;C:\Windows\System32\OpenSSH;$env:PATH"
$env:HPC_CLUSTERS_CONFIG = "C:\Users\james\demo-hpc\clusters.yaml"
Remove-Item Env:\HPC_SUBMIT_ONCE -ErrorAction SilentlyContinue   # confirm OFF
# then launch: claude
```

**Pre-flight DONE when:** §1.4 prints `OK` on demo venv + hoffman2 + discovery,
`preflight` (the skill) passes, and `HPC_SUBMIT_ONCE` is unset.

---

## 2. THE SUBMIT-ONCE DRILL — the apex dispatch→id-window kill

This is the hard gate. The contract makes recovery a **READ**: the dispatching
shell persisted the scheduler id in a cluster-durable jobmap marker; reconcile
adopts it with **zero re-qsub**. We prove that live.

### 2.1 The happy path first (flag ON, no kill) — prove promote fires clean

Before injecting a fault, prove flag-ON normal operation: the submit MINTS a
`submitting` record, dispatches, and PROMOTES to `in_flight`.

```powershell
$env:HPC_SUBMIT_ONCE = "1"      # per-window flip; read live on every call
# drive /submit-hpc through S1 → S2 (canary) → greenlight → S3 (main array)
```

**Expected evidence (happy path):**
- On the cluster: `<remote_path>/.hpc/submit/<run_id>.jobmap` exists (pending
  marker) AND `<run_id>.jobmap.wave-0.id` exists with a leading `0 ` (rc==0).
  ```bash
  "$SSH" ... hoffman2 'cat ~/<remote_path>/.hpc/submit/<run_id>.jobmap; \
    echo ---; cat ~/<remote_path>/.hpc/submit/<run_id>.jobmap.wave-0.id'
  ```
- Locally: the run reaches `in_flight` with real `job_ids` (the promote ran).
  ```powershell
  Get-Content 'C:\Users\james\demo-hpc\<exp>\.hpc\runs\<run_id>.json' | ConvertFrom-Json |
    Select-Object status, job_ids
  # status = in_flight ; job_ids = [ real scheduler id ]
  ```
- The array is ONE array in `qstat`/`squeue` under the token (no duplicate).

If the happy path promotes clean and harvests normally, flag-ON byte-for-byte
outcome-equivalence to flag-OFF is demonstrated. Now the kill.

### 2.2 The kill-step design — which process, when

**The window:** `mint_submitting_record` runs BEFORE the qsub round-trip and sets
`status=submitting, job_ids=[]`. `promote_submitting_record` runs AFTER the id is
read and flips to `in_flight`. The apex orphan is a drop **after the scheduler
accepts the array but before its stdout job id reaches the client** — i.e. while
the local process is blocked in the `_submit_main_array` SSH round-trip, or in the
few local ops between that round-trip returning and the promote.

**Which process:** the local demo submit process — the `hpc-agent` process driving
S3 `launch_main_array` (the MAIN array is the meaningful window; the canary also
mints a submitting record if you prefer a cheaper target). If the submit ran
detached, read its PID from the worker heartbeat; if foreground, it is the
`hpc-agent`/`python` process in the demo session.

**The trigger (mechanical, retryable):** from a SECOND terminal, poll the
scheduler for the array appearing under the correlation token. The instant it
appears, the scheduler has ACCEPTED but the client is still mid-round-trip →
**kill the local submit process NOW.**

```bash
# discovery (Slurm) — poll by comment/token; kill on first hit
TOKEN='<run_id>#0'
while : ; do
  "$SSH" ... discovery "squeue -h -u jc_905 -o '%i %k' | grep -F '$TOKEN'" && break
  sleep 1
done
echo ">>> ARRAY ACCEPTED — KILL THE LOCAL SUBMIT PROCESS NOW <<<"
```

```bash
# hoffman2 (SGE) — the token rides -ac context, read via qstat -j
TOKEN='<run_id>#0'
while : ; do
  JID=$("$SSH" ... hoffman2 "qstat -u jamesdc1 | awk 'NR>2{print \$1}'" | head -1)
  [ -n "$JID" ] && "$SSH" ... hoffman2 "qstat -j $JID | grep -F 'HPC_TOKEN=$TOKEN'" && break
  sleep 1
done
echo ">>> ARRAY ACCEPTED — KILL NOW <<<"
```

Kill the local process:
```powershell
# foreground: Ctrl-C twice, or:
taskkill /F /PID <submit_pid>
```

**If you miss the window** (check `hpc-agent status`: the run already shows
`in_flight`): this run promoted cleanly — no orphan minted. Because `run_id` is
deterministic on swept params, change ONE swept param to mint a FRESH `run_id`
and retry the kill. Do NOT re-submit the same run_id (the front door refuses it →
`_RECONCILE`).

### 2.3 Expected evidence at each step

| Step | Where | Mechanical check | Pass |
|---|---|---|---|
| **Orphan minted** | local sidecar / status | `status` field of `.hpc\runs\<run_id>.json` | `submitting`, `job_ids: []` |
| Orphan visible in status | `hpc-agent status` / relay | grep the relay render | line `... <run_id> submitting — dispatch in flight / awaiting id` |
| Orphan visible in doctor | `doctor` (once the tick lapses) | grep doctor proposal | `submit stalled since ... status submitting: the dispatch window lapsed ...` |
| Marker persisted | cluster | `cat .hpc/submit/<run_id>.jobmap` | JSON `{"token":"<run_id>#0","state":"pending",...}` |
| Id durable server-side | cluster | `cat .hpc/submit/<run_id>.jobmap.wave-0.id` | `0 <raw scheduler stdout with job id>` |
| Token in scheduler | cluster | `squeue -o "%k"` / `qstat -j <jid>` | shows `<run_id>#0` |
| **Reconcile ADOPTS** | run reconcile (status-watch / doctor→reconcile) | sidecar `status` after a reconcile tick | `submitting → in_flight`, `job_ids` = the marker's id |
| **ZERO re-qsub** | cluster | count arrays under the token | **exactly ONE** array id (the adopted one), never two |
| Adoption logged | local log | grep the reconcile log | `reconcile: adopted orphaned array <run_id>: ... no re-qsub` |

The load-bearing assertion is the last two: **one array, no re-dispatch.** That is
the duplicate-array the whole contract exists to prevent.

### 2.4 Failure modes and what each means

| Observation | Meaning | Action |
|---|---|---|
| TWO arrays under the token after reconcile | re-qsub happened — the contract FAILED | **HARD STOP.** Do not flip the default. Capture the run_id, both job ids, the sidecar, and the reconcile log; file a finding. |
| Run stuck `submitting` forever, never adopts | recovery stayed UNKNOWN (rung 3): jobmap read severed every tick, OR marker absent AND announce absent mis-read | Check `.hpc/submit/` exists on the cluster and the ack fires (`__HPC_JOBMAP_ACK__` in the read output). A persistent sever = a transport/shared-FS issue, not a submit-once defect — but note it. |
| Adopts an id but the array is not actually live | phantom-id adopt (Δ4 gate leaked) | Check the wave-id marker's leading rc; if `rc!=0` it should have gone `abandoned` (safe-resubmit), not adopted. A `rc==0` marker whose id names no array = a real defect → STOP. |
| Run goes `submitting → abandoned` then re-submits at attempt+1 | rung-2 "never dispatched" or rung-1b clean-miss — the array genuinely never entered the queue | This is CORRECT if the kill landed before qsub accepted. Confirm the scheduler shows NO array under `<run_id>#0` and exactly one under `<run_id>#1`. |
| `submitting` orphan silently GC'd (record gone) | an old co-resident wheel pruned it (Δ3) | §1.4 should have caught this. A prune here = an env was missed → STOP, re-deploy, restart. |

---

## 3. THE NEW-CAPTURE FIRST-EXERCISE — what a healthy run now ALSO produces

Independent of the kill, a normal flag-ON (or flag-OFF) submit through the canary
gate now stamps four new reproducibility dimensions. Check each on the MAIN run's
sidecar `.hpc\runs\<run_id>.json` and in the S2 brief. All are **best-effort +
disclose-never-block** — a `could_not_capture` is honest evidence, not a failure.

### 3.1 env_lock (U-ENV1) — resolved-environment snapshot

Captured in the canary's real activation over one SSH exec (`pip freeze` →
lockfile → `python -V`, first that resolves wins).

```powershell
Get-Content '...\.hpc\runs\<run_id>.json' | ConvertFrom-Json |
  Select-Object env_lock_sha, env_lock_status
```
**Pass:** `env_lock_status = "captured"` and `env_lock_sha` is a 64-hex string.
`could_not_capture` with `env_lock_sha=null` is acceptable-but-note (means the
snapshot script's ack never fired — check the run env has `pip`/`python3`).

### 3.2 hw_facts (U-HW1) — placement facts, zero new round-trip

Reduced from the canary's `_runtime.json` (node / cpu_model / partition) that rode
home on the fingerprint pull.

```powershell
Get-Content '...\.hpc\runs\<run_id>.json' | ConvertFrom-Json |
  Select-Object hw_sha, hw_status, hw_facts
```
**Pass:** `hw_status = "captured"`, `hw_sha` a 64-hex string, `hw_facts` carries
`node`/`cpu_model`/`partition`. `could_not_capture` means the dispatcher wrote no
`_runtime.json` (a pre-U-HW1 remote combiner) — confirm the cluster wheel is the
new one (§1.4), since this is exactly what the refresh fixes.

### 3.3 S1 reducibility disclosure

Surfaced at the S1 walk boundary (beside the deploy-payload disclosure), BEFORE
compute — an irreducible plan no longer waits for a 40-min post-compute pull.

**Pass:** the S1 brief carries a reducibility block. A reducible plan (the demo's
default) is byte-unchanged — no block, no gate. If you WANT to see the disclosure
fire, declare an irreducible plan; otherwise confirm the reducible-plan brief is
unchanged (the disclosure is opt-in visibility, never a refusal).

### 3.4 canary reducer-check (rung 2)

Only fires if the run declares a **custom** `aggregate_cmd` (the demo uses the
built-in mean → `skipped`). To first-exercise it, declare a custom reducer for one
run.

**Pass (custom reducer):** the S2 result's `reducer_check.status` is:
- `passed` — reducer ran clean against the canary's one real row;
- `disclosed` — reducer RAN and failed; verbatim stderr in the brief; a bare `y`
  still crosses (might be a benign "needs ≥2 rows");
- `unverified` — channel severed/timeout; UNKNOWN, never a pass;
- `skipped` — no custom reducer (the demo default).

Check via the S2 brief (`reducer_check` rides `SubmitAndVerifyResult`).

**These NEVER fail the submit** — every one degrades to an honest recorded status.
The check is that the STATUS is recorded (no silent skip), not that it is green.

---

## 4. WATCH LIST — known residuals to observe (note, don't necessarily block)

- **PBS/marker-only recovery:** neither demo cluster is PBS, but if a PBS target
  is ever added, rung-1b's token read-back path is scheduler-specific — the
  marker-only adopt (rung-1a) is the load-bearing one there. On hoffman2 (SGE) the
  token rides `-ac HPC_TOKEN=`; confirm `qstat -j` echoes it (§2.3 row 6). If the
  SGE `-ac` context is not surfaced by the cluster's `qstat -j`, rung-1b can't
  disambiguate — the run stays `submitting` (UNKNOWN, safe) rather than mis-adopt.
- **Campaign breaker attribution (`7c0622ec`):** if you drive this under a
  campaign, confirm a `submitting` child is counted as OUTSTANDING (not 0) and
  does NOT reset the consecutive-failure streak. Mechanical check: with a
  `submitting` orphan present, `campaign_status.in_flight` ≥ 1 and three real
  consecutive failures still trip the breaker.
- **arm-complete streaming / custom reducer:** the reducer-check runs the SAME
  `cluster_reduce` the final harvest runs. If the demo declares a custom reducer,
  watch that the check's `python3` binds the RUN's env interpreter (the
  py3.11-vs-3.12 class is exactly what it catches pre-array).
- **Windows SSH round-trip width:** no ControlMaster on Windows means every leg is
  a full handshake — the kill window in §2.2 is comfortably wide *because* of this.
  On a faster-multiplexed box the window narrows; widen retries accordingly.
- **Marker-append reachability (O5):** the append is a single fork-free `mv`, so
  the append-killed window is near-unreachable; the load-bearing recovery is
  rung-1b (the token query), not the append. Do not expect to hit an
  append-severed state by hand.

---

## 5. THE DEFAULT-FLIP CRITERION — the bar to flip `HPC_SUBMIT_ONCE` default-ON

**The flip is the user's call.** This runsheet names the bar. Flip only when ALL
of the following are in hand from THIS run:

1. **Happy path (§2.1):** flag-ON submit MINTS `submitting`, dispatches, and
   PROMOTES to `in_flight` with the real id, and harvests normally — outcome
   byte-equivalent to flag-OFF.
2. **The apex kill (§2.3):** a deliberately orphaned submit is recovered by
   reconcile with the load-bearing pair proven: **exactly ONE array under the
   token** (zero re-qsub) AND the run transitions `submitting → in_flight` by
   ADOPTION of the marker id — captured in the log
   (`adopted orphaned array ... no re-qsub`).
   *Or* a clean safe-resubmit variant proven: kill-before-accept → `submitting →
   abandoned` → one fresh array at `attempt+1`, with NO array ever standing under
   `attempt 0`.
3. **No skew casualty (§1.4 + §2.4):** no `submitting` record was silently GC'd —
   every env passed the Δ3 import-canonical guard, and the orphan survived every
   prune tick until reconcile owned it.
4. **New captures clean (§3):** `env_lock` and `hw_facts` stamp `captured` on a
   healthy run (or an explained `could_not_capture`); the reducer-check records a
   status (if a custom reducer was exercised); no capture ever failed a submit.
5. **Campaign attribution (§4)**, if driven under a campaign: a `submitting` child
   counts as outstanding and does not disarm the breaker.

When (1)–(3) hold with the one-array / adopt-no-reqsub evidence captured, the
submit-once contract has been validated against a real scheduler — the lone gate
`submit_once_enabled`'s docstring names. The flip itself is a one-line change to
that predicate's default (or removing the flag gate), landed as a normal PR with
this run's evidence linked. **Until then, `HPC_SUBMIT_ONCE` stays a per-window
proving-run flag, never a production default.**

---

## Drift log

- 2026-07-17: Created. Runsheet for proving run #15, the submit-once live-fire
  hard gate (user ruling #5). Verified against source at `main`/`bfdf2677`:
  the live flip `c73f5cfe` (`submit_flow.py` mint→promote, gated on
  `submit_once_enabled`), the recovery ladder (`reconcile._recover_submitting`,
  rungs 1a/1b/2/3), the jobmap marker + `HPC_TOKEN` correlation
  (`infra/jobmap.py`), the hermetic drills (`tests/faultinject/test_submit_once.py`),
  the Δ3 skew guard (`state/index.prune_terminal_runs` keys on `TERMINAL_STATUSES`),
  the four new captures (`env_lock_capture` / `hw_facts_capture` / S1 reducibility
  / `_check_reducer_on_canary` in `submit_and_verify.py`), the campaign-breaker fix
  `7c0622ec`, and the release pipeline (`docs/internals/release-pipeline.md`) +
  demo/cluster conventions (`demo_windows_hoffman2_env` memory). Kill-step: poll
  the scheduler by `<run_id>#0` token, kill the local submit process on first
  array appearance (scheduler-accepted-before-id-read window); retry with a fresh
  param-derived run_id if the promote wins the race. Flip bar: one-array /
  adopt-no-reqsub proven live + no skew casualty + captures clean.
