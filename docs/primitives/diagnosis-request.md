---
name: diagnosis-request
verb: query
side_effects: []
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: precondition_failed
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent diagnosis-request --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.recover.diagnosis.diagnosis_request
---
# diagnosis-request

Composes the code-only DIAGNOSIS REQUEST for one run parked on a human
decision — the kernel side of the park-time diagnosis seam. A read-only
investigator agent (spawned by a plan such as `queue-drain.js`, never by the
kernel) calls this first to learn WHAT TO LOOK AT: the parked verb/stage/reason
off the pending-decision marker (`state/journal.read_pending_decision` + the
block's durable terminal record), failure-signature matches over evidence the
run's stores already hold (classified by THE one catalog entry point,
`infra.failure_signatures.classify` — never a second matcher), the LOCAL
log/artifact paths worth reading (paths only, never content), and the CLOSED
category vocabulary (`CLASSIFIER_CATEGORIES`) the investigator must name its
classification from.

Pure read, code-composed end to end: no agent judgment enters the request, and
the kernel never consumes the investigator's output — findings come back
through [`attach-diagnosis`](attach-diagnosis.md) as display-only advisory
data.

## Inputs

- `run_id` (str) — the parked run to compose the request for.

## Outputs

`{run_id, block, workflow, awaiting_since, stage_reached, reason, is_anomaly,
signature_matches[], categories[], read_paths{}, worker_logs[], attach_target,
diagnosis_attached, note}` — see `schemas/diagnosis_request.output.json`.
`read_paths` names existing local files only (run sidecar, journal record,
monitor log, last-status snapshot, decision-brief journal, block terminal);
`worker_logs` lists the run's local detached-worker logs; `attach_target` is
where `attach-diagnosis` will write the dossier. `is_anomaly` is decided in
code: `(block, stage)` membership in `infra.block_chain.ANOMALY_TERMINATORS`,
falling back to the park brief's own answer-menu OVERRIDE projection when the
stage is unknown.

## Errors

- `spec_invalid` — malformed run_id.
- `precondition_failed` — the run is not parked on a decision (no
  pending-decision marker); there is nothing to investigate.

## Idempotency

Pure read over durable state — same parked boundary, same request (modulo
which named files currently exist).

## Notes

Doctrinal envelope (non-negotiable): the request is the ONLY kernel-side half
of the seam besides the opaque attach channel. The kernel never spawns
investigators; the investigator reads only the named paths, never runs cluster
commands, never journals decisions, never answers parks; and its attached
dossier never enters the decision-brief provenance journal, the answer menu's
code-authored options, or any gate.
