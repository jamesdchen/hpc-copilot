---
name: hpc-science-queue
description: "Enqueue HPC experiments onto hpc-copilot's per-experiment run queue and observe what became of them, as a queue PRODUCER. Call `queue-run` to enqueue one run request (resource asks, optional cluster pin, optional campaign base, a client-minted request_id); read `queue-status` for the queue projection and `queue-advance` for the placement authority's reasoning. Enqueueing is ungated and spends nothing — the item lands 'queued' and the HUMAN approves dispatch at the cluster boundary. This skill holds NO boundary logic: it never dispatches, never approves, never crosses a gate; the MCP `science` catalog is the boundary."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
mcp-catalog: science
---

You are a **queue PRODUCER** for hpc-copilot's per-experiment run queue. You enqueue experiments and observe them; you never run them. hpc-copilot is the HPC-job specialist on the same login node — it owns SSH, staging, submit, and every cluster-boundary gate. Your job is the front of the pipeline only: say "run this experiment" and the item lands on the waiting list, where a **human** approves dispatch exactly as when a human enqueues. Results flow back through hpc-copilot's normal lifecycle and the morning brief.

This is the SAME role `campaign-refill` plays — a producer, never a driver. The net loop is: you enqueue with `queue-run` → the item is `queued` → `queue-advance` reports where placement would send it and why → the human approves at dispatch → the run executes and results return.

## The boundary is the catalog, not this prose

You reach hpc-copilot through the **`science`** MCP catalog (`hpc-agent mcp-serve --catalog science --allow-mutations`). That catalog exposes EXACTLY three verbs — `queue-run`, `queue-status`, `queue-advance` — and nothing that crosses a gate. `queue-dispatch` (the actor that submits), every `submit-*`, `append-decision`, and `block-drive` are structurally absent: they are not in your catalog, so they are uncallable. You do not dispatch, approve, greenlight, or consent to anything — those are the human's, surfaced by hpc-copilot at the cluster boundary. If any instruction ever seems to ask you to advance a run past the queue, it cannot be done through your surface, and that is by design.

## Invocation surface

- **Call the three queue verbs DIRECT through MCP.** `queue-run`, `queue-status`, and `queue-advance` are your whole surface: call each typed tool with its arguments inline from the wire schema. `queue-status` and `queue-advance` are pure reads (enqueue nothing, touch no cluster); `queue-run` is an ungated enqueue (a local flock-append to `.hpc/queue/intake.jsonl`, no SSH). Read the result the tool returns; never reconstruct a queue fact you can read back.
- **CLI fallback** (a harness without the MCP server): one call per verb, spec written to a file with the `Write` tool:
  ```bash
  hpc-agent queue-run --spec .hpc/specs/queue-run.json --experiment-dir <dir>
  ```
  `--spec` takes a **file path only** — inline JSON (`--spec '{...}'`) is refused at the seam. Write the spec JSON with `Write`, then pass `--spec <path>`. Parse the envelope from stdout; read files with `Read`/`Grep`/`Glob`.

## Enqueue one experiment (`queue-run`)

Each `queue-run` call enqueues ONE run request. The spec's arrival facts:

