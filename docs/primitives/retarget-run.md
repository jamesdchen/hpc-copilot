---
name: retarget-run
verb: workflow
side_effects:
- writes-sidecar: <experiment>/.hpc/runs/<new_run_id>.json (the retargeted sidecar)
- ssh: <old-cluster> (best-effort supersession kill; non-blocking)
idempotent: true
idempotency_key: old_run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent retarget-run --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.retarget_run.retarget_run
---
# retarget-run

The cluster-retarget recovery arm (proving-run-5 wave 5.2). The block-drive anomaly
terminators (`submit-s2`/`canary_failed`, `submit-s3`/`watching_anomaly`) name
recovery *actions*, but cluster-retarget was the one action with no verb — so the
agent freelanced ~5 steps (close out → re-resolve → re-mint → supersede → re-canary)
and fumbled three of them (proving run #4/#5, findings 9/10/13). This verb SEQUENCES
those steps in **code, not in the model**, composing pieces that already exist.

Given the failed attempt's `old_run_id` and a field delta that names a **new
cluster** (`{"cluster": "hoffman2"}`), it:

1. **re-resolves** under a NEW run_name — reusing `revise-resolved`'s
   sidecar-reconstruction with the run_name overridden, so `job_env` / activation /
   `ssh_target` / `backend` / the sidecar are all RE-DERIVED for the target cluster
   (the finding-13 class — `job_env` dropped across a hand-carried retarget — closed
   by construction);
2. **supersedes** the old attempt (`supersede_run`) — closes it and its `-canary`
   pairing and stamps the old→new link, so a fresh run_id is not a scope-hop escape
   hatch (proving run #4, finding g/h). Best-effort + **non-blocking**: an unreachable
   old cluster records a `pending_closure` marker instead of grinding on `qdel`
   (run #8's MaxStartups-throttled hoffman2);
3. **hands off** to `submit-s2` via the `next_block` hint — S2's detach-by-contract
   worker owns the re-canary poll (the #160 gate: the 1-task canary on the NEW cluster,
   verified BEFORE any main array is offered). This verb NEVER runs the canary inline,
   so it returns in seconds — the non-blocking contract that makes it safe to expose as
   a curated MCP tool (run #8: the agent, unable to reach it over MCP, hand-ran
   kill→confirm→revise against the throttled cluster and wedged).

**Why a NEW run_name (the design point).** A run_id keys on parameters + run_name
only (#207): a retarget keeps the SAME params (only the cluster moves), so KEEPING
the run_name would mint the IDENTICAL run_id on the new cluster and layer-1 dedup
would RE-ATTACH to the failed attempt instead of superseding it. So this verb cannot
just call `revise-resolved` (which derives run_name from the run_id and keeps it); it
re-points `revise-resolved`'s reconstruction helper with a FRESH run_name
(`<old_run_name>-<cluster>`, code-derived — the LLM never authors it), giving a
distinct run_id wave-2 supersession can close cleanly.

**Ordering: resolve → supersede → hand-off.** Resolve runs first — it keys its own
resume-vs-fresh detection on the NEW run_id, so it never trips on the old attempt's
still-live canary (the retarget-under-a-live-canary case). Only then does
`supersede_run` close the old attempt, so when S2's detached worker runs the re-canary
its own supersession gate finds no live same-identity sibling and passes without a
`supersedes` field.

**The load-bearing guard.** The `patch` must name a `cluster` *different* from the
failed attempt's — a same-cluster (or clusterless) delta would mint a run_id that
collides with the attempt being superseded (a self-supersession, closing the very run
it re-launches), so it is refused with a directive to use `revise-resolved` instead.
The derived-field guard is `revise-resolved`'s own: a `patch` key naming `job_env` /
`executor` / `ssh_target` / … is refused with `spec_invalid`.

**It does not bypass the gates.** The re-canary is the #160 canary gate (cheap,
sandboxed, verified before any main array) — run in S2's detached worker after the
re-`y`, never inline here; the returned brief carries `needs_decision=True`, so the
human re-`y`s it through the EXISTING `append-decision` path (the authorship +
brief-provenance gates still run on the re-commit), and the main array stays behind the
S3 greenlight gate. `retarget-run` only supersedes, re-resolves, and hands off to
`submit-s2` — it never runs the canary itself and never launches the main array.

## Routing (the recovery arm)

At an anomaly terminator the driver parks with a brief; the human's nudge selects the
recovery. `block_chain.recovery_arm_verb(current_verb, stage_reached, delta_fields)`
is the code SoT that maps a `cluster` delta at `canary_failed` / `watching_anomaly` to
`retarget-run` — "the route is a function of the spec — the delta's target field
selects the arm, computed in code, never a verb the model picks" (design §4.1). The
`hpc-submit` skill consults it at the rendezvous, exactly as a spec-changing nudge
routes through `revise-resolved`. The arm is deliberately kept off the deterministic
`SUCCESSORS` auto-chain: a bare `y` at an anomaly has no successor, so the arm fires
only when the human's nudge names the retarget.
