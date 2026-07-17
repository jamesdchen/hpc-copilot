---
status: plan
created: 2026-07-17
---
# Anti-vendor-lockout — the buildable plan

**User ask (standing target, 2026-07-09, "the large one"):** make the stack
fully drivable by NON-Claude harnesses. This is the plan of record. It is scoped
by two sharpening rulings recorded 2026-07-17: **MCP is a PROJECTION, never
load-bearing on trust** (the trust anchor is the typed utterance log + the
journal — `docs/internals/harness-contract.md`, "The CLI is the invariant
substrate"), and the **amplification doctrine** (bare `y` stands; no gate
tightening — `docs/design/human-amplification-blocks.md` §2). Nothing in this
plan may move the trust anchor INTO a harness surface.

The headline finding of the inventory: **the anti-lockout SKELETON is already
built and the capability SURFACE is already portable.** The residual lockout is
narrow and concrete — it lives in ACTIVATION (how a foreign harness installs its
own capability providers), in the UNPROVEN capabilities (2, 3, 4, 5 have no
independent second-harness exercise), and in agent-facing PROSE (SKILL.md is
Claude-idiom-saturated even though the procedure is code-homed). This plan
attacks those three, and — equally important — names the postures the plan must
NOT disturb.

---

## 1. Inventory of the lockout surface (verified against code 2026-07-17)

### What is ALREADY portable (do not re-litigate)

- **(a) MCP-only verbs: NONE.** The MCP catalog is a strict DERIVED SUBSET of
  the CLI registry. A verb is curated iff its Result declares `next_block`
  (`_kernel/extension/mcp_server.py::_declares_next_block`) or it is in the
  hand-listed `_CURATED_EXTRA_VERBS` (recovery/opt-in + the read verbs skills
  name MCP-direct). Nothing is reachable through MCP that is not a CLI verb;
  `scripts/lint_skill_mcp_reachability.py` pins skills to only name
  curated-reachable verbs. Some CLI verbs are deliberately NOT MCP-exposed
  (`export-dossier`, `export-attestations`, `scope-status`) — the opposite of
  lockout: still fully CLI-driveable. **The CLI registry IS the contract; MCP is
  a projection over it, consistent with today's ruling.**

