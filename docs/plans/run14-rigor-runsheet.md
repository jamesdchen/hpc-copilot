# Run-14 RIGOR CEREMONY RUNSHEET (purpose #2)

**Scope.** The RIGOR LAYER LIVE EXERCISE owed since run-13's plan (never reached — the
aggregate stage ended in an operator bypass, finding 14). This runsheet lets the demo
session execute `evidence-brief → CONCLUSION → REGISTRATION → optional CHALLENGE` as a
sequence of pre-filled calls instead of discovering the verb surfaces live.

- **Repos:** dev repo (this file) `C:\Users\james\CC Allowed\hpc-agent` @ `d02570f6`;
  experiment repo `C:\Users\james\CC Allowed\harxhar-clean` (READ-ONLY for this session,
  the demo drives it live). Windows; never raw cluster ssh; drive everything via
  block-drive / append-decision.
- **Every gate claim below carries a `file:line` or `path::symbol` citation.** Where the
  machinery is PLANNED-not-satisfiable-by-this-run's-state, it is marked **[BLOCKER]** or
  **[CANNOT SATISFY]** with the reason — no invented steps.

---

## 0. Ground-truth facts (computed from the LOCAL files this session; cite each)

### The lgbm leg — the citable artifact + run identity
Source: `harxhar-clean/.hpc/runs/causal_tune_tree_lgbm-7905102a.json` (sidecar).

| Fact | Value | Where |
|---|---|---|
| `run_id` | `causal_tune_tree_lgbm-7905102a` | sidecar `run_id` |
| `cmd_sha` | `7905102a28e5bae7107cf7fdbc7edcc1557761c8b5fd50a56afa1b0ad566a3a6` | sidecar `cmd_sha` (8-hex prefix **`7905102a`**) |
| `cluster` | `carc` | sidecar `cluster` |
| `audited_source` | audit_id `causal_tune_tree`, source `specs/causal_tune_tree.py`, template `packs/rv/templates/rv_audit.py` | sidecar `audited_source` |
| `data_manifest_sha` | `28f32015586edef1d76d5af822b254d0e895d79d232610c522ae4bc947c235fb` | sidecar `data_manifest_sha` |
| `summary_artifact` (declared) | `causal_tune_tree/metrics_table.csv` | sidecar `summary_artifact` |
| `task_count` | 900 (9 buckets × 100 chunks) | sidecar `task_count` |
| `aggregate_cmd` | `python3 specs/reduce_causal_tune_tree.py` (disk sha `2cedee8f34…`) | sidecar `aggregate_defaults` |

### The aggregate output (purpose-1 product — the numbers the conclusion is ABOUT)
`sha256sum` computed this session on the two local files:

| Artifact | sha256 (this session) |
|---|---|
| `_aggregated/causal_tune_tree_lgbm-7905102a/causal_tune_tree_lgbm-7905102a.json` (the envelope) | `6499d8053b209381670de8508dbddba3074f8932bc7937b5376463fa6d895dcc` |
| `_aggregated/causal_tune_tree_lgbm-7905102a/metrics_aggregate.json` (the `cluster_reduce` output) | `b57b9f33df691822fd77429b581be7a6461db1fd215d4a628405163b25870ed7` |

- The envelope declares `schema="causal_tune_tree metrics_table v1"`, `n_arms=9`, and 9
  rows (`model=lgbm`, buckets `all_features/baseline/implied_vol/liquidity/market_ew/
  market_vw/moments/sentiment/vol_demand`). **All nine `qlike` read 0.126–0.131 vs
  `incumbent_qlike=0.13415`** — the expected ~0.13 band. `metrics_aggregate.json`
  provenance = `{source: cluster_reduce, reduced_at: 2026-07-16T19:37:47+00:00,
  incomplete_waves: []}` (the deterministic reducer computed every number — sanctioned).
- **HONESTY NOTE (artifact shape).** Both JSONs carry `"metrics_table_csv":
  "metrics_table.csv"`, but **no `metrics_table.csv` exists in that directory** (verified
  by `find`; the only `metrics_table.csv` on disk belongs to the *linear* run
  `causal_tune_linear-de448128`). The lgbm leg's **citable artifact is the JSON
  envelope**, not a CSV. Cite the `.json` sha, and do not tell the human a CSV exists.

### State the ceremony sits on — what EXISTS and what DOES NOT
Verified this session by `ls`/`find`/`sha256sum` under `harxhar-clean/`:

| Precondition | State | Consequence for this run |
|---|---|---|
| Scope tags (`.hpc/scopes/`) | **ABSENT** (dir does not exist) | The run carries NO scope tags — consistent with the audit config recording `observables: null` and no tags (`.hpc/notebooks/causal_tune_tree.decisions.jsonl` line 1). `evidence-brief` must key on **lineage**, not tags (§1). |
| Notebook audit | **PASSED, CURRENT** | `causal_tune_tree` audit: data-selection/target-construction/metrics auto-cleared; feature-construction + baseline human-signed; final `notebook-status … passed` at module token `fa32224cfe9b` (`.hpc/notebooks/causal_tune_tree.decisions.jsonl` line 32). Disk `specs/causal_tune_tree.py` raw sha = `fa32224cfe9b40…` (matches). → the `notebook-audit` prerequisite kind IS satisfiable. |
| Pack `rv` template | **CURRENT** | `packs/rv/templates/rv_audit.py` disk sha `8a0ccccc213cc732…` == the sha recorded in `packs/rv/manifest.json` `files[templates/rv_audit.py]`. |
| Sealed dossier (`.hpc/_dossier/…`) | **ABSENT** | Must be exported (§3) before a conclusion/registration can cite `bundle_sha256`. `export-dossier` is a mutate verb the session CAN run — a step, not a blocker. |
| Registrations (`.hpc/registrations/`) | **ABSENT** | No registration exists yet; `verify-registration` on this run returns `status=absent` until §6 files one. |
| Determinism-fingerprint ledger | **ABSENT** | `_aggregated/_fingerprints/` holds only a `_pulls/` subdir — **no `7905102a…​.jsonl`**. → `evidence-brief` shows NO envelope for this lineage, and a registration `reproduction` prerequisite with a `requires` floor **[CANNOT SATISFY]** (n=0). |
| Conformance ledger (`_aggregated/_conformance/`) | **ABSENT** | `conformance-record` is out of scope for this run (production emitter, `agent_facing=False`; `conformance-record.md:28`). |

---

## STEP LIST (ordered; each with the exact spec body)

Legend: **[CODE]** = agent runs the verb (query/mutate). **[HUMAN]** = a decision point;
the exact utterance to type is given with an offered-consent hint. Relay every code render
VERBATIM (run-14 watch list, SESSION_HANDOFF finding 8/relay rules).

### Step 1 — [CODE] evidence-brief over the lineage (the "evidence-brief over the scope tags" act)
`evidence-brief` REQUIRES at least one of `tags` / `lineage`; an unkeyed call is refused
(`docs/primitives/evidence-brief.md:47-49`). **This run has no tags**, so key on lineage:

```json
{ "lineage": "causal_tune_tree_lgbm-7905102a" }
```
MCP `mcp__hpc-agent__evidence-brief` (or `hpc-agent evidence-brief --spec … --experiment-dir .`).

- **Expected result on THIS state:** `conclusions: []` (none filed yet), `activity: {}`
  (no tags), `envelopes: []` (no fingerprint ledger — see §0), `cache: miss|disabled`.
  Relay the `render` verbatim. This is a read-only `query` — no SSH, no writes
  (`evidence-brief.md:26-30`).
- **Offered-consent hint to the human:** "No prior conclusions or determinism envelope
  exist for this lineage yet — this brief is the clean baseline before we file one.
  Proceed to file the conclusion? (y)". (No typed sha needed here; evidence-brief is a
  reporter.)

### Step 2 — [CODE, optional] scope-status (confirm no scopes, for the record)
```json
{}
```
`mcp__hpc-agent__scope-status`. Omitting `scope` reports every scope under `.hpc/scopes/`;
a missing tree reports `{}` (`docs/primitives/scope-status.md:24-27`). Expected: `{}`.
Include only if the human asks "what's locked?" — otherwise skip.

### Step 3 — [CODE] export-dossier (mint the sealed subject the conclusion + registration bind to)
```json
{ "run_id": "causal_tune_tree_lgbm-7905102a" }
```
`mcp__hpc-agent__export-dossier` (or CLI). Idempotent, keyed on `run_id`
(`docs/primitives/export-dossier.md:147-151`). It seals the sidecar, decision journal,
briefs, block terminals, the harvested aggregate, AND — because the sidecar echoes
`audited_source` — the audited `specs/causal_tune_tree.py` + `packs/rv/templates/rv_audit.py`
+ the notebook attestation journal + renders (`export-dossier.md:29-39`).

