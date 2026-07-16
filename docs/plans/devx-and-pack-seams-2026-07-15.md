# DevX + multi-pack seam hardening — 2026-07-15 (banked proposals)

Status: **BANKED — no proposals executed yet.** Produced by two parallel
read-only research agents (pack-seam inventory; devx-friction sweep), spot-
checked against HEAD. Motivated by run-13 finding 1 (the audit-template
compose tiebreak inverting on the receipt-bound domain pack — see
`docs/design/history/run13-findings.md`) and by the maintainer's intent to
drive hpc-copilot across MANY use cases (labs / domains / packs). The
2026-07-13 architecture review is fully executed (see its delta + PR #17);
nothing here duplicates it. Verify file:line citations live before acting.

---

## Part 1 — Pack-seam hardening (multi-use-case readiness)

### Inventory verdicts (full audit in the research transcript; key rows)

SAFE (order-independent or refuses ambiguity loudly): `resolve_reader_calls`,
`resolve_failure_patterns` + `_match_pack_patterns` (sorted union + per-pack
echoes), `resolve_axis_hints` + `_apply_axis_hints` (any disagreement →
`verdict="conflict"` → human), `resolve_pack_echoes` (all packs echoed),
`ops/pack_gate.py::assert_pack_receipts_current` (**the reference design**:
cross-pack bind index, no tiebreak, refusal names every failing slot),
`refresh_op.py`, all `state/pack.py` loaders.

HEURISTIC (can pick wrong, silently): `compose_audit_template`
(`state/pack_declarations.py:655-722`, tiebreak at `:716`) and its three
callers (`compose_audit_template_from_repo`, `interview.
_compose_audit_template_default` at `ops/memory/interview.py:108-147` —
whose docstring states the backwards preference — and
`ops/audit_preflight.py:341-352`, where finding 1 fired). Second-order:
`audited_source_echo` (`ops/notebook_gate.py:192-206`) inherits the pick, so
a wrong compose mis-attributes WHICH pack's standards gated the run on the
sidecar/dossier.

ASSUMES-SINGLE: `resolve_template_pack_echo`
(`pack_declarations.py:516-585`) — first-match on file-identity collision,
silent. LATENT: `resolve_tolerances` (`:434-451`) and
`resolve_registration_fields` (`:454-467`) have **no id-collision precedence
rule**; inert today, but the fingerprint / registration-kernel consumers
would inherit silent first-wins the day they land.

### Facts that constrain the fix

- `derived_from` exists **nowhere in code**: zero matches in `src/`; it is a
  planned field (`docs/design/domain-packs.md:568-666`,
  `docs/design/program-init.md` — status PLANNED, unbuilt) that only
  `program-init` would stamp. No existing manifest (quant v0.2.0, rv)
  carries it. The finding-1 fix-direction "derivation edge wins" is NOT
  buildable near-term.
- `tests/state/test_pack_declarations.py:379`
  `test_compose_audit_template_referenced_pack_outranks_optin_order` **pins
  the backwards behavior** and must flip with the fix.
- The enforcement-map pack rows (`engineering-principles.md:377-383`,
  `tests/contracts/test_pack_boundary.py`) constrain vocabulary/boundary but
  say nothing about multi-pack SELECTION or tie disclosure — the compose
  heuristic sits in an unenforced gap.

### Proposed work (ranked)

**Maintainer ruling 2026-07-15: fixes are done PROPERLY, no near-term
stopgaps.** The proper fix is the full derivation-lineage build, sequenced:

P1a **Build `program-init` + `derived_from`** per
  `docs/design/program-init.md` (status PLANNED → build): the verb that
  instantiates a program template from a domain skeleton stamps
  `derived_from: {pack, seam, version, sha}` mechanically into the program
  pack's manifest; `PackManifest` (`state/pack.py:150-165`) +
  `parse_manifest` learn the field. Existing packs (quant v0.2.0, rv) get
  the stamp by re-running the init/reseal path over the real lineage (rv
  derives from the quant skeleton), not by hand-editing.

P1b **Rewrite `compose_audit_template` selection with NO heuristics**:
  (i) exactly one candidate → it wins; (ii) >1 candidate and a derivation
  edge exists among them → the DERIVED (program) template wins; (iii) >1
  candidate and no edge → loud `SpecInvalid` naming every candidate (the
  pack_gate refusal model — remedy: pass `template` explicitly, or fix the
  lineage). The receipt-bindings tiebreak is retired outright. The compose
  disclosure names EVERY candidate + the deciding rule, so any pick is
  auditable at preflight. Flip the backwards test
  (`test_compose_audit_template_referenced_pack_outranks_optin_order`) in
  the same commit; add the enforcement-map row: **a multi-candidate pack
  seam resolves by disclosed rule or refuses loudly — never a silent
  heuristic** + fire-path test.

P1c Derive `audited_source_echo` from the same disclosed selection as P1b
  (not an independent first-match walk over manifest `files`); same
  treatment for `resolve_template_pack_echo`'s file-collision first-match
  (collision → loud, not first-wins).

P1d Legislate id-collision handling for `tolerances` and
  `registration_fields` NOW, before their consumers land, under the same
  P1b enforcement-map row: a collision either refuses loudly or resolves by
  a disclosed, journaled rule. Cheap: the rule + a fires-test; no consumer
  exists to migrate.

---

## Part 2 — DevX levers beyond the executed architecture review

Baseline facts: 9,781 tests selected (10,021 collected; collection alone
16.2s), 624 test files; 6 regen scripts ≈ ~20s serial with no aggregate
entry point; 844 commits since 2026-07-01; most-churned files are GENERATED
(`operations.json` 48 touches, `docs/generated/operations.md` 36,
`docs/primitives/README.md` 34, `cli/_verb_module_map.py` 33).

### Batch A — mechanizable now (no ruling; ~one day total)

A1 **`regen-all` entry point** (S). Six regen scripts, three docs each
  listing a different subset (`adding-a-primitive.md:236-241` names five;
  `handoff-packages-2026-07-12/HANDOFF.md:63-66` six + check;
  `sync-checklist.md:198-204` four). One `scripts/regen_all.py` (or
  console-script) in dependency order + `--check`; re-point all docs +
  pre-commit at it; delete the per-doc enumerations.

A2 **Merge driver for generated artifacts** (S–M). The top cross-worktree
  collision surface is pure-function-of-registry files; `.gitattributes`
  has no `merge=` driver. Mark the generated set `merge=ours` (or a custom
  driver) + post-merge `regen-all`. Kills the dominant swarm merge-conflict
  class.

A3 **Slow tier on one CI leg** (S). `ci.yml:206-210` runs the multi-hour
  `-m slow` tier on all three Python legs; gate it on 3.12 (the file
  already hoists lints/mypy to one leg on the same reasoning).

A4 **Drop the redundant coverage re-run** (S). `ci.yml:211-234` runs a
  third full non-slow pass purely for a non-gated coverage number; fold
  `--cov` into the existing 3.12 default run.

A5 **Regen-debt ledger → strict-xfail CI gate** (S). The ledger self-
  describes as manual pending a test that "does not exist yet"
  (`regen-debt-ledger.md:20-22`) while carrying rows RED on their branches.
  Build the strict-xfail punch-list it specifies (precedent:
  `test_recovery_registry.py`).

A6 **Frozen-count literal lint** (S). Extend the d-pins contract test:
  `\d+ (primitives|verbs|schemas|error codes)` literals in
  `docs/internals/` + `docs/workflows/` must equal live counts or sit in a
  cited allowlist. (Live drift: `adding-a-primitive.md:4` says 167;
  registry is 169.)

A7 **Fix `adding-a-primitive.md` content** (S). Correct the count to
  verify-live phrasing; list all six regen scripts (or point at A1); add
  the missing `build_verb_module_map.py` step (in pre-commit but absent
  from the recipe — a session following the recipe ships a stale map).

### Batch B — needs a maintainer ruling

B1 **Change-impact test selection for the inner loop** (M–L; biggest
  throughput lever). No test-impact mechanism exists (only xdist
  `-n auto`). Proposed resolution shape: an ADVISORY fast lane (testmon or
  diff→test-path map) for the inner agent iteration only; the full suite
  stays mandatory at CI and in the release skill. RULING: is an advisory
  fast lane acceptable under the "verify a guard can fire" culture?

B2 **Constitution readability** (M). `engineering-principles.md` self-
  admits it exceeds the single-read cap (~31k tokens, lines 19-22) while
  CLAUDE.md instructs every session to read it; the P6.4 history split
  moved only 26 lines. RULING: split shape — per-section enforcement-map
  files + generated index, vs. a parallel keyed justification table —
  without severing normative rows from their "fires when" context (the
  risk the doc itself names at :37-43).

B3 **Generalize the handoff-package model** (M). The 2026-07-12 handoff
  package (architect-memo pre-settling all design calls + file-disjoint
  units + integrator merge order + executable workflow scripts) is the
  repo's best swarm mechanism and is a one-off. RULING: how much to
  formalize — a `docs/plans/_TEMPLATE-handoff/` scaffold + a disjointness
  checker over declared per-unit file sets, vs. keep bespoke.

B4 **Mutation-testing blind spot on cluster verbs** (M). The lazy-import
  pattern silently blocks mutmut exactly on `submit_flow` /
  `aggregate_flow` / transport (`mutation-testing.md:72-80`), where a
  silent wrong-path costs real cluster time. RULING: (a) module-scope
  import extraction on a shortlist + a scheduled (not per-PR) mutmut run,
  vs. (b) accept the gap and lean on the higher-leverage lints the doc
  names.

B5 **Onboarding index** (S–M). The fresh-session read chain (CLAUDE.md →
  principles → architecture.md → adding-a-primitive → sync-checklist →
  plans) has no ordered index with token budgets. Note: the devx sweep
  flagged `SESSION_HANDOFF.md` as missing — it is NOT missing; it lives at
  `C:\Users\james\SESSION_HANDOFF.md` (deliberately outside the repo, it
  carries cluster/demo state). RULING: what belongs in a tracked in-repo
  onboarding index vs. the home-dir handoff vs. git log.

### Suggested order

1. Batch A as one wave (A1 first — A2 and A5 depend on it).
2. Part 1 in sequence (P1a → P1b+P1c → P1d) — no ruling pends; the
   maintainer ruled proper-fix-only on 2026-07-15.
3. Rule B1 (biggest lever) and B2; B3-B5 opportunistic.

## Drift log

- 2026-07-15: banked. Nothing executed.
- 2026-07-15 (later): maintainer rulings — (1) NO verification-at-scale
  material in this repo (explored in a separate repo; the earlier
  convergence note was removed); (2) all fixes properly done, no near-term
  stopgaps — Part 1 rewritten as the full program-init/derived_from build
  with a no-heuristics selection law.
- 2026-07-15 (fable sweep): 15-agent dive+premortem+critic sweep over this
  plan produced the implementation handoff —
  `docs/plans/fable-sweep-devx-2026-07-15/` (ARCHITECT-MEMO.md + full unit
  specs in sweep-results.json). Batch B (B1-B5) is NOT in the sweep — its
  items still need maintainer rulings. Known corrections to this doc from
  the sweep: the generated file is `src/hpc_agent/operations.json` (not
  `config/`); A6's pattern alternation should include 'regen scripts'.
- 2026-07-16 (late): Batch B RULED by maintainer — B1 YES (advisory diff→test fast lane; CI/full-suite gates unchanged), B2 per-section split + GENERATED index (maintainer delegated shape; severance risk avoided by splitting on the existing self-contained sections), B3 template + disjointness checker, B4 BUILD (module-scope import extraction + scheduled mutmut, sequenced AFTER latency wave-1 integration — shared seams), B5 folds into B2's index. B1-B3 dispatched same night.