- **(d) Worker/subagent spawning: the `claude -p` worker is DELETED** (§6 worker
  removal). Detached blocks run as harness-neutral `hpc-agent` subprocesses
  (`_kernel/lifecycle/detached.py` — "NO `claude -p` worker"), not a `claude`
  binary. `_wire/spawn_contract.py` survives as the SHARED shape only; its
  `spawn_request` field is always `None`. The #137 OAuth auth blocker is now
  explicitly OUT OF SCOPE of the contract (`harness-contract.md`, "The AUTH
  boundary… anti-vendor-lockout T2"): how a harness authenticates its own
  processes to its model provider is harness-side; core neither checks, stores,
  nor forwards provider credentials.

- **(e) `ANTHROPIC_*` reads in core: NONE.** Grep over `src/hpc_agent/` returns
  zero. Every `claude -p` mention is a docstring describing the removed
  transport. The only Claude-namespaced env reads are benign toggles:
  `CLAUDE_CONFIG_DIR` (probe read path, honored), `CLAUDE_CODE_SESSION_ID`
  (`cli/skill_returns.py`, graceful-absent), `CLAUDE_HPC_VALIDATE_OUTPUTS`
  (`_kernel/contract/schema.py`, opt-in).

- **The procedure is CODE-HOMED, not prose-homed.** `_wire/spawn_contract.py::
  DECISION_POINTS` enumerates the judgement branches of all four workflows,
  each tagged `code` vs `judgement` and bound to a backing primitive; only ~7
  points across all four are genuine LLM judgement. `block-drive` owns the
  sequencing table; the block-gate + authorship/relay gates live on the verbs.
  The SKILL.md prose is a relay-and-translate shim, not the procedure.

- **The conformance kit is SHIPPED** (`conformance-kit.md` status flipped
  2026-07-09; K1–K10 landed). TCK in the wheel
  (`pytest --pyargs hpc_agent.conformance --harness-adapter …`), the negotiation
  verb `ops/harness_capabilities.py` (contract v1.1.0), two reference adapters,
  the `conformance:` CI matrix. `notebook_render.py` is a PROVEN second harness
  for capability 1.

### The residual lockout (what this plan attacks)

**(1) ACTIVATION is Claude-Code-only and not env-overridable — the single
biggest concrete lockout.** `agent_assets.py::install_agent_assets` writes
`~/.claude/settings.json` hooks and the `commands/skills/agents` trees. The
`.claude`-shaped layout and all TEN hook-module needles
(`_UTTERANCE_CAPTURE_NEEDLE`, `_ANSWER_CAPTURE_NEEDLE`, `_RELAY_AUDIT_NEEDLE`,
`_ALERT_COUNT_NEEDLE`, `_STOP_MULTIPLEX_NEEDLE`, the three stop-guards,
`skill_return_autofetch`, `decision_rendezvous_autofetch`,
`scheduler_write_fence`) are HARDCODED. `DEFAULT_CLAUDE_DIR()` is
`Path.home()/".claude"`; the install/WRITE path honors ONLY a `claude_dir`
kwarg, NOT `CLAUDE_CONFIG_DIR` (unlike the probe/READ path in
`ops/harness_capabilities.py::_claude_dir`, which does). **Net: the capabilities
are portable but there is no ACTIVATION story for a foreign harness — no way for
`hpc-agent setup` to materialize a non-Claude harness's capability providers.**
- *What breaks:* a foreign harness has hooks/skills it can never install through
  hpc-agent; it must hand-wire its own provider bindings with no supported seam.
- *Conformant replacement shape:* a harness-PROFILE seam — Claude Code becomes
  ONE profile of `install_agent_assets`; the settings.json/needle layout is that
  profile's asset shape, not the universal one. Detection stays behavioral (no
  self-asserted manifest — the detection-only doctrine holds).
- *Kit coverage:* NONE today (the kit certifies capability BEHAVIOR, never
  install/activation).
- *Size:* L (touches the hottest core file; needs ruling R1).

**(2)/(3) Capabilities 2 (relay-enforcement) and 3 (backgrounding) have NO
independent second-harness exercise — the head of the risk register.** The only
second harness (`notebook_render.py`) proves capability 1 ONLY and is honestly
PARTIAL on 2 & 3. Capabilities 2 & 3 are self-certified in CI by
`adapters/claude_code.py` — but that is OUR hook cores driven in-process, not a
foreign harness. The two SPECIFIED-but-UNBUILT conforming shapes for capability
2 — the **response-gateway** ACT (`verify_relay` pre-delivery) and the **OTel
GenAI** INSPECT (`harness-contract.md`, "Capability 2, split: INSPECT vs ACT") —
have no adapter and no fixture.
- *What breaks:* "declared == detected == behaved" for 2 & 3 has never been
  proven by anything other than Claude Code. The gateway/OTel product claim is
  prose, not a checkable claim.
- *Conformant replacement shape:* reference adapters for the gateway ACT shape
  and an OTel-GenAI INSPECT fixture, plus a foreign-backgrounding adapter
  (non-Claude detach/wake) exercising `test_capability_backgrounding.py`.
- *Kit coverage:* the ASSERTIONS exist (`test_capability_relay.py`,
  `test_capability_backgrounding.py`, outcome-shaped via `EnforcementOutcome`);
  only the foreign ADAPTERS are missing.
- *Size:* M each (needs ruling R3 — build now vs pull-on-demand).

