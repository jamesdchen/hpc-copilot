# Runbook: live-verify campaign async-refill (RFC §10 gate)

> **This runbook is the gate that flips async refill (#362) from "experimental"
> to "shipped".** It is **not runnable offline.** Async refill's mechanism is
> fully unit-tested, but its *payoff* — a pool that stays full across iteration
> boundaries — is only observable on a real cluster. Per
> [`docs/design/campaign-async-refill.md`](../design/campaign-async-refill.md) §10
> and the [implementation plan](../design/history/campaign-async-refill-implementation-plan.md)
> Phase 3, the feature stays experimental until **all four** criteria below pass
> on a real **CARC** or **Hoffman2** campaign. Green unit tests are necessary,
> not sufficient.

The helper [`scripts/campaign_async_live_verify.py`](../../scripts/campaign_async_live_verify.py)
is a **measurement aid**, not an automated pass: it samples the live campaign
through the real `hpc-agent` CLI verbs and emits per-criterion PASS / FAIL /
NEEDS-HUMAN with the evidence it sampled. It never fabricates a cluster result —
the kill (criterion 2) and byte-for-byte artifact equality (criterion 3) are
yours to perform / judge. It exits non-zero if a measurable criterion fails.

---

## Acceptance criteria (none skippable)

| # | Criterion | How it's judged |
|---|-----------|-----------------|
| 1 | **Pool occupancy ≈ K** across iteration boundaries (no drain-to-zero); measurably higher utilization than the synchronous baseline on the same straggler-heavy workload. | measured (script) + compared to the baseline run |
| 2 | **Crash-safe resume** — kill `hpc-campaign-driver` mid-stream, restart; it reconstructs in-flight/told sets from `.hpc/` with **no stranded** and **no double-told** trials. | interactive (you kill) + measured diff (script) |
| 3 | **Default unchanged** — the same campaign with `async_refill` **off** reproduces today's synchronous batch behavior byte-for-byte. | measured drain-to-zero (script) + **human** artifact diff |
| 4 | **Polling within the connection-storm envelope** (#346) — one `qstat`/login-node query per group regardless of in-flight count. | measured group cardinality (script) + **human** qstat count in logs |

---

## Prerequisites

- An **onboarded** experiment repo (`.hpc/tasks.py` exists) reachable from the
  submit host, with working cluster creds for `--cluster carc` **or**
  `--cluster hoffman2` (see the demo-env notes). Confirm with
  `hpc-agent load-context --experiment-dir .`.
- `optuna` installed in the submit-host env (the async strategy + the script's
  best-effort double-told cross-check both read the optuna study).
- The async optuna strategy scaffolded (Step 1).
- A **deliberately heterogeneous** workload (Step 0) — without stragglers there
  is nothing for async refill to win over the synchronous baseline.

---

## Step 0 — make the workload straggler-heavy

Async refill only pays off when trials finish out of order. Engineer **one slow
outlier per batch of K** so the synchronous loop visibly idles on it while the
async loop refills around it. The cheapest way is a deliberate sleep in your
task keyed off a proposed param, e.g. in `src/train.py`:

```python
# Make ~1 in K trials a slow outlier so the pool has something to drain on.
import os, time
if int(os.environ.get("HPC_TASK_ID", "0")) % 4 == 0:
    time.sleep(20 * 60)   # 20-min straggler; the other 3 finish in ~2 min
```

Pick durations so the straggler is ~10× the median trial. Keep this identical
between the async run and the baseline run — the comparison is only valid on the
**same** workload.

## Step 1 — scaffold the async strategy + init the async manifest

```bash
# Emit the continuous-async optuna variant (tell-by-trial_token + constant_liar):
hpc-agent scaffold-strategy --name optuna --async-refill --output-dir .

# Write a campaign manifest with the async opt-in and the pool target K=4:
hpc-agent campaign-init --experiment-dir . --campaign-id ebm_carc \
    --async-refill --max-in-flight 4 \
    --metric val_loss --target 0.0 --direction minimize \
    --max-iters 40 --max-jobs 40
```

Sanity-check the manifest carries the **top-level** opt-in:

```bash
cat .hpc/campaigns/ebm_carc/manifest.json   # expect "async_refill": true, "max_in_flight": 4
```

And that `campaign-advance` engages the refill ladder (before any submit it may
return `refill` with a `refill_count`, or `continue`):

```bash
hpc-agent campaign-advance --experiment-dir . --campaign-id ebm_carc \
    --async-refill --max-in-flight 4
# expect data.decision == "refill" (data.refill_count up to K) or "wait_in_flight"
# when the pool is full — NEVER a sync-only decision while slots are free.
```

## Step 2 — start the driver looping

The driver advances **exactly one step per tick**, stateless across ticks,
disk-as-truth — so the continuous loop is just a `/loop` (or cron) around it. Do
**not** build or expect a daemon.

```bash
/loop 30m hpc-campaign-driver --experiment-dir . --allow-agent-steps
```

Let it run until the pool fills (≈ K trials in flight). Confirm:

```bash
hpc-agent campaign-status --experiment-dir . --campaign-id ebm_carc
# expect data.in_flight to climb toward 4 as refill submits new iterations
```

---

## Criterion 1 — pool occupancy ≈ K (no drain-to-zero)

With the driver looping and the pool warm, in a second shell:

```bash
.venv/Scripts/python.exe scripts/campaign_async_live_verify.py \
    --experiment-dir . --campaign-id ebm_carc --cluster carc \
    --max-in-flight 4 --samples 16 --interval 30 --skip-crash-safe
```

(Use `--skip-crash-safe` here to run only the occupancy + poll checks; do the
crash-safe check in its own pass below.) Size `--samples × --interval` to span at
least one straggler completion so an **iteration boundary** is observed.

**Expected:** `in_flight` oscillates around 4 and **never returns to 0** while
the straggler is running; `iterations` keeps advancing (new trials submitted
around the straggler).

**PASS:** the window spanned an iteration boundary, `in_flight_min > 0`, and
`in_flight_mean ≈ K`. With a baseline summary present (Criterion 3), the async
mean is **strictly higher** than the baseline mean.

**FAIL:** `in_flight` drained to 0 between iterations, or `campaign-advance`
did not return `refill`/`wait_in_flight` (the async ladder isn't engaged — check
the manifest), or the async mean was not higher than the baseline.

The script writes `.hpc/live-verify/ebm_carc.async.json` for the Criterion-3
comparison.

---

## Criterion 2 — crash-safe resume (no stranded / double-told)

Run the script **without** `--skip-crash-safe`, on a TTY, while the driver is
looping and the pool is full:

```bash
.venv/Scripts/python.exe scripts/campaign_async_live_verify.py \
    --experiment-dir . --campaign-id ebm_carc --cluster carc --max-in-flight 4 \
    --settle 180
```

It snapshots the in-flight/told sets, then **prompts you to kill the driver
mid-stream and restart it**:

1. While trials are in flight, **stop the running driver** (Ctrl-C the `/loop`,
   or kill the cron tick / PID). The BEFORE snapshot shows the in-flight set you
   are interrupting.
2. **Restart it identically:** `/loop 30m hpc-campaign-driver --experiment-dir . --allow-agent-steps`.
3. Press ENTER; the script waits `--settle` seconds for the restarted driver to
   reconcile from `.hpc/`, then re-snapshots and diffs.

**Expected:** the restarted driver reconstructs the in-flight/told sets from
`.hpc/` alone — every interrupted trial either completes or is still tracked in
flight; the optuna study's COMPLETE-trial count equals the completed-iteration
count (no re-tell).

**PASS:** no vanished sidecars, no stranded run_ids (a before-in-flight run that
is afterward in neither the completed nor the in-flight set), completed count did
not regress, and study COMPLETE trials == completed records.

**FAIL:** any stranded/vanished trial, a regressed completed count, or the study
has **more** COMPLETE trials than completed records (a double-tell). If `optuna`
can't be read on the submit host, the script reports NEEDS-HUMAN and tells you to
inspect `.hpc/campaigns/ebm_carc/optuna.db` by hand.

---

## Criterion 3 — default-off reproduces synchronous behavior

Repeat the **same** workload (Step 0 unchanged) with async **off**. Either init a
sibling sync campaign or re-init this one without the async flags:

```bash
hpc-agent campaign-init --experiment-dir . --campaign-id ebm_carc_sync \
    --metric val_loss --target 0.0 --direction minimize --max-iters 40 --max-jobs 40
# (no --async-refill / --max-in-flight => synchronous staged barrier)
```

Start a driver for it, let it run, then:

```bash
.venv/Scripts/python.exe scripts/campaign_async_live_verify.py \
    --experiment-dir . --campaign-id ebm_carc_sync --cluster carc \
    --baseline --samples 16 --interval 30
```

**Expected:** the synchronous staged barrier — `in_flight` rises to 1 for an
iteration then **drains to 0** before the next iteration is proposed (the
sawtooth that async eliminates); `campaign-advance` (no async) returns
`wait_in_flight` while a run is in flight and **never** `refill`.

**PASS (measured):** observed `in_flight_min == 0` (drains between iterations),
`in_flight_max ≤ 1`, and `campaign-advance` never returned `refill`. The script
writes `.hpc/live-verify/<cid>.baseline.json` for the Criterion-1 comparison.

**PASS (human cross-check):** compare the artifacts of an async run and a
synchronous run **seeded identically** — the per-trial proposed params and
metrics must match byte-for-byte (async refill must not change *what* gets run,
only *when*). The script cannot judge this; record your diff result here.

**FAIL:** a `refill` decision on the default path, `in_flight` exceeding the
synchronous bound, or no drain-to-zero — any of these means the default path is
not byte-identical.

---

## Criterion 4 — polling within the connection-storm envelope (#346)

While the async pool is full (after Criterion 1), the script reports the
**poll-group cardinality** — distinct `(cluster, ssh_target)` groups across the
campaign's in-flight runs — against `in_flight`:

```bash
# run as part of the async pass above, or standalone after the pool is full:
.venv/Scripts/python.exe scripts/campaign_async_live_verify.py \
    --experiment-dir . --campaign-id ebm_carc --cluster carc \
    --expected-groups 1 --skip-crash-safe --samples 4 --interval 15
```

**Expected:** `batch-status` collapses polling to **one `qstat`/login-node query
per group regardless of run count**, so the poll-group count stays flat (1 for a
single cluster) even as `in_flight` climbs to K.

**PASS (measured):** `poll_groups_max ≤ --expected-groups` while `in_flight_max`
reached > 1 — polling did not scale per-run.

**PASS (human cross-check):** with the driver running, confirm the **actual**
number of `qstat`/login queries per poll equals the group count, **not**
`in_flight`. Count them in the driver / monitor-flow stderr or the ssh-throttle
log (e.g. `grep qstat <driver log>` across one poll). Record the count here.

**FAIL:** poll groups exceed the login-node count, or the qstat count scales with
`in_flight` — the connection storm reignited.

---

## Sign-off

Record the verdicts (the script prints a final per-criterion summary and exits
non-zero on any measurable failure):

| Criterion | Verdict | Evidence |
|-----------|---------|----------|
| 1. Occupancy ≈ K | | async mean vs baseline mean |
| 2. Crash-safe resume | | stranded / double-told diff |
| 3. Default unchanged | | drain-to-zero + artifact diff |
| 4. Poll envelope | | group cardinality + qstat count |

**Only when all four are PASS** (the two measured criteria green from the script,
the two human cross-checks signed off) does async refill (#362) land as
**non-experimental**. Anything short of that leaves it experimental — do not flip
the flag in defaults or docs until this gate is green on a real cluster.
