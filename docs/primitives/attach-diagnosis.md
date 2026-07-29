---
name: attach-diagnosis
verb: mutate
side_effects:
- file_write: <experiment_dir>/.hpc/runs/<run_id>.diagnosis.json
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent attach-diagnosis --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.recover.diagnosis.attach_diagnosis
---
# attach-diagnosis

The durable ATTACH channel of the park-time diagnosis seam: stores a read-only
investigator agent's findings for one parked run as an OPAQUE,
provenance-marked proposal — `<experiment_dir>/.hpc/runs/<run_id>.diagnosis.json`,
beside the block terminal records (written with the same atomic tmp +
`os.replace` + flock idiom as `state/block_terminal.py`).

The verb validates SHAPE only. The one closed-set check the schema declares:
`classification` must be a catalog category
(`infra.failure_signatures.CLASSIFIER_CATEGORIES`, the set
[`diagnosis-request`](diagnosis-request.md) disclosed) or the literal
`unmatched`. Provenance `{authored_by: "agent", attached_at}` is stamped by the
state-layer writer (`state/diagnosis.write_diagnosis`) — never accepted from
the caller, so every stored dossier is honestly labelled agent-authored.

**Ungated on purpose:** advisory data spends nothing — no greenlight, no
consent, no budget. Re-attach OVERWRITES (newest diagnosis wins; the dossier
is advisory, not an audit trail).

## Inputs

- `run_id` (str) — the parked run the dossier belongs to.
- `classification` (str) — a `CLASSIFIER_CATEGORIES` member or `"unmatched"`;
  anything else is refused (`spec_invalid`).
- `evidence_excerpts` (list, ≤20) — `{path, lines}` quoted log evidence,
  bounded per excerpt.
- `proposed_actions` (list, ≤10) — `{label, rationale,
  suggested_response_text}` drafted recovery options. Proposals only:
  `suggested_response_text` is text the HUMAN may choose to type — it is never
  auto-filled and never becomes an answer-menu option.

## Outputs

`{run_id, path, attached_at, classification, proposed_actions_count,
overwrote}` — the pointer fields the park surfaces relay.

## Errors

- `spec_invalid` — malformed run_id, a classification outside the closed set,
  or an over-bound payload (Pydantic shape refusal).

## Idempotency

Idempotent per content: re-attaching overwrites the sidecar with the newest
dossier; replaying the same spec rewrites the same content (only the
provenance `attached_at` stamp moves).

## Notes

The trust boundary, pinned by
`tests/ops/recover/test_diagnosis.py::TestAdvisorySeparation`: the dossier is
DISPLAY-ONLY advisory matter. It never enters the decision-brief provenance
journal (`state/decision_briefs`), never appears among answer-menu options
(that surface is code-AUTHORED data only — agent mappings are not laundered
into it), and no gate reads it. Surfaces (`compose_park_notice`, `doctor`'s
parked notes, the morning digest's `parked` section) carry a pointer + count
(`diagnosis: attached (N proposed action(s), agent-authored, advisory) —
<path>` / `diagnosis: none`) rendered by the ONE shared line
(`state/diagnosis.diagnosis_pointer_line`); the human reads the render from
disk. A corrupt or unprovenanced dossier reads as absent (fail-open).
