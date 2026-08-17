# Post-exploration check — a worked example

The canonical happy path for the **post-exploration fidelity checker**: a run
that already happened — hand-rolled script, raw `sbatch`, ad-hoc result dirs,
observed by nothing — is adopted, aggregated in code, claim-checked against a
human-claimed number, and attested. Doctrine:
[`docs/design/post-exploration-checker.md`](../../docs/design/post-exploration-checker.md)
(origin plan:
[`docs/plans/expost-trust-2026-07-30.md`](../../docs/plans/expost-trust-2026-07-30.md)).
The principle it implements: **gate by irreversibility and attestation, never
by step.**

Every command below is `hpc-agent <verb> --spec <path>` — `--spec` takes a
**file path**, never inline JSON — and every verb answers with the one-line
stdout envelope (`{"ok": true, "idempotent": <bool>, "data": {...}}`, exit
codes 0/1/2/3; see
[`docs/integrations/CONTRACT.md`](../../docs/integrations/CONTRACT.md)). The
`adopt-run` verb has LANDED; the envelope `data` bodies shown here mirror the
landed contract — [`docs/primitives/adopt-run.md`](../../docs/primitives/adopt-run.md)
and the wire models win on any field-level disagreement.

## 1. The scenario

A researcher's agent explored freestyle. It hand-rolled `sweep.py`, submitted
a raw 12-task SLURM array itself, and invented its own result layout — no
hpc-agent verb ever saw the run:

```bash
# what the agent did, entirely outside hpc-agent:
sbatch --array=0-11 --wrap \
    'python sweep.py --task-id $SLURM_ARRAY_TASK_ID --alpha 0.3'
# -> Submitted batch job 8571202
```

By morning the array is finished and the remote tree looks like this:

```
/scratch/jdoe/vol-sweep/
├── sweep.py                      # hand-rolled, never templated
├── slurm-8571202_*.out           # raw scheduler logs
└── results/
    ├── task_0/metrics.json       # {"qlike": 0.2119, "rmse": 0.0440, "n_samples": 5000}
    ├── task_1/metrics.json
    ├── ...
    └── task_11/metrics.json      # 12 tasks, one metrics.json each
```

`squeue -u jdoe` is empty; `sacct -j 8571202` shows all 12 array elements
`COMPLETED`. The run is terminal, and nothing observed it: no canary, no
fingerprint sample, no journal record. That is exactly what the checker is
for — exploration stayed fast and ungated; trust is relocated to after the
fact.

## 2. Adopt the run

`adopt-run` mints the run record from what ACTUALLY EXISTS. Adoption facts are
ELICITED — from the human or from observed scheduler state, never invented —
and `cmd_sha` is DERIVED from the exact per-task command, never free-typed.
The spec for this (terminal) scenario:

```json
{
  "run_id": "vol-sweep-freestyle-01",
  "command": "python sweep.py --task-id $SLURM_ARRAY_TASK_ID --alpha 0.3",
  "cluster": "discovery",
  "ssh_target": "jdoe@discovery",
  "remote_path": "/scratch/jdoe/vol-sweep",
  "profile": null,
  "terminal_evidence": "sacct -j 8571202 reports all 12 array tasks COMPLETED; squeue -u jdoe is empty; results/task_0..task_11/metrics.json all present",
  "results_sample": "results/task_3"
}
```

Field notes:

- `command` — the EXACT per-task command the run executed. `cmd_sha` is
  derived from it inside the verb; the spec has **no** `cmd_sha` field to
  type.
- `job_ids` is ABSENT — the run already finished — so `terminal_evidence` is
  REQUIRED (a settle with no evidence is refused; same rule as
  [`settle-run`](../../docs/primitives/settle-run.md)).
- `results_sample` — one real **task dir** (or a glob over task dirs), a
  LOCAL path: the researcher mirrored the result tree down first
  (`scp -r discovery:/scratch/jdoe/vol-sweep/results .`), because inference
  never probes over ssh — a remote / non-resolving anchor yields an
  elicitation envelope, not a guess. The verb infers `result_dir_template`
  (`results/task_{task_id}`) and `task_count` (12) from it and verifies
  `summary_artifact` presence (default `metrics.json`). Alternatively state
  `result_dir_template` + `task_count` directly and omit the sample.