**(4) Agent-facing PROSE is Claude-idiom-saturated with no harness-neutral
projection.** Every workflow SKILL.md (`hpc-submit`, `hpc-status`,
`hpc-aggregate`, `hpc-campaign`, `hpc-notebook-audit`) is MIXED: a
harness-neutral CLI spine (`block-drive`/`append-decision`/`revise-resolved`/…)
wrapped in Claude-Code idioms — "your final action MUST be a tool call" (Claude
Code end-of-turn semantics), `run_in_background`/`wait-detached`,
`AskUserQuestion` free-text authorship, `CronCreate`/`CronDelete`, the
permission-classifier/`&`-compound notes, MCP-first framing. `hpc-notebook-audit`
is the most coupled ("the Claude Code harness IS the audit surface"). The
autonomous skills (`hpc-build-executor`, `hpc-classify-axis`,
`hpc-wrap-entry-point`) are mostly CONTRACT-SHAPED (return a `.hpc/_returns/*`
FILE, not a chat message) with only the emit-skill-return convention as a
Claude-ism. The slash commands (`commands/*.md`) are Claude-Code by definition
(`$ARGUMENTS`, `Skill(...)`) but each maps 1:1 to a portable CLI entry.
- *What breaks:* a foreign harness reading SKILL.md gets Claude-specific
  instructions; there is no PROSE-NEUTRAL runbook projecting the code-homed
  procedure (`DECISION_POINTS`) for it.
- *Conformant replacement shape:* a normative harness-runbook doc projected FROM
  `DECISION_POINTS` + the block catalog (the relay contract stated once,
  harness-neutrally), with the SKILL.md files re-cast as the Claude-Code PROFILE
  of that runbook. NOT a trust surface — a translation aid.
- *Kit coverage:* none (prose is not kit-tested; the DECISION_POINTS registry
  consistency test is the closest pin).
- *Size:* M (docs-heavy; needs ruling R5).

