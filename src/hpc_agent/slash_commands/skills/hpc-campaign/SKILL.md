---
name: hpc-campaign
description: "Start the campaign workflow with the code-driven chain (`block-drive`, first block `campaign-greenlight`) and relay each decision brief to the human for a `y`/nudge; on `y` commit the approved input spec to the journal's `resolved` and let the driver advance. A campaign spec is greenlit ONCE at start; execution then runs fully asynchronously (reconcile ticks self-chain in code) with no per-iteration human boundary — only anomaly briefs and the completion brief. The skill never resolves a decision and never interprets raw results."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Start the campaign workflow by invoking the **`block-drive`** verb — the code-driven chain (design [§2](../../../../docs/design/human-amplification-blocks.md), [§4](../../../../docs/design/human-amplification-blocks.md), [§6](../../../../docs/design/block-drive.md)). It starts at the **`campaign-greenlight`** block and exits at each human touchpoint returning a **brief**. You are the translator at those rendezvous points **only**: render the brief as a proposal, take the human's `y` or nudge, and on `y` commit the approved input spec so the next `block-drive` tick advances. You do **not** read `next_block` and dispatch the next verb yourself — that sequencing is re-homed off the LLM into the driver's chaining table (design §6).

A campaign is **not** a linear per-run chain. Its spec — goal, budget, strategy, stop criteria, anomaly policy, async-refill — is **greenlit once at start** and is the complete contract; execution then runs **fully asynchronously against the spec** (reconcile ticks self-chain in code while healthy; the strategy picks batches deterministically) with **no per-iteration human boundary** (design §4). So there are exactly three campaign blocks (`meta/campaign/blocks.py`), one per §4 touchpoint: `campaign-greenlight` (start), `campaign-watch` (a read-only health/anomaly digest of the async execution — it observes, it never runs a tick), and `campaign-complete` (the completion brief). Each hands back `{block, stage_reached, needs_decision, reason, brief, next_block?, campaign_id?}`.

The slash `/campaign-hpc` is the human-interview wrapper (path picking, slug, spec authoring); an external autonomous agent invokes this skill directly.

## This skill is the Claude-Code profile of the harness runbook

This SKILL is **one harness's profile** of a harness-NEUTRAL procedure — it is NOT the source of truth for the workflow. The block SEQUENCE (`campaign-greenlight` → `campaign-watch` → `campaign-complete`), the decision points (each tagged code-vs-judgement with its backing verb), and the consent protocol — **park → typed `y` → `append-decision` → the driver advances** (a campaign is greenlit ONCE at start, then runs fully async with NO per-iteration consent boundary) — are the SUBSTRATE, stated harness-neutrally in [`docs/generated/harness-runbook.md`](../../../../docs/generated/harness-runbook.md). That page is GENERATED from `_wire/spawn_contract.py::DECISION_POINTS` + `infra/block_chain.py` (edit `DECISION_POINTS`, never the runbook prose or this framing). A foreign (non-Claude-Code) harness drives this same workflow from that runbook and the `hpc-agent` CLI verbs alone — the CLI is the invariant substrate ([`docs/internals/harness-contract.md`](../../../../docs/internals/harness-contract.md)).

Everything ELSE below is this profile's Claude-Code-specific shim: the same neutral relay/translate role, bound to Claude Code's surfaces. Where a Claude idiom appears it is the **Claude-Code binding** of a neutral runbook step — a foreign harness supplies its own binding for the same step:

| Claude-Code idiom in this skill | Neutral runbook step it binds |
|---|---|
| "Your final action MUST be a tool call"; end-of-turn on any non-tool-call message | *Advance* — commit the `y` and fire the next `block-drive` tick without ending the turn (Claude Code fires end-of-turn on a non-tool-call message; another harness advances however it ends a turn) |
| `/loop <interval>` polling of the cheap `campaign-watch` read (never blocking) | The async health-read cadence — after the one greenlight there is no per-iteration wait; a foreign harness polls the same read on its own schedule |
| A standing overnight consent journaled once via `append-decision`; the no-push-channel disclosure | Capability-3 wake plus the honest push-channel tier (`harness-capabilities`) — the consent and its caps are substrate; only the wake delivery is harness-specific |
| Batch tool calls in one message; the `&`-compound permission-classifier note; MCP-first typed tools | Claude Code's concurrency / gating / typed-tool surfaces over the CLI verbs (the MCP catalog is a projection of the CLI registry) |
| The relay itself — render the greenlight / anomaly / completion brief, take `y`/nudge, re-present | The neutral relay contract (runbook, "Relay each decision brief"); this skill is the Claude-Code translator at those rendezvous |