- **CAPTURE `bundle_sha256` from the result** — call it `<DOSSIER_SHA>`. It is the
  `content_sha` a registration binds (R2, `docs/design/registration-kernel.md:78-98`) and a
  valid `dossier` citation for the conclusion (evidence-memory `CITATION_KINDS`,
  `docs/design/evidence-memory.md:216`).
- **Expected `gaps`:** the fingerprint-ledger store is absent → one recorded gap for
  `determinism-fingerprint` (reported, never fatal — `export-dossier.md:117-123`). Do not
  treat the gap as an error; disclose it.

### Step 4 — CONCLUSION filing — the sha-prefix-cited finding
A conclusion is written ONLY via `append-decision` under `scope_kind="conclusion"`,
`block="conclusion"` — there is no conclusion verb (Lock 1,
`docs/design/evidence-memory.md:267-269`). `citations` MUST be non-empty and each is
resolved server-side against its kind's ONE resolver; the append REFUSES on any
unresolvable/mismatched sha (`evidence-memory.md:243-252`). Valid `CITATION_KINDS` = `run`
/ `dossier` / `fingerprint` / `attestation` (`evidence-memory.md:214-219`).

> **HONESTY NOTE (what can be cited).** The raw `metrics_aggregate.json` sha (`b57b9f33…`)
> is **not** a citation kind. Bind the numbers into the conclusion through the **`run`**
> citation (sha = `cmd_sha`) and the **`dossier`** citation (sha = `<DOSSIER_SHA>` from
> §3), which seals the harvested aggregate. That is the sanctioned evidence linkage.

#### 4a — [CODE] append-decision (this will REFUSE and open the sign-off popup — expected)
```json
{
  "scope_kind": "conclusion",
  "scope_id": "causal-tune-tree-lgbm-beats-incumbent",
  "block": "conclusion",
  "response": "<HUMAN TYPES — see 4b>",
  "evidence_digest": {
    "envelope_sha": "6499d8053b209381670de8508dbddba3074f8932bc7937b5376463fa6d895dcc",
    "reduced_at": "2026-07-16T19:37:47+00:00",
    "n_arms": 9,
    "qlike_band": "0.126-0.131 vs incumbent 0.13415"
  },
  "proposal": "File: across all 9 exog buckets, lgbm causal-tune beats the incumbent QLIKE (0.13415); every arm DM-negative (dm_better='a'), p from 1e-8 to 1e-53.",
  "resolved": {
    "conclusion_id": "causal-tune-tree-lgbm-beats-incumbent",
    "tags": [],
    "concludes": [ { "scope_kind": "run", "scope_id": "causal_tune_tree_lgbm-7905102a" } ],
    "citations": [
      { "kind": "run", "ref": "causal_tune_tree_lgbm-7905102a",
        "sha": "7905102a28e5bae7107cf7fdbc7edcc1557761c8b5fd50a56afa1b0ad566a3a6" },
      { "kind": "dossier", "ref": ".hpc/_dossier/causal_tune_tree_lgbm-7905102a.zip",
        "sha": "<DOSSIER_SHA from Step 3>" }
    ],
    "finding": "Across all 9 exogenous-feature buckets, the lgbm causal-tune model's out-of-sample QLIKE (0.126-0.131) beats the deployed incumbent (0.13415); Diebold-Mariano favors the tuned model in every bucket (p 1e-8 .. 1e-53). n=218934 per arm."
  }
}
```
- `tags: []` is allowed (disclosed, not refused — `evidence-memory.md:185`).
- `concludes` is optional identity linkage (`evidence-memory.md:187-189`).
- The gate re-canonicalizes and re-resolves the citations; `content_sha` is bound
  server-side (cannot be asserted — `evidence-memory.md:196-207`). No `content_sha` field
  is placed on the spec.

#### 4b — [HUMAN] the typed sign-off (Lock 3)
The `response` must be a non-bare utterance that **names `conclusion_id` token-exact AND at
least one cited sha by an 8+-hex prefix** matched against the verified citation set
(`docs/design/evidence-memory.md:273-282`; sibling of `_assert_conclusion_authorship`).

- **Exact utterance to type (offered-consent hint):**
  > `File conclusion causal-tune-tree-lgbm-beats-incumbent — the lgbm arms cited at run 7905102a beat the incumbent QLIKE across all nine buckets.`
  - `causal-tune-tree-lgbm-beats-incumbent` = the `conclusion_id`, token-exact.
  - `7905102a` = the 8-hex prefix of the cited `run` sha (`cmd_sha`). (After Step 3 you may
    instead cite the `<DOSSIER_SHA>`'s first 8 hex — either verified citation's prefix
    satisfies the bar.)
