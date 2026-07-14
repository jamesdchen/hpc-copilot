# Architecture review — 2026-07-13 — VERIFICATION DELTA (2026-07-14)

Status: **VERIFIED DELTA.** The parent plan
[`architecture-review-2026-07-13.md`](architecture-review-2026-07-13.md) still
carries a `Status: BANKED — … no proposals executed yet` header. **That header
is stale.** The plan was pinned at `f3594d9`; the overwhelming majority of its
proposals were executed in the journal-package split and the fable-sweep
hardening train between that pin and HEAD (`61e0aad`). This delta records the
per-proposal status re-verified against the live tree on 2026-07-14 (three
independent read-only agents, one per cluster; each item checked against current
file:line evidence and attributed to the landing commit where one exists).

Read this before scheduling any architecture-review work: most of it is done.

## Scoreboard

| Cluster | Done | Partial | Remaining | Refuted-now |
|---|---|---|---|---|
| P1 — layering blind spots | 6 | 0 | 0 | 0 |
| P2 — split the god seats | 6 | 0 | 1 | 0 |
| P3 — CI/enforcement repairs | 5 | 0 | 1 | 0 |
| P4 — consolidations | 6 | 1 | 2 | 0 |
| P5 — contract hygiene | 2 | 0 | 2 | 0 |
| P6 — docs truth pass | 7 | 1 | 1 | 0 |
| Also surfaced | 3 | 0 | 3 | 1 |

## What is GENUINELY LEFT (the real worklist)

Ranked roughly by value-to-risk. Everything not listed here is DONE — see the
per-cluster evidence below.

1. **P2 · `infra/transport.py` module→package split** — the last un-split god
   seat, and it has *grown* since the pin (2,219 → **2,319 ln**). Only the P1
   manifest/prune carve-out landed; the package split (and its
   `scheduler-integration.yml` `paths:` rider) is still open. Highest-value
   remaining architectural item.
2. **P5a · promote-to-public sweep + a holding lint** — all four named symbols
   are still private-but-cross-imported: `reconcile._sibling_run_ids`,
   `run_record._current_homedir` (10 importers), `remote._capture_via_select`
   (imported by the kernel + block_drive), `detached._pid_alive` (7 ops
   importers). Rename the genuinely-shared ones public and add a lint.
3. **P5b · declare the plugin API** — the notebook-render example plugin still
   imports far past the documented seam (`ops.notebook`, `ops.decision`,
   `state.*`, `cli._dispatch`, `_kernel.registry`, `execution.mapreduce`). Any
   reorg breaks a shipped, CI-gated plugin. Version the wider surface or narrow
   the plugin.
4. **P4 · three consolidation tails:**
   - *(partial)* `verify_op` still hand-rolls `_sha_json` (`ops/…/verify_op.py`)
     for `view_sha`; fold it through `determinism.canonical_sha` like `prereqs`
     and `audit_view` already were.
   - *(remaining)* preflight commons — `SubCall` / `_synth_error_subresult` /
     `_run_subprocess` are still duplicated across the four `_preflight.py`
     modules (status/aggregate/submit/classify_axis). Extract the triplet, re-point.
   - *(remaining)* test-suite fixture extraction — fake-optuna (×2),
     `journal_home`, `run_cli` still scattered (the `tests/_ssh_fakes.py`
     addendum item N5 did land).
5. **P6.7 · retire the 11 legacy `inputs:` frontmatter blocks** — exactly 11
   primitive docs still carry `inputs:` though `primitives/README.md` already
   declares the block obsolete and nothing validates it against schemas.
6. **P3 · ci.yml/docs.yml auto-commit race** (mild) — both workflows still
   auto-commit overlapping `docs/` files; worst case is a spurious failed push.
   Give the doc regen one owning workflow.