The block-loop relay below is load-bearing and unchanged — this framing only names what is substrate versus what is this harness's binding.

## Invocation surface

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **MCP-first (preferred):** the typed `campaign-greenlight` / `campaign-watch` / `campaign-complete` tools from `hpc-agent mcp-serve`.
- **Read-only QUERY verbs go DIRECT through MCP — never a spec-file round-trip.** `status-snapshot`, `read-decisions`, `verify-relay`, `doctor`, `net-triage` are pure reads: call the typed MCP tool with inline args and read the result — do NOT `Write` a `.hpc/specs/*.json` file and shell `--spec` just to read state back (three tool calls where one MCP call suffices). Never relay a number you remember; relay what the query returned.
- **CLI fallback:** one call per block, spec written to a file with the `Write` tool:
  ```bash
  hpc-agent campaign-greenlight --spec <path> --experiment-dir <dir>
  ```
  `--spec` takes a **file path only** — inline JSON (`--spec '{...}'`) is refused at the seam. Literally: `Write` the spec JSON to `.hpc/specs/campaign-greenlight.json`, then run
  ```bash
  hpc-agent campaign-greenlight --spec .hpc/specs/campaign-greenlight.json --experiment-dir .
  ```
  Parse the envelope from stdout. Read files with `Read`/`Grep`/`Glob`, never a shell `python -c` / `bash -c` / `jq` (the auto-mode classifier hard-blocks those). To get a verb's input schema, use `hpc-agent describe <verb> --schema` (or the MCP tool's `inputSchema`) — never `find`/`cat`/`inspect` a schema file.

## The driver loop

`block-drive` chains the three campaign touchpoints in code (`campaign-greenlight` → the async `campaign-watch` surface → `campaign-complete`); you translate at the rendezvous points it stops at. Each tick:

1. **Invoke `block-drive`.** The first call starts at `campaign-greenlight` — an un-greenlit manifest returns `needs_greenlight` with the digested spec brief. Later calls consume the approved spec from the journal's `resolved` and advance — or re-run `campaign-greenlight` for a fresh digest when a nudge edited the spec. The route is computed in code, never a verb you pick.
2. **Render the brief the driver returns as a proposal.** At greenlight, the digested spec; at an anomaly, the `anomaly_brief` (a §5 loud-fail guard tripped, or a budget halt); at completion, spend vs budget, iterations, stop reason, a code-extracted per-iteration outcome table, and an empty `proposed_interpretations` slot. Relay the code-drafted digest; never re-interpret it. The greenlight brief also carries an additive `evidence` field — the ADVISORY evidence-memory point digest for the campaign's declared tags (its `render` is code-composed; relay it VERBATIM when present, and treat an `{unavailable}` stub as a disclosed no-op). It NEVER blocks the greenlight: priors inform the human, they never gate the spec.
3. **The human answers `y` or nudges.** A single `y` approves the proposed input spec; a nudge edits the campaign spec (goal, budget, strategy, stop criteria) and re-presents. Loop until `y`.
4. **On `y`, commit the approved input spec to the journal's `resolved`, then invoke `block-drive` again to advance.** The commit *is* the approval (design §3, §5). Append the record:
   ```bash
   hpc-agent append-decision --spec <path> --experiment-dir <dir>
   ```
   `scope_kind: "campaign"`, `scope_id: <campaign_id>`, `block: <terminated block>`, `evidence_digest: <brief>`, `proposal: <what you surfaced>`, `response: "y"`, and the approved input spec under `resolved` (a spec, never the nudge string). At greenlight, the `confirm: true` path stamps the marker **and journals its own decision** (the block composes `append-decision`). **Do not end your turn after committing without firing the next tick** — the decision-rendezvous Stop-hook (design §5) blocks the stop until the driver advances. **Your final action MUST be a tool call, not a chat message** — the harness fires end-of-turn on any non-tool-call message, so a closing narration silently ends your turn and the driver never resumes; make the next `block-drive` tick the turn's last act.