- **Expected refusals (working-as-designed — relay verbatim, re-open the popup):**
  - a bare `y` → refused, `_is_bare_ack` (`evidence-memory.md:273`);
  - a response missing the `conclusion_id` token or lacking any cited-sha prefix → refused
    (`evidence-memory.md:275-277`);
  - a citation whose `sha` does not resolve on this namespace (e.g. a mistyped
    `<DOSSIER_SHA>`) → append refused with the recorded-vs-recomputed pair
    (`evidence-memory.md:243-252`). Remedy: fix the cited sha, retry.

#### 4c — [CODE, optional] evidence-brief again → the conclusion now leads
Re-run Step 1's call; the new conclusion is the digest lead with its citations
re-resolved `verified` (`evidence-brief.md:64-72`). Relay verbatim. (Its `view_sha` may be
bound into a re-sign, but a conclusion `view_sha` is OPTIONAL — `evidence-memory.md:200-202`.)

### Step 5 — REGISTRATION ceremony (the maximal human tier)
A registration is one more attestation, written ONLY via `append-decision` under
`scope_kind="registration"`, `block="registration"` — **no registration verb, no chain,
no next_block** (Lock 1, `docs/design/registration-kernel.md:277-292`;
`docs/internals/principles/registration-kernel.md:26`). The subject is the **sealed
dossier** bound by `bundle_sha256` (R2). The gate recomputes FOUR legs server-side:
dossier sha vs a live dry re-gather, template sha vs disk bytes, every chain entry's
`content_sha` vs its kind's checker, and the `view_sha` vs the deterministic
`verify-registration` projection (Lock 2 + `view_sha` leg,
`registration-kernel.md:296-330`).