7. **P6.2 residue** (out of the item's named 4-doc scope, but real) —
   `hpc-campaign-driver` ghost references still linger in `design/`, `runbooks/`,
   and `decide-monitor-arm.md` (the `campaign-lifecycle.md` / changelog refs are
   sanctioned historical framing).
8. **Dead-code pass** (apply verify-a-guard-can-fire before deleting):
   `plugin_worker_prompt_roots` (`_kernel/registry/plugins.py:214`, consumer
   gone); `prepare_phase2_spec` (still registered in `operations.json` +
   `_verb_module_map.py`, self-declared non-production); `evidence_brief_op`'s
   landed-T4 `except ImportError` fallback (now unreachable —
   `ops/evidence_render.py::render_brief` exists).

## Per-cluster evidence

### P1 — Close the layering blind spots — **ALL DONE (6/6)**

- `ops/transfer/{manifest,prune}.py` → `infra/{manifest,prune}.py`; `ops/transfer/`
  gone; transport no longer imports `ops` (`74f8ec5`).
- `incorporation ↔ ops/submit_flow` cycle broken: the ~950-line guard library is
  extracted to `infra/executor_guard.py`; both sides import `infra.*`, not each
  other; `_GENERATED_SHIPPABLE` moved to `infra/transport.py` (`94325fd`).
- `campaign_spec_identity` is public at `meta/campaign/blocks.py:74`; `overnight.py`
  uses the public name (`2995c56`).
- The directional lint was extended and **over-delivered**: `lint_subject_imports.py`
  carries `infra→ops` and `incorporation→{ops,meta}` (plus addendum N3
  `_kernel→ops`, `state→ops`) with synthetic fire-path tests (`b3cb048`).
- `run_story.py → ops.overnight` correctly left as the sanctioned lazy import.
- Template-boundary test extended to `scaffolds/` with a documented `reduce.history`
  allowlist-with-rationale (`26f64bf`).

### P2 — Split the god seats — **6/7 DONE**

- **DONE** `ops/decision/journal.py` (3,801) → `ops/decision/journal/` package (12 gate
  submodules + `_shared.py`); the three whole-module `inspect.getsource(journal)`
  test scans were re-pointed to concatenated submodule sources in-commit
  (`f1bd4d4`, `191ff4c`).
- **DONE** `incorporation/build/submit_spec.py` 1,345 → **753 ln** (guards to
  `infra/executor_guard.py`).
- **DONE** `_kernel/extension/mcp_server.py` 2,054 → **1,762 ln** (`mcp_elicitation.py`
  split; `de51dac`).
- **DONE** `_kernel/hooks/relay_audit_stop.py` → `relay_audit_stop/` subpackage with
  the entry-module path preserved so installed `~/.claude/settings.json` hooks
  aren't orphaned (`2254a41`).
- **DONE** `agent_assets.py` merge-skeleton collapsed (`1be3bd5`).
- **DONE** `execution/mapreduce/reduce/status.py` 1,431 → **1,171 ln**
  (`scheduler_profile.py` extracted; `ef0a7bb`) — homed in `reduce/`, not
  `infra/backends`.
- **REMAINING** `infra/transport.py` — see worklist item 1.

### P3 — CI/enforcement repairs — **5/6 DONE** (`9bda665`, `6143b96`)

- **DONE** `regen-pr` self-heals: `build_*.py --write` runs before every `--check`
  gate (docs.yml pattern).
- **DONE** `lint_plugin_manifests` moved into the `plugins` job (where a plugin is
  actually installed) — now able to fire.
- **DONE** lints + mypy hoisted to the single 3.12 leg; only pytest runs on all three.
- **DONE** fire-path tests added to the challenge/conformance/evidence boundary scans.
- **DONE** SGE integration CI: decision recorded as a known gap with a Revisit
  trigger (`docs/internals/scheduler-integration-ci.md`).
- **REMAINING** ci.yml/docs.yml auto-commit race — worklist item 6.

### P4 — Consolidations — **6 DONE / 1 PARTIAL / 2 REMAINING**

- **DONE** `select_window` divergence resolved by deleting the callerless
  `conformance.py` variant (`d8dbf8b`).
- **DONE** the three canonical-sha spellings delegate to `determinism.canonical_sha`
  (`6880607`).
- **PARTIAL** ops-side canonical-sha fold: `prereqs` + `audit_view` folded
  (`016334e`); **`verify_op` not** — worklist item 4.
- **DONE** `FailureCategory` twins single-sourced from `CLASSIFIER_CATEGORIES` with
  drift tests.
- **DONE** interview.json readers → shared `state/interview_doc.py` skeleton, 11
  callers re-pointed (`3031b13`).
- **DONE** detach `_*_cmd_sha` collapsed to `state/runs.py::read_run_cmd_sha`;
  `DetachedHandleFields` base in `_wire/_shared.py` inherited by 6 models (`df5d89c`).
- **DONE** `infra/inspect/_persist.py` routed through `infra.io` atomic writers
  (`fb64e3a`).
- **DONE** schema-resolution ladder deduped to `schema_candidate_ladder()` with a
  reachability lint (`e946e76`).
- **REMAINING** preflight commons + test-suite fixture extraction — worklist item 4.

### P5 — Contract hygiene — **2/4 DONE**

- **REMAINING** P5a promote-to-public sweep + lint — worklist item 2.
- **REMAINING** P5b declare the plugin API — worklist item 3.
- **DONE** P5c `_wire` suffix lint (`scripts/lint_wire_suffix.py` + test, wired in CI;
  siblings schema-reachability + mirror-ledger also landed).
- **DONE (to the plan's floor)** P5d `_wire/fixtures` layout documented with its
  `__module__` constraint (`c496136`); the optional move-with-regen was deliberately
  skipped.

### P6 — Docs truth pass — **7/8 DONE**

- **DONE** 1 architecture.md worker surfaces removed; 3 dangling slash commands fixed;
  4 engineering-principles.md split to a `-history` sibling; 5 docs/README nav map
  includes `design/`; 6 bug-sweep honest status; 8 one regen-debt ledger.
- **PARTIAL** 2 `hpc-campaign-driver` ghost cleared from the 4 named docs; residue in
  `design/`/`runbooks/`/`decide-monitor-arm.md` — worklist item 7.
- **REMAINING** 7 retire the 11 legacy `inputs:` frontmatter blocks — worklist item 5.

### Also surfaced

- **REFUTED-NOW** checkpoint.py `run_iterations` fresh-vs-resume "bug" (bug-sweep #30):
  the code tests `resume_point == 0` with an explicit anti-`state is None` comment,
  and did so already at the pin — the claim never matched the code.
- **DONE** `errors._registry_remediation` deleted; `status_preflight` stale docstring
  corrected (`ec94367`).
- **REMAINING** dead-code: `plugin_worker_prompt_roots`, `prepare_phase2_spec`,
  `evidence_brief_op` T4 fallback — worklist item 8.

---

*Method: three independent read-only Opus agents, one per cluster (P1+P2, P3+P4,
P5+P6+surfaced), each re-verifying every proposal item against the live tree at
`61e0aad` with file:line evidence and git attribution. Raw per-cluster tables:
`architecture-review-2026-07-13-delta/` is not banked separately — the tables are
folded inline above.*