A campaign is greenlit **once**, then runs asynchronously: `watching_healthy` (`continue` / `wait_in_flight`) is **no boundary** — ticks self-chain in code; surface the health digest and let the human walk away. `watching_refill` (advance found free pool slots with budget headroom) is likewise **no boundary** — `block-drive` hands off in code to the `campaign-refill` actor (`ops/campaign_refill.py`) the greenlight already authorized; relay the digest, never treat it as a decision. Only `watching_anomaly` and `watching_complete` are rendezvous points. **NEVER hand-compute a decision or interpret raw results:** code digests the campaign's durable state into each brief; the human decides.

On any connection failure (an SSH timeout, `ssh_unreachable`, `ssh_circuit_open`), run `hpc-agent net-triage` — the bounded, breaker-aware connectivity differential — before concluding a network cause; never diagnose with improvised ssh probes.

## Never-stall

Campaign execution is asynchronous by design — after the greenlight there is **no** per-iteration wait. `campaign-watch` is a cheap read; poll it on a schedule (`/loop <interval> /campaign-hpc`, or a cron-scheduled tick) rather than blocking. Anomaly and completion briefs arrive as notifications from the async driver.

## Overnight mode — standing consent (notebook-audit.md item 8)

**`status-watch` (or the campaign's own async reconcile self-chain) is the ONLY sanctioned watch for cluster state — never a hand-rolled local-log tail on a cluster job (structurally blind: wrong machine).** For an overnight anomaly boundary, arm the wake so its terminal re-invokes the driver; a poll loop that scrapes a remote log is the improvisation class run #11 demonstrated.

**When the human authorizes the campaign to keep advancing across an anomaly boundary while they sleep, that is a STANDING CONSENT — their OWN typed utterance accepting the fallout, journaled once via `append-decision` under `block: overnight-consent` (scope `campaign`).** Never compose it (a bare `y` or a synthesized utterance is refused). Its `resolved` MUST carry the hard caps (`expires_at` morning boundary + `budget_cap` and/or `walltime_cap`) and the spec-identity binding (`cmd_sha`); consent dies on a spec change. Pair it with an armed wake (`resolved.wake = {"kind": "status-watch", ...}`) in the same breath — a pre-consent no watch can consume is theater. In the morning, surface the overnight brief: everything the consent consumed, with `failed_at` vs `surfaced_at` so the disclosure latency of any overnight anomaly is visible; where the harness declares no push channel that latency is part of the accepted fallout.

## Strategy authoring (path B — before greenlight)

A closed-loop campaign's `.hpc/tasks.py` **is** the strategy. Scaffold it with `hpc-agent scaffold-strategy --name {optuna,pbt} --output-dir <experiment_dir>` — never hand-roll a controller, and never `Read` the framework's `optuna_strategy.py` / `pbt_strategy.py` from site-packages to learn the contract. The load-bearing invariants the template already wires (you customize only the search space):

- **ask/tell run ONLY on the orchestrator; compute nodes call ONLY `resolve(task_id)`.** The optimizer import is local to `_propose`; proposals are indexed by completed count (load-idempotent).
- **`trial_token` is the reconciliation key** — stripped from `cmd_sha` (never busts dedup) but exported as `$HPC_KW_TRIAL_TOKEN` and re-paired with results; opaque bytes the framework never interprets.
- **`_optuna_trial_number` (or equivalent unique marker) is mandatory on path B** — without it repeat params collide on `cmd_sha`, the second iteration dedupes, and the campaign silently collapses. `campaign-greenlight`'s validation surfaces `stochastic_marker_missing` as a hard gate.
- **Custom reduce is an `aggregate_cmd` on the sidecar, run cluster-side** (env-var I/O; pulls back one JSON).

See [campaign-lifecycle.md](../../../../docs/internals/campaign-lifecycle.md) and [campaign-seam.md](../../../../docs/design/campaign-seam.md).

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required |
| `campaign_id` | Required |
| `path` | Caller (`"A"` manual grid; `"B"` strategy-driven) |

## Notes

- **The skill never resolves a decision and never interprets raw results.** Code digests the campaign's durable state (manifest, sidecars, budget join, stop reason) into each brief; the human decides the greenlight, any anomaly, and the final interpretation.
- **Greenlit once, then asynchronous.** There is no per-iteration human loop by design; async-refill correctness (drain-before-stop, budget headroom) is the driver's job, not a decision the skill relays.
- **Every `y`/nudge is journaled** under the campaign scope (append-only, one record per exchange) — the greenlight decision, the anomaly acknowledgements, and the completion interpretation.
