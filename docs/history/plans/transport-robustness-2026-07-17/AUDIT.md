# Transport Robustness Audit — the network/cluster reach inventory

**Status:** READ-ONLY audit (step 1 of the ruled robustness sequence). No
source/test touched.
**Date:** 2026-07-17. **Diagnosis under test:** "ssh stuff is brittle and
there's a lot of bugs surrounding the connections."
**Sequence this feeds:** (1) THIS AUDIT → (2) contract the transport to two
primitives (`sync-files`; `run-idempotent-script-returning-JSON`) → (3)
fault-injection/kill-drill suite → (4) daemon rung ladder (daemon ruled OFF;
affinities noted only).

Every absolute path below is under
`C:\Users\james\CC Allowed\hpc-agent\src\hpc_agent\`.

---

## 0. How the stack is layered (so the tables read correctly)

There are **two disjoint remote planes**, and the difference is the spine of
this audit:

| Plane | Entry seam | Consults asyncssh engine? | Preamble / self-destruct wrapper? | Breaker + slot |
|---|---|---|---|---|
| **Command plane** | `infra/remote.py::ssh_run` (line 714) | YES (capture-mode only, `ssh_engine.engine_ssh_run`, line 806) | YES — `build_remote_command` wraps every cmd in `HPC_AGENT_OP=… timeout -k <grace> <deadline>s bash -c '<cmd>'` (line 248) | via `_with_ssh_backoff`→`guarded_call` |
| **Transfer plane** | `infra/transport/**` (`rsync_push`/`rsync_pull`/`_tar_ssh_push`/`tar_ssh_pull`/`_ssh_bounded`/`_remote_preclean`/`_deploy_transfer`) | **NO** — always one-shot `bounded_subprocess.run_capture_bounded` | **NO** — byte-equal raw shell, bare `python3` for manifest snippets | breaker via explicit `guarded_call` wrap at some sites; slot via `guarded_call` only |

The command plane funnels through **one** seam (`ssh_run`); the transfer plane
funnels through **another** (`run_capture_bounded`). This is already an
enforcement-mapped fact: `docs/internals/principles/lifecycle-verdicts.md`
**Row 9 (2.4)** — "Engine-seam laws EXTEND to the transfer plane … every
transfer-plane op reaches the cluster through the ONE-SHOT `run_capture_bounded`
bounded runner, NEVER `remote.ssh_run` — so it never consults the engine";
**Row 8 (2.4)** governs the pull-manifest cache; the engine-seam law itself
("Any engine failure falls back to one-shot; the asyncssh engine is never
load-bearing … CAPTURE-ONLY, gate skipped for streaming, any `EngineUnavailable`
falls straight through to the permanent one-shot hard fallback") is the
lifecycle-verdicts engine row (line ~78).

**The two-primitive contract maps cleanly onto these two planes**: the transfer
plane IS `sync-files` (already contracted, already one-shot, already resumable);
the command plane splits into `run-idempotent-script-returning-JSON` (the
reporter/scheduler-query/combiner reads) and a small residue of
`exec-command-sequence` sites that are the migration's real work.

---

## 1. Primitive seams — the substrate every site inherits from

These are not "sites" in the leaf sense; they are the machinery. Read them as
the shared failure-handling every consumer inherits (or fails to).

| Seam | file:line | Shape | Round-trips | Failure handling | Live-channel-correctness? | Idempotent? |
|---|---|---|---|---|---|---|
| `ssh_run` | `infra/remote.py:714` | exec-command / probe | 1 (+ engine fallthrough = up to 1 more one-shot) + backoff ladder (1+4 attempts) | breaker (`guarded_call`), client deadline (`SSH_TIMEOUT_SEC=60`), remote self-destruct (`timeout -k`), throttle-retry ladder, named-pipe retry, connect-rate throttle | Depends on caller cmd; the *seam* is drop-tolerant (retries idempotent, refuses non-idempotent post-dispatch) | Governed by `idempotent=`/`non_idempotent_remote()` |
| `engine_ssh_run` (asyncssh) | `infra/ssh_engine.py:942` | held command channel | 1 over a warm conn | breaker-gated open, per-cmd asyncssh timeout + in-loop `_await_bounded` + thread `future.result` backstop, keepalive death-detect, `EngineUnavailable`→one-shot | **F55 hazard**: a POST-dispatch failure of a non-idempotent cmd is refused, not re-run | Never re-runs a `dispatched` non-idempotent cmd one-shot |
| `build_remote_command` | `infra/remote.py:248` | wrapper | — | LAYER-1 remote `timeout` self-destruct + LAYER-2 `HPC_AGENT_OP` marker | — | — (inert wrapper) |
| `capture_via_select` / `_capture_windows` | `infra/remote.py:683/632` | capture reader | — | POSIX select-drain closes pipes on child exit; Windows bounded post-kill drain (the S2 wedge fix) | — | — |
| `run_capture_bounded` | `infra/bounded_subprocess.py:74` | one-shot bounded exec | 1 | **process-TREE kill** on timeout (killpg / `taskkill /T`), bounded post-kill drain, stdin=DEVNULL isolation | — | — (transport callers own idempotency) |
| `ssh_circuit.guarded_call` | `infra/ssh_circuit.py:663` | breaker + slot gate | wraps 1 attempt | consult breaker BEFORE, hold per-host slot, record outcome AFTER | — | — |
| circuit breaker | `infra/ssh_circuit.py` | persistent per-host FSM | file-backed | 3 consecutive conn-failures → open; exp cooldown; half-open single-probe; preamble-degradation classifier | — | fail-open |
| `ssh_slots` | `infra/ssh_slots.py` | cross-proc per-host slot (N=2) | file-backed | atomic O_EXCL claim, pid-liveness reclaim, bounded 120s wait, flat sub-second poll, proactive reaper | — | fail-open |
| `ssh_throttle` | `infra/ssh_throttle.py` | per-proc connect-rate cap | — | `safe_interval` (OFF by default) | — | — |
| `ssh_options.ssh_argv` / `ssh_env` | `infra/ssh_options.py:811/866` | argv/env builder | — | BatchMode, ConnectTimeout(15s), keepalives(30s×60), crypto, ControlMaster (POSIX) / named-pipe (Win) with legacy fallback, ssh-config Unix-socket-master neutralize | — | — |
| `run_with_named_pipe_retry` | `infra/ssh_options.py:322` | one-shot Win retry | +1 on marker | sticky verdict, no sleep, single retry on `getsockname failed: Not a socket` | — | — |
| `retry.run_with_retry` / `RetryPolicy` | `infra/retry.py:95/40` | retry-as-data | — | the single backoff surface `_with_ssh_backoff` builds on | — | — |

### Key structural observations at the seam layer

1. **The engine (default ON since 2026-07-16) is only ever reached by the
   command plane, capture-mode.** `ssh_run` line 801: `if capture:` gate. A
   streaming (`capture=False`) command NEVER touches the engine. The transfer
   plane never calls `ssh_run` at all. So the daemon-affinity note (step 4) is:
   a persistent-connection daemon would subsume the engine's role for the
   command plane only; the transfer plane's one-shot dials are a separate lever.

2. **Two independent timeout ladders exist and do not share a definition.** The
   command plane uses `SSH_TIMEOUT_SEC` (60s) + `_BACKOFF_DELAYS_SEC`
   (2/4/8/16); the transfer plane uses `RSYNC_TIMEOUT_SEC` (1800s), a distinct
   `PRECLEAN_TIMEOUT_SEC` (300s), and its own tight connect-retry
   (`connect_failure_retry_delays`, 1 retry × 15s ConnectTimeout). The engine
   has a THIRD set (`_connect_timeout`, `_LOOP_DEADLINE_MARGIN`,
   `_RESULT_MARGIN`, `IDLE_CLOSE_SEC`). Three parallel deadline regimes is
   itself a brittleness source (a change to one does not propagate).

3. **The named-pipe ControlMaster fallback is per-process sticky and
   window-racy.** `mark_named_pipe_broken()` flips a module global; every ssh
   site independently wraps its `_attempt` in `run_with_named_pipe_retry`. A
   site that FORGOT the wrap (see below) never demotes. This is a
   consistency-of-application risk, not a design risk.

4. **`ssh_argv` is the single argv seam — but only the command plane and scp
   use it.** rsync goes through `ssh_env()`/`RSYNC_RSH`, a parallel path with
   its own override logic. Two ways to build the ssh invocation is a
   drift surface (`_ssh_config_override_opts` vs `_ssh_multiplex_opts` must stay
   in agreement; the code notes they happen to return the same bytes on Windows
   "today").

---

## 2. Transfer plane — the `sync-files` candidate primitive

All sites route through `run_capture_bounded` (one-shot, tree-kill). None
consult the engine. None carry the self-destruct/preamble wrapper. Row 9
enforcement-mapped.

| Site | file:line | Owner | Shape | Round-trips | Failure handling | Live-channel-correctness? | Idempotent re-run? | Two-primitive fit |
|---|---|---|---|---|---|---|---|---|
| `rsync_push` | `transport/__init__.py:796` | submit staging | file-sync | 1 rsync (delta over wire) OR the rsync-less delta path (see below) | breaker (`_with_ssh_backoff`), `RSYNC_TIMEOUT_SEC`, tree-kill, named-pipe retry, connect-rate throttle | No — rsync temp+atomic-rename; a drop leaves partial delta, next call re-derives | **Yes, safe-retry** (delta re-derives) | **fits as-is** (canonical `sync-files`) |
| `rsync_push` rsync-less DELTA path | `transport/__init__.py:862–990` | submit staging (native Windows LIVE path) | file-sync (multi-step) | **many**: 1 remote hash-manifest read + N batched `tar\|ssh` pushes + per-batch push-manifest checkpoint write + prune plan (read prior manifest + `rm`) + final manifest write | each batch under `guarded_call`; batches bounded+resumable+checkpointed; prune fail-open | No — checkpoint-after-each-batch makes landed batches durable; a mid-op drop resumes | **Yes, safe-retry** (per-batch checkpoint is the design) | **fits**, but is the highest round-trip count in the stack — a prime `sync-files` consolidation target |
| `_tar_ssh_push` | `transport/__init__.py:421` | rsync_push fallback | file-sync stream (`tar c \| ssh tar x`) | 1 transfer + (if delete) stage-drop + preclean + swap = up to 4 bounded ssh | tree-kill, pump-thread deadlock guards (#9), stage-then-swap (F-G: destructive window collapsed to seconds), named-pipe retry | **Partially** — stage-then-swap means a mid-transfer drop leaves the LIVE tree intact (only staging dirty); the swap window is seconds | Yes (staging is idempotent; swap is `cp -a` merge + `rm`) | **justified-exemption** — inherently streaming pipe; gets the `sync-files` *contract* (resumable, non-destructive-until-complete) but stays a pipe |
| `tar_ssh_pull` | `transport/_pull.py:797` | harvest/aggregate pull | file-sync (batched delta) | 1 remote hash-manifest + N batched `ssh tar c \| tar x`, each with tight connect-retry | breaker (`guarded_call` inside `_pull_transfer_with_retry`), connect-retry (rank-25), tree-kill, pump guards, `is_retry_safe` classification | No — batches land directly local; a died pull re-derives landed files via next delta | **Yes, safe-retry** (local-side resumable) | **fits** (the `sync-files` pull analogue) |
| `rsync_pull` | `transport/__init__.py:1364` | harvest/aggregate pull | file-sync | 1 rsync, OR delegates to `tar_ssh_pull` on rsync-less | breaker (`_with_ssh_backoff`), `RSYNC_TIMEOUT_SEC`, tree-kill | No | Yes | **fits as-is** |
| `_remote_preclean` | `transport/__init__.py:341` | delete=True push | exec-command (find/rm) | 1 bounded ssh | own `PRECLEAN_TIMEOUT_SEC` (300s), named-pipe retry, tree-kill | Two-pass find-then-rm; only runs AFTER a complete staged transfer (F-G), so a drop mid-preclean leaves live tree partially cleaned but staging intact | Mostly (find/rmdir idempotent) | needs-migration → folds into `sync-files`'s delete semantics |
| `_ssh_bounded` (stage-drop / swap) | `transport/__init__.py:397` | tar push stage/swap | exec-command | 1 bounded ssh each | tree-kill, named-pipe retry, `PRECLEAN_TIMEOUT_SEC` | The `cp -a … && rm -rf stage` SWAP is the one genuinely destructive step; a drop mid-`cp -a` leaves a partially-merged live tree | **cp -a is NOT atomic** — partial merge possible on drop | **HEAT-MAP: needs-migration** (the one transfer-plane write with a torn-live-tree window) |
| `_rsync_deploy` | `transport/__init__.py:1064` | deploy_runtime | file-sync | 1 rsync (`-az`, no `--inplace`, no `--delete`) | breaker (`_with_ssh_backoff`), `SSH_TIMEOUT_SEC`, tree-kill | No — temp+rename atomic per file (#F20: not `--inplace` precisely so a concurrent array reading `_hpc_dispatch.py` never sees a torn file) | Yes | **fits as-is** |
| `_deploy_transfer` | `transport/__init__.py:1109` | deploy_runtime | file-sync | 1 (rsync or `tar\|ssh`) | raises on non-zero; tree-kill via inner | No | Yes | **fits** |
| `deploy_runtime` prelude | `transport/__init__.py:1319` | deploy_runtime | exec-command-sequence (mkdir+find-delete+pyc-purge+cat manifest) | 1 ssh (folded), then transfer, then manifest write | via `ssh_run` (command plane!) for the prelude; transfer via transfer plane | The prelude `rm/find` is idempotent; manifest write is SEPARATE leg AFTER transfer (#F53) so a torn deploy never leaves a manifest attesting unshipped code | Yes (idempotent prep) | **split**: prelude is `run-idempotent-script`; transfer is `sync-files` |
| `push_run_sidecar` | `transport/__init__.py:1150` | double-canary 2nd probe | exec-command (base64→file) | 1 bounded ssh | RAISES on failure (caller MUST know it landed), tree-kill, named-pipe retry | No — single atomic `base64 -d > file` write | Yes (overwrite) | **fits** (a one-file `sync-files`) |
| `_write_deploy_manifest` | `transport/__init__.py:1178` | deploy_runtime | exec-command (base64→file) | 1 bounded ssh | fail-open (suppress Timeout/OSError) | No | Yes | fits |
| `_write_push_manifest` / `_read_prior_push_manifest` / `_execute_prune` | `transport/_prune.py` | delta push bookkeeping | exec-command | 1 each | fail-open; atomic remote temp+mv | No (temp+mv atomic) | Yes | fits |
| `_remote_push_manifest` / `_remote_pull_manifest` | `transport/_delta.py`, `_pull.py:235` | delta hash | **exec-script returning JSON** (base64→`python3`, stdlib-floor, no activation) | 1 bounded ssh | returns None on any trouble → full-copy fallback | No — pure read | Yes | **fits `run-idempotent-script-returning-JSON`** (the cleanest example in the codebase) |
| `run_combiner` / `run_combiner_checked` / `run_final_reduce` | `transport/_combiner.py:42/92/135` | aggregate reduce | exec-script (cluster-side pandas reduce, **carries activation**) | 1 ssh via `ssh_run` (command plane) | stage-heartbeat, `ssh_run` deadline+breaker; combiner writes output cluster-side, caller pulls the small JSON | **Mid-reduce drop loses the computed table** unless `--force` re-run; but output is written cluster-side then pulled, so re-run is safe | Yes with `--force` (overwrites `_combiner/wave_N.json`) | **run-idempotent-script** (the reduce is idempotent given `--force`) |
| `run_combiner_batch[_checked]` | `transport/_combiner.py:241/274` | aggregate reduce (fused) | exec-script (N waves in 1 exec, sentinel-framed) | 1 ssh, N cluster-side combiner invocations | `BATCH_END_SENTINEL` = positive-evidence-of-complete-stream; its ABSENCE ⇒ truncation ⇒ per-wave fallback (E3) | **This is the model migration**: sentinel-ack detects a severed stream and refuses to trust a partial | Yes (each wave `--force`-able) | **exemplary `run-idempotent-script`** — the pattern step-2 should generalize |

**Transfer-plane exemptions (the streaming pipes):** `_tar_ssh_push` and
`tar_ssh_pull`/`_pull_transfer` move bytes through a `tar c | ssh | tar x` pipe
with a byte-counting pump thread. They cannot be reframed as a discrete
request/response and are the **justified streaming exemption**. The contract
they get instead of "one round-trip returning JSON" is: **(a) resumable** (delta
re-derives landed files/batches), **(b) non-destructive-until-complete**
(stage-then-swap on push; direct-land on pull), **(c) positive-evidence of
completion** (returncode of BOTH pipe halves folded; a pump error forces rc≠0 so
a truncated stream never reads as success). All three already hold. Step-3
fault injection must target exactly the pump/EPIPE/kill seams here.

---

## 3. Command plane — consumer call sites

Sub-sections: §3a submit leg · §3b status/scheduler-query · §3c preflight probes
· §3d submit-flow+verify · §3e detection/preflight-child · §3f monitor/reconcile/
harvest · §3g aggregate/cluster-reduce/recover.

### 3a. Scheduler submit leg (the non-idempotent apex)

| Site | file:line | Owner | Shape | Round-trips | Failure handling | Live-channel-correctness? | Idempotent? |
|---|---|---|---|---|---|---|---|
| `_execute_command` | `infra/backends/_remote_base.py:197` | RemoteSGE/Slurm submit | exec-command-sequence (`bash -lc 'echo BIN=…; cd … && qsub/sbatch'`) | 1 ssh (or 2 on stale-bin-cache 127-fallback: cached-path attempt → login-shell attempt) | **`non_idempotent_remote()` scope (line 254)** — client timeout NOT retried, post-dispatch engine failure NOT re-run one-shot (F54/F55); breaker via `ssh_run` | **THE apex hazard**: the scheduler ACCEPTS the qsub exactly once dispatched, and the remote half deliberately outlives the client by `REMOTE_DEADLINE_MARGIN_SEC` (60s). A drop AFTER dispatch but BEFORE the client reads the job-id = **a submitted array whose id we never learned** → orphan risk | **NOT safe-retry** by construction; the guard is "surface, never re-run" |
| `_setup_log_dir` | `infra/backends/_remote_base.py:295` | submit | exec-command (`mkdir -p`) | 1 ssh | via `ssh_run` (idempotent default) | No | Yes |
| `preflight_executor_exists` | `infra/backends/_remote_base.py:113` | submit stage-gate | probe (`test -f`) | 1 ssh | via injected `ssh_run` | No | Yes |
| `build_remote_backend` closure `ssh` | `infra/backends/remote_factory.py:68` | all backends | — | wraps `ssh_run(cmd, ssh_target=…)` — the single-arg callable that the `non_idempotent_remote()` ambient scope reaches | — | — | — |

**Submit-leg mid-drop note (the single most dangerous correctness window in the
stack):** the login-shell `bash -lc` first submit piggybacks `command -v qsub`
onto its own round-trip, and the job-id regex parses **stdout**. If the channel
severs between `qsub` accepting the array and stdout reaching the client, the
client sees a timeout/`EngineUnavailable(dispatched=True)` and — correctly —
refuses to re-run, but is left WITHOUT the job-id. Recovery then depends on the
scheduler-query/reconcile path finding the orphan by job-name, not job-id. This
is the primitive whose contract (`run-idempotent-script-returning-JSON`) does
**NOT** fit — submit is the codebase's one true non-idempotent actuation and is
a **justified exemption** that needs its own "submit-once, discover-id-out-of-
band" contract in step 2.

### 3b. Status / scheduler-query / marker reads (`infra/cluster_status.py`)

| Site | file:line | Owner verb | Shape | Round-trips | Failure handling | Live-channel-correctness? | Idempotent? |
|---|---|---|---|---|---|---|---|
| `ssh_status_report` | `cluster_status.py:184` | monitor/aggregate/recover status | exec-script-returning-JSON (import-guard + reporter, **carries activation**) | 1 ssh | **sentinel-ack** (`__HPC_STATUS_ACK__`) — a rc-0 read with NO ack ⇒ severed/truncated ⇒ RAISE (never parse-and-trust); reporter structured-error lifted; deterministic-vs-transient classification | **Drop-safe by refusal**: a severed stream is surfaced UNKNOWN, never read as "0 rows / all insufficient" | Yes (pure read) |
| `ssh_batch_scheduler_states` | `cluster_status.py:345` | monitor reconcile | exec-command-returning-tokens (one `qstat`/`squeue` for ALL jobs) | 1 ssh | **positive-evidence ack** (`scheduler_query_ran`) — missing ack ⇒ RAISE `SshUnreachable`, refuses to read absence as "all jobs terminal" | **Drop-safe by refusal**: prevents flipping a fleet of live runs to terminal on one blip | Yes |
| `ssh_marker_scan` | `cluster_status.py:291` | monitor terminal-state | exec-command (`ls\|grep`, **plain sh, NO activation, NO python**) | 1 ssh | `\|\| true` so rc always 0; absence proves only absence-of-failure-marker, never success | Robust to broken run-env (the whole point); absence read conservatively | Yes |
| `ssh_list_run_sidecars` | `cluster_status.py:325` | diagnostic | exec-command (`ls`, plain sh) | 1 ssh | best-effort, `[]` on error | No | Yes |

### 3c. Preflight probes (`ops/preflight/check.py`, `infra/ssh_agent.py`)

| Site | file:line | Owner | Shape | Round-trips | Failure handling | Live-channel? | Idempotent? |
|---|---|---|---|---|---|---|---|
| `_cluster_ssh_echo_check` | `ops/preflight/check.py:173` | preflight | probe (`echo ok`) | 1 ssh | `_cluster_ssh_timeout` (derived from `SSH_TIMEOUT_SEC`, NOT a tighter restated constant — the run-#8 false-trip lesson), TCP-gated | No | Yes |
| `_cluster_combined_probe` | `ops/preflight/check.py:198` | preflight | probe (echo + `command -v uv`, ONE conn) | 1 ssh (was 2 fanned) | sentinel-token presence parse, SUCCESS-only verdict cache (breaker-invalidated) | No | Yes |
| `agent_available`/`agent_detail` | `infra/ssh_agent.py:19/47` | preflight/doctor | LOCAL probe (`ssh-add -l`) | 0 remote (local pipe) | 5s timeout, FileNotFound-tolerant | No | Yes |
| bare `ssh -V` version probe | `infra/ssh_options.py:228` | multiplex/gcm capability | LOCAL probe | 0 remote | 5s timeout, `@functools.cache` | No | Yes |

### 3d. Submit-flow orchestration + verify (`ops/submit_flow.py`, `submit_and_verify.py`, `verify_canary.py`)

All submit legs inherit the `non_idempotent_remote()` scope from the backend
(§3a); every read/probe is idempotent, breaker-wrapped, ack-gated.

| Site | file:line | Owner | Shape | Round-trips | Failure handling | Live-channel? | Idempotent? |
|---|---|---|---|---|---|---|---|
| `_preflight_probe` (`ssh true`) | `submit_flow.py:198` | connectivity gate | probe | ≤2 | full guards, raises `SshUnreachable` | No | Yes |
| `_preflight_runtime_check` (`command -v uv`) | `submit_flow.py:362` | runtime gate | probe | 1 | raises `SpecInvalid` | No | Yes |
| `_run_executor_existence_preflight` | `submit_flow.py:587` | stage-gate | probe (`test -f`) | 1 per executor | full guards | No | Yes |
| `_push_and_deploy → rsync_push` | `submit_flow.py:837` | staging | file-sync | 1 (or delta path) | transfer-plane | No | Yes |
| `_push_and_deploy → deploy_runtime` | `submit_flow.py:849` | staging | file-sync fan-out | multi | transfer-plane | Partial (torn deploy re-heals via #F53) | Yes |
| `_make_single_array_submission → submit_plan/submit_one` | `submit_flow.py:1751/1784` | **main/MPI array submit** | exec-script (submit) | `_setup_log_dir` mkdir + 1 qsub per wave/batch | non-idempotent scope | **YES (apex)** | **NO** |
| `_fire_canary → push_run_sidecar` | `submit_flow.py:2494` | 2nd-canary sidecar | file-sync | 1 | raises on fail | No | Yes |
| `_fire_canary → submission` | `submit_flow.py:2529` | canary qsub | exec-script (submit) | mkdir + qsub | non-idempotent | **YES** | **NO** |
| `_pull_canary_task0_metrics` (×1/×2) | `submit_and_verify.py:176/251` | fingerprint pull | file-sync | 1 | best-effort (raise → "no sample") | No | Yes |
| `_remote_checkpoint_probe` | `verify_canary.py:406` | canary probe | probe (python 1-liner) | 1 opt | try/except → `probe_failed` | No | Yes |
| `_fetch_vtail` | `verify_canary.py:710` | canary tail | exec-command-seq (ack-wrapped) | 1 | torn read → miss-shaped fields, never false verdict | No (ack guards) | Yes |
| `_light_poll → read_announcements` / `ssh_batch_scheduler_states` | `verify_canary.py:814/832` | canary poll | probe | 1/poll | classified transient vs deterministic | No | Yes |
| `_terminal_status_report → ssh_status_report` | `verify_canary.py:876` | canary terminal | exec-script (reporter) | 1 (+1 half-open) | breaker-aware single retry; else `reporter_unreachable` | No | Yes |

*Note:* `verify_canary` = N idempotent light polls + 1 heavy reporter + 1 vtail
+ optional checkpoint probe; never-pass-unverified on any ambiguous read.

### 3e. Detection / preflight-child / scheduler-query (`scheduler_resolve.py`, `verify_submitted.py`, `submit_preflight.py`, `backends/query.py`, `runtime_preflight.py`)

| Site | file:line | Owner | Shape | Round-trips | Failure handling | Live? | Idempotent? |
|---|---|---|---|---|---|---|---|
| `probe_cluster` | `scheduler_resolve.py:153–177` | cluster scheduler detection | probe (multi) | **~6–12 sequential ssh_run** (`command -v` sbatch/qsub/bsub, banners, 4 qsub markers) | each `_run` swallows all → "absent"; inherits ssh_run guards | No | Yes |
| `verify_submitted` | `verify_submitted.py:94` | post-submit verify | probe (scheduler state) | 1 | ack-verified (`scheduler_query_ran`) — refuses to read silence as "all terminal"; rc≠0 → `SshUnreachable` | No | Yes |
| `runtime_uv_preflight` | `runtime_preflight.py:89` | uv gate | probe | 1 | raises `SpecInvalid` | No | Yes |
| `submit_preflight._run_subcalls` | `submit_preflight.py:246` | preflight fan-out | spawns child `hpc-agent` CLI procs | 4 local spawns; the `check-preflight --cluster` child does the ssh probe indirectly | 60s/child; PASS TTL-cached; **no breaker at this layer** (child owns it) | No | Yes |
| `query_sacct/pbs/sge` | `backends/query.py:162/447/713/795` | LOCAL scheduler-binary probes | probe (local subprocess) | 1 (or 1/id loop) | `timeout=30`, FileNotFound/Timeout → `{unavailable}`; **no breaker, no retry** | n/a (LOCAL; FileNotFound on a laptop — the remote path uses `ssh_batch_scheduler_states`) | Yes |

**scheduler_resolve rank note:** `probe_cluster` fires **6–12 sequential cold
dials** for scheduler detection — the second-highest round-trip site after the
rsync-less delta push, and unlike the delta path it is a pure serial probe
chain with no batching. A ban-risk and latency multiplier; a strong `run-
idempotent-script-returning-JSON` consolidation target (one `command -v` batch
+ one banner read).

### 3f. Monitor / reconcile / harvest / recover (`ops/monitor/*`, `ops/migrate/harvest.py`, `monitor_flow.py`)

Every reader is positive-evidence ack-gated: a severed rc-0 read RAISES or
degrades to UNKNOWN, never mis-settles. **Corruption risk lives in interrupted
write-sequences, not the channels.** None of these files open a `remote_op()`
or `non_idempotent_remote()` scope (the qsub-shaped watcher/resubmit legs
inherit it from the backend).

| Site | file:line | Owner verb | Shape | Round-trips | Failure handling | Live-channel-correctness? | Idempotent? |
|---|---|---|---|---|---|---|---|
| `record_status → ssh_status_report` | `monitor/status.py:174` | poll-run-status | exec-script (reporter walk) | 1 (pure-API = 0) | ack-gated severed→UNKNOWN; journal written AFTER return | No | Yes |
| `_reconcile_one` fan | `monitor/reconcile.py:584/606` | reconcile-journal | exec-command-seq + 3 concurrent probes | **~5–6 per run × (1+N siblings)**: 1 announce + 2–3 concurrent (waves/alive/report) + 0–2 serial settle/failure-feature | each future try/except → `unable_to_verify`; terminal siblings skipped | **See corruption note — the settle arm is the sharpest risk** | Yes by design EXCEPT the transition-gated harvest |
| `reconcile_stale → batch_status` | `monitor/reconcile_stale.py:187` | reconcile-stale | probe (ONE `qstat`/`squeue` per login node) | = # `(target,scheduler)` groups (~1) | `SshUnreachable` caught → leave ALL open (never actuate on blip); ack-gated | No — single probe; closes are local writes | Yes (explicit no-op re-run; no harvest/qsub) |
| `_ingest_runtime_at_terminal → rsync_pull` | `monitor/terminal.py:54` | terminal ingest | file-sync (filtered pull) | 1 | rc≠0/OSError/Timeout → 0; **best-effort, no breaker** | No — `append_sample` dedups `(run_id,task_id)` | Yes |
| `kill` sequence | `monitor/kill.py:173/267/317` | kill | exec-command (qdel/scancel) + probe + reconcile | 1 cancel + 1 alive-check + reconcile (~3–6) | intent journaled BEFORE mutation; rc of cancel IGNORED (count from alive-check); never overstates | No — drop understates, never corrupts | Yes (qdel naturally idempotent; NOT wrapped in `non_idempotent_remote`) |
| `announce.py` readers | `monitor/announce.py:112/212/277` | census | probe (`ls\|wc`, marker census, N-run fold) | 1 | rc≠0→raise; no-ack→zero-counts/not-present refuse | No | Yes |
| `wait_for_announce_change` | `monitor/announce.py:423` | census long-poll | **interactive/stream** (blocking remote `sh` sleep loop, epoch-bounded) | 1 (long) | all caught→`{woke:False}`; ack-absence→re-census; watchdog `stamp()` before dial | No — a wake is a HINT, never a settle | Yes |
| `fetch_logs / fetch_task_logs` | `monitor/logs_atom.py:184/222` | logs | exec-script / fused log-tail | 1 (all_failed=2; pure-API=0) | severed sections → `missing+ssh_error`, never "empty" | No | Yes |
| `watcher_install` ladder | `monitor/watcher_install.py:135/152/161/212/394` | watcher-install | probe + file-sync + crontab RMW + **qsub job rung** | ~3–5 serial | crontab register **fail-closed under mkdir-lock** (drop leaves table untouched); **job rung (:394) is a non-idempotent qsub** | crontab: No (locked); **job rung: post-dispatch drop can ORPHAN + re-install DUPLICATES watchers** | crontab yes (marker-dedup); **job rung NO** |
| `update_run_constraints` | `monitor/update_constraints.py:155` | update-run-constraints | exec-command-seq (`scontrol update` PER job) | **= len(job_ids), serial** | per-job try/except → failed, continue; feature+jid regex injection-guarded | No — drop mid-loop leaves some jobs updated; scontrol idempotent; sidecar may drift | Yes on (run_id, target feature set) |
| `multi_parent_harvest → rsync_pull` | `migrate/harvest.py:357` | migrate-remainder harvest | file-sync (2 filtered parent pulls) | **2** | rc≠0 → `RemoteCommandFailed` **REFUSE** (never fabricate over partial mirror); tree-kill via bounded runner | No — read-only; drop→partial→refuse | Yes (pure read + deterministic ownership reduce; actuates nothing) |
| `monitor_flow` ticks | `monitor_flow.py:404/259/322/847/1135/217/901…/1552/1256/1329` | monitor-flow | probe/exec-script/file-sync/long-poll + terminal harvest + resubmit qsub | 0–1 per leg per tick | rich: transient swallowed+retried; `SshCircuitOpen`→cooldown wait + `pool_failover`; deterministic-env→escalate after 3; `harvest_on_terminal` in `finally` + abnormal-exit positive-evidence gate | No (best-defended terminal path) | Yes (harvest ledger append-only; resubmit composites journal idempotently) |

**NEW corruption finding — reconcile settle arm (not a channel bug, a
write-sequence-interruption bug):** `reconcile.py` settle arms
(`:388–405`, `:987–1005`, `:1080–1104`, and `_settle_from_announcements`) do
`update_run_status(last_status)` → `mark_run(terminal)` →
`harvest_on_terminal(...)`, and the harvest is **transition-gated**
(`if str(verdict) != pre_reconcile_status`). A session-death/exception AFTER
`mark_run` writes terminal but BEFORE `harvest_on_terminal` completes leaves the
run **terminal-with-no-harvest**, and a re-reconcile sees no transition and
**will NOT re-fire the harvest** — the guaranteed harvest is silently dropped.
Unlike `monitor_flow`, reconcile has **no `finally`/abnormal-exit backstop**
around this sequence. This is the one place "idempotent re-run" does not
self-heal, and it is a live-channel-mid-op exposure (the control session is the
"channel" here). → heat-map rank, and a step-2/step-3 target.

### 3g. Aggregate / cluster-reduce / recover (`ops/aggregate/*`, `aggregate_flow.py`, `ops/recover/*`, `infra/inspect/*`)

All `ssh_run` sites inherit the full stack; none use `non_idempotent_remote()`
(all reduce/probe here are `idempotent=True`).

| Site | file:line | Owner | Shape | Round-trips | Failure handling | Live-channel-correctness? | Idempotent? |
|---|---|---|---|---|---|---|---|
| `cluster_reduce` | `aggregate/cluster_reduce.py:307` + pull `343/352` | cluster-reduce mode | exec-command-seq (`cd&&mkdir&&<activation>&&export&&<aggregate_cmd>`) + rsync_pull | 2 | full stack; carries `remote_activation_for_sidecar` | No — but runs **arbitrary user `aggregate_cmd`** which may write NON-atomically; torn file never pulled (rc-gated) + force re-run overwrites | Yes; **no partial-reduce resume — a drop re-pays the full ≤1800s reduce** |
| `combine_wave → run_combiner_checked` | `aggregate/combine.py:224` | per-wave combine | exec-script | 1 (journal-hit = 0) | full stack; combiner writes `wave_N.json` atomically (mkstemp+os.replace) | No — atomic cluster write + deterministic | Yes (key `(run_id,wave)`) |
| `combine_waves → run_combiner_batch_checked` | `aggregate/combine.py:321` | fused N-wave combine | exec-command-seq (sentinel-framed) | 1 fused | missing `__HPC_BATCH_END__` → per-wave fallback (E3) | No | Yes |
| `verify_per_task_outputs` | `aggregate/runner.py:100` | pre-reduce probe | probe (ack-gated `[ -f ]` loop) | 1 | **rc-0-no-ack ⇒ RAISES** (won't read severed silence as "all present") | No (ack is the truncation defense) | Yes |
| `_read_remote_sidecar` / `verify_combiner_artifact` / `write_remote_provenance` | `aggregate/runner.py:37/159/220` | pre/post probes + provenance write | probe / exec-command (base64→file) | 1 each | full stack; provenance is best-effort overwrite | No | Yes |
| `local_reduce` | `aggregate/local_reduce.py:110` | pure-API reduce | exec-script **LOCAL** (`shell=True`, cwd) | **0 network** | `timeout`, no breaker/tree-kill | n/a (local) | Yes |
| `aggregate_stream` census + pull | `aggregate/stream.py:163/191/272` | streaming aggregate | probe + file-sync (summary-only, `include=[summary]`) | ≈2N for N parents | non-zero pull REFUSES (never reduce over partial mirror) | No | Yes |
| `aggregate_flow` harvest engine | `aggregate_flow.py:489/537/667/956/1176/1198/1379/1828/1932/1952/2172` | aggregate-flow | probe + exec-script (combine/final-reduce) + file-sync (pulls) | many (per-wave combine loop + final reduce=2 + summary/per-task pulls + fingerprint memo) | full stack; ack-gated reads RAISE on severed; cardinality gate refuses foreign rows; agg-memo fingerprint failure → memo inert | No — atomic cluster writes; pulls re-derive | Yes (key `run_id`) |
| `_remote_tree_fingerprint` (agg-memo) | `aggregate_flow.py:1379` | memo | probe (`find\|sort\|sha256sum`, metadata-only) | 1 | any failure → `None` → memo inert (never blocks) | No | Yes |
| `stray_sweep` | `recover/stray_sweep.py:165/186` | stray-sweep | probe (`ps -u`) + exec-command (`kill` marked strays if reap) | 1 (+1 reap) | full stack; reap errors suppressed best-effort; `op="stray-sweep"` | No — drop → strays remain, next sweep re-checks | Yes (kill idempotent, only marked+over-age) |
| `_clear_failed_markers` | `recover_flow.py:247` | resubmit prep | exec-command (`rm -f -- <markers>`) | 1 | full stack; caught → warn, best-effort | No | Yes (`rm -f` of absent = no-op) |
| `fetch_failures → ssh_status_report` / `fetch_task_logs` | `recover/failures_atom.py:120/159` | failures | probe (reporter + per-task log fetch) | ≥1 (scales with #failed tasks) | full stack; activation seeded | No (read) | Yes |
| `inspect_deployment` / `dir_digest._digest_remote` | `inspect_deployment.py:244`, `dir_digest.py:492` | inspect/digest | probe (`test -e`/`find`, fixed digest script) | 1 | full stack; scratch-confined + shell-quoted; sentinel-presence tolerates chatter | No | Yes |
| `_CommandRunner.run` (slurm/sge/pbs inspect) | `infra/inspect/_common.py:172` (+ slurm.py:250/286, sge.py:168, pbs.py:119) | cluster inspect | probe (merged echo-delimited scheduler queries) | 1–2 (slurm=2, others=1, #295 merged) | full stack; TimeoutError→124, missing bin→127 | No (single-shot reads) | Yes |
| **`_open_log` (TUI `l` keybind)** | `execution/mapreduce/reduce/tui.py:384/390` | interactive log view | **RAW `subprocess.call(["ssh",…,"less <log>"])`** | 1 (interactive) | **BARE — no breaker, no timeout, no tree-kill; blocking; FileNotFoundError swallowed** | Yes — interactive pager holds channel open by design | Yes (read-only) |
| `net_triage` probes | `recover/net_triage.py:201/216/237` | net-triage | probe (HTTPS GET / getaddrinfo / raw `create_connection` to :22) | 1 each per host | bounded by construction; **deliberately breaker-free but single-shot**; TCP probe SKIPPED when breaker open+cooling or DNS failed (reads breaker state, refuses to burn its half-open probe) | No | Yes (`side_effects=[]`) |
| `doctor` / `doctor_install` / `notify` | `recover/doctor.py`, `doctor_install.py:79`, `notify.py:161` | recover | LOCAL only (`git rev-parse`, `schtasks`/`crontab`, OS notifier) | **0 network** (doctor is "No SSH" by contract) | local bounded, fail-open | n/a | Yes |

**Cluster-reduce mid-op-drop verdict (the ruled-important hazard):** the three
reduce seams (`run_final_reduce`/`_cluster_final_reduce`, `run_combiner*`,
`cluster_reduce`) all (a) **carry activation** (`remote_activation_for_sidecar`
with `fallback_cluster` backfill, so a degraded login node's Lmod/conda is the
failure surface, run-13 class), and (b) do **NOT** lose/corrupt the table on a
drop — the framework combiner writes atomically (mkstemp+fsync+os.replace), the
reduce is deterministic (Neumaier-compensated, sorted iteration), and every seam
is `idempotent=True`/`--force`. **BUT there is no partial-reduce
checkpoint/resume**: LAYER-1 `timeout -k` reaps the orphaned reducer after
`client+60s` and the client re-runs the **entire ≤1800s reduce from scratch**.
So the hazard is **delay + full recompute**, not wedge/corruption — except the
`cluster_reduce` arbitrary-user-`aggregate_cmd` edge, which may write its output
non-atomically (torn file, never pulled because pull is rc-gated, force re-run
overwrites).

---

## 4. Brittleness heat-map — ranked next-bug predictions

Ranking rule: **multi-round-trip × live-channel-mid-op-dependent × bare (or
inconsistent) failure handling.** The important qualifier the sweeps confirmed:
the *channels* are almost universally safe (every reader is positive-evidence
ack-gated → a severed read RAISES/UNKNOWN, never mis-settles). So the real
next-bug surface is **interrupted write/actuate-sequences** and the few sites
missing the standard guards, not the reads.

**Rank 1 — Scheduler submit leg** (`_remote_base.py:197`, reached from
`submit_flow.py:1751/1784/2529`, `watcher_install.py:394`). Non-idempotent by
construction; the qsub's remote half outlives the client by 60s. A post-dispatch
drop cannot DUPLICATE (the `non_idempotent_remote()` guard stops re-run) but can
**ORPHAN**: the job-id is parsed from stdout and the sidecar/journal write
happens AFTER `_execute_command` returns, so a drop in the accept→id-read window
leaves a live array with no local record. Highest severity (real compute
stranded); recovery is out-of-band (job-name reconcile) and untested under
injection.

**Rank 2 — reconcile settle arm** (`reconcile.py:388–405/987–1005/1080–1104`,
`_settle_from_announcements`). `update_run_status → mark_run(terminal) →
harvest_on_terminal`, harvest **transition-gated**. A session-death/exception
between `mark_run` and harvest leaves the run **terminal-with-no-harvest**, and
re-reconcile sees no transition and **never re-fires the harvest** — the
guaranteed harvest is silently dropped. **No `finally`/abnormal-exit backstop**
here (unlike `monitor_flow`). The one place "idempotent re-run" does not
self-heal → a genuine latent correctness bug, not just a prediction.

**Rank 3 — `_tar_ssh_push` stage-swap `cp -a … && rm -rf stage`**
(`transport/__init__.py:318`/`684`). The one transfer-plane write with a
**non-atomic torn-live-tree window** — a drop mid-`cp -a` leaves a partially
merged live tree that a concurrent array could import. Every other transfer step
is atomic (temp+rename) or staged; this one is not.

**Rank 4 — `rsync_push` rsync-less delta path**
(`transport/__init__.py:862–990`). The **highest round-trip count anywhere**
(manifest read + N batches + N−1 checkpoints + prune read + rm + final write)
AND the **live native-Windows push path** (the primary demo/relay environment).
Well-guarded (checkpointed/resumable) but every extra dial is another
intrusion-filter count and another drop-point — a ban-risk + latency multiplier.

**Rank 5 — `scheduler_resolve.probe_cluster`** (`scheduler_resolve.py:153–177`).
**6–12 sequential cold dials** for scheduler detection, a pure serial probe
chain with no batching — the second-highest round-trip site and a burst pattern
the slot limiter/breaker were built to prevent. Latency + ban-risk; trivially
consolidatable to 1–2 dials.

**Rank 6 — cluster-side reduce, no-resume + preamble-degradation**
(`transport/_combiner.py`, `aggregate_flow.py:1176`, `cluster_reduce.py:307`).
Carries conda/module activation → a degraded login node's Lmod/conda wedges it
(run-13 class). No corruption (atomic + deterministic + `--force`) but a drop
re-pays the **entire ≤1800s reduce** with no checkpoint. The `cluster_reduce`
arbitrary-`aggregate_cmd` edge can also write non-atomically.

**Rank 7 — `_open_log` TUI raw-ssh pager** (`tui.py:384/390`). The **single
truly BARE site** in the codebase: raw `subprocess.call(["ssh",…,"less"])` with
NO breaker, NO timeout, NO tree-kill, blocking, `FileNotFoundError` swallowed. It
routes argv through `ssh_argv` but bypasses the entire backoff/breaker/slot
stack. Low blast radius (interactive convenience, holds the channel open by
design) but it is the one command-plane hole in the "every dial is guarded"
invariant — a contract test should either wrap it or explicitly exempt it.

**Rank 8 — `watcher_install` job rung** (`watcher_install.py:394`). A
non-idempotent qsub whose post-dispatch drop can orphan a watcher job, and whose
**re-install DUPLICATES watchers** (each install submits a new self-resubmitting
job). Same class as rank 1 but lower stakes.

*Deliberately NOT ranked (correct by design):* `net_triage` breaker-free
single-shot socket probes (bounded, skip-when-cooling); `terminal.py` /
`migrate/harvest.py` / `reconcile_stale` refuse-or-dedup reads; every ack-gated
reporter/scheduler-query read.

---

## 5. Migration work-list toward the two primitives

### Mechanical (shape already fits; consolidation only)
- `rsync_push` / `rsync_pull` / `_rsync_deploy` / `_deploy_transfer` → the
  canonical `sync-files`. Already one-shot, resumable, atomic-per-file.
- `_remote_push_manifest` / `_remote_pull_manifest` → the canonical
  `run-idempotent-script-returning-JSON` (base64→`python3`, stdlib-floor).
- `ssh_status_report` / `ssh_batch_scheduler_states` → `run-idempotent-script-
  returning-JSON` with the sentinel-ack ALREADY implemented (the reference
  pattern; step-2 should extract the ack wrapper as the primitive's spine).
- `push_run_sidecar` / `_write_deploy_manifest` / `_write_push_manifest` →
  single-file `sync-files`.

### Design-needed
- **Submit leg** (`_remote_base.py:_execute_command`): needs a bespoke
  "submit-once, discover-id-out-of-band" contract — it is NOT
  `run-idempotent-script`. The `non_idempotent_remote()` guard + job-name
  reconcile is the seed; formalize the id-discovery leg.
- **`_tar_ssh_push` stage-swap**: the `cp -a && rm` swap needs an atomic-rename
  discipline (or a marker-guarded two-phase commit) to close rank-2's window.
- **deploy_runtime**: it straddles both planes (prelude via command plane,
  transfer via transfer plane, manifest via transfer plane). Splitting it into
  a `run-idempotent-script` (prep) + `sync-files` (transfer) + `sync-files`
  (manifest) is a design decision about ordering guarantees (#F53 must survive).
- **Three parallel deadline regimes** (command 60s ladder / transfer 1800s+300s
  / engine `_connect_timeout`+margins): step-2 should decide whether the two
  primitives share ONE deadline-derivation seam.
- **Two argv-build paths** (`ssh_argv` for ssh/scp vs `ssh_env`/`RSYNC_RSH` for
  rsync): consolidate so the primitive has one connection-option owner.
- **reconcile settle-arm backstop** (rank 2): the transition-gated harvest needs
  a re-drivable trigger (a `finally`/abnormal-exit backstop or a harvest-owed
  ledger) so a session-death between `mark_run` and `harvest_on_terminal` does
  not silently drop the guaranteed harvest.
- **`scheduler_resolve.probe_cluster`** (rank 5): 6–12 serial dials → one
  `run-idempotent-script-returning-JSON` batch.

---

## 6. Reusable assets and where each is inconsistently applied

| Asset | Definition | Applied consistently? | Gap |
|---|---|---|---|
| `run_capture_bounded` (tree-kill one-shot) | `bounded_subprocess.py:74` | YES across the whole transfer plane (Row 9 pins it) | Command-plane `ssh_run` does NOT use it — it has its own `capture_via_select`/`_capture_windows`. Two capture readers. |
| `run_with_named_pipe_retry` (Win sticky fallback) | `ssh_options.py:322` | Applied at `ssh_run`, `rsync_push`, `_tar_ssh_push`, `_remote_preclean`, `_ssh_bounded`, `_ssh_capture`, `_pull_transfer` | **Verify no ssh-touching site forgot the wrap** — the #173 preclean was "the LAST ssh-touching surface that wasn't wrapped" per its own comment; a future site can regress silently. A contract test should enforce it. |
| `guarded_call` (breaker + slot) | `ssh_circuit.py:663` | `ssh_run` always; transfer plane only where explicitly wrapped (`rsync_push` full-copy fallback, each delta batch, `_pull_transfer_with_retry`) | **Inconsistent**: `_remote_preclean`/`_ssh_bounded`/`push_run_sidecar`/`_write_*_manifest` call `run_capture_bounded` directly with NO `guarded_call` → these dials bypass the breaker and the slot limiter. **And `tui.py::_open_log` (rank 7) bypasses the entire stack with raw `subprocess.call(["ssh",…])`** — the one command-plane hole. `net_triage` is breaker-free by design (single-shot). A contract test should enforce "every ssh-touching site routes through `guarded_call` OR is a named exemption." |
| sentinel-ack (positive-evidence-of-complete-stream) | `cluster_status.py` (`split_ack`), `_combiner.py` (`BATCH_END_SENTINEL`) | Applied to status report, scheduler-query, combiner-batch | NOT applied to the single-wave `run_combiner`, nor to any generic `ssh_run` exec. The migration should make the ack the primitive's default. |
| `non_idempotent_remote()` scope | `remote.py:204` | ONLY the submit leg | Correct — it is the only non-idempotent actuation. But it is ambient/contextvar-threaded through a single-arg callable; fragile to a refactor that bypasses `ssh_run`. |
| circuit breaker + slots + throttle | `ssh_circuit`/`ssh_slots`/`ssh_throttle` | breaker/slots via `guarded_call`; throttle called directly in transfer entry points | throttle is OFF by default and only in transfer entry points, not the command plane's `ssh_run` (which calls `throttle_connection` at line 833 — actually present). Slots bypassed on the direct-`run_capture_bounded` sites (above). |
| retry-as-data (`RetryPolicy`/`run_with_retry`) | `retry.py` | command-plane backoff only | transfer-plane connect-retry (`_pull_transfer_with_retry`) hand-rolls its own loop instead of using `run_with_retry`. |
| detached-worker pattern | `_kernel/lifecycle/detached.py`, `heartbeat.py` | the async/park-and-poll execution model | (daemon-affinity note: a daemon would replace the per-op dial, but the detached-worker + journal-resume pattern is the existing "survive a dropped control channel" mechanism — step-4 should build ON it, not replace it) |

---

## 7. Fault-injection test-point inventory (step-3 spec seed)

Each seam below exercises a DISTINCT failure path; a sever/hang/garbage
injection at each is a separate drill.

| Injection point | Seam | Distinct path exercised |
|---|---|---|
| Sever mid-`conn.run` on warm engine conn | `ssh_engine._do_run` | `EngineUnavailable(dispatched=True)` → non-idempotent refusal vs idempotent one-shot fallthrough |
| Sever during engine connect | `ssh_engine._open` | breaker record (throttle vs fatal classification), slot release on connect-fail |
| Hang the engine loop (never-returning coro) | `ssh_engine._submit` backstop | `future.result` timeout → `EngineUnavailable(event loop wedged)` |
| Sever in qsub dispatch→job-id window | `_remote_base._execute_command` | **submit orphan** — the apex drill; verify no re-run + job-name reconcile finds it |
| rc-0 read but drop the ack line | `cluster_status.ssh_status_report` | `ack_rc is None` → RAISE (never parse truncated) |
| Drop the scheduler-query ack | `ssh_batch_scheduler_states` | `SshUnreachable` (never "all jobs terminal") |
| Truncate the fused combine stream (no `BATCH_END_SENTINEL`) | `_combiner._parse_batch_output` | per-wave fallback (E3) |
| Kill ssh mid-`tar\|ssh` push | `_tar_ssh_push` | pump EPIPE → pump_error → rc forced ≠0; tar reaped; staging intact |
| Kill ssh mid-pull | `_pull_transfer` | inverted pump; partial batches land, next delta re-derives |
| Timeout mid stage-swap `cp -a` | `_ssh_bounded` swap | **torn live tree** (rank-2 — currently NOT closed; the drill should FAIL until fixed) |
| Named-pipe `getsockname` failure injected in stderr | `run_with_named_pipe_retry` | sticky verdict flip + single rebuild-retry |
| 3 consecutive connect failures | `ssh_circuit` | breaker opens; next attempt fast-fails; half-open probe |
| Slot exhaustion (N=2 held) under a 3rd acquirer | `ssh_slots.acquire_slot` | bounded 120s wait, disclosure, `SshSlotWaitTimeout` |
| Preamble hang (module load / conda) with cheap-probe-OK | `ssh_circuit.is_preamble_degraded` | degradation classifier → host-retarget advice |
| Remote self-destruct fires (rc 124) | `build_remote_command` `timeout -k` | orphan bound; caller classifies 124 transient |
| NAT idle-drop on long-silent leg | keepalives (`_ssh_keepalive_opts` / engine keepalive) | channel stays alive vs library-declared-dead |
| Session-death between `mark_run(terminal)` and `harvest_on_terminal` | `reconcile.py` settle arm | **rank-2 latent bug** — verify re-reconcile does NOT re-fire the transition-gated harvest (drill should FAIL until a backstop is added) |
| Drop mid `cluster_reduce` arbitrary `aggregate_cmd` write | `aggregate/cluster_reduce.py:307` | non-atomic user output torn on cluster; verify never pulled (rc-gate) + force re-run overwrites |
| Re-run `watcher_install` | `watcher_install.py:394` | duplicate watcher jobs (non-idempotent qsub) |
| Sever mid-scheduler-detection dial chain | `scheduler_resolve.probe_cluster` | partial "absent" verdict from a mid-chain drop (each `_run` swallows → absent) |
| Kill/hang the raw TUI ssh pager | `tui.py::_open_log` | **bare site** — no timeout/breaker/tree-kill; verify it cannot wedge the TUI or leak an ssh |

---

## 8. Known-accepted exemptions (with the contract they get instead)

| Exemption | Why it can't be `run-idempotent-script-returning-JSON` | Contract it gets instead |
|---|---|---|
| `_tar_ssh_push` / `tar_ssh_pull` / `_pull_transfer` (tar-stream pipes) | Inherently a streaming byte pipe with a pump thread; no discrete request/response | **`sync-files` streaming variant**: resumable (delta) + non-destructive-until-complete (stage-swap / direct-land) + positive-evidence completion (both-halves rc + pump-error fold) |
| Scheduler submit leg (`_execute_command`) | The ONE genuinely non-idempotent actuation — the scheduler accepts exactly once and the remote half outlives the client | **"submit-once, discover-id-out-of-band"**: `non_idempotent_remote()` (never re-run post-dispatch) + job-name reconcile for id discovery |
| `run_combiner*` cluster-side reduce | Runs the real pandas/scientific reducer on the login node; needs conda/module activation (not stdlib-floor) | **`run-idempotent-script` with `--force` idempotency**: output written cluster-side then pulled; re-run overwrites `_combiner/wave_N.json` |
| Streaming `ssh_run(capture=False)` | Inherits parent stdio; the engine channel cannot frame it | Stays one-shot binary path; never consults engine (enforcement-mapped) |
| Local probes (`ssh-add -l`, `ssh -V`) | No remote host | Bounded 5s local subprocess; cached |

---

## 9. Proposed step-2 unit list (contract-the-transport-to-two-primitives)

Each unit: sites covered · mechanical-or-design · risk.

| Unit | Sites | Mechanical / Design | Risk |
|---|---|---|---|
| **U1 — `sync-files` primitive extraction** | `rsync_push`, `rsync_pull`, `_rsync_deploy`, `_deploy_transfer`, `push_run_sidecar`, `_write_*_manifest` | Mechanical (shapes fit) | Low — pure consolidation; regression risk in the delta/checkpoint bookkeeping |
| **U2 — `run-script-returning-JSON` primitive + sentinel-ack spine** | `ssh_status_report`, `ssh_batch_scheduler_states`, `_remote_{push,pull}_manifest`, `run_combiner*` | Mechanical spine, Design for ack-as-default | Medium — must preserve the exact severed-stream-refusal semantics (Row-mapped); extracting the ack wrapper touches the most correctness-critical reads |
| **U3 — submit-once contract** | `_execute_command` submit leg | Design | **High** — the non-idempotent apex; the id-discovery leg is the real new design; needs the step-3 dispatch-window drill to validate |
| **U4 — stage-swap atomicity fix** | `_tar_ssh_push` `cp -a && rm` swap, `_ssh_bounded` | Design | Medium — close the torn-live-tree window (rank-2); atomic-rename or two-phase marker |
| **U5 — breaker/slot uniformity + one contract test** | direct-`run_capture_bounded` sites missing `guarded_call` (`_remote_preclean`, `_ssh_bounded`, `push_run_sidecar`, `_write_*_manifest`) + the bare `tui.py::_open_log` (rank 7) + a contract test enforcing "every ssh-touching site routes through `guarded_call` + named-pipe-retry OR is a named exemption" | Mechanical + Design (contract test) | Low-Medium — wrapping adds slot pressure; must confirm no deadlock with the delta path already holding a dial |
| **U6 — single deadline-derivation + single argv-owner** | the three deadline regimes; `ssh_argv` vs `ssh_env` | Design | Medium — cross-cutting; a wrong unification could regress a platform-specific override (Windows named-pipe / ssh-config neutralize) |
| **U7 — engine/one-shot seam preservation** | `ssh_run` engine gate | Mechanical (keep as-is under the primitives) | Low — the engine is already never-load-bearing; the primitives sit ABOVE it |
| **U8 — reconcile settle-arm harvest backstop** | `reconcile.py` settle arms (rank 2) | Design | **High** — a latent correctness bug (terminal-with-no-harvest); needs a `finally`/abnormal-exit or re-drivable-harvest ledger like `monitor_flow`'s; the transition-gate must not be the only trigger |
| **U9 — scheduler-detection dial consolidation** | `scheduler_resolve.probe_cluster` (rank 5), the inspect merges (already #295) | Mechanical | Low — fold 6–12 `command -v`/banner dials into one `run-idempotent-script-returning-JSON` |

**Daemon (step-4) affinity note only (daemon ruled OFF):** U2's `run-script-
returning-JSON` and the command plane's engine are the surfaces a persistent
daemon would subsume (one warm connection replacing per-op dials). The transfer
plane (U1) and the submit-once contract (U3) are daemon-orthogonal — they stay
one-shot/actuation regardless. Build step-4 ON the detached-worker + journal-
resume pattern (§6), not as a replacement.
