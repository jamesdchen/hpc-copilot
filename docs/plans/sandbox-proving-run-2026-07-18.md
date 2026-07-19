# Sandbox proving runs — the autonomous rung between hermetic tests and live proving runs

**Status: PLANNED (this doc). No code lands with this commit.** Maintainer ask
(2026-07-18, verbatim intent): *"is it better for devx to do in-claude session
sandbox runs? … a lot of the kinks will be ironed out autonomously in a dev
session"* — ruled YES, planned fully here. Base facts verified at `1ac2e46a`.

## 1. The thesis and the jurisdiction ladder

A live proving run today adjudicates TWO different things at once:

1. **Contract kinks** — spec shapes, block-chain sequencing, gate provenance
   rules, journal-namespace coupling. Discoverable with no cluster at all.
2. **Cluster-environment truth** — login-shell PATH, banners, per-node /tmp,
   MaxStartups throttles, scheduler dialect quirks. Discoverable ONLY live.

The 2026-07-18 drill attempt produced six snags; **five were class 1** (bare
block-drive fresh-start, boolean `*_resolved` walk flags, placeholder pattern
refusals, provenance-gate resolved-shape, namespace-scoped utterance log) and
**one was class 2** (F7, the SGE login-shell PATH). Every class-1 snag burned a
human round-trip that an autonomous sandbox run would have eaten silently.

The target ladder, with strict jurisdiction:

| Rung | Instrument | Adjudicates | Runs |
|---|---|---|---|
| 1 | hermetic tests (`tests/`, faultinject, conformance self-run) | logic, gate semantics, recovery ladders | every push, xdist |
| **2** | **sandbox proving run (THIS PLAN)** | **the full harness contract end-to-end: block loop, briefs, gates, submit-once, kill drill, reconcile — against a real scheduler API** | any dev session, autonomously; CI on dispatch + submit-path PRs |
| 3 | live proving run | cluster-environment truth only | human-scheduled windows |

Rung 3 keeps its certification monopoly: **no default flip, no contract
"validated live" claim, ever cites rung-2 evidence.** Rung 2 exists so rung 3
starts from a pipeline already proven end-to-end and surprises only on class-2
territory.

## 2. What already exists (verified substrate, reuse — never rebuild)

- **`ci/slurm/` + `.github/workflows/scheduler-integration.yml`** — dockerized
  single-node Slurm (real `sbatch`/`squeue`/`sacct`), sshd on :2222, throwaway
  keypair, wheel installed into BOTH container python and driver env,
  `ci_clusters.yaml` generated, **`HPC_JOURNAL_DIR` already isolates the
  journal home** (`docs/internals/scheduler-integration-ci.md`).
- **`tests/integration/scheduler/test_scheduler_smoke.py`** — drives
  `submit_flow → monitor_flow → aggregate_flow` (the flow atoms) with no
  transport/scheduler mocks. It deliberately BYPASSES the block loop, the
  decision journal, and every gate — which is exactly the uncovered layer.
- **Conformance-kit fixture discipline** (`conformance/relay_fixtures.py`
  `seed_triple`/`fixture_repo`) — the sanctioned precedent for seeding journals
  inside ephemeral namespaces to test enforcement without weakening it.
- **The hermetic submit-once drills** (`tests/faultinject/test_submit_once.py`)
  — the kill-window state machine, no SSH.
