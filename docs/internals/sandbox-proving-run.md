# Sandbox proving runs — the autonomous rung between hermetic and live

**Status: normative jurisdiction; implementation PLANNED.** The build units
(U1–U7) of the plan are not yet landed; this page fixes the ladder, the trust
doctrine, and the certification boundary those units build under, so no future
unit can quietly widen what rung-2 evidence means.

- Plan: [`docs/plans/sandbox-proving-run-2026-07-18.md`](../plans/sandbox-proving-run-2026-07-18.md) (units U1–U9, sequencing, risks — the build spec)
- Substrate: [`scheduler-integration-ci.md`](scheduler-integration-ci.md) (the container Slurm lane the sandbox rung reuses), [`.github/workflows/scheduler-integration.yml`](../../.github/workflows/scheduler-integration.yml), [`ci/slurm/`](../../ci/slurm)
- Seeding precedent: [`src/hpc_agent/conformance/relay_fixtures.py`](../../src/hpc_agent/conformance/relay_fixtures.py) (the conformance kit's sanctioned ephemeral-namespace fixture posture)

## 1. The rung ladder and its jurisdictions

A live proving run today adjudicates TWO different things at once:

1. **Contract kinks** — spec shapes, block-chain sequencing, gate provenance
   rules, journal-namespace coupling. Discoverable with no cluster at all.
2. **Cluster-environment truth** — login-shell PATH, banners, per-node /tmp,
   MaxStartups throttles, scheduler dialect quirks. Discoverable ONLY live.

The 2026-07-18 drill attempt produced six snags; five were class 1, and every
class-1 snag burned a human round-trip that an autonomous sandbox run would
have eaten silently. The ladder, with strict jurisdiction:

| Rung | Instrument | Adjudicates | Runs |
|---|---|---|---|
| 1 | hermetic tests (`tests/`, faultinject, conformance self-run) | logic, gate semantics, recovery ladders | every push, xdist |
| 2 | **sandbox proving run** | the full harness contract end-to-end: block loop, briefs, gates, submit-once, kill drill, reconcile — against a real scheduler API | any dev session, autonomously; CI on dispatch + submit-path PRs |
| 3 | live proving run | cluster-environment truth only | human-scheduled windows |

Rung 2 exists so rung 3 starts from a pipeline already proven end-to-end and
surprises only on class-2 territory. Each rung's evidence is citable only for
its own jurisdiction — §3 is the enforcement of that rule.

The existing container smoke ([`tests/integration/scheduler/test_scheduler_smoke.py`](../../tests/integration/scheduler/test_scheduler_smoke.py))
drives the flow atoms (`submit_flow → monitor_flow → aggregate_flow`) with no
transport/scheduler mocks, but deliberately BYPASSES the block loop, the
decision journal, and every gate. That bypassed layer — not the atoms — is
rung 2's territory.

## 2. Trust doctrine — the part that must never bend

**Gates are never bypassed in the sandbox — they fire for real against a
seeded, namespace-isolated substrate.**

- **Ephemeral journal home, always.** The sandbox journal home is ALWAYS an
  ephemeral `HPC_JOURNAL_DIR` (CI: `$RUNNER_TEMP`; local: a tmpdir). No
  exceptions.
- **Structural refusal of the real home.** Seeding helpers REFUSE to run when
  `HPC_JOURNAL_DIR` is unset or resolves inside `~/.claude/hpc` — the helper
  is structurally incapable of touching a production namespace. The refusal
  lives in code, not in reviewer vigilance.
- **Seeded provenance stamps.** Every seeded utterance carries
  `{"seeded_by": "sandbox-proving", "run": <sandbox_run_ref>}` in the record
  — an additive field the gate ignores and auditors read.
- **Support-code posture.** The seeding helper lives under `tests/`/`ci/`
  support, is NOT shipped in the wheel, and is named for what it is
  (`sandbox_seed.py`), mirroring the conformance kit's fixture posture.
- **What a pass means.** A sandbox run proves *the gates fire correctly* —
  including REFUSALS (the negative assertions are first-class). It never
  proves *a human approved anything*.

## 3. What a sandbox run can NEVER certify

Rung 3 keeps its certification monopoly. No rung-2 evidence, however green,
ever grounds:

1. **A default flip.** A shipped default changes on live evidence only; a
   sandbox green is never the cited justification.
2. **A "validated live" claim.** The phrase and its equivalents
   ("production-proven", "cluster-verified") attach to rung-3 runs only.
3. **Cluster-environment truth.** Login-shell PATH, module systems, banners,
   per-node /tmp, MaxStartups throttles, fairshare behavior, per-site
   scheduler dialect — absent from a container by definition. The 2026-07-18
   F7 snag (the SGE login-shell PATH) is the canonical example: class 2,
   stays live.

The honest container gap is part of this boundary, not a defect: the container
is single-node Slurm — no fairshare, no throttles, no multi-node — and those
are class-2 by definition. An SGE container (U9) covers the SGE *dialect*,
never a site's *login profile*; class-2 residue exists per-site regardless.
Do not oversell U9.

## 4. Traceability — the 2026-07-18 snags vs the catching unit

Each snag from the motivating drill attempt, and the rung-2 unit that would
have caught it (unit specs live in the plan):

| 2026-07-18 snag | Rung-2 unit that would have caught it |
|---|---|
| bare block-drive fresh-start dead-end | U3 (first driver step) / U5.1 |
| `*_resolved` boolean shape | U3 / U5.2 |
| `PLACEHOLDER` pattern refusal | U5.3 |
| provenance gate resolved-shape | U3 (greenlight step) / U5.4 |
| namespace-scoped utterance log | U5.5 (the decoy-namespace pin) |
| SGE login-shell PATH (F7) | **not rung-2** — class 2, stays live (SGE container = U9) |

## 5. Maintainer rulings (2026-07-19)

1. **Blocking: advisory forever.** The lane's purpose is a simulation
   environment for the dev loop — contract kinks get eaten autonomously. Its
   teeth live in the workflow: the pre-push gate for submit-path changes and
   every live runsheet's pre-flight (§6). A GitHub required-check optimizes
   for a multi-contributor drive-by threat model this repo does not have, and
   would add flake surface to every submit-path PR.
2. **Evidence JSON: CI artifact only.** The artifact's job is per-run
   evidence — prove the contract held for THAT run, diagnose where it didn't.
   Durability of *claims* is carried by the docs that cite the run (the
   runsheet pre-flight cites sha + workflow-run id; the GitHub run URL is the
   durable pointer), not by accumulating every run's JSON. A trend ledger
   answers a different question; add one only if trend-reading becomes a real
   activity, and never by CI committing to main (loop hazard).
3. **U9 (SGE container): build speculatively, with the core wave** — the
   maintainer overrode the plan's "on next defect" recommendation. The §3
   caution stands: U9 covers the SGE dialect, never a site's login profile.

## 6. The runsheet pre-flight hook

Every future live runsheet's pre-flight gains one line:

> sandbox proving run green at \<sha\>? (rung-2 gate — cite the workflow-run id)

No runsheet TEMPLATE file exists today — the runsheets under `docs/plans/`
(e.g. [`proving-run-15-runsheet.md`](../plans/proving-run-15-runsheet.md)) are
dated historical records, not templates, and are never retro-edited. Until a
template exists, this section is the canonical specification of the line;
copy it into each new runsheet's pre-flight as it is written. The lane stays
advisory (ruling 1): the line is a question with a cited sha, never a
required check.

## 7. Invocation surfaces (U7)

Three bindings drive the same driver
([`scripts/run_sandbox_proving.py`](../../scripts/run_sandbox_proving.py)),
in decreasing order of sanction for this repo's dev box:

- **CI dispatch — the sanctioned native-Windows binding.** The dev box has
  no docker, so the autonomous path is the GitHub lane:
  `gh workflow run scheduler-integration.yml` then `gh run watch` (add
  `-f with_kill_drill=true` to also run the U4 kill drill — the workflow
  tolerates the script's absence with a skip, never an error, until U4
  lands). The `sandbox-proving` job runs after `slurm-smoke` on the same
  container build; the evidence JSON + markdown land as the
  `sandbox-proving-evidence` workflow artifact (ruling 2 — per-run evidence,
  never a committed ledger).
- **CI on pull_request.** The lane fires on its surfaces (`ci/`,
  `scripts/run_sandbox_proving.py`, `src/hpc_agent/**`, plus the smoke
  lanes' original scoping) and stays advisory — never a required check
  (ruling 1).
- **Docker-capable machines: `--local`.** `python scripts/run_sandbox_proving.py
  --local` is the self-contained binding: it stands the [`ci/slurm/`](../../ci/slurm)
  container up itself (build, throwaway keypair, wheel install, readiness
  wait — the workflow's bring-up mirrored step-for-step), runs the chain
  with `HPC_SUBMIT_ONCE=1`, and tears the container down on exit
  (`--keep-container` to keep it for inspection). `HPC_JOURNAL_DIR` must
  still point at an ephemeral tmpdir — the §2 guard refuses otherwise —
  and on a docker-less host `--local` errors with the dispatch binding
  above as its guidance.

## Drift log

- 2026-07-19: U7 invocation surfaces landed — the `sandbox-proving` job in
  `scheduler-integration.yml` (`needs: slurm-smoke`; path-filtered +
  dispatchable; the `with_kill_drill` dispatch input skips cleanly until U4
  lands) and this §7 (dispatch binding for docker-less Windows; the
  `--local` contract for docker-capable machines).

- 2026-07-19: Created (plan U8). The ladder, trust doctrine, never-certify
  list, traceability table, and the 2026-07-19 rulings are lifted normative
  from the plan doc; the build units U1–U7 remain PLANNED there. No runsheet
  template exists, so §6 specifies the pre-flight line rather than editing
  one into place.
