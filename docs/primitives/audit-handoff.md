---
name: audit-handoff
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent audit-handoff --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.audit_handoff_op.audit_handoff
---
# audit-handoff

Project the durable records of a passed notebook audit into a **DRAFT
`InterviewSpec`** — the deterministic seat for the audit→interview bridge
(`docs/design/notebook-audit.md`, audit-handoff note). After an audit passes, the
submit interview re-derives facts the audit flow already holds; the interim fix
was a load-bearing PROSE mapping in `/new-experiment-hpc` step 4 (the rot class).
This read-only `query` verb replaces that prose: it reads the durable audit
records and the source, and emits a draft the caller confirms and passes to the
`interview` primitive.

## The one rule: derive-and-disclose or placeholder — never guess

Every draft field is either DERIVED from a durable record / a syntactic scan (and
disclosed), or emitted as an explicit **placeholder** the caller must fill. The
verb never invents a value, because the caller passes the draft to the interview
and a guessed field would become a **journaled fact** there — the `halo_expr`
failure class (a fabricated value laundered into provenance).

| Draft field | Source |
|---|---|
| `goal` | The audit-OPEN intent utterance journaled on the `notebook-audit-config` seat. No recorded goal → a `goal` placeholder, never an invented sentence. |
| `task_axes` | The audit-OPEN compute-shape utterances (the human's free-text names for what varies across tasks). GUIDANCE for the caller's `task_generator`. |
| `audited_source` | The verb inputs (`source` / `audit_id` / `template`) plus the recorded config roots (`read_recorded_config`). Always derivable. |
| `entry_point` | The single `@register_run` function found by scanning the source. Zero or several → a disclosed placeholder; the ambiguity is surfaced, never resolved by picking. `entry_point_candidates` lists every one found. |
| `summary_artifact_candidates` | An AST scan for writes under `$HPC_RESULT_DIR` — detected-and-disclosed; multiple candidates are listed and the caller confirms. |
| `task_generator` / `task_count` / `produced_by` | ALWAYS placeholders — a materializer / fan-out count / provenance the audit records never hold. |

## The prerequisite: the audit-open intent seat

`goal` and `task_axes` are read from the `notebook-audit-config` record
(`read_audit_intent`) — the same immutable audit-open seat that carries the
config roots. `notebook-record-config` gained optional `goal` / `task_axes` fields
for exactly this: the audit-open flow elicits intent + compute-shape from the
human, and recording them there makes them durable (before, they lived only in
chat). An audit that never recorded intent still projects — `goal` becomes a
placeholder and the missing axes are disclosed.

## The `$HPC_RESULT_DIR` write scanner (declared coverage)

`summary_artifact` candidates are found by an AST scan, honest about its reach
(pinned by `tests/ops/notebook/test_audit_handoff.py`):

- **Result-dir base**: a name bound to `os.environ["HPC_RESULT_DIR"]`,
  `os.environ.get(...)`, or `os.getenv(...)` (also `RESULT_DIR`), optionally
  wrapped in `Path(...)`, plus one hop of name aliasing.
- **Filename forms**: `os.path.join(base, "a.json")`, `Path(base) / "a.json"`
  (the `/` operator), and `f"{base}/a.json"` (f-string).
- **The honest gap**: a computed tail (a non-literal join arg, an f-string with a
  formatted tail) is DISCLOSED in `unverifiable_result_writes`, never dropped.
- **Not covered** (a safe miss — added by hand, never a false journaled fact):
  `str.format` / `%` / `+` construction, `str.join`, and transitive aliasing
  through an intermediate joined directory.

The scan does **not** inspect the surrounding call for a write vocabulary — every
path BUILT on `$HPC_RESULT_DIR` is a candidate output (identity + path arithmetic
only; naming a write function would cross the Q1 library-knowledge boundary).

## Inputs

An `AuditHandoffSpec` (`hpc_agent._wire.queries.audit_handoff`):

- `audit_id` (string, required) — the notebook decision-journal scope whose
  audit-open config/intent record is projected.
- `source` (string, required) — experiment-relative path to the audited source
  `.py` (jupytext percent format), AST-scanned.
- `template` (string, required) — experiment-relative path to the template `.py`,
  carried verbatim into the draft `audited_source`.

## Outputs

`data` is an `AuditHandoffResult`: `goal`, `entry_point` (+ `entry_point_candidates`),
`audited_source`, `task_axes`, `summary_artifact_candidates`,
`unverifiable_result_writes`, `placeholders` (`{field, reason}` the caller must
fill), and `disclosures` (advisory notes; never blocks). The whole result is a
deterministic function of the journal records + the source bytes.

## Errors

- `spec_invalid` — an unreadable `source` path, or a `source` that is not
  parseable Python. Not retry-safe; fix the path or the source.

## Idempotency

Pure read. Derived state recomputed from the records + the source on every call;
no natural identity key, nothing written.

## Usage

```
hpc-agent audit-handoff --spec spec.json --experiment-dir .
```

where `spec.json` is `{"audit_id": "<id>", "source": "<py relpath>", "template":
"<py relpath>"}`. Run it once the audit `passed`, confirm the draft, fill the
placeholders, and pass the result to the `interview` primitive — the one
non-load-bearing line `/new-experiment-hpc` step 4 now collapses to.