- **`hpc-agent interview`** — regenerates the fixture experiment
  deterministically (proven today: it is how the drill's sweep was minted).

## 3. Trust doctrine (the part that must never bend)

**Gates are never bypassed in the sandbox — they fire for real against a
seeded, namespace-isolated substrate.**

- The sandbox journal home is ALWAYS an ephemeral `HPC_JOURNAL_DIR` (CI:
  `$RUNNER_TEMP`; local: a tmpdir). Seeding helpers REFUSE to run when
  `HPC_JOURNAL_DIR` is unset or resolves inside `~/.claude/hpc` — structurally
  incapable of touching a production namespace.
- Seeded utterances carry `{"seeded_by": "sandbox-proving", "run": <sandbox_run_ref>}`
  provenance in the record (additive field; the gate ignores it, auditors read it).
- The seeding helper lives under `tests/`/`ci/` support, is NOT shipped in the
  wheel, and is named for what it is (`sandbox_seed.py`), mirroring the
  conformance kit's fixture posture.
- A sandbox run proves *the gates fire correctly* (including REFUSALS — see U5's
  negative assertions); it never proves *a human approved anything*.

## 4. Plan units

### U1 — Sandbox experiment generator (S)
`tests/integration/scheduler/sandbox_fixture.py`: builds a scratch experiment
dir — `train.py`-style `@register_run` executor (the pi shape, plus an optional
failing-executor variant for U6), then invokes the REAL `interview` primitive
(`produced_by: {kind: agent, operator: sandbox-proving}`) to materialize
`tasks.py`/`interview.json`/`axes.yaml`. Parameterized sweep so successive
sandbox runs mint fresh `run_id`s (the determinism lesson from today).

### U2 — Sanctioned authorship seeding (S)
`tests/integration/scheduler/sandbox_seed.py`: writes the utterance log +
(where a scenario needs it) prior decision records into the SANDBOX namespace
only, with the §3 guards and provenance stamps. API:
`seed_utterance(journal_home, experiment_dir, text)` /
`seed_prior_signoff(...)`. Unit-tested against the guard (refuses real home).

### U3 — The block-loop driver (M — the core)
`scripts/run_sandbox_proving.py` (driver-side, wheel-external): drives the FULL
chain the way a harness does — `block-drive` fresh start → S1 walk (asserting
the recorded-resolution booleans) → resolve (asserting run_id minting) →
fused `--approve` greenlights (asserting the provenance gate accepts
brief-shaped `resolved` and the authorship gate accepts the seeded utterance) →
S2 stage+canary → S3 submit+watch → S4 harvest, with `HPC_SUBMIT_ONCE=1`,
against the container cluster. Every brief's envelope shape is asserted
(`stage_reached`/`needs_decision`/`next_block`/`brief` keys). Output: a
machine-readable evidence JSON mirroring the run-15 §2.3 table + a human
markdown render.

### U4 — The autonomous kill drill (M)
Second fixture run: after the S3 greenlight, read the detached S3 lease PID
from the sandbox `_detached/`, poll the container `squeue -o '%i %k'` for
`<run_id>#0`, kill the local dispatch process in the window, then assert the
full recovery contract: sidecar `submitting/job_ids=[]` → jobmap marker
`pending` + wave-0.id rc==0 on the container → reconcile ADOPTS →
`in_flight` with the marker's id → **exactly one array under the token, zero
re-qsub** → adopted array harvests. Window-miss → parameter-bump retry loop
(bounded, 3 attempts), same as the live runsheet. The hermetic faultinject
drills stay; this is their scheduler-API twin.

### U5 — Contract-kink regression pins (S)
One test module encoding today's five class-1 snags as permanent assertions,
each with the incident date in its docstring:
1. bare `block-drive {workflow: submit}` fresh-start returns the actionable
   skip (never a crash);
2. walk `*_resolved` flags are booleans and honor recorded repo resolutions;
3. resolve placeholders: schema-valid placeholder `run_id`/`cmd_sha` are
   overridden by `compute-run-id` (and invalid shapes refuse with the
   spec_skeleton remediation);
4. provenance gate: full-input-spec `resolved` REFUSES; brief-shaped `resolved`
   passes;
5. authorship gate: un-uttered `goal`/`task_generator` REFUSES even in the
   sandbox (negative control); seeded utterance passes; and the gate provably
   reads ONLY the sandbox namespace (a decoy utterance planted in a second
   namespace must NOT unlock — the namespace-coupling pin).

### U6 — Anomaly arms (M, second wave)
Scenario matrix driven by the same driver: (a) failing-executor canary →
`canary_failed` brief → resubmit-failed arm; (b) `scancel` the array mid-watch
→ `watching_anomaly` → reconcile arm; (c) doctor + fleet scan inside the
sandbox namespace (stall a driver by killing its tick process); (d) alerts-ack
round-trip. Each asserts the BRIEF (code-rendered) rather than internal state
where a brief exists — the same relay-doctrine the live runs follow.

### U7 — Invocation surfaces (S)
- **CI:** a `sandbox-proving` job appended to `scheduler-integration.yml`
  (same container build, runs U3→U6 after the existing smoke; still
  non-required, path-filtered + dispatchable).
- **Dev box (no docker on native Windows):** `gh workflow run
  scheduler-integration.yml` + `gh run watch` — the dev session's autonomous
  path TODAY. Document as the sanctioned Windows binding.
- **Docker-capable dev machines:** `scripts/run_sandbox_proving.py --local`
  stands the container up itself (reusing `ci/slurm/`), runs, tears down.

### U8 — Docs + jurisdiction (S)
`docs/internals/sandbox-proving-run.md`: the rung ladder + jurisdiction table
(§1), the trust doctrine (§3), the "what a sandbox run can NEVER certify"
list (default flips, live-validation claims, cluster-env truth), and the
traceability table (§5). Cross-link from `proving-run` runsheets: every future
live runsheet's pre-flight gains one line — "sandbox proving run green at
<sha>? (rung-2 gate)".

**LANDED (2026-07-19):** [`docs/internals/sandbox-proving-run.md`](../internals/sandbox-proving-run.md)
carries the ladder, doctrine, never-certify list, traceability, and the §7
rulings normatively. No runsheet TEMPLATE file exists (the runsheets under
`docs/plans/` are dated historical records), so the doc's §6 specifies the
pre-flight line for future runsheets instead of editing one into place.

## 5. Traceability — today's snags vs this plan

| 2026-07-18 snag | Rung-2 unit that would have caught it |
|---|---|
| bare block-drive fresh-start dead-end | U3 (first driver step) / U5.1 |
| `*_resolved` boolean shape | U3 / U5.2 |
| `PLACEHOLDER` pattern refusal | U5.3 |
| provenance gate resolved-shape | U3 (greenlight step) / U5.4 |
| namespace-scoped utterance log | U5.5 (the decoy-namespace pin) |
| SGE login-shell PATH (F7) | **not rung-2** — class 2, stays live (SGE container = future U9) |

## 6. Sequencing, sizes, risks

- Order: U1+U2 (parallel, S) → U3 (M) → U5 (S) → U4 (M) → U7 (S) → U6 (M) →
  U8 (S). U1–U5+U7 is the shippable core (one CI job green = the rung exists);
  U6 widens coverage after.
- Estimated: core ≈ one focused build wave (2–3 Opus builders + verifier, the
  established protocol); U6 a second wave.
- **Risks:** container Slurm fidelity (single-node, no fairshare/throttles —
  fine: those are class-2 by definition); CI wall-clock (container build is
  gha-cached; the block loop adds minutes — budget 20→30 min timeout); Windows
  dev box cannot run locally (mitigated by U7's dispatch binding — and the
  20-min CI round-trip is still autonomous, just not instant); flake posture
  (inherits the lane's non-required status until stable, per its own doctrine).
- **SGE/PBS gap named:** rung 2 covers the slurm family first. An SGE container
  (U9, unplanned) would have caught F7's *dialect* but not hoffman2's *login
  profile* — class-2 residue exists per-site regardless. Do not oversell U9.

## 7. Maintainer rulings (2026-07-19 — all three RULED)

1. **Blocking: advisory forever.** The lane's purpose is a simulation
   environment for the dev loop — Claude Code ships faster by eating
   contract kinks autonomously. Its teeth live in the workflow: the
   pre-push gate for submit-path changes and every live runsheet's
   pre-flight ("sandbox proving run green at \<sha\>?", U8). A GitHub
   required-check optimizes for a multi-contributor drive-by threat model
   this repo does not have, and it would add flake surface to every
   submit-path PR.
2. **Evidence JSON: CI artifact only.** The artifact's job is per-run
   evidence — prove the contract held for THAT run, diagnose where it
   didn't. Durability of *claims* is carried by the docs that cite the run
   (the runsheet pre-flight cites sha + workflow-run id — the GitHub run
   URL is the durable pointer), not by accumulating every run's JSON. A
   trend ledger answers a different question; add one only if trend-reading
   becomes a real activity, and never by CI committing to main (loop
   hazard).
3. **U9 (SGE container): build speculatively, with the core wave** —
   maintainer overrode the plan's "on next defect" recommendation
   (2026-07-19). §6's "do not oversell U9" caution stands: U9 covers the
   SGE *dialect*, never a site's login profile.

## Drift log

- 2026-07-19: U8 LANDED — `docs/internals/sandbox-proving-run.md` created
  (ladder, doctrine, never-certify list, traceability, §7 rulings carried
  normatively; `docs/internals/README.md` index row added). No runsheet
  template file exists, so the pre-flight line is specified in the internals
  doc's §6 rather than edited into a template.
- 2026-07-19: §7 RULED — blocking = advisory forever (teeth in the
  workflow: pre-push gate + runsheet pre-flight, not a required check);
  evidence JSON = CI artifact only (claims cited via run ids in docs; no
  ledger, never CI-commits-to-main); U9 = build speculatively with the
  core wave (maintainer override of "on next defect").
- 2026-07-18: Created. Verified against: `scheduler-integration.yml` (container
  lane + `HPC_JOURNAL_DIR` isolation), `tests/integration/scheduler/`
  (smoke drives flow atoms, bypasses block loop), `conformance/relay_fixtures.py`
  (seeding precedent), the six snags of the 2026-07-18 drill attempt
  (five class-1, one class-2 — the plan's motivating incident).
