# Fable sweep — 2026-07-13 (experiment-runtime hardening plan)

Status: **BANKED — sweep complete, verified, no fixes applied yet.** This doc
is the execution plan for a hardening swarm. Produced by a 10-lens finder
fan-out over the experiment-runtime failure surface, 4-agent triage
(3 shards + cross-lens dedup), a 14-agent adversarial verification wave
(2 independent refute-by-default votes per high, 1 per medium/low), a
completeness critic, and finally a **5-agent Fable hard-kill panel** that
re-judged all 22 highs + WEAKs from a votes-stripped brief (guilty-until-
proven, with empirical repros). The panel materially recalibrated the set —
it downgraded 7 findings the Opus wave had over-rated and rescued 1 it had
wrongly weakened (see the panel note under Verdict). Line pins are as of
`fb8428c` (code-identical to `d731b12`); re-validated empty
`git diff fb8428c..HEAD -- src/ tests/` at banking time. Verify live before
acting — this doc narrates a plan and is outside the pinned truth surfaces.

**Machine-readable source of truth:** [`fable-sweep-2026-07-13/`](fable-sweep-2026-07-13/)
— `verified-findings.json` (all 57 findings: scenario, fix sketch, evidence,
triage rationale, every skeptic vote, the `fable_panel` re-judgment with its
fix-sketch concerns and new evidence, merge/compounding annotations,
adjudications), `critic-gaps.json` (follow-up sweep worklist + live-cluster
checks), `RULINGS.md`, `live-smoke-notes.md`. Swarm agents should consume the
JSON, not re-parse this prose — and read each finding's `fable_panel.fix_sketch_concerns`
before implementing, as the panel found real soundness gaps in several fix
sketches (F05/F07/F17/F23/F35/F37/F47/F53 in particular).

**Scope guard:** every finding was mandatorily deduped against the fixed
2026-07-11 bug sweep (and its refuted list — do not re-report those) and the
banked architecture review. Two ledger entries claimed fixed but never landed
(F49, F22) were re-verified as still-live and are included here.

**Baseline:** full suite GREEN at `76ef29c` — 9,455 passed / 25 skipped /
68 xfailed, 95s. Any red after a fix belongs to the fix.

---

## Verdict

57 verified findings (54 CONFIRMED / 3 WEAK; **13 high / 32 medium / 9 low**
after the Fable panel — down from 20/26/9 at the Opus round). The sweep's lens
was narrow on purpose: *what breaks when this repo is actually used to run
experiments*. Three generators dominate:

1. **Evidence monocultures.** The announce census consults no scheduler; the
   scheduler layer trusts an ack while discarding rc/stderr; the aggregate
   integrity gate reads a field the combiner fills tautologically. When the
   single evidence channel lies, the system settles wrong states *silently*.
2. **Guards that structurally cannot fire** — the repo's own
   engineering-principles class, with seven+ confirmed live instances:
   the partial-submit pre-stamp no path reads (F47), the cross-cluster
   REFUSE parked behind the dedup short-circuit (F48), the preflight gate
   wrapped in an unconditional `_ok()` (F29), consent caps metered against
   a `spent_*` nothing writes (F16), `all_tasks_present` (F07), the ack
   sentinel's stated purpose (F35), `validate_clusters_config` with zero
   callers (F32). Every fix here must ship the fire-path test the repo's
   own doctrine demands.
3. **Retry/resume asymmetry.** Submission, deploy, and recovery were each
   built crash-safe on the *first* attempt but not on the *retry*: the retry
   path misses the pre-stamp (F47), reads the manifest the interrupted
   transfer already landed (F53), re-executes commands already dispatched
   (F54/F55), and re-reads announce markers a resubmit never cleared (F23).

**Fable panel recalibration (why the highs shrank from 20 to 13).** The Opus
verification wave confirmed 55/55 with zero refutations — a pattern that flags
possible deference (a verifier below the finders' tier tends to defer, not
refute). The Fable hard-kill panel, working guilty-until-proven from a
votes-stripped brief, changed 8 of 22 verdicts:

- **7 downgraded** — the highs that were riding on overstatement:
  **F06/F07** (high→med): the same evidence *is* disclosed loudly at the
  harvest boundary — `needs_decision=True`, not silent wrong data; the residue
  is a contradictory green on the standalone verify verb.
  **F13** (high→med): the retracted-y launch is an approved-then-retracted
  spec (visible in the brief), the stall self-heals on the next doctor tick,
  and there's a *third* un-unified seat (`block_gate`) the fix must also cover.
  **F29** (high→med): submit-flow runs the same uv probe inline and raises, so
  the "doomed array" can't fire on the primary path — false-green-then-loud.
  **F01** (high→med): the curated catalog (install default) excludes the three
  escapee verbs; the wedge is the agent session's control surface (restart to
  clear), not a lost campaign.
  **F30** (high→WEAK/med) and **F48** (high→WEAK/med): both have real recovery
  guards prior rounds missed — F30's reconcile raises loud with a mint command
  + A5 sidecar reconstruction; F48's block-drive S2 parks loudly on dedup and
  S3 refuses on a cluster-keyed canary. The silent-success path survives only
  on the discouraged direct-submit surface.