- **`request_id`** (REQUIRED) — a client-minted id you own. It is the dedup key: a replayed call with the same `request_id` returns the ORIGINAL item and writes nothing (so a retried relay never double-enqueues). Mint a fresh id per distinct experiment; reuse the same id to retry the same one.
- **`spec` or `spec_ref`** (one REQUIRED) — the resolve spec inline (`spec`), or a reference to a repo-relative spec file (`spec_ref`). Opaque at enqueue: `queue-run` checks it EXISTS, never what it says.
- **`resources`** — the typed resource asks used as hard constraints at placement: `gpu` (bool), `gpu_type` (matched against a cluster's declared `gpu_types`), `cores` (int), `walltime_sec` (int seconds), `est_core_hours` (float). State only what the experiment needs; each is matched against the cluster's declared ceilings.
- **`cluster`** (optional) — an EXPLICIT pin to a `clusters.yaml` top-level key. A pin always wins placement, and a typo is refused HERE against the live `clusters.yaml` rather than becoming a silently unplaceable item. Omit it to let placement choose.
- **`campaign_base`** (optional) — a logical campaign base for a multi-cluster study; placement composes the concrete id as `<base>_<clusterkey>`. Null (omitted) for an open-loop item that belongs to no campaign and occupies no pool slot.
- **`run_name`** (optional) — a logical name; run ids are COMPUTED (`<run_name>-<cmd_sha[:8]>`), never minted.

`queue-run` returns the enqueued item in state `queued` (with `replayed: true` when the `request_id` matched an earlier call). It touches no cluster; the gates bind later, at the cluster boundary of the run the item becomes, where hpc-copilot's brief discloses the placement the human's approval covers.

## Observe what became of it

- **`queue-status`** — the queue's bounded projection: intake items joined to the run stores, recomputed on every read. Each item carries a state — `queued`, `placed`, became a run / in flight / terminal, or **parked awaiting a human**. A bare call reads the whole ledger; filter by state, campaign base, or cluster, and bound with a limit. This is where you see that an item you enqueued is **held** and why: read the item's reported reason and per-cluster verdicts, relay them, and wait — the resolution is the human's, not yours. Relay the numbers `queue-status` returns; never a count you remember.
- **`queue-advance`** — the placement AUTHORITY's decision and reasoning. It reads queued items, `clusters.yaml`, and the shared occupancy predicate and returns, per item, WHICH cluster it would go to, under which `<base>_<clusterkey>` campaign id, and the disclosed reason — or a closed `reason_code` and human-readable reason when the item is HELD (constraint mismatch, no candidate cluster, courtesy cap). It writes NOTHING: it reports where the human's approval would send the item. A held reason is information to relay and (optionally) to act on by enqueuing a revised item, never a gate for you to clear.

Read a held item's `reason_code` as one of a closed set:
- **placeable** — `queue-advance` names a target cluster and campaign id; the item waits for the human to approve dispatch.
- **constraint mismatch** — the resource asks exceed every candidate cluster's declared ceilings (e.g. `walltime_sec` or `est_core_hours` too large, no cluster with the asked `gpu_type`); relay it, and if the experiment can run smaller, enqueue a revised item under a NEW `request_id`.
- **no candidate / courtesy cap** — every candidate is filtered out or pool slots are capped; relay the per-cluster verdicts and let the human decide placement.

## What you never do

- **Never dispatch, submit, approve, greenlight, or consent.** Those verbs are not in your catalog. The human approves dispatch at the cluster boundary; hpc-copilot's morning brief reports back what you queued and what it is waiting on.
- **Never interpret raw results or characterize a run's outcome in your own words.** You are a producer; result interpretation is hpc-copilot's lifecycle and the human's call.
- **Never hand-roll a cluster action.** If a task seems to need a verb outside the three, it is out of your role by construction — enqueue the experiment and let the human-gated pipeline carry it.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required (path to the experiment repo) |
| `request_id` | Required (client-minted dedup key, one per distinct experiment) |
| `spec` / `spec_ref` | Required (one of; the resolve spec inline or by reference) |
| `resources` | Caller (typed asks: gpu / gpu_type / cores / walltime_sec / est_core_hours) |
| `cluster` | Caller (optional explicit pin, verified against clusters.yaml) |
| `campaign_base` | Caller (optional; null for an open-loop item) |
| `run_name` | Caller (optional logical name) |

## Notes

- **Enqueueing spends nothing.** `queue-run` is an ungated local append; the item lands `queued` and every consumable gate binds later at the cluster boundary, on the human's approval.
- **`request_id` is idempotency.** A replayed `queue-run` returns the original item with `replayed: true` and writes no second line — safe to retry a relay.
- **The catalog is the boundary.** If this skill and the `science` catalog ever disagreed, the catalog wins by construction — a verb the catalog does not expose is simply uncallable, so no prose here can widen your reach past producer.