- `profile` — optional; `null` for a hand-rolled executor no registered
  profile describes (the journal record's profile then defaults to
  `"adopted"`).

```bash
hpc-agent adopt-run --spec .hpc/specs/adopt-run.json --experiment-dir .
```

Expected envelope (annotated; `data` shape illustrative of the frozen
contract):

```json
{
  "ok": true,
  "idempotent": false,
  "data": {
    "stage_reached": "adopted_terminal",  // sidecar + journal written, settled on the evidence
    "needs_decision": false,
    "reason": "adopted 'vol-sweep-freestyle-01' as terminal (complete) on directed evidence — sidecar + journal record written and settled through the settle-run mechanism",
    "run_id": "vol-sweep-freestyle-01",
    "cmd_sha": "9f2c…64-hex…",            // DERIVED from `command` — never caller-typed
    "status": "complete",                 // journal status after adoption
    "job_ids": [],                        // [] on the terminal branch
    "task_count": 12,                     // inferred from results_sample
    "result_dir_template": "results/task_{task_id}",
    "sidecar_path": "…/.hpc/runs/vol-sweep-freestyle-01.json", // the run's identity record
    "next_block": {                       // the {verb, why, spec_hint} hand-off
      "verb": "aggregate-check",          // terminal ⇒ straight to the aggregate gate
      "why": "the adopted run is terminal — aggregate-check verifies readiness, aggregate-run computes the numbers in code; verify-reproduction external-baseline is the claim-comparison path",
      "spec_hint": { "run_id": "vol-sweep-freestyle-01" }
    }
  }
}
```