- **1 upgraded** — **F39** (WEAK/low→CONFIRMED/med): the panel *reproduced*
  the PBS banner poisoning (5/5 dotted-number cases) and found the trigger is
  framework-induced (the submit path's own `bash -lc` login shell), not
  site-conditional as the downgrade assumed.
- **F12 and F37 held at high**, independently reaching the same verdict as the
  earlier inline rulings (F12 reproduced in-venv; F37's fix sketch hardened —
  the `-M` value is the SLURM cluster name, not the config key).

The 13 surviving highs (**F05 F11 F12 F17 F18 F23 F35 F36 F37 F47 F53 F54
F55**) were held under the harshest scrutiny, several with fresh local repros
(F53's rsync-interrupt, F35's rc-127 empty-queue, F18's AST version floor,
F36's upstream OpenPBS/TORQUE source lookup). Treat these as the load-bearing
set.

## Work packages, ranked

Rank is value-to-risk for a multi-day campaign. WP-F and WP-G are quick wins
that can land first chronologically. Each package lists its findings
(severity as re-judged by the Fable panel), the shared root cause, and the fix
shape; per-finding fix sketches, the panel's fix-sketch concerns, and mandatory
regression tests live in `verified-findings.json`. Package *order* is unchanged
by the recalibration — WP-A/B/C still lead on the surviving highs — but several
individual findings are now medium, so within-package priority should follow
the current tags.

### WP-A — Trust the numbers: aggregation integrity (F05 F06 F07 F08 F09 F10)

The worst outcomes in the sweep: silently wrong science.
- **F05 (high)** `_combiner/wave_N.json` is not run-namespaced and is
  delete-protected, so run B adopts run A's partials: the no-force refusal is
  journaled as "already combined" and both reducers merge foreign
  `grid_points` without a `run_id` check (the files carry one).
- **F06 (med)** resubmit never invalidates `combined_waves`, so recovered
  tasks' results are permanently excluded from the aggregate.
- **F07 (med)** `all_tasks_present` is a tautology — combiner echoes the full
  wave_map into `task_ids` while errored tasks live only in `errors`, which
  the invariant never reads.
- **F08/F09 (med)** the incremental pull diffs waves by *filename only*: a
  force-recombined remote wave is never re-pulled; a torn local wave is
  silently dropped from the reduce AND pinned forever.
- **F10 (med)** `_aggregated/` is in no exclude set: pushed back to the
  cluster and `--delete`-clobbered every submit.

Fix shape: run-namespace or run_id-filter the combiner outputs end to end
(write, refuse, recover, reduce, final-reduce); make resubmit invalidate the
affected waves; make the integrity gate read the evidence the partial already
carries; stop filename-only pull diffs; add `_aggregated/` to
`PROTECTED_OUTPUT_DIRS`. F06+F07 compound (the vacuous gate is why the
exclusion goes undetected) — fix in the same package and test them together:
fail 40 of 100 tasks, recover, re-aggregate, assert the numbers include the
recovered tasks and the gate fails until they do.

### WP-B — Monitoring evidence integrity: the announce census and settle logic (F17 F23 F19 F25 F26 F27 F28 F40)

One seam, many symptoms: once any announce marker exists, the census leg is
the *only* evidence source — no walk, no scheduler, `unknown=0`, `running=0`.
- **F17 (high, merged F24)** tasks that die with no marker (preemption
  handler, SIGKILL, node crash — the handler provably never announces) read
  `missing→pending` forever: the watch rides the full budget to TIMEOUT and
  auto-resume/auto-recover (gated on FAILED) are unreachable.
- **F23 (high)** the inverse: stale `.failed` markers that no resubmit path
  clears settle a LIVE re-run as FAILED on the next tick — auto-resume burns
  its cap in two poll intervals launching duplicate arrays.
- **F19 (med)** partial reproductions (`task_sample`) can never settle —
  nothing shrinks the expected total.
- **F25/F26 (med)** the terminal-FAILED tick is unguarded: stale pack
  declarations (SpecInvalid by design) or an SSH blip during auto-recover's
  resubmit kills the whole watch non-terminal; the counter-write guard exists
  in the auto-resume twin but not the newer copy.
- **F27 (med)** `consecutive_env_polls` never resets on success — three
  non-consecutive rc-127 blips over hours kill a healthy watch.
- **F28 (med)** census `last_status` carries no `waves` block, so
  `auto_combine_waves` silently no-ops for announce-era runs (feeds WP-A).
- **F40 (low)** SGE suspended states read as RUNNING.

Fix shape: (1) give the census a liveness cross-check for the missing bucket
(one cheap scheduler-states call when the census is partial-and-static) so
the existing bounded-unknown watchdog can fire; (2) make the dispatcher
best-effort announce on its kill-path exits and make every resubmit clear or
epoch the markers for the resubmitted ids; (3) wrap the FAILED-tick composite
calls (HpcError/OSError → degrade to the plain FAILED surface, never kill the
watch); (4) reset the env-poll counter on success; (5) thread `task_sample`
and a `waves` derivation into the census. F17/F23 share the marker-lifecycle
fix — one owner.

### WP-C — Duplicate execution and submit idempotency (F47 F48 F54 F55 F56 F49 F50 F51 F52)

Everything that can put the same work on the cluster twice or route it
nowhere.
- **F47 (high)** partial-submit retry re-qsubs all waves: the crash-safety
  pre-stamp is written but no submit path reads it (dead guard), and the
  job_ids stamp then *replaces* the pre-stamped ids — untracked ghost arrays.
- **F48 (WEAK/med)** `_dedup_existing` ignores cluster: a retarget to cluster B
  silently "succeeds" as dedup against cluster A; the purpose-built REFUSE
  guard is unreachable on the primary path.
- **F54 (high)** client-side TimeoutError is retried for the non-idempotent
  qsub leg while the remote half deliberately outlives the client by 60s —
  duplicate arrays on any slow qmaster. Default path, no opt-in.
- **F55 (high) / F56 (med)** the SSH engine collapses post-dispatch failures
  into `EngineUnavailable`, which the seam swallows and re-executes one-shot;
  one failed command discards the shared connection under in-flight peers.
  **Smoke correction: `mcp-serve` defaults the engine ON — these are not
  opt-in for MCP-driven usage.**
- **F49 (med, "fixed" ledger entry that never landed)** SGE+MPI specs with no
  `pe_name` still submit N ranks onto one slot via the direct wire surface.
- **F50 (med)** the canary decision is evaluated twice around a multi-hour
  rsync against a wall-clock TTL — a mid-stage expiry fires a canary whose
  sidecar never shipped.
- **F51 (weak/med)** stale in_flight canary job id reused as the afterok gate
  on replay. **F52 (low)** per-spec `rsync_excludes` silently ignored in
  batch mode.

Fix shape: make the sidecar pre-stamp readable by `_dedup_existing` (refuse
or reconstruct, never fresh-qsub over landed job_ids); reuse `_resolve_layer1`
in the dedup front door so the cluster check cannot drift; thread an
idempotence flag through `ssh_run` (no timeout-retry, no engine one-shot
fallback for dispatched non-idempotent commands); honor the inflight veto on
the engine's failure-path discard; land the F49 validators the ledger already
claims; evaluate the canary decision once per spec and thread it through.

### WP-D — Scheduler backend round-trips (F35 F36 F37 F38 F39)

The PBS/multi-cluster half of the backend matrix is untested against reality
and fails silent-negative. **All five fix shapes are gated on the live-cluster
checklist in `critic-gaps.json` — validate the domain assertions first.**
- **F35 (high)** `scheduler_query_ran` returns ok for ANY rc on the
  explicit-id families with stderr discarded: a down slurmctld or missing
  binary reads as "queue empty" → healthy campaign settled abandoned, kills
  false-confirmed. rc 126/127 is cleanly distinguishable today.
- **F36 (high)** PBS array ids lose their `[]` at capture; every later
  qstat/qdel addresses a nonexistent id (tests pin the broken round-trip).
- **F37 (high, ruled)** liveness/state/cancel builders never emit `-M` while
  sbatch routes with `--clusters=` — deterministic blindness for any
  non-default `slurm_cluster`, and `query_sacct` proves the flag is known.
- **F38 (med)** TORQUE `C`-state rows count as alive/RUNNING for the
  keep_completed window — kill verification reports failure after success.
- **F39 (med)** PBS job-id regex lacks the line anchor its SLURM/SGE
  siblings deliberately have.

Fix shape: per-family rc semantics with stderr capture in the ack; preserve
`[]` through the id round-trip (regex + builders + tests that currently pin
the bug); optional `cluster` kwarg on the three builders threaded from the
call sites that already hold it; state-column awareness in
`parse_alive_output`; anchor the PBS regex. Compounding: F35 is the amplifier
that turns F36/F37 query emptiness into false terminal settles — fix F35
first or together.

### WP-E — Deploy/transport atomicity (F53 F20 F57 F58)

- **F53 (high)** the deploy-cache manifest rides the same non-atomic transfer
  as the files it attests and lands FIRST on the wire (sorted flist,
  `--inplace`): interrupt + retry = success reported, nothing shipped, stale
  or torn framework code all campaign. The tar fallback shares the hazard.
- **F20 (med)** `--inplace` rewrites the live dispatcher under running
  arrays; the retry wrapper converts the torn window into terminal
  `.hpc_failed` markers.
- **F57 (med)** the rsync-less delta push silently ignores internal-slash
  excludes — ships what the user excluded, only in delta mode.
- **F58 (med)** the push manifest is rewritten local-only after a refused
  prune, permanently downgrading prunable extras to never-touched ANOMALYs
  and defeating the printed remediation.

Fix shape: manifest never rides the transfer (write it in a separate leg
after success — the `_write_push_manifest` pattern already exists); drop
`--inplace` or stage-and-rename; give `_path_excluded` real anchored
semantics in lockstep with the remote snippet; write the manifest as the
union including unpruned extras.

### WP-F — Shipped cluster runtime floor (F18 F21 F22) — quick win

- **F18 (high)** `zip(strict=True)` (3.10+) and `removeprefix` (3.9+) in
  files documented "stdlib-only, any cluster python3" — every wave combine
  and the final reduce crash on <3.10 clusters *after* the campaign burned
  its hours. Introduced by the fix train's own ruff pass; CI never executes
  shipped files on an older interpreter.
- **F21 (low)** `gpu_preamble.sh` clobbers user `PYTORCH_CUDA_ALLOC_CONF`,
  ignoring its own override convention. **F22 (low)** the other
  never-landed ledger fix: 0-byte non-CSV results read complete in the
  legacy `check_results` twin.

Fix shape: two one-line compatibility fixes + the real prize: a version-floor
gate (vermin or AST feature check) over `_build_deploy_items`' ship list in
CI, so modernization lints can never silently raise the floor of deployed
files again.

### WP-G — Preflight and config honesty (F29 F31 F32 F33 F34) — quick win

- **F29 (med)** `hpc-agent preflight` exits 0/ok:true with every check
  failing (no CliShape exit mapping), so submit-s1 briefs show a green
  preflight over a broken environment. Reproduced twice in-venv.
- **F31 (med)** `setup --cluster` exits 0 on a red probe and its "24h cache
  marker" has zero consumers — the README's "Step 6b gate" does not exist.
- **F32 (med)** `validate_clusters_config` has zero callers +
  `extra='ignore'`: config typos silently disable features and explode hours
  later. **F33 (low)** an empty cluster entry crashes preflight itself with
  an internal envelope — the documented remediation is circular.
  **F34 (low)** asset installs never prune removed skills/agents.

Fix shape: map `all_ok=False` to `EXIT_CLUSTER_ERROR` at both seats; either
wire a real marker consumer or delete the marker + README prose; call the
validator from preflight with near-miss key detection; isinstance-guard the
None entry; manifest-stamped asset pruning.

### WP-H — Campaign/overnight decision integrity (F11 F12 F13 F14 F15 F16)

- **F11 (high)** consuming an anomaly halt under consent is a no-op that
  reports `watching_healthy` all night — nothing clears the halt, refill is
  ordered behind it, and a second distinct anomaly is masked with no ledger
  line.
- **F12 (high, ruled)** a consent recorded after the driver parked is never
  consulted — the night is lost and the morning brief has no field that says
  why (`consumed_count: 0`, no reason).
- **F13 (med)** driver and stop-guard disagree on "y then a later record":
  the driver scans past a retraction nudge and launches the retracted spec;
  the guard goes silent on any unrelated later record.
- **F14 (med)** the pending marker is cleared before the resumed span runs —
  crash/OSError legs lose the human's edit silently.
- **F15 (med)** the consent `cmd_sha` binding is ambiguous between two
  derivations, with the docs and the visible marker both pointing at the one
  consumption always refuses. **F16 (med)** consent caps meter against
  `spent_*` fields nothing writes — the mandatory caps cannot fire.

Fix shape: make consumption either enable continuation or honestly report
the halt; consult the consent on the awaiting_decision resume path; unify
the y-then-later-record predicate across driver and guard (stop at the first
same-boundary record of either kind); re-park transactionally; validate the
consent token at record time against what consumption compares; feed the
spend meter at the sites that know the cost.

### WP-I — State, identity, and journal hygiene (F30 F41 F42 F43 F44 F45 F46)

- **F30 (WEAK/med)** renaming/moving the experiment dir (or NFS path aliasing)
  silently forks the journal namespace — everything vanishes, no detection,
  no relink verb.
- **F41 (med)** the canary TTL cache is keyed on parameter-only `cmd_sha` in
  a machine-global file: identical kwargs from different code/repos skip the
  canary AND satisfy the S3 gate.
- **F42 (med)** crash between run-write and index refresh → terminal run
  reads in_flight forever (repro'd; one-line filter fix).
- **F43 (med)** detached leases carry a bare pid — NFS multi-login-node
  duplicates workers; pid reuse wedges with a self-heal promise that is
  false.
- **F44/F45/F46 (low)** count-based sidecar pruning evicts live runs; the
  sidecar write-lock comment claims an exclusion that doesn't exist; read
  paths scaffold ghost namespaces (F46 reproduced live in this session —
  see `live-smoke-notes.md`).

Fix shape: detect/relink forked namespaces (durable token in `.hpc/` beats
the path hash long-term); widen the canary key with code identity; check
`record.status` in `find_in_flight_runs`; stamp host+create_time in leases;
status-aware pruning; a non-creating journal probe for readers.

### WP-J — MCP/agent surface (F01 F02 F03 F04)

- **F01 (med)** the blocking-verb fence omits monitor-flow / verify-canary /
  submit-flow: one tool call wedges the single-threaded server for up to 24h,
  and the default in-process runner has no timeout at all.
- **F02 (med)** the liveness heartbeat is swallowed by `redirect_stderr`
  under the default runner — the wedge above is also silent.
- **F03 (med)** CONTRACT.md's retry table misclassifies `outputs_missing`
  (contradicts both the code and cli-spec.md) — generate the table from
  `errors.py`. **F04 (med)** `prompts/get` serves slash bodies that instruct
  tools an MCP-only client structurally lacks; the executable
  `start_instruction` is the never-taken fallback.

Fix shape: extend the fence + bound the default runner; save the real stderr
handle at heartbeat start; generate the contract table; invert the prompt
preference.

## Swarm execution guidance

- **Order:** WP-F and WP-G first (small, isolated, high honesty-payoff), then
  WP-A → WP-B → WP-C → WP-D → WP-E → WP-H → WP-I → WP-J. WP-D's fix shapes
  are **gated on the live-cluster checklist** (`critic-gaps.json`
  `live_cluster_checks`) — land the rc/stderr capture and tests that encode
  today's *intended* semantics, but do not finalize PBS/federation behavior
  without the checks.
- **Hot-file coordination:** `ops/submit_flow.py` (WP-C), `ops/monitor_flow.py`
  (WP-B), `infra/transport.py` (WP-E, WP-A/F10), `infra/backends/_engine.py`
  (WP-D), `infra/remote.py`+`ssh_engine.py` (WP-C) are each owned by one
  package — serialize any cross-package edit to these files; worktree
  isolation per package otherwise.
- **Doctrine compliance:** most fixes resurrect dead guards — every one must
  ship its fire-path test (the repo's own enforcement-map rule). Several
  fixes must *re-point tests that currently pin the bug* (F36's
  `test_pbs.py` qdel shape, F35's ack cases, F23's announce tests) — re-point
  in the same commit, per the re-point-first precedent.
- **Verification bar:** suite green at every merge (baseline above); for
  WP-A/WP-B add the end-to-end scenario tests named per finding (fail →
  recover → re-aggregate; resubmit → census; no-marker death → settle), not
  just unit pins.
- **Ledger honesty:** F49/F22 falsify two "retired" rows in
  `upstream-fixes-2026-07.md` — correct that ledger in the same PRs that land
  the real fixes, and record the miss in its drift log.

## Follow-up sweep worklist (unswept ground)

From `critic-gaps.json`, ranked: (1) the onboarding on-ramp
(`incorporation/` + wrap/classify/build skills) — mandatory step-0, zero
coverage; (2) task-level checkpoint/resume durability under mid-write kills;
(3) persisted-state schema-version skew across upgrades; (4) scale limits
(10k-task arrays, ARG_MAX, month-long journals); (5) conformance negotiation
with degraded MCP clients; (6) batch-script/log-path injection + secrets in
shipped trees; (7) the quant pack cluster-side (same classes as F18/F05/F07);
(8) clock skew and locale. Each entry carries a concrete probe.