**(c)+(5) Capabilities 4 (trusted display) and 5 (stop-hook append) have no
detection seam and no second implementation; two enforcement behaviors have no
named capability.** `harness_capabilities` reports `trusted_display: "unknown"`
and `stop_hook_append: "unknown"` — honest non-answers, no code-observable
install marker. The elicitation client bit is `"per-session"`/unknown. The MCP
elicitation path is Claude-Code/MCP-client-specific TODAY but — per the
MCP-projection ruling — is correctly NON-LOAD-BEARING: it degrades to the hook
path silently, introduces no sign-off write verb, and leaves the authorship BAR
unchanged (`harness-contract.md`, "MCP elicitation as a second capability-1
channel"). **This is a posture to KEEP, not a lockout to fix.** Separately,
`_kernel/hooks/scheduler_write_fence.py` (conduct rule 7, PreToolUse) and the
`decision_rendezvous_stop_guard` continue-after-commit behavior enforce real
guarantees that map to NO named contract capability — a foreign harness's only
protection is that mutating scheduler verbs live inside block code.
- *Conformant replacement shape:* either define detection seams / new named
  capabilities (6 = scheduler-mutation fence, 7 = commit-then-continue), or
  formally record them as code-backstopped-only.
- *Kit coverage:* none.
- *Size:* S–M (mostly a ruling + doc; R4).

---

## 2. The second-harness proof, and the risk register it defines

**What `notebook_render.py` PROVED conforms:** capability 1 (the utterance log)
end-to-end from a NON-Claude channel. A human types a sign-off into a rendered
`.ipynb` cell (out-of-band from the LLM), `notebook-ingest-signoffs
--write_utterance_log` lands it through the SAME write API
(`state/utterances.py::append_utterance`, same locator, frozen schema,
no-scaffold, provenance filter, fail-open), and the authorship gate cannot tell
which conforming harness produced the utterance. It detects BY BEHAVIOR (D-K3),
declares nothing else, and is honestly reported `partial: utterance-log`. This
is the existence proof that the trust anchor is harness-swappable.

**The risk register = every capability with NO independent (non-Claude-Code)
exercise:**

| Capability | Independent proof today? | Gap |
|---|---|---|
| 1 — utterance log | YES — `notebook_render` | none |
| 2 — relay-enforcement (INSPECT) | NO | OTel-GenAI INSPECT fixture unbuilt |
| 2 — relay-enforcement (ACT) | NO | response-gateway reference adapter unbuilt |
| 3 — backgrounding/wake | NO | only core-constant + our in-process adapter; no foreign detach/wake adapter |
| 4 — trusted display | NO | no detection seam, no second render surface, no kit noun |
| 5 — stop-hook append | NO | no passive seam; env-declared only; no conforming prober |
| (rule 7) scheduler-mutation fence | NO | not a named capability at all |
| (commit-then-continue) rendezvous | NO | folded into relay/backgrounding assumptions; unnamed |

Capability 1 is the only one whose portability is CHECKED by a foreign
implementation. Everything else is portable-BY-SPEC-only. **This table is the
CANONICAL living gap list** (T1): the burn-down waves (C/D) close its rows, and a
new capability with no independent exercise is added here first. That list is the
plan's priority order.

### The memory-note stale-claims reconciliation (T1)

The standing memory note `project_anti_vendor_lockout.md` (2026-07-09, "the large
one") is 7 days stale at plan time. T1 verifies every "planned / unbuilt / missing"
claim it carries against code and records the current truth with a citation. Each
row is the note's framing, the VERDICT, and the `path::symbol` (or verb/test)
proving it — so the note can be trusted-by-reconciliation, not re-archaeologied.

| Memory-note claim (2026-07-09) | Verdict | Code citation proving current truth |
|---|---|---|
| The conformance CERTIFICATION path is "the missing artifact" / kit is skeleton-only | **STALE — SHIPPED** | `src/hpc_agent/conformance/` (adapter protocol, capability tests, `report.py`, reference adapters `claude_code.py` + `notebook_render.py`); `docs/design/conformance-kit.md` status `shipped`, K1–K10 landed; the `conformance:` CI matrix job self-certifies both adapters. The status FLIP itself lagged the landing — the drift T1 is chartered to catch (recorded in that doc's header + drift log). |
| The `claude -p` worker is a live component; #137 OAuth auth blocker is in-scope | **STALE — worker DELETED, auth OUT OF SCOPE** | `_kernel/lifecycle/detached.py` runs detached blocks as harness-neutral `hpc-agent` subprocesses ("NO `claude -p` worker"); `_wire/spawn_contract.py::spawn_request` survives as the shared shape only, always `None`. The auth boundary is contract-OUT-OF-SCOPE: `docs/internals/harness-contract.md`, "The AUTH boundary … anti-vendor-lockout T2". |
| MCP assumes / requires a Claude client (an MCP-only lockout) | **STALE — MCP is a strict derived subset, non-load-bearing** | `_kernel/extension/mcp_server.py::_declares_next_block` curates the MCP catalog as a SUBSET of the CLI registry; `scripts/lint_skill_mcp_reachability.py` pins skills to curated-reachable verbs only. Some CLI verbs are deliberately NOT MCP-exposed — the opposite of lockout. MCP-projection ruling (2026-07-17): non-load-bearing on trust. |
| `ANTHROPIC_*` / Claude-namespaced env reads are load-bearing in core | **STALE — none load-bearing** | Grep over `src/hpc_agent/` returns zero `ANTHROPIC_*` reads; every `claude -p` mention is a docstring for the removed transport. The only Claude-namespaced reads are benign toggles: `CLAUDE_CONFIG_DIR` (probe read path), `CLAUDE_CODE_SESSION_ID` (`cli/skill_returns.py`, graceful-absent), `CLAUDE_HPC_VALIDATE_OUTPUTS` (`_kernel/contract/schema.py`, opt-in). |
| The two enforcement guarantees (scheduler fence, rendezvous continue) map to a named capability | **CONFIRMED-UNNAMED at plan time → RULED promoted 2026-07-17 (R4)** | `_kernel/hooks/scheduler_write_fence.py` (rule 7 fence) and `_kernel/hooks/decision_rendezvous_stop_guard.py` (commit-then-continue) enforce real guarantees with NO named contract capability. T2 records them as capabilities 6/7 (`docs/internals/harness-contract.md`), code-backstopped-only. |

The reconciled verdicts feed forward: the SHIPPED-kit and DELETED-worker rows
retire the note's "unbuilt certification path" and "auth-assumption" framings; the
risk register above (not the note) is now the live gap surface.

---

## 3. Build-unit waves (file-disjoint where possible)

House style follows `conformance-kit.md`: settled decisions, file-disjoint
Opus-sized units, enforcement rows, drift log. Cite `path::symbol`, not line
numbers. **Every unit that touches a trust seam carries the doctrine guardrail
in §4.**

### Wave A — inventory-verification + honest doc corrections (cheap, parallel; catches drift)

- **T1 — status/claim reconciliation.** Verify every "planned/unbuilt" claim in
  the 2026-07-09 memory note against code and correct it (the conformance kit is
  SHIPPED, the worker is DELETED, MCP has no lockout). Land the risk-register
  table (§2) as a living doc. Files: THIS doc + `docs/design/conformance-kit.md`
  reservation note. Size: S.
- **T2 — the two unnamed enforcement behaviors, recorded.** Document
  `scheduler_write_fence` (rule 7) and the rendezvous commit-then-continue as
  EITHER candidate capabilities 6/7 OR explicitly code-backstopped-only. Docs
  only; gated on ruling R4. Files: `docs/internals/harness-contract.md` (new
  section). Size: S.
- **T3 — capability 4/5 seam audit.** Decide, per-capability, keep-"unknown" vs
  define-a-probe; record. Preserve the elicitation-non-load-bearing posture
  verbatim (guardrail G3). Docs only. Files: `docs/internals/harness-contract.md`.
  Size: S.

### Wave B — the ACTIVATION seam (the biggest lockout; sequential — hot file)

- **T4 — decouple the install/write path from `.claude`.** Make
  `install_agent_assets` honor an explicit harness-config target uniformly (at
  minimum honor `CLAUDE_CONFIG_DIR` in the WRITE path as the probe path already
  does; ideally introduce a harness-PROFILE seam where the settings.json+needle
  layout is Claude Code's PROFILE, not the universal shape). Files:
  `agent_assets.py`, `cli/setup.py`. Conformance owed: an activation/behavior
  test that a profile installs providers a foreign detection-by-behavior probe
  then sees. Gated on ruling R1. Size: L.
- **T5 — a prose-neutral harness RUNBOOK.** A normative doc projecting the
  code-homed procedure (`_wire/spawn_contract.py::DECISION_POINTS` + the block
  catalog + the relay contract) harness-neutrally, so a foreign harness drives
  the workflows without reading Claude-Code SKILL.md. Re-cast SKILL.md as the
  Claude-Code PROFILE that points at it. Files: new
  `docs/internals/harness-runbook.md` + light SKILL.md front-matter edits
  (disjoint from T4). Gated on R5. Size: M.

### Wave C — burn down the risk register (parallel; new adapter files, disjoint)

- **T6 — response-gateway reference adapter (capability 2 / ACT).** A
  `conformance/adapters/response_gateway.py` implementing `run_enforcement_point`
  as a pre-delivery `verify_relay` pass (no hooks), certified by the EXISTING
  `test_capability_relay.py` triples (outcome-shaped seam already supports it).
  Files: new adapter + CI matrix row. Gated on R3. Size: M.
- **T7 — OTel-GenAI INSPECT fixture (capability 2 / INSPECT).** A fixture proving
  the final agent-visible message is readable from GenAI-conformant telemetry,
  feeding the same ACT assertions. Files: new fixture under
  `conformance/fixtures/`. Gated on R3. Size: M.
- **T8 — foreign-backgrounding adapter (capability 3).** A non-Claude detach/wake
  adapter exercising `test_capability_backgrounding.py` against the journal
  rendezvous, proving backgrounding is not Claude-Code-shaped. Files: new
  adapter + CI row. Size: M.

### Wave D — capability 4/5 second implementations (deferred; needs R4 outcomes)

- **T9 — trusted-display detection seam + a second render surface**, IF R4 says
  name it. Files: `ops/harness_capabilities.py` + kit noun. Size: M.
- **T10 — stop-hook-append conformance prober** (the D1 two-shape probe) so a
  foreign harness can activate capability 5 by behavior, not just env markers.
  Size: M.

**Regen tails:** any new `@primitive` (none currently planned — the activation
seam reuses `install-commands`) → `scripts/bake_operations_json.py --write` +
schema regen + registry-count pins. New CI matrix rows for T6/T8 adapters.

---

## 4. Doctrine guardrails (the plan may NOT cross these)

- **G1 — the trust anchor never moves into a harness surface.** The journal +
  the typed utterance log are fixed. No unit may add a CLI/MCP/skill affordance
  that writes an utterance (`harness-contract.md` §2, "The LLM must never gain a
  sanctioned write call"; pinned by `tests/contracts/`).
- **G2 — detection-only, never self-assertion.** A harness PROFILE (T4) is still
  DETECTED BY BEHAVIOR; installing a profile grants NO trust — the gate reads the
  DETECTED seam, the kit proves by behavior. A `capabilities:` manifest a harness
  writes about itself is the guard-the-LLM-satisfies failure one level up.
- **G3 — elicitation stays NON-LOAD-BEARING.** Keep the degrade-to-hook path, the
  no-sign-off-verb rule, and the unchanged authorship BAR. Per today's ruling,
  MCP is a projection; nothing here may make a client-render capability a trust
  precondition.
- **G4 — the CLI stays the invariant substrate.** Every portability unit adds
  capability providers AROUND the CLI; none forks or replaces the verb surface.
- **G5 — amplification doctrine.** Bare `y` stands; no unit tightens a gate or
  adds friction to buy portability. Skips stay honest (partial, named tier —
  never rounded up to conforming).

## 5. User rulings needed

- **R1 (activation scope).** Does `hpc-agent setup` grow a harness-PROFILE
  registry (Claude Code = one profile; a foreign harness ships its own asset
  profile), or do we only make the existing install env-overridable and leave
  foreign activation to the harness? Blocks T4.
- **R2 (negotiation version-gating — the task's explicit question). RULED
  2026-07-17: NO — report-only/additive-only within major 1.** Gating would
  move trust into a self-declared version string, the exact "trust into a
  harness surface" move the doctrine forbids; consistent with G2/G5 and the
  MCP-is-projection ruling. (Ruled as the doctrine-forced option under the
  standing authorization; recorded here per "record the decision either way.")
- **R3 (risk-register burn-down timing).** Build the gateway/OTel/foreign-bg
  adapters NOW (Wave C), or reserve them and pull when a real foreign harness
  appears? Trade: proving the claim vs YAGNI.
- **R4 (unnamed enforcement). RULED 2026-07-17: promote to NAMED capabilities
  6/7** — recording already-enforced behavior is docs-only bookkeeping with no
  downside, and an unnamed capability cannot acquire a second-harness proof.
  Unblocks T2/T9/T10 (the T2 docs work itself remains queued with Wave A).
- **R5 (prose-neutral runbook).** Is the harness runbook a GENERATED projection
  from `DECISION_POINTS`, and does a foreign harness consume it or the
  machine-readable contract only? Blocks T5.

---

## Drift log

- **Created 2026-07-17 (plan draft).** Sourced from the standing memory note
  `project_anti_vendor_lockout.md` (2026-07-09, "the large one") and sharpened by
  the 2026-07-17 MCP-projection ruling (`harness-contract.md`, "The CLI is the
  invariant substrate") + the amplification doctrine
  (`human-amplification-blocks.md` §2). Inventory verified against code at
  `main`; two parallel Explore sweeps grounded §1(b)/(e).
- **Memory-note corrections banked at plan time** (the note is 7 days stale):
  (1) the conformance CERTIFICATION path the note calls "the missing artifact"
  is SHIPPED (`conformance/`, K1–K10, CI matrix live) — its own drift log records
  a status-flip lag the anti-vendor-lockout inventory (unit T1 here) is chartered
  to catch; (2) the `claude -p` worker (#137) is DELETED, so the auth-assumption
  class the note flags persists only in harness-side spawning, now OUT OF SCOPE
  by contract (T2 boundary); (3) MCP is confirmed a strict derived subset of the
  CLI registry — no MCP-only lockout, contra any "MCP elicitation assumes a
  Claude client" reading: that path is non-load-bearing by ruling.
- **Scope narrowed at draft time:** the plan does NOT chase elicitation
  portability (guardrail G3 keeps it non-load-bearing) and does NOT chase the
  auth blocker (contract T2 out-of-scope). The real residual is activation (T4),
  the unproven capabilities 2–5 (Wave C/D), and prose (T5).
- **T5 landed (2026-07-17): the prose-neutral harness runbook, GENERATED.** R5
  answered "generated projection": `docs/generated/harness-runbook.md` is
  projected from `_wire/spawn_contract.py::DECISION_POINTS` + the
  `infra/block_chain` sequence (`ORDER`) / consent (`GATED_BLOCKS`) tables by
  `scripts/build_harness_runbook.py` (`--check`/`--write` per house convention,
  bare refused rc 2, standard GENERATED banner). Per workflow it renders the
  block sequence (consent boundaries flagged inline), the park → typed `y` →
  `append-decision` consent protocol (stated once + per-workflow), and the
  decision-point table (id · shape · code-vs-judgement · backing verb) — in
  CLI-verb vocabulary with NO Claude idioms. Wired into `scripts/regen_all.py`
  `_STEPS` + `REGEN_SCRIPTS` as step 8 (before `check_no_pending_primitive_docs`,
  alongside `build_principles_index`); the pipeline is now 9 steps
  (`test_doc_frozen_counts` reads the length via AST — no literal to bump). Tests
  (`tests/scripts/test_build_harness_runbook.py`): `--check`/`--write` round-trip
  fires on a hand-edit; a COMPLETENESS pin (every `DECISION_POINTS` workflow +
  every decision-point id/verb projected; a new workflow without regen drifts);
  and a DENYLIST pin over `CLAUDE_IDIOM_DENYLIST` (`run_in_background`,
  `AskUserQuestion`, `CronCreate`/`CronDelete`, `tool call`, `final action`,
  `Skill(`) asserting the render carries none. A pointer added to
  `docs/internals/harness-contract.md` ("The CLI is the invariant substrate"):
  edit `DECISION_POINTS`, never the runbook prose; it is a translation aid, not a
  trust surface. Guardrails held: G1/G4 (no new write affordance; the CLI stays
  the substrate — the runbook is a read-only projection), G5 (bare `y` stands —
  stated verbatim in the protocol). Gates green: `regen_all --check` (9/9), lint
  gauntlet (26/26), ruff/format/mypy, 9 runbook tests. The SKILL.md re-cast as the
  Claude-Code PROFILE (light front-matter edits) is NOT in this unit — deferred.
- **Wave A landed (2026-07-17): T1 + T2 + T3, docs-only.** T1 — the memory-note
  stale-claims reconciliation table (§2) verifies every "planned/unbuilt/missing"
  claim against code with a citation, and the risk register is marked the CANONICAL
  living gap list. T2 — capabilities 6 (scheduler-write fence) and 7
  (decision-rendezvous commit-then-continue) named in
  `docs/internals/harness-contract.md` under the R4 ruling, recorded
  code-backstopped-only (no negotiation seam, no kit assertion; declared == detected
  == behaved UNCLOSED for both; the seam-wiring follow-on OWNS the MINOR
  contract-version bump, so the version stays 1.1.0). T3 — the capability 4/5
  detection-seam audit: both KEEP `"unknown"` in Wave A, missing seam + Wave-D
  follow-on (T9/T10) recorded per capability, G3 restated. No code touched; the
  enforcing hooks predate the record. A reservation note is added to
  `docs/design/conformance-kit.md`'s drift log pointing here as the living gap
  list.
