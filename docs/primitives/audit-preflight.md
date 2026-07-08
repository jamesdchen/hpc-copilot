---
name: audit-preflight
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent audit-preflight --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.audit_preflight.audit_preflight
---
# audit-preflight

The GO/NO-GO brief for the notebook-audit loop. It composes EXISTING substrate
checks — template present + parses + git-committed-clean, version skew, declared
roots exist and are non-empty, and prior audit state (resuming vs fresh) — into
one read-only query that renders a decision-ready brief. It detects nothing new
and blocks nothing itself: a NO-GO is a *prediction* the human acts on, and the
gates it predicts (the graduation gate, the parse/commit disciplines) remain the
enforcement. The kickoff prose collapses to "run audit-preflight; if GO, begin" —
a sentence that cannot rot because it delegates to code (`submit-preflight` is the
precedent).

## Inputs

- `template` (string) — path (experiment-relative or absolute) to the audit
  template `.py`. Checked for: present, parses via `parse_percent_source`, and
  git-committed-clean at that path. An uncommitted / dirty / untracked template
  is an "unsigned template" NO-GO — the commit IS the signature.
- `source_roots` (list[str], optional) — opaque import roots (the linked-sources
  lint's roots). Omitted → defaults from the audit's recorded configuration when
  `audit_id` names an existing audit; otherwise `[]`.
- `input_roots` (list[str], optional) — opaque data-path roots (the executes-live
  lint's roots). Same defaulting as `source_roots`.
- `audit_id` (string, optional) — the caller-authored audit slug. When it names
  an existing audit, its recorded roots default the roots above and its journal
  decides resuming-vs-fresh. Omit for a fresh standalone preflight.

## Outputs

`{verdict, audit_id, template, template_state, resuming, journal_records,
source_roots, input_roots, blockers, disclosures, brief}` — see
`schemas/audit_preflight.output.json`. `verdict` is `"GO"` (zero blockers) or
`"NO-GO"`. Each `blockers[]` entry carries `{check, blocker, remedy}` with a
pre-drafted remedy. `disclosures[]` holds non-blocking notes (the data-manifest
drift line and the resuming note). `brief` is the code-rendered D8 decision-ready
brief — relay it VERBATIM.

## Errors

- `spec_invalid` — a malformed `audit_id` (not filesystem-safe) surfaced by the
  audit-journal reader, or a spec that fails model validation.

## Idempotency

Pure read (no `idempotency_key`). Same inputs against the same tree yield the
same verdict and brief; running it never mutates anything.

## Notes

- **Composes, never detects.** Template parse reuses `parse_percent_source`;
  git-committed-clean reuses the bounded, fail-open `git_output` helper; version
  skew reuses `doctor`'s existing detector; roots defaulting reuses
  `read_recorded_config`; prior-state reuses the audit-journal reader.
- **Never a blocker either way:** the data-manifest drift disclosure is a
  Phase-1a seam — until the `data-manifest` verb merges it renders a standing "no
  manifest" line; the drift counts wire in afterward. Version skew fail-opens
  (no git / no embedded sha / not the source repo → no skew blocker).
- **Substrate only.** Process-not-substrate prereqs ("envs refreshed tonight")
  stay in kickoff prose; only parse / commit / skew / roots live here.
