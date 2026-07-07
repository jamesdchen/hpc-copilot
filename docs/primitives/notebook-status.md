---
name: notebook-status
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent notebook-status --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook_status.notebook_status
---
# notebook-status

Report the **per-section audit state** of an audited source `.py` against its
template inventory and its audit journal. A pure read: it recomputes each
section's current hash from the source on disk, replays the `audit_id` decision
journal, and reduces every required (template) section to a T6 status — plus the
whole-module gate predicate `passed`.

This is the read-side of the notebook-audit substrate (`docs/design/notebook-audit.md`,
Wave B / T6). The graduation gate (T9) consumes `passed`; a human/agent driving
the audit loop reads `sections` to see which sections still need attention.

## Inputs

A `NotebookStatusSpec` (`hpc_agent._wire.queries.notebook_status`):

- `audit_id` (string, required) — the notebook decision-journal scope id whose
  sign-off / auto-clear records are reduced (caller-authored slug; the journal
  lives at `.hpc/notebooks/<audit_id>.decisions.jsonl`).
- `source` (string, required) — experiment-relative path to the audited source
  `.py` (jupytext percent format). Its per-section shas are recomputed **fresh
  on every call**, so an edit revokes stale trust by construction.
- `template` (string, required) — experiment-relative path to the template `.py`.
  Its section slugs are the **required inventory** the rollup is computed over.

## Outputs

`data` is a `NotebookStatusResult`:

```
{
  "audit_id": "<id>",
  "sections": [
    {
      "slug": "<section slug>",
      "status": "signed_current | auto_cleared | signed_stale | unsigned",
      "current_section_sha": "<64-hex, or null if absent from source>",
      "signed_section_sha":  "<64-hex the newest record attested, or null>",
      "view_sha": "<projection sha the human saw, or null>",
      "attestor": "human | code | null"
    }
  ],
  "passed": <bool>
}
```

The status vocabulary (`hpc_agent.state.notebook_audit`):

- `signed_current` — current, newest valid record is a HUMAN sign-off.
- `auto_cleared` — current, newest valid record is a CODE auto-clear (no human
  attention was spent; the record never reads as a human ack).
- `signed_stale` — the section was human-signed, then its source moved; the
  approval is revoked. Informational — it still fails the gate.
- `unsigned` — no valid record, OR a stale **auto-clear** (drift = unsigned by
  construction; a machine clearance has no human to inform).

`passed` is the graduation gate's whole-module predicate: **every required
section is current** (`signed_current` or `auto_cleared`). A newest record wins
regardless of class — a human sign-off after an auto-clear supersedes it, and
vice versa (the attestation kernel's newest-first rule).

## Errors

- `spec_invalid` — an unreadable `source`/`template` path (naming which), or a
  malformed percent-format module (a bad, duplicate, or misplaced
  `# hpc-audit-section:` marker — the parser's boundary guards). Not retry-safe;
  fix the path or the source.

## Idempotency

A pure query with no side effects and no natural identity key. Derived state:
recomputed from the `.py` on disk + the journal on every call, so replaying after
more sign-offs / edits simply reflects the current state.

## Usage

```
hpc-agent notebook-status --spec spec.json --experiment-dir .
```

where `spec.json` is `{"audit_id": "<id>", "source": "<py relpath>", "template":
"<py relpath>"}`. Sign-offs are appended out-of-band via `append-decision`
(`block=notebook-sign-off`) — there is deliberately no sign-off verb here (the
no-unlock-verb doctrine); auto-clears are written by the gate/skill wave. This
verb only reads.