The verb writes the sidecar, mints the journal record, and settles the
terminal run in one act. The adopted run remains what it is — never observed
by the tool: no canary fired for it, no fingerprint sample accreted (the
sidecar carries adopt-run's `extra.adopted` marker, stating that plainly). Re-invoking with the same `run_id` is refused as a
duplicate — the envelope names the existing sidecar; adoption is not a
re-runnable mutation.

## 3. The check sequence

### 3.1 `aggregate-check` — readiness + integrity gate

```json
{ "run_id": "vol-sweep-freestyle-01", "run_preflight": true,
  "reconcile_scheduler": null, "allow_partial": false }
```

```bash
hpc-agent aggregate-check --spec .hpc/specs/aggregate-check.json --experiment-dir .
# expect: {"ok": true, ..., "data": {"block": "check", "stage_reached": "ready",
#          "needs_decision": false, "run_id": "vol-sweep-freestyle-01", "brief": {...}}}
```

`stage_reached: "ready"` — terminal, preflight clean, no blocking integrity
issues — greenlights straight to `aggregate-run`. Any integrity issue
(missing tasks, cross-run contamination) is surfaced in
`brief.integrity_issues`, **never auto-masked**
([`docs/primitives/aggregate-check.md`](../../docs/primitives/aggregate-check.md)).

### 3.2 `aggregate-run` — deterministic combine + reduce

**Doctrine: the reducer — never the LLM — computes every number.** The verb
carries its greenlight gate inside its body — a call the run's journal does
not greenlight is refused with a self-remediating message; commit the
greenlight through `append-decision` first.

```bash
hpc-agent aggregate-run --spec .hpc/specs/aggregate-run.json --experiment-dir .
# expect: {"ok": true, ..., "data": {"block": "run", "stage_reached": "harvested",
#          "needs_decision": true, "brief": {"results_table": [...],
#          "error_sweep": {...}, "proposed_interpretations": []}}}
```

The run has no cluster-side `_combiner/` (nothing hpc-agent submitted ever
combined), so the no-combiner per-task fallback fires: it pulls the 12
`metrics.json` sidecars and runs the SAME deterministic `reduce_metrics`
weighted-mean the observed path uses
([`docs/primitives/aggregate-run.md`](../../docs/primitives/aggregate-run.md)).
The reduced table for this layout keys on the run_id itself — the flattened
metric keys downstream are therefore `vol-sweep-freestyle-01.<metric>`.
Relay `brief.results_table` VERBATIM; `proposed_interpretations` arrives
EMPTY — the human concludes from the numbers, the code never does.

### 3.3 `verify-reproduction` (external-baseline mode) — the claim-check

The researcher claims "QLIKE ≈ 0.2113, RMSE ≈ 0.0441" from their notebook.
Those values are HUMAN-AUTHORED free text, transcribed verbatim — never
rounded, never corrected. The spec pairs `repro_run_id` with an
`external_baseline` block ONLY — do NOT pass `original_run_id` or a top-level
`tolerance`; they are mutually exclusive with `external_baseline` and the
verb refuses the pairing:

```json
{
  "repro_run_id": "vol-sweep-freestyle-01",
  "external_baseline": {
    "claimed_values": {
      "vol-sweep-freestyle-01": { "qlike": 0.2113, "rmse": 0.0441 }
    },
    "tolerance": {
      "default_abs_tol": null,
      "default_rel_tol": 0.01,
      "per_key": {
        "vol-sweep-freestyle-01.rmse": { "abs_tol": 0.0005, "rel_tol": null }
      }
    },
    "claimed_data_sha": null
  }
}
```

- `claimed_values` — nested or pre-flattened; both sides are flattened by
  joining keys with `.`, so the comparator sees
  `vol-sweep-freestyle-01.qlike` etc. Keys must match the reduced table's
  keys.
- `tolerance` — caller-owned. `default_abs_tol` / `default_rel_tol` apply to
  every numeric key without an override; a `per_key` entry FULLY replaces the
  default for that key. Absent tolerance means exact comparison
  ([`docs/primitives/verify-reproduction.md`](../../docs/primitives/verify-reproduction.md)).
- `claimed_data_sha` — optional; without a manifest at claim time the
  drift disclosure says so ("cannot distinguish result decay from data
  drift").

```bash
hpc-agent verify-reproduction --spec .hpc/specs/claim-check.json --experiment-dir .
# expect (match): {"ok": true, ..., "data": {"stage_reached": "match",
#   "needs_decision": false, "reason": "<the code-rendered consistency sentence>",
#   "receipt": {"receipt_kind": "claim-check", ...}, "receipt_path": "…/claim_check_receipts.jsonl"}}
# expect (mismatch): exit 0, "stage_reached": "mismatch", "needs_decision": true —
#   a dated FINDING naming per-key diffs + the drift disclosure; never blocking.
```

The receipt embeds the claim verbatim and lands in its own append-only ledger
— `_aggregated/vol-sweep-freestyle-01/claim_check_receipts.jsonl` — NEVER the
reproduction ledger. The naming lock is enforced at the storage layer too.

### 3.4 `evidence-brief` — the attestation digest

```json
{ "lineage": "vol-sweep-freestyle-01" }
```

```bash
hpc-agent evidence-brief --spec .hpc/specs/evidence-brief.json --experiment-dir .
# expect: {"ok": true, ..., "data": {"conclusions": [...], "envelopes": [...],
#          "citations_status": [...], "render": "<deterministic markdown digest>"}}
```

A read-only projection over the sealed records — sidecar, journal, receipts,
the reduced table's provenance — code-composed, no recommendation, no
interpretation. The human attests against `render`, relayed verbatim
([`docs/primitives/evidence-brief.md`](../../docs/primitives/evidence-brief.md)).

## 4. The sentence

On a claim-check match over this ADOPTED run, the ONE honest verdict —
CODE-rendered by the comparator (the module constant
`CLAIM_CONSISTENT_SENTENCE_ADOPTED`, selected because the sidecar carries
adopt-run's `extra.adopted` marker), relayed verbatim, never composed or
paraphrased by the LLM — is:

> **"the claim is consistent with the adopted run's records (within caller tolerance)"**

(For a fresh run the tool observed itself — `hpc-claim-check`'s double-fresh
flow — the code renders the other constant: "the claim is consistent with a
fresh observed run (within caller tolerance)".)

What the tool will **NEVER** say: **"reproduced."** "Reproduced" requires two
OBSERVED runs, and this run was never observed by the tool — no canary fired
for it, no fingerprint sample accreted, no bind-lock closed over its
payloads. Calling a claim-match a reproduction would launder unattested
history into the trust chain; the anti-laundering seam refuses a
reproduction-kind receipt over any external baseline by construction (the
naming lock, ruling 6b —
[`docs/design/post-exploration-checker.md`](../../docs/design/post-exploration-checker.md)
§3, §5). An unobserved run earns "consistent with" — nothing more. A
mismatch is a dated FINDING that names the moved dimension (code, env, or
data), never an accusation, never a block.

## 5. Variant: the jobs are still running

Same scenario, but the agent adopts mid-flight — the array is still in the
queue. `job_ids` present ⇒ the run is still running, and `terminal_evidence`
is not required (the scheduler will supply the terminal state):

```json
{
  "run_id": "vol-sweep-freestyle-02",
  "command": "python sweep.py --task-id $SLURM_ARRAY_TASK_ID --alpha 0.3",
  "cluster": "discovery",
  "ssh_target": "jdoe@discovery",
  "remote_path": "/scratch/jdoe/vol-sweep",
  "job_ids": ["8571202"],
  "results_sample": "results/task_3"
}
```

(`results_sample` is again a LOCAL task dir — mirror the partial result tree
down first, or state `result_dir_template` + `task_count` explicitly.) The
envelope comes back with `"stage_reached": "adopted_in_flight"`,
`"status": "in_flight"`, and a `next_block` whose `verb` is `"status-watch"`.
Drive the watch to terminal:

```bash
hpc-agent status-watch --spec .hpc/specs/status-watch.json --experiment-dir .
# spec: {"monitor": {"run_id": "vol-sweep-freestyle-02", ...cadence/budget...}}
```

Reconcile is the only source of run state — never infer "still running" from
an open log or elapsed time. On the watch's clean terminal
(`stage_reached: "watch_terminal"`), continue at §3 unchanged: the check
sequence is identical once the run is terminal.

## 6. Pitfalls

- **Don't free-type `cmd_sha`.** The spec carries `command`; the sha is
  derived inside the verb (parameter identity is computed or it is nothing).
  A hand-typed sha has nothing to bind to and cannot enter the spec.
- **Empty `terminal_evidence` is refused.** `job_ids` absent means the caller
  asserts the run finished — that assertion needs evidence (scheduler output,
  result dirs present, queue empty). No evidence, no settle; the refusal is
  the feature.
- **Layout inference needs a sample.** Omit `result_dir_template`/`task_count`
  AND `results_sample` and the verb has nothing to infer the layout from — it
  answers with an `ok: true` **needs_elicitation** envelope
  (`stage_reached: "needs_elicitation"`, `needs_decision: true`), whose
  `reason` names exactly what to supply and states "Nothing was written" —
  not a `spec_invalid` refusal. Bring one real LOCAL task dir (or glob) or
  state `result_dir_template` + `task_count`; `summary_artifact` defaults to
  `metrics.json`.
- **Unobserved runs mint no fingerprint.** Adoption enters the run at
  fingerprint n=0 and the claim-check appends NO fingerprint sample — the
  fingerprint history starts from OBSERVED runs only. To accrue
  observed-tier evidence (double canary, determinism fingerprints), drive a
  fresh run through the opt-in loops instead.
- **Duplicates are refused.** A second `adopt-run` with the same `run_id`
  does not overwrite the first adoption; fix the spec under a new run_id or
  operate on the existing record.
- **Never upgrade the verdict's wording.** Relay the code-rendered sentence
  verbatim; "consistent with" is the ceiling for an adopted run. Even
  `hpc-claim-check`'s double-fresh-observed flow never says "reproduced."