#### 5-PRE — [BLOCKER] the registration TEMPLATE must exist first
A registration's `resolved` must carry a `template` (relpath) + `template_sha`, and the
template is a **caller-authored `{fields, prerequisites}` JSON file — core ships NONE; an
attempted registration with no resolvable template is a LOUD refusal**
(R5, `docs/design/registration-kernel.md:225-256`;
`docs/internals/principles/registration-kernel.md:28`). **No such file exists in
`harxhar-clean`** (verified: no `*registration*` file anywhere but `packs/README.md`;
`packs/rv/manifest.json` `fills_slots: []`). → **The full registration ceremony CANNOT
run until the human authors a template.** This is a caller/human act (the "three unsigned
HUMAN slots" — registration template fields — `registration-kernel.md:240-244`), not
something the agent invents.

- **Minimal viable template (the human authors + saves; suggested path
  `packs/rv/templates/rv_registration.json`)** — the ONLY prerequisite kind THIS run's
  state can satisfy is `notebook-audit`:
  ```json
  {
    "fields": ["strategy-owner", "go-live-window"],
    "prerequisites": [
      { "slot": "audit-current", "kind": "notebook-audit",
        "subject_id": "causal_tune_tree" }
    ]
  }
  ```
  - `notebook-audit` currency = every required section signed/auto-cleared AND recomputed
    module sha == the entry's `content_sha` (`registration-kernel.md:132`). Satisfiable:
    the audit passed at `specs/causal_tune_tree.py` (§0).
  - **Do NOT add a `reproduction` slot with a `requires` floor** — no fingerprint ledger
    exists (§0), so the floor reads `n=0` / not-available and the whole append is refused
    (`registration-kernel.md:663-668` T4 stub retirement + `principles/…:32`;
    R4 partial-registration refusal `registration-kernel.md:219-224`). **[CANNOT SATISFY]**
    this run.
  - **Do NOT add a `scope-budget` slot** — no scope is locked (§0); it would need a
    `scope-lock` first and a `max_looks` integer (`registration-kernel.md:627-636`).

#### 5a — [CODE] verify-registration to render the brief the human signs over
`verify-registration` is a read-only `query` (`docs/primitives/verify-registration.md`).
Run it by `run_id` to get the pre-append projection + `view_sha`:
```json
{ "run_id": "causal_tune_tree_lgbm-7905102a" }
```
- **Before any registration exists it returns `status: "absent"`** (reporter, never raises
  — `verify-registration.md:22-24, 48-51`). That is expected and fine; the human's binding
  witness derives from the pre-append projection the GATE recomputes, not from a post-hoc
  verify (the T7 coherence note, `registration-kernel.md:698-707`). Relay the brief verbatim.

#### 5b — [CODE] append-decision under block `registration` (refuses → sign-off popup)
```json
{
  "scope_kind": "registration",
  "scope_id": "causal-tune-tree-lgbm-live",
  "block": "registration",
  "response": "<HUMAN TYPES — see 5c>",
  "evidence_digest": { "dossier_sha": "<DOSSIER_SHA from Step 3>", "audit_id": "causal_tune_tree" },
  "proposal": "Register causal_tune_tree_lgbm-7905102a for go-live, prereq audit-current (notebook-audit).",
  "resolved": {
    "registration_id": "causal-tune-tree-lgbm-live",
    "run_id": "causal_tune_tree_lgbm-7905102a",
    "dossier_sha": "<DOSSIER_SHA from Step 3>",
    "template": "packs/rv/templates/rv_registration.json",
    "template_sha": "<raw-bytes sha of the template the human authored in 5-PRE>",
    "fields": { "strategy-owner": "<human value>", "go-live-window": "<human value>" },
    "prerequisites": [
      { "slot": "audit-current", "kind": "notebook-audit",
        "subject_id": "causal_tune_tree",
        "content_sha": "<recomputed audit module sha — from the 5a brief's prerequisite leg>" }
    ]
  }
}
```
- Every declared `fields` slug must carry a non-empty value (COUNTING completeness,
  `registration-kernel.md:256-260`); every declared prerequisite must appear in the chain
  and read CURRENT, else refused naming the slug (R4, `registration-kernel.md:219-224`).
- All shas are server-recomputed; a fabricated `dossier_sha`/`template_sha`/chain sha /
  `view_sha` is refused with the recorded-vs-recomputed pair (Lock 2,
  `registration-kernel.md:296-320`). Read the CURRENT `content_sha` for the audit slot off
  the 5a `verify-registration` brief's `prerequisites[].recomputed_sha` — do not invent it.

#### 5c — [HUMAN] the registration sign-off (Lock 3 — the strongest bar)
Bare acks refused; the response must **NAME `registration_id` token-exact AND name at
least one prerequisite by an 8+-hex prefix of one chain entry's `content_sha`**
(`docs/design/registration-kernel.md:304-320`;
`docs/internals/principles/registration-kernel.md:30`). There is NO auto-clear / waiver
tier — the attestor is ALWAYS human (`registration-kernel.md:318-320`).

- **Exact utterance to type (offered-consent hint), where `<AUDIT_SHA8>` = the first 8 hex
  of the audit slot's `content_sha` shown in the 5a brief:**
  > `Register causal-tune-tree-lgbm-live for go-live — the audit-current prerequisite <AUDIT_SHA8> is signed and holds at this dossier.`
  - `causal-tune-tree-lgbm-live` = `registration_id`, token-exact.
  - `<AUDIT_SHA8>` = an 8-hex prefix of the verified `notebook-audit` chain entry's
    `content_sha` (copy it verbatim from the rendered 5a brief — it cannot pre-exist in a
    human's vocabulary, which is the point).
- **Expected refusals (relay verbatim):** bare `y`; a response naming the id but no
  prereq sha prefix; a drifted dossier or stale audit slot (would flip a leg non-current);
  a template that fails to resolve on disk. Each is working-as-designed; the remedy is to
  fix the named leg, never to improvise around the gate (SESSION_HANDOFF relay rule 1).

#### 5d — [CODE] verify-registration → expect `status: "current"`
Re-run 5a's call. Expect `status: current`, `template: current`, the `audit-current`
prerequisite `current`, `fields.missing: []` (`verify-registration.md:48-83`). Relay the
brief verbatim. The deployment refusal itself lives caller-side — core only reports
(`verify-registration.md:22-27`).

---

## Appendix A — CHALLENGE + resolution (OPTIONAL act)
Structured dissent against a committed record. Filed/resolved ONLY via `append-decision`
under the `challenge`-family blocks — no challenge verb (Lock 1,
`docs/design/challenge-attestation.md:387-390`); read via the `challenge-status` query
(`docs/primitives/challenge-status.md`). A good target for the exercise is the conclusion
filed in Step 4 (a challenge may target any committed attestation the `CITATION_KINDS`
resolvers address — a conclusion resolves via the `attestation` kind,
`challenge-attestation.md:104-110`).

### A1 — [CODE] challenge-status (thread/target view — expect empty)
```json
{ "subject_kind": "conclusion", "subject_id": "causal-tune-tree-lgbm-beats-incumbent" }
```
`mcp__hpc-agent__challenge-status`. Exactly one addressing; a bare half-address is refused
(`challenge-status.md:38-52`). Expected: no standing challenges. Relay verbatim.

### A2 — [CODE] append-decision, block `challenge` (file the dissent — refuses → popup)
`citations` MUST be non-empty (`challenge-attestation.md:294-296`); the gate verifies a
committed record exists at exactly the target `content_sha` (`challenge-attestation.md:282-284`).
```json
{
  "scope_kind": "challenge",
  "scope_id": "lgbm-window-too-short",
  "block": "challenge",
  "response": "<HUMAN TYPES — see A3>",
  "resolved": {
    "challenge_id": "lgbm-window-too-short",
    "target": { "kind": "attestation", "subject_kind": "conclusion",
      "subject_id": "causal-tune-tree-lgbm-beats-incumbent",
      "content_sha": "<the conclusion's content_sha — from the Step 4c evidence-brief / read-decisions>",
      "scope": { "scope_kind": "conclusion", "scope_id": "causal-tune-tree-lgbm-beats-incumbent" } },
    "citations": [
      { "kind": "run", "ref": "causal_tune_tree_lgbm-7905102a",
        "sha": "7905102a28e5bae7107cf7fdbc7edcc1557761c8b5fd50a56afa1b0ad566a3a6" }
    ],
    "grounds": "The DM tests use overlapping chunks (halo=24000); the effective independent-sample count is far below n=218934, so the p-values overstate significance."
  }
}
```
The target `content_sha` is read from the conclusion record (Step 4c `evidence-brief`
`conclusions[].content_sha`, or `read-decisions {scope_kind:"conclusion",
scope_id:"causal-tune-tree-lgbm-beats-incumbent"}` → the bound record).

### A3 — [HUMAN] the challenge sign-off (same raised bar)
Non-bare; name `challenge_id` token-exact + cite an evidence sha by 8-hex prefix
(`challenge-attestation.md:405` + the reused registration R6 bar).
- **Exact utterance:**
  > `File challenge lgbm-window-too-short — the overlapping-halo DM test at run 7905102a overstates significance.`

### A4 — [CODE] challenge-status → shows `open`; then [HUMAN] resolve
```json
{ "challenge_id": "lgbm-window-too-short" }
```
Expect one `open` entry (`challenge-status.md:54-73`). Resolve via append-decision block
`challenge-verdict`, `resolved: {challenge_id, verdict: "upheld"|"dismissed", reasoning:
"<mandatory non-empty>"}` (`challenge-attestation.md:301-305`); withdrawal =
`challenge-withdraw` with a mandatory `reason`. Same authorship floor. The conclusion is
never blocked by an open challenge — the flag rides beside its status (C-status,
`challenge-status.md:69-71`).

---

## BLOCKERS SUMMARY (gates this run cannot pass as-is)
1. **[BLOCKER] Registration has no template.** Core ships none and refuses a registration
   with no resolvable template (`docs/design/registration-kernel.md:225-256`). The human
   must author `{fields, prerequisites}` (5-PRE) before Step 5 can proceed. Everything
   through Step 4 (conclusion) is unblocked.
2. **[CANNOT SATISFY] `reproduction` prerequisite kind.** No determinism-fingerprint ledger
   and no `reproduction_receipts.jsonl` exist (§0); a `reproduction` slot with a `requires`
   floor refuses at `n=0` (`registration-kernel.md:663-668`;
   `docs/internals/principles/registration-kernel.md:32`). The run-14 purpose-1
   reproduction is an AGGREGATE re-run, which mints no fingerprint sample — so it does not
   feed this kind. Keep the registration chain to `notebook-audit` only.
3. **[CANNOT SATISFY without setup] `scope-budget` prerequisite.** No scope is locked; a
   `scope-budget` slot needs a prior `scope-lock` + an integer `max_looks`
   (`registration-kernel.md:627-636`).
4. **[HONESTY] "evidence-brief over the scope tags" is really over the LINEAGE.** This run
   has no scope tags (§0); key `evidence-brief` on `lineage`, and expect empty
   `activity`/`envelopes`.
5. **[HONESTY] The lgbm citable artifact is the JSON envelope, not a CSV.** The declared
   `metrics_table.csv` is absent for this run; cite `causal_tune_tree_lgbm-7905102a.json`
   (`6499d805…`) / `metrics_aggregate.json` (`b57b9f33…`).
