# SUBMIT-ONCE — the submit-once, discover-id-out-of-band contract (design)

Unit U3 · transport-robustness sequence · **DESIGN ONLY, no `src/**` change
lands with this memo** · baseline main @ `0e25b7de` · 2026-07-17.

**Feeds from:** `AUDIT.md` §3a (offender #1, the non-idempotent apex),
§9 unit U3 (rated **High**), §5 "Design-needed", §7 injection point
"Sever in qsub dispatch→job-id window". **Sequence:** AUDIT → contract the
transport to two primitives → fault-injection drill → daemon ladder (OFF). This
memo is the bespoke contract the submit leg gets *instead of*
`run-idempotent-script-returning-JSON` (`AUDIT.md` §8 exemption row 2), and it
will be premortemed before any build.

**Claim (enforcement row owed at land):** `transport.submit-once-discover-id`.
Owed in `docs/internals/principles/lifecycle-verdicts.md` (§8 below drafts it).
This docs-only unit records the row; it does not mechanize it — there is no
`src/**` to guard yet.

---

## 1. Problem — the orphan window this closes, and only this

The scheduler submit leg is the codebase's **one genuinely non-idempotent
actuation** (`AUDIT.md` §8). The dispatch and the id-read are one round-trip
whose two halves the network can split:

- `RemoteHPCBackend._execute_command`
  (`src/hpc_agent/infra/backends/_remote_base.py:197`) runs
  `bash -lc 'echo BIN=…; cd $REPO_DIR && qsub/sbatch …'` over one `ssh_run`,
  wrapped in `non_idempotent_remote()` (`:254`) so a client-side `TimeoutError`
  is not retried and a post-dispatch engine failure is not re-run one-shot
  (F54/F55). The remote half deliberately outlives the client by
  `REMOTE_DEADLINE_MARGIN_SEC` (`remote.py:_remote_deadline_seconds:224`).
- The **job id is parsed from stdout** (`backends/__init__.py:submit_one:580`,
  `JOB_ID_REGEX.search(result.stdout)`).
- The **journal `RunRecord` is written only AFTER the id is in hand**:
  `submit_and_record` builds the record with `job_ids=list(job_ids)` already
  populated (`ops/submit/runner.py:598`) and calls `upsert_run` at `:634` — this
  is the audit's "journal write happens after return" finding.

So a drop **after `qsub`/`sbatch` accepts the array but before its stdout job id
reaches the client** leaves: array LIVE on the cluster · control plane holding a
timeout/`EngineUnavailable(dispatched=True)` · **no job id anywhere the client
can read** · **no journal record** (upsert never ran). The array is an orphan no
existing verb can reconcile — `find_in_flight_runs` (`state/index.py:136`) keys
on journal records that were never written, and every scheduler-query recovery
path (`ssh_batch_scheduler_states`, `verify_submitted`) queries **by job id**,
which we do not have.

**The current partial mitigation and its gap.** A crash-safety *pre-stamp*
writes the just-parsed ids onto the per-run **sidecar** immediately after qsub,
BEFORE `submit_and_record` (`submit_flow.py:2537`), and
`_refuse_prestamped_without_journal` (`:2365`) refuses a re-submit that finds
landed sidecar ids but no journal (F47). This closes the *post-id* crash window.
It does **not** close the apex window: when the drop is *before the id is read*,
the sidecar carries **no** `job_ids`, so `_refuse_prestamped_without_journal`
sees an empty list and returns without refusing (`:2384`) — a re-submit then
re-`qsub`s the **same deterministic run_id**, producing a **second live array**
(duplicate) while the first stays orphaned. The apex hazard is therefore
**orphan → duplicate**, and neither `non_idempotent_remote()` (which only
governs retries *within* one submit call) nor F47 (which needs the id to have
been read) can see it.

**Root cause, stated for the fix:** the scheduler-assigned job id exists in
exactly one place — the qsub stdout on the response channel — and nothing on the
cluster persists it. The run identity (`run_id`) is *already* cluster-durable
(the pre-qsub sidecar under `<remote_path>/.hpc/runs/<run_id>.json`, and the
announce dir `<remote_path>/.hpc/announce/<run_id>/`, `announce.py:103`), but the
**id↔identity binding** is not. Close that and recovery becomes a read.

---

## 2. Constraints (verbatim from the task + the sequence's standing rulings)

> The submit must carry a control-plane-minted token the CLUSTER persists
> independently of the response channel.

> On a drop, the control plane re-dials and derives truth: token-marker present
> + job id recorded → adopt id, journal, continue; marker present + no id →
> qstat/squeue by token-name to discover; nothing → genuinely never dispatched →
> safe re-submit. Prefer extending existing verbs.

> Every branch must land on positive evidence (the ack-sentinel discipline);
> enumerate what UNKNOWN means at each rung.

> Mint the RunRecord with status=submitting BEFORE the remote dispatch (evidence
> durable, verdict revisable — the lifecycle principle), so an orphan is at worst
> submitting-with-no-id, a state reconcile CAN own.

> Single submit first; the campaign/refill path rides the same contract.

Inherited invariants that bound the design (not re-litigated here): the submit
leg stays `non_idempotent_remote()` (never blind-retried post-dispatch); the
transfer plane and reads are untouched; positive-evidence ack-gating is the
house discipline (`announce.py:_ANNOUNCE_ACK:72`, `cluster_status` sentinel-ack);
the marker/digest **wakes or informs, never settles** (row 11,
`cluster-agent-design.md` §5 mandate ii).

---

## 3. Design

### 3.0 Shape in one paragraph

Before dispatch, the control plane writes a **jobmap marker** on the cluster
(`token → pending`) and mints the journal record as **`submitting`**. The
`qsub`/`sbatch` is issued so that **the remote shell itself appends the parsed
job id into the same marker** (`token → job_id`), making the response channel
*optional* for correctness — the id is now cluster-durable. On any drop, the
control plane re-dials and reads the marker: id present → **adopt** (write
`job_ids`, promote `submitting → in_flight`); pending-but-no-id →
**disambiguate** by a scheduler query keyed on the token-name; marker absent →
**never dispatched → safe re-submit**. Recovery is owned by an extended
`reconcile`, not a new verb. The token is `run_id` (already cluster-durable),
carried into the scheduler **job name** as a short correlation hash for the
qstat/squeue fallback path.

### 3.1 The idempotency token (design question 1)

**The token is `run_id` plus a per-submit `attempt` ordinal**, written
`<run_id>#<attempt>`. `run_id` is deterministic on swept parameters (#207) and
is *already* the cluster-side key for the sidecar and the announce dir, so no new
identity is invented — the token is minted by the control plane and the cluster
persists it as a path component (mandate satisfied without a new namespace).
`attempt` (0-based, incremented once per `submit-flow` invocation for a run_id)
discriminates a *legitimate later resubmit's* marker from an *orphan's* marker of
the same run_id, so recovery never adopts a stale attempt's id onto a fresh one.

Evaluation of the three carriers the task names:

**(a) Job-NAME embedding (`qsub -N` / `sbatch -J` with the token) — REJECTED as
the primary carrier; RETAINED as a short correlation key.** Verified against the
schedulers' real name constraints:

| Scheduler | `-N`/`-J`/`--job-name` name limit | Source |
|---|---|---|
| SGE / UGE (`qsub -N`) | POSIX: **≤15 chars, alphanumeric, first char alphabetic** | [POSIX qsub](https://man7.org/linux/man-pages/man1/qsub.1p.html), [Grid Engine qsub man](https://gridscheduler.sourceforge.net/htmlman/htmlman1/qsub.html) |
| TORQUE (`qsub -N`) | **< 16 chars** ("Specified job names must be less than 16 characters") | [prisms-center/pbs #11](https://github.com/prisms-center/pbs/issues/11), [TORQUE qsub](https://docs.adaptivecomputing.com/torque/4-0-2/Content/topics/commands/qsub.htm) |
| PBS Pro (`qsub -N`) | ~230+ chars (relaxed vs TORQUE) | [Argonne LCRC PBS Pro](https://argonne-lcrc.github.io/user-guides/running-jobs-at-lcrc/pbs-pro/) |
| Slurm (`sbatch -J`) | **No hard length limit** (stored in the job record); `squeue` display truncates unless `-o %j` / `--name` | [sbatch(1)](https://slurm.schedmd.com/sbatch.html); centers commonly document "≤15, first alphabetic" as convention only, e.g. [OSC](https://www.osc.edu/supercomputing/batch-processing-at-osc/job-scripts) |

Decisive fact: this codebase's `run_id`s (e.g. `pi-train-d363e2a3` = run_name +
8-hex cmd_sha) **already exceed 15 chars**, and `job_name` is *already* consumed
byte-for-byte in log-path construction (`_engine.py:1219-1222,1230-1231`,
`--output %x_%A_%a`) and in canary naming (`f"{spec.job_name}_canary"`,
`submit_flow.py:2531`). Embedding the full token in the name is infeasible on
SGE/TORQUE and would collide with those consumers. So the name carries **only a
short, scheduler-safe correlation hash** — `h` + first 10 hex of
`sha256(f"{run_id}#{attempt}")` (11 chars, alpha-first, alnum) — appended in a
place that does not disturb the log-path/canary uses (see OPEN-1). Its sole job
is the §3.4 rung-2 `qstat`/`squeue`-by-name fallback; it is **never** the
authoritative id binding.

**(b) Pre-submit marker FILE the remote writes atomically, then the submit
appends the id — ADOPTED as the load-bearing carrier.** This is the mechanism
that makes the response channel optional. Protocol (§3.2). It inherits the
codebase's proven cluster-marker discipline (the announce plane: filename/path
encodes state, pure `ls` reads it, ack-gated) rather than inventing one.

**(c) Both — ADOPTED, with (b) authoritative and (a) a degraded fallback.** The
marker (b) is authoritative because it records the *exact* id the scheduler
returned; the name hash (a) is the recovery path *only when the marker write did
not complete* (the id was accepted but the remote shell was killed by
`timeout -k` mid-append — a genuinely narrower window than the original). Two
independent cluster-durable traces of the same dispatch, each closing the other's
residual window.

### 3.2 The cluster-side jobmap marker protocol

One marker per run: `<remote_path>/.hpc/submit/<run_id>.jobmap` (new
`.hpc/submit/` sibling of `.hpc/announce/`, `.hpc/runs/`). It is JSON, written
**by the remote shell**, atomically (temp + `mv`, the same
temp+rename discipline the transfer plane's manifest writers use,
`AUDIT.md` §2 `_write_push_manifest`).

Lifecycle, all remote-side so it survives a severed client:

1. **Pre-dispatch (`token → pending`).** The control plane's submit command, in
   the *same* `bash -lc` round-trip that will run qsub, first writes
   `{"token":"<run_id>#<attempt>","state":"pending","attempt":N,
   "at":<epoch>,"waves":{}}` to the jobmap via temp+`mv`. (One extra remote
   statement, no extra round-trip — folded into the existing
   `_execute_command` command string, `_remote_base.py:268`.)
2. **Dispatch + append (`token → job_id`).** The command captures the scheduler
   stdout server-side:
   `JID=$(qsub …) ; rc=$? ; <append JID+rc into jobmap.waves atomically> ;
   printf '%s\n' "$JID"` — so the id is persisted **on the cluster before it is
   echoed to the client**. The `HPC_AGENT_OP`/`timeout -k` wrapper
   (`build_remote_command:248`) already bounds the remote half; the append lands
   inside that bound. The client still parses the echoed id on the happy path
   (byte-identical to today); the marker is the durable backup.
3. **Steady state.** The jobmap is inert after a clean submit. It is *not*
   consulted on the happy path — only by recovery (§3.4) and by the F47-class
   refusal, which gains a third input (marker id) beyond the sidecar.

Why the marker and not "just fsync the sidecar earlier": the sidecar is written
**client-side** then rsync'd; it cannot record an id the client never received.
The jobmap is written **server-side by the dispatching shell**, which *is* the
process that holds the id. That is the whole point — the id's producer persists
it, not its consumer.

Ack discipline (house rule): every jobmap read echoes a positive
`__HPC_JOBMAP_ACK__` sentinel after a successful `cd`/read, exactly like
`announce.py:_ANNOUNCE_ACK` — an absent ack ⇒ severed/truncated ⇒ UNKNOWN, never
"no marker" (§4).

### 3.3 Journal ordering — the `submitting` state (design question 4)

**Rule: mint the `RunRecord` with `status="submitting"` BEFORE the remote
dispatch, and promote it to `in_flight` (with `job_ids`) only after the id is in
hand.** This inverts today's "write after return" (`runner.py:634`) so the
durable evidence ("a submit for run_id X was attempted at T against cluster C")
exists before the actuation, and the *verdict* (which job ids, terminal or not)
is filled in after — the lifecycle principle (evidence durable, verdict
revisable). An orphan is then at worst a **`submitting` record with empty
`job_ids`**, a state reconcile can own, instead of *no record at all*.

New ordering inside the submit atom:

1. `RunRecord(status="submitting", job_ids=[], attempt=N)` → `upsert_run` (+
   the initial watchdog stamp already at `runner.py:636`, so a driver death in
   the dispatch window leaves a lapsed `next_tick_due`).
2. write jobmap `pending` (§3.2 step 1, remote) and dispatch (§3.2 step 2).
3. on id read: `update_run_status(job_ids=…)` then `mark_run(status="in_flight")`
   — the promote. On dispatch failure with an id (partial multi-wave):
   `job_ids` stamped, stays recoverable.
4. on any drop between 1 and 3: the record is left `submitting` — reconcile
   owns it (§3.4).

**`submitting` is a new `JournalStatus`, NOT a `LifecycleState`.** It is
non-terminal (absent from `TERMINAL_STATUSES`, `vocabulary.py:70`) and distinct
from `in_flight`. `LifecycleState` (`vocabulary.py:75`, the monitor-flow
envelope) does **not** gain it — a `submitting` run has no live cluster jobs to
compute a lifecycle verdict over yet; it is a pre-monitor state.

**Migration — every reader of `RunRecord.status` must tolerate `submitting`.**
Enumerated by reading `state/index.py` + `state/journal.py` + consumers:

| Reader | file:line | Today | Required change |
|---|---|---|---|
| `JournalStatus` enum | `vocabulary.py:53` | 4 values | **ADD** `SUBMITTING = "submitting"`; keep OUT of `TERMINAL_STATUSES` |
| `mark_run` validation | `journal.py:398` | `status in set(JournalStatus)` | free once enum extended (the promote `submitting→in_flight` and the mint both go through here / `upsert_run`) |
| `prune_terminal_runs` | `index.py:353` | prunes `status != "in_flight"` | **FIX (latent bug):** a `submitting` record is non-terminal and MUST NOT be pruned — change the guard to "keep `in_flight` OR `submitting`" (or "keep `status not in TERMINAL_STATUSES`"). Without this, a `submitting` orphan is *garbage-collected*, losing the only evidence of the orphan. |
| `find_in_flight_runs` | `index.py:136,159,176` | returns only `status == "in_flight"` | **UNCHANGED** — a `submitting` run has no `job_ids`, so monitor/campaign must NOT treat it as live. It is surfaced to *reconcile*, not the monitor, via the new scan below. |
| `find_submitting_runs` (NEW) | `index.py` | — | **ADD** — the scan reconcile/doctor use to find `submitting` records to recover. Mirrors `find_stalled_runs` structure. |
| `find_stalled_runs` | `index.py:241,277` | scans `find_in_flight_runs` only | **EXTEND** to also read `submitting` runs whose `next_tick_due` lapsed → a submit that died in the window surfaces as a `doctor` recovery proposal (routes to reconcile, not re-arm). |
| `_resolve_layer1` dedup | `runner.py:_resolve_layer1` (via `runner.py:422`, `submit_flow.py:2331`) | branches on in_flight / complete / terminal-failure | **ADD a `submitting` branch:** an existing `submitting` record for this run_id means "a prior submit is in its dispatch window / orphaned" → route to **reconcile-recovery, refuse a blind re-submit** (do not `_PROCEED`, do not `_DEDUP`). This is the front-door guard that replaces the leaky `_refuse_prestamped_without_journal:2384` empty-ids gap. |
| `is_resubmittable_terminal` | `journal.py:585` | `status in {failed, abandoned}` | **UNCHANGED** — `submitting` is correctly excluded (not resubmittable by a plain submit; only reconcile transitions it out). |
| `_rebuild_index` / `_refresh_index_entry` | `index.py:129`, `journal.py:691` | carries `status` through verbatim | free (string pass-through) |
| status-snapshot / doctor renders | `ops/monitor/*`, `ops/recover/doctor.py` | render status string | **ADD** a `submitting` render ("submitting — dispatch in flight / awaiting id"); doctor names the reconcile-recovery action |
| wire status output | `schemas/status.output.json`, `_wire/queries/status.py` | enumerates `LifecycleState` | **AUDIT:** if the status schema enumerates journal statuses, add `submitting`; if it maps journal→lifecycle, map `submitting → in_flight`-shaped or a new display value (OPEN-3) |

The migration's safety property: **`submitting` is a state ONLY reconcile (and
its recovery read) transitions out of.** No monitor poll, no plain resubmit, no
prune, no aggregate touches it. That containment is what makes the new state
cheap to add — its blast radius is the reconcile owner plus the prune fix.

### 3.4 The recovery read (design question 2) — extend `reconcile`, add no verb

On a drop, the control plane re-dials and derives truth from the jobmap. This is
**an extension of `reconcile`** (`ops/monitor/reconcile.py`), the existing owner
of "re-derive a run's truth from cluster evidence" — not a new probe verb
(prefer-existing-verbs constraint). Reconcile already reads the announce census
(`read_announcements`) and scheduler states; it gains one input, the jobmap, and
one entry condition, `status == "submitting"`. The read maps each outcome to the
existing machinery:

| Marker outcome | Meaning | Action (existing machinery) |
|---|---|---|
| jobmap present, `waves` has id(s), `attempt` matches | dispatched, id known | **ADOPT:** `update_run_status(job_ids=…)` → `mark_run("in_flight")`. Hand off to the normal monitor/announce path (`read_announcements` by run_id already works — the announce dir is run_id-keyed, `announce.py:103`). |
| jobmap present, `state:"pending"`, no id, ack seen | dispatch may or may not have fired | **DISAMBIGUATE:** scheduler query keyed on the §3.1(a) name-hash — `qstat -u $USER` (SGE, whole-user-queue, ack via rc==0, `profile.py:97`) / `squeue --name`/`-o %j` (Slurm) / `qstat` (PBS). Hit → recover the id from the queue, ADOPT. Clean miss (query ran, ack fired, name absent) → **never landed → mark for safe re-submit** (transition `submitting`→resubmittable, clear jobmap). |
| jobmap ABSENT, ack seen (dir readable) | pre-dispatch marker never written → dispatch never started | **SAFE RE-SUBMIT:** genuinely never dispatched; clear `submitting`, allow a fresh submit of `<run_id>#<attempt+1>`. |
| any read with NO ack (severed/truncated) | UNKNOWN | **REFUSE to settle** — leave `submitting`, re-census next tick (§4). Never read absence-of-ack as "no marker". |

Adoption reuses the announce plane wholesale: once `job_ids` are on the record
and status is `in_flight`, `read_announcements`/`read_announcements_batch`
(`announce.py:75,240`) and `ssh_batch_scheduler_states` settle the run exactly as
for any submit — the orphan is now a normal in-flight run. No id-by-name
correlation is needed on the happy adopt path; it is the rung-2 fallback only.

---

## 4. Failure ladder — every rung lands on positive evidence (design question 3)

The ack-sentinel discipline (`announce.py:72`, `cluster_status` sentinel-ack)
governs every recovery read: **a rc-0 read with no positive ack is severed, and
severed ⇒ UNKNOWN ⇒ leave `submitting` and re-census — never a settle.** What
UNKNOWN means at each rung, and the human-facing disclosure:

| Rung | Positive evidence required | If evidence present | If ABSENT (UNKNOWN) | Human-facing disclosure |
|---|---|---|---|---|
| 0 · happy submit | client parses id from stdout AND marker append confirmed | promote `submitting→in_flight` | (n/a — this is the online path) | "submitted run X: jobs […]" |
| 1 · drop, jobmap read | `__HPC_JOBMAP_ACK__` sentinel | proceed to rung 1a/1b by marker contents | severed read → stay `submitting`, retry next reconcile tick | "run X is **submitting** — dispatch in flight, verifying whether the scheduler accepted it; not yet confirmed" |
| 1a · marker has id | id string + matching `attempt` | ADOPT: write job_ids, `in_flight` | id field unparseable → treat as rung 1b (disambiguate) | "adopted orphaned array X: jobs […] (recovered from cluster marker)" |
| 1b · marker pending, no id | scheduler-query ack (`scheduler_query_ran` / rc==0 SGE) AND name-hash lookup | hit → ADOPT recovered id; clean miss → mark resubmittable | query severed → stay `submitting`; **never** read query silence as "not running" (the `ssh_batch_scheduler_states` refusal, `AUDIT.md` §3b) | "run X **submitting** — asked the scheduler by name; queue unreachable, will re-check" (severed) / "run X was never accepted by the scheduler — safe to resubmit" (clean miss) |
| 2 · marker absent, dir ack seen | `cd` into `.hpc/submit/` acked, file genuinely absent | SAFE RE-SUBMIT (`attempt+1`) | dir unreadable/no ack → stay `submitting` | "run X was never dispatched — resubmitting as attempt N+1" |
| 3 · everything severed | — | — | whole read severed → `submitting` held, `next_tick_due` lapses → `doctor`/`find_stalled` surfaces it | "run X stuck **submitting** since T — cluster unreachable; reconcile when connectivity returns" |

The load-bearing inversion: **absence is never trusted; only an acked read
advances a rung.** A `submitting` run can therefore sit UNKNOWN across many
severed ticks and never mis-settle — worst case is delay + a `doctor` surfacing,
identical in spirit to the reconcile-stale "leave ALL open on a blip" posture
(`AUDIT.md` §3f, `reconcile_stale`).

---

## 5. Scope — single submit first, campaign/refill rides the same contract (Q5)

The contract is defined on the **submit atom** (`_execute_command` +
`submit_and_record`), which is the single seam *every* array submission funnels
through — `submit_one`/`submit_plan` (`backends/__init__.py:525,623`), the canary
(`submit_flow.py:2529`), recover-flow's `_submit_one_batch`, and the campaign
refill path all call it (`AUDIT.md` §3a/§3d). So the campaign/refill path
generalizes **for free**, not by a second design:

- **Multi-wave `submit_plan`.** Each wave is one `submit_one` → one jobmap
  `waves` entry (`{wave: job_id}`). A drop mid-plan leaves the jobmap with the
  landed waves' ids AND the pending marker for the wave in flight — recovery
  adopts the landed ids and disambiguates only the in-flight wave. This subsumes
  the existing `partial_submit_job_ids` accounting (`submit_flow.py:2157`) with a
  cluster-durable source instead of an exception attribute the crash can eat.
- **Campaign refill.** `campaign_refill` (`ops/campaign_refill.py`) mints child
  runs each with its own `run_id`+`attempt`, each getting its own jobmap; the
  campaign's `find_runs_by_campaign` (`index.py:183`) join is unchanged (it reads
  `campaign_id`, which the `submitting` record carries from mint,
  `run_record.py:182`). A `submitting` child is simply "not yet live" to the
  campaign loop — the same treatment `find_held_runs` gives a parked child
  (neither live nor done). The campaign never blind-resubmits a `submitting`
  child; reconcile owns it, exactly as for a single submit.
- **`auth_ids` / provenance joins.** `verify_relay`'s `auth_ids` set
  (`ops/decision/journal/verify_relay.py:1222`: run_id ∪ campaign_id ∪
  superseded_by/supersedes) is status-agnostic, so a `submitting` record's ids
  are authorized identically — no change.

The campaign contract is therefore **the single-submit contract applied per
child**, with the campaign_id join carrying the set. Nothing about refill needs a
distinct id-discovery design.

---

## 6. Non-goals

- **Not a daemon / persistent connection.** The submit-once contract is
  actuation, daemon-orthogonal (`AUDIT.md` §9 affinity note). It stays one-shot
  regardless of the daemon ladder.
- **Not changing `non_idempotent_remote()`.** The submit stays never-blind-
  retried; this memo adds *recovery*, not *retry*. The guard and the contract are
  complementary (`AUDIT.md` §6 row).
- **Not a general marker for idempotent reads.** Reads are already ack-gated and
  drop-safe (`AUDIT.md` §4); the jobmap is only for the one non-idempotent
  actuation.
- **Not solving the `watcher_install` job rung** (`AUDIT.md` rank 8, a separate
  non-idempotent qsub) — it is the same *class* and could adopt this contract
  later, but is out of scope for U3's single-submit-first cut.
- **No default-on cutover semantics to decide here** — the state machine lands
  behind the same telemetry-gated caution as the rest of the sequence; a proving
  run precedes trusting adoption over resubmit in the field.

---

## 7. Enforcement rows owed at land

Drafted for `docs/internals/principles/lifecycle-verdicts.md` (mechanized only
when `src/**` lands):

> **Row (submit-once):** the scheduler submit leg persists its
> `run_id#attempt` token and the parsed job id in a cluster-side jobmap marker
> written by the dispatching shell, and mints the journal record `submitting`
> BEFORE dispatch. A submit that reaches the scheduler without a durable
> id-binding, or a `submitting` record that any path other than reconcile
> transitions out of, or a recovery read that settles on an un-acked (severed)
> marker read, is the fire.
> *Enforced by:* the §7 fault-injection drill "Sever in qsub dispatch→job-id
> window" (`AUDIT.md` line 440) — the orphan must be adopted from the marker with
> **no** re-`qsub`; a planted severed jobmap read must leave the run `submitting`,
> never settle; a planted `submitting` record must be pruned by nothing.
> *Fires when:* the journal write moves back after dispatch; `prune_terminal_runs`
> treats `submitting` as terminal; `_resolve_layer1` proceeds/dedups a
> `submitting` run instead of routing to reconcile.

Plus the standing house guards this reuses (no new lint): the jobmap read carries
the ack sentinel (contract-test parity with `announce.py`); the marker
**wakes/informs, never settles** (row 11 restated — adoption re-reads truth via
`read_announcements`, it does not trust the marker's mere presence as a lifecycle
verdict).

---

## 8. Open questions (each with its decision criterion)

- **OPEN-1 · Where the name-hash rides.** `job_name` is consumed byte-for-byte by
  log paths (`_engine.py:1219-1231`) and canary naming (`submit_flow.py:2531`), so
  the §3.1(a) 11-char correlation hash cannot simply be appended to `job_name`
  without perturbing those. Options: (i) a separate scheduler *context/comment*
  field (Slurm `--comment`, SGE `-ac`/context via `qsub -ac key=val`, PBS custom
  resource) queried by `qstat -j`/`scontrol show job` instead of the name;
  (ii) a name *suffix* with the log-path/canary consumers updated in lockstep;
  (iii) drop (a) entirely and rely on the marker (b) alone, accepting the
  narrower "id accepted but append killed by `timeout -k`" residual window.
  **Decide by:** whether the fault-injection drill can actually produce the
  marker-append-killed window under `timeout -k`; if it is unreachable in
  practice (the append is a sub-millisecond `mv` inside a 60s+ bound), pick (iii)
  and delete the name-hash complexity. *Leaning (iii)* — the marker append is far
  inside the remote deadline, so (a) may be dead weight.

- **OPEN-2 · `attempt` allocation and its durability.** `attempt` must increment
  monotonically per run_id and be known before the record is minted. Options: a
  counter on the journal record bumped under the run lock at mint; or derive it
  from `max(existing jobmap attempt, journal attempt)+1` on the recovery path.
  **Decide by:** whether a resubmit can race a still-`submitting` orphan of the
  same run_id — if `_resolve_layer1`'s new `submitting` branch (§3.3) refuses a
  concurrent submit outright, `attempt` only ever advances after reconcile
  resolves the prior, and a simple record-field counter suffices.

- **OPEN-3 · Wire/schema surface for `submitting`.** Does `status.output.json` /
  `_wire/queries/status.py` enumerate journal statuses (needs the new value in the
  schema + a schema-version bump) or project journal→`LifecycleState` (map
  `submitting` to a display value)? **Decide by:** reading the status query's
  actual projection — if it already maps (e.g. `in_flight` journal →
  `in_flight`/`timeout` lifecycle), add a `submitting` display projection and no
  schema value; if it echoes journal status raw, bump the schema. (Not resolved
  here — flagged for the build unit to read `_wire/queries/status.py` first.)

- **OPEN-4 · `.hpc/submit/` deploy + cleanup.** The jobmap dir must exist before
  the pre-dispatch write (fold a `mkdir -p .hpc/submit` into the same
  `_execute_command` string, cost-free) and should be pruned when the run reaches
  a terminal state (piggyback on the existing terminal harvest, or leave inert —
  the announce dir sets the precedent of leaving markers). **Decide by:** whether
  leaving stale jobmaps risks a false adopt on a *future* same-run_id run — the
  `attempt` discriminator (OPEN-2) is exactly the guard, so cleanup is hygiene,
  not correctness; leave inert unless the drill shows attempt-confusion.

- **OPEN-5 · Does the marker-append survive `timeout -k` grace on ALL layouts?**
  The remote command must finish the atomic `mv` even when the client already
  timed out. The `REMOTE_DEADLINE_MARGIN_SEC` (60s) + `timeout -k` grace should
  cover a sub-ms `mv`, but a login node under fork exhaustion (run-12 finding-20
  class) could delay it. **Decide by:** the step-3 injection at
  `build_remote_command`'s `timeout -k` boundary — if the append can be reaped
  before completing, rung-1b (name-query) is load-bearing and OPEN-1(iii) is off
  the table.

---

## Drift log

- 2026-07-17: Created (unit U3, transport-robustness sequence). Docs-only; the
  build is premortem-gated. Fixes the token (`run_id#attempt`, marker-file
  authoritative, name-hash correlation fallback), the cluster-side jobmap
  protocol, the `submitting`-before-dispatch journal ordering + its reader
  migration (incl. the `prune_terminal_runs` latent-bug fix), the reconcile-owned
  recovery read, the positive-evidence failure ladder, and the campaign/refill
  generalization. Scheduler name limits verified against POSIX/Grid-Engine/
  TORQUE/PBS-Pro/Slurm docs (§3.1). Five OPEN questions carry decision criteria
  tied to the step-3 fault-injection drill. Enforcement row owed at land (§7).
