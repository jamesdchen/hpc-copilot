---
status: plan
---
# Anti-vendor-lockout — driving the stack from a non-Claude harness

**Status: PLANNED (2026-07-09), not yet implemented.** This is the durable
hand-off (the `docs/design/notebook-audit.md` / `conformance-kit.md` pattern):
settled decisions with recorded rationale, file-disjoint Opus-sized task units,
enforcement-row candidates, and boundary-drift flags. Cite `path::symbol`, never
line numbers. Record implementation drift in the drift log at the foot of this
document when the units land.

This plan builds out the vendor-lock-in defense whose **normative skeleton is
already in tree** (`docs/internals/harness-contract.md`): the three-capability
contract, the sha canonicalization, the utterance write API, LSP-style capability
negotiation (`ops/harness_capabilities.py`), the INSPECT/ACT split, and the
**conformance kit — which has LANDED since the last planning note** (see the
inventory reconciliation below). The skeleton proves the contract is
*implementable* by a second harness; this plan closes the gap to *a stranger's
harness can actually be driven end-to-end, and certifies itself doing so.*

## Product intent

The claim the stack sells (`harness-contract.md`, "The CLI is the invariant
substrate"): **trust flows through ARTIFACTS, never membership.** Every trusted
thing — an approval, a sign-off, a receipt, a conclusion — is an attestation over
a journal a *conforming harness* fed, and the harness is swappable. Claude Code is
ONE conforming implementation (hooks over `~/.claude/settings.json`); the notebook
render is a SECOND (partial). The product is not "we are a Claude Code tool"; it
is "the audit loop is defined against three capabilities, and implementations
compete under a published, executable contract."

The honest state today (verified 2026-07-09, code not prose): the contract is
published and versioned (`HARNESS_CONTRACT_VERSION = "1.0.0"`), the kit exists and
runs in CI against two OUR-OWN adapters, and negotiation is real. What is NOT yet
true, and what this plan makes true:

1. **No non-Claude harness has ever been driven end-to-end.** Both certified
   adapters are ours; every INSPECT path in tree parses Claude Code's transcript
   format; the only working capability-1 write channel in practice is the Claude
   Code hook. The kit CERTIFIES a harness — nothing yet DRIVES one that isn't
   Claude Code.
2. **The portable rides named in the contract are named, not built.** OTel GenAI
   INSPECT, the response-gateway ACT shape, and a second MCP elicitation client
   are all specified as conforming implementations and have zero reference code.
3. **The install surface is monolithically Claude-shaped.** `agent_assets.py`
   wires eight distinct `~/.claude` hook entries, `.claude.json` MCP registration,
   and `permissions.allow` rules — with no harness-neutral seam a second harness
   installs its capability providers through.

## Inventory reconciliation (verified against code 2026-07-09)

The known lockout inventory MOVED substantially since the memory note. Verified
per item, citing the seam:

### Item 1 — worker-auth class (#137): SHRUNK to residue

The `claude -p --bare` §6 worker spawn transport was **DELETED**
(`_kernel/lifecycle/drive.py` docstrings, `detached.py`: "DETACHED `hpc-agent`
subprocess, never a `claude -p` worker"; `_wire/spawn_contract.py` documents it as
the removed transport). The detached path is now a plain `hpc-agent` subprocess —
**no live spawn site strands an OAuth worker anymore.** The class persists only as:

- **Dead scaffolding:** `ops/memory/interview.py::_maybe_write_claude_permissions`
  still writes `<campaign_dir>/.claude/settings.json` with `Bash(hpc-agent:*)`, its
  prose still citing "the spawned `claude -p --bare` worker" that no longer spawns.
- **A documented user burden:** the wheel README / PKG-INFO tell OAuth users to
  `export ANTHROPIC_API_KEY` — the auth assumption is now a documented human step,
  not a code path. This is legitimately harness-side (see "What stays
  Claude-specific"), but it is UNSTATED in the contract.

### Item 2 — MCP elicitation: the declared-but-dark leg SHIPPED

The degradation leg the note predicted has landed:
`_kernel/extension/mcp_server.py::_client_elicitation` is detected per-session at
`initialize` from the client's declared `capabilities.elicitation`, and
`_client_elicitation_dark` marks the channel dark for the rest of a session when a
prior elicitation timed out / returned nothing (run #11), degrading every later
authorship refusal to the hook path silently and honestly. The server bit is real
(`ELICITATION_SERVER_IMPLEMENTED = True`). **Still true:** NO second client proves
elicitation — the working capability-1 channel in every real session is the Claude
Code `UserPromptSubmit` hook. Elicitation is a specified, server-ready SECOND
channel with no certified client.

### Item 3 — conformance certification path: BUILT (the note is stale)

`src/hpc_agent/conformance/` is fully landed: `adapter.py` (the Protocol),
`conftest.py` (+`_loader.py`, `--harness-adapter`), `reference_adapter.py`,
`fixture_repo.py`, `report.py` (`CONTRACT_VERSION`, `conforming`/`partial`
verdicts), and BOTH reference adapters — `adapters/claude_code.py` (fully
conforming) and `adapters/notebook_render.py` (honestly partial). The capability
tests (`test_capability_{utterance_log,relay,backgrounding}.py`,
`test_negotiation.py`, `test_canonicalization.py`, `test_attestation_export.py`)
ship in the wheel. `ops/export_attestations.py` (in-toto/DSSE) landed. The
**self-conformance CI job exists** (`.github/workflows/ci.yml`, `conformance:` job,
matrix `adapter: [claude-code, notebook-render]`, in-toto installed, plugins-style
isolated). Registry is **162**, not the note's 159.

**What remains open for item 3:** the kit is a certification MECHANISM with no
EXTERNAL certified harness and no published "certify your harness" on-ramp. And
`docs/design/conformance-kit.md` still carries a `Status: PLANNED` header though
its waves K1–K10 have effectively landed — a stale-doc defect this plan corrects.

### Item 4 — relay/Stop conduct: split specified, INSPECT still Claude-only

Capability 2 is split INSPECT/ACT in the contract with named conforming shapes
(hooks + response gateway for ACT; OTel GenAI for INSPECT). The reference adapter
routes through the REAL hook cores (`adapters/claude_code.py` drives
`_kernel/hooks/relay_audit_stop.py::build_hook_output` in-process). **But the only
INSPECT implementation in tree is Claude Code transcript parsing:**
`relay_audit_stop.py::final_assistant_text` / `_prior_assistant_texts` parse
Claude Code's JSONL transcript schema directly (`type == "assistant"`, message
content blocks). The conduct rules the hooks enforce have no harness-neutral
statement + per-harness shim map. `telemetry.py` emits hpc-agent's OWN block
events to OTel (optional `[otel]` extra) but NOT GenAI semantic-convention spans
over the final agent message — the INSPECT ride is unbuilt.

### Beyond-inventory lockout points found (grep sweep of src)

- **The install surface (`agent_assets.py`) is the single largest coupling.** It
  wires eight Claude-Code hook entries (skill-return autofetch + Stop guard,
  rendezvous autofetch + Stop guard, scheduler write-fence, alert-count
  SessionStart, utterance capture, answer capture, relay-audit Stop), the
  `.claude.json` MCP server registration, and `Skill(...)` / `Bash(hpc-agent:*)`
  `permissions.allow` grants — all shaped as Claude Code settings, with **no
  abstraction a second harness installs its providers through.** Each hook command
  is a `bash -c` string invoking `python -m hpc_agent._kernel.hooks.X`
  (`_hook_python`, `_build_*_command`) — a Claude-Code hook-command encoding.
- **`~/.claude` path assumption** (`agent_assets.DEFAULT_CLAUDE_DIR`,
  `harness_capabilities._claude_dir` honoring `CLAUDE_CONFIG_DIR`) threads the
  Claude config location through detection and install.
- **Skills/slashes are Claude-Code-shaped prose** (`slash_commands/**` — `.md`
  SKILL + slash files). Capability negotiation certifies the audit LOOP but
  nothing maps a skill's human-facing flow onto a non-Claude harness's affordance
  vocabulary.
- **Detection is by Claude-hook NEEDLE for our own harness**
  (`harness_capabilities._needle_installed` over `agent_assets` module-path
  needles). This is correct for Claude Code but is the seam a non-Claude harness
  is detected-BY-BEHAVIOR instead (conformance-kit D-K3) — the asymmetry is
  undocumented in the contract.

## The design center (proposed — flagged decisions marked ⚑ for the user)

The skeleton's philosophy holds verbatim and is NOT re-litigated: detection over
declaration, a guard the LLM satisfies is not a guard, honest named degradation,
one canonicalization, the CLI as invariant substrate. This plan adds **reference
implementations of the already-specified conforming shapes** and **one new
abstraction** (the capability-provider install seam), plus doc closure. It does
NOT change the contract's normative bullet lists except additively.

- **AVL-A — INSPECT gets a portable reference provider.** The Claude transcript
  reader is refactored behind an INSPECT seam; an OTel-GenAI reader becomes a
  second provider. The final-message text is the seam's output; the audit is
  unchanged.
- **AVL-B — ACT gets its second reference shape.** A response-gateway reference
  implementation applies `verify_relay` pre-delivery — the named-but-unbuilt ACT
  shape, runnable in front of any model with no harness hooks.
- **AVL-C — elicitation gets a second client proof.** A non-Claude MCP client
  reference drives capability-1-via-elicitation end-to-end, closing "no second
  client proves it," riding the existing `tests/_mcp_harness.py::FakeMcpClient`.
- **AVL-D — the install surface gets a harness-neutral provider seam.** A
  `CapabilityProviderInstaller` protocol; the Claude `~/.claude` writer becomes ONE
  implementation of it. ⚑ (see judgment calls — this is the largest unit and the
  one most arguably harness-side.)
- **AVL-E — worker-auth residue is excised and the auth boundary is stated.**
- **AVL-F — the conduct rules get a harness-neutral statement + per-harness shim
  map, and the certification on-ramp is published.**

## Task units (file-disjoint, Opus-sized, phased with dependencies)

Standing rules inherited from the slate: regen commits strictly serial; every
`@primitive` change runs all six regen scripts (`[[dev-regen-list]]`);
enforcement-map edits append-only; every unit lands with a fires+passes test pair;
each wave ends regen → full suite → commit. `[∥]` = parallelizable within its
phase; `[seq]` = serialized on a hot/shared file.

### Phase 0 — truth reconciliation (do FIRST; unblocks honest planning)

- **T1 [∥]** — Flip `docs/design/conformance-kit.md` `Status:` to IMPLEMENTED and
  add its drift log entry (K1–K10 landed: both adapters, the CI job,
  `export_attestations`, `report.py`, `HARNESS_CONTRACT_VERSION`). Record the ONE
  genuinely-open kit item: no external certified harness, no published on-ramp
  (handed to T8). Files: `docs/design/conformance-kit.md` only. Seat: none (doc);
  a `tests/contracts/` doc-status pin is OUT of scope. No second-harness proof.
- **T2 [∥]** — Contract addendum: document the **detection asymmetry** (our
  harness detected by hook needle; a foreign harness detected-BY-BEHAVIOR, the
  D-K3 rule) and the **auth boundary** (the `ANTHROPIC_API_KEY` requirement is a
  harness-side concern, OUT OF SCOPE of the contract exactly as disabling a capture
  hook is — the honest-trust-limit class). Files:
  `docs/internals/harness-contract.md` (additive prose under "The honest trust
  limit" and "Capability negotiation"). Seat: the existing
  `tests/contracts/test_harness_contract.py` doc-prose pins extend by one line each.

### Phase 1 — INSPECT/ACT reference implementations (parallel; new files)

- **T3 [∥]** — **INSPECT seam + OTel-GenAI provider.** Extract the final-message
  read behind a provider protocol so `relay_audit_stop.py::final_assistant_text`
  (Claude transcript JSONL) is ONE provider and a GenAI-conventions reader is a
  SECOND. New file `ops/relay/inspect_provider.py`
  (`FinalMessageProvider` Protocol + a `TranscriptProvider` delegating to the
  existing parser + a `GenAiSpanProvider` reading the standardized `gen_ai`
  output events). The Stop hook keeps calling the transcript provider; the seam is
  what a non-hook harness targets. **Test/enforcement seat:**
  `tests/ops/test_inspect_provider.py` — a GenAI-shaped fixture yields the same
  final text as a transcript fixture for the same logical reply.
  **Second-harness proof:** wire the conformance relay module's INSPECT leg
  (`conformance/test_capability_relay.py` already hands the adapter the final text
  outcome-shaped) to exercise BOTH providers behind one adapter, proving INSPECT is
  provider-agnostic. ⚑ (GenAI event schema stability — flagged.)
- **T4 [∥]** — **Response-gateway ACT reference.** A thin proxy reference that
  applies `ops/decision/verify_relay.py::verify_relay` to an outgoing message
  BEFORE delivery, holding back / amending a contradicted relay — the named ACT
  shape that needs no harness hooks. New file
  `examples/gateways/relay_response_gateway/` (an EXAMPLE, not core — the plugins
  lane precedent: normative reference code that ships unpublished). **Seat:** its
  own `tests/` proving the outcome-shaped `EnforcementOutcome` (blocked/reason)
  matches the hook core on the SAME relay fixtures (`conformance/relay_fixtures.py`
  — reuse, do not fork). **Second-harness proof:** a new conformance adapter
  `conformance/adapters/response_gateway.py` whose `run_enforcement_point` drives
  the gateway, certified against the existing `test_capability_relay.py` triples —
  proving the two ACT shapes certify through one seam.

### Phase 2 — elicitation second-client proof (after Phase 0; independent)

- **T5 [∥]** — **A non-Claude elicitation client reference** driving
  capability-1-via-elicitation end-to-end with no Claude Code in the loop, riding
  the existing duplex rig `tests/_mcp_harness.py::FakeMcpClient`. It declares
  `capabilities.elicitation` at `initialize`, receives the CODE-RENDERED
  `elicitation/create` prompt, returns typed free-text, and proves the server
  `append_utterance`s harness-side and the authorship gate grants at tier 1 — the
  consumer-defined pass, but through elicitation instead of the hook. New file
  `examples/clients/elicitation_reference/` + a conformance leg. **Seat:**
  `conformance/test_capability_utterance_log.py` gains an elicitation-channel
  assertion (additive minor under D-K6). **Second-harness proof:** IS the proof —
  it certifies capability 1 with zero Claude Code, the elicitation analogue of the
  notebook-render adapter. ⚑ (whether this belongs in the kit vs examples —
  flagged.)

### Phase 3 — the install-surface abstraction (largest; serialized on hot files)

- **T6 [seq]** — **`CapabilityProviderInstaller` protocol + Claude reference.**
  Define a harness-neutral install protocol (each conforming harness installs its
  capability-1/2/3 providers into ITS config), and refactor the existing
  `agent_assets.py` `~/.claude` writer to be the ONE reference implementation
  behind it. The eight hook entries, MCP registration, and permission grants become
  the Claude installer's `install_providers()` body — byte-identical output, no
  behavior change; the point is the SEAM, not a rewrite. New file
  `ops/install/provider_installer.py` (the Protocol + a `ClaudeCodeInstaller`
  wrapping the current functions). **Seat:**
  `tests/contracts/test_provider_installer_boundary.py` — the Claude installer is
  ONE implementation of the Protocol; `install_agent_assets` output is unchanged
  (golden-equal to today). **Second-harness proof:** a stub `NullInstaller` in the
  test proving a second harness can satisfy the Protocol without touching
  `~/.claude`. ⚑ (Is this abstraction worth the churn, or is install legitimately
  harness-side? — the single biggest judgment call; see below.)

### Phase 4 — conduct statement + certification on-ramp (docs + one guide)

- **T7 [∥]** — **Harness-neutral CONDUCT statement + per-harness shim map.** A doc
  stating the conduct rules the hooks ENFORCE (relay verbatim, sign-off authorship,
  no-scaffold, scheduler write-fence) independent of Claude Code, with a table
  binding each rule to its Claude Code hook AND its conforming alternative
  (gateway/GenAI/elicitation/second-harness). New file
  `docs/internals/conduct-rules.md`; cross-link from `harness-contract.md`. Seat:
  none (doc). No code.
- **T8 [∥]** — **Publish the certification on-ramp.** A "certify your harness"
  guide: the `pytest --pyargs hpc_agent.conformance --harness-adapter …` invocation,
  the adapter Protocol walkthrough, the `conforming` vs `partial` verdict meaning,
  and the two-shapes-of-ACT / two-channels-of-cap-1 options. New file
  `docs/guides/certify-your-harness.md` + README pointer. Seat: none (doc). This is
  the external-consumer artifact item 3 lacked.

### Phase 5 — worker-auth residue (independent; small)

- **T9 [∥]** — Excise or relabel the dead `claude -p --bare` scaffolding.
  `ops/memory/interview.py::_maybe_write_claude_permissions` + its prose: ⚑ EITHER
  remove (the worker is gone) OR generalize to a harness-neutral "grant the
  hpc-agent CLI in this dir" with the `claude -p` prose deleted. Files:
  `ops/memory/interview.py` (+ its test). Seat: the existing interview test asserts
  whichever decision lands. Flag: removing changes an artifacts-list output; keeping
  retains a Claude-Code project-settings write for "any claude launched here."

## Dependencies

```
T1, T2  (Phase 0)  ── do first
T3, T4  (Phase 1)  ── independent of Phase 0
T5      (Phase 2)  ── needs T2's contract addendum for the asymmetry note
T6      (Phase 3)  ── independent, serialized on agent_assets.py
T7, T8  (Phase 4)  ── T8 needs T1 (kit status truthful); T7 needs T2
T9      (Phase 5)  ── independent
```

No two units share a file. T3/T4 both touch conformance ADAPTERS but in disjoint
new files (`response_gateway.py` vs the INSPECT wiring, which lives in
`test_capability_relay.py` — coordinate if concurrent, else serialize those two).

## Non-goals (recorded)

- **NOT a second harness we ship and maintain.** The deliverables are reference
  PROVIDERS and PROOFS (examples-lane + conformance adapters), not a supported
  non-Claude product harness. Trust flows through artifacts; we prove the seam is
  real, we do not become a harness vendor.
- **NOT a plugin/SDK abstraction over Claude Code's hook API.** T6 abstracts our
  INSTALL surface, not Claude Code. We do not wrap or re-implement any harness's
  native hook/tool mechanism.
- **NOT changing the canonicalization, the record schema, or any capability's
  normative bullet list** except additively (D-K6 deprecation posture). The sha
  canon is the canonical v2 trigger and is untouched.
- **NOT a self-asserted capability manifest anywhere.** Every provider is DETECTED
  or proven-by-behavior; no unit introduces a `capabilities:` config a harness
  writes about itself (the guard-the-LLM-satisfies failure, one level up —
  conformance-kit Q1).
- **NOT removing Claude Code's status as the fully-conforming reference.** Claude
  Code stays first among conforming harnesses; this plan makes it ONE of several,
  not the only one.
- **NOT a browse/dashboard/registry of harnesses.** The kit's verdict is the
  artifact; there is no "harness marketplace."

## What stays Claude-specific, and why (be honest)

Some bindings are legitimately harness-side. Naming them is the point — a
contract that pretends everything is portable is dishonest:

- **The `~/.claude` config location, `.claude.json`, the hook-command bash
  encoding, and `Skill(...)`/`Bash(...)` permission grammar** are Claude Code's
  native config. A second harness has its OWN install target. T6 abstracts the
  SEAM (so a second harness has somewhere to plug in), but the Claude installer's
  body stays Claude-shaped by definition — that is the reference implementation, not
  a leak.
- **The `_kernel/hooks/*` modules** ARE the Claude Code capability-provider
  implementation. They are one conforming harness's code; they do not become
  portable, and should not. The portability lives in the CLI substrate they wrap
  and the seams (INSPECT/ACT/install) they satisfy.
- **Transcript JSONL parsing** (`relay_audit_stop.final_assistant_text`) is
  legitimately Claude-side once it sits behind the T3 INSPECT seam: it is one
  provider's private business, exactly as a GenAI reader's span parsing is its own.
- **The `ANTHROPIC_API_KEY` requirement for any spawned `claude`** is a harness /
  environment concern, the same class as disabling a capture hook — OUT OF SCOPE of
  the contract (T2 states this explicitly rather than leaving it implied). Core
  spawns no model; whatever harness drives the CLI owns its own auth.
- **Skills/slashes as `.md` prose** stay Claude-Code-shaped assets. The
  harness-neutral artifact is the CLI verb each skill projects (the block-drive
  doctrine); a second harness reaches the same verbs without the `.md` files. T7's
  conduct statement, not a skill rewrite, is the portable layer.

## Enforcement rows (accrue to `docs/internals/engineering-principles.md`)

| Rule | Enforced by | Fires when |
|---|---|---|
| INSPECT is provider-agnostic: the final-message read routes through the `FinalMessageProvider` seam; no audit path hard-codes transcript parsing outside the transcript provider | T3 `tests/ops/test_inspect_provider.py` + a route-through pin | a new audit surface parses `type == "assistant"` transcript JSONL directly instead of calling the seam |
| The two ACT shapes certify through ONE outcome-shaped seam; no kit test inspects HOW a harness blocked | T4 gateway adapter + the existing `EnforcementOutcome` seam (conformance-kit Q1, extended) | a relay test asserts hook JSON shapes or gateway internals |
| Both capability-1 channels (hook, elicitation) satisfy the SAME write API and gate at the SAME tier | T5 elicitation-client conformance leg | the elicitation path lands a record the reader rejects, or gates at a different tier than the hook |
| The Claude installer is ONE implementation of `CapabilityProviderInstaller`; `install_agent_assets` output is unchanged by the refactor | T6 `tests/contracts/test_provider_installer_boundary.py` (golden-equal + Protocol-conformance) | the refactor changes installed settings bytes, or a second install path bypasses the Protocol |
| No self-asserted capability manifest is introduced by any provider/installer | T6 boundary test + the conformance-kit Q1 detection-only pin | an installer or adapter writes a `capabilities:` file about itself instead of being detected/behaved |
| The kit's status doc stays truthful | T1 (manual) + the existing `test_harness_contract.py` version pin | `conformance-kit.md` claims PLANNED while the CI job runs its waves |
| The auth boundary and detection asymmetry are stated, not implied | T2 `test_harness_contract.py` doc-prose pins | the contract omits the harness-side auth line or the detected-by-behavior rule |

## Boundary-drift flags (the Q1 watch list — written before implementation)

- **The kit never weakens a filter or an outcome to admit a new provider.** A
  GenAI INSPECT reader, a gateway, or an elicitation client that cannot satisfy the
  EXISTING fixtures is NON-CONFORMING — the fixtures are normative; pressure to
  soften them to make a reference pass is the feature working (conformance-kit Q1,
  verbatim).
- **Abstracting the install seam must not leak a self-declaration.** The moment an
  installer writes a manifest a harness reads back to claim a capability, detection
  is defeated one level up. Providers are DETECTED (needle for Claude, behavior for
  a foreign harness), never self-asserted.
- **The reference gateway/client/GenAI-reader stay in the examples/conformance
  lanes, offline, no network, no token.** A reference that needs a live harness or a
  socket has crossed the lane (the plugins/conformance-job isolation rule).
- **"What stays Claude-specific" is a boundary, not a TODO.** The named
  harness-side bindings are not lockout to be eliminated; a future contributor
  "portablizing" the `_kernel/hooks/*` modules or the `~/.claude` writer body has
  misread the boundary — the portability is the SEAM, not the reference body.
- **No unit changes the normative bullet lists non-additively.** Every capability's
  MUST list, the frozen record schema, and the canonicalization are touched only
  under the D-K6 additive-minor posture; a change that fails a previously-conforming
  reference adapter is a MAJOR and is out of this plan's scope.
- **The CLI stays the invariant substrate.** No unit forks or replaces a verb; every
  provider wraps the SAME CLI/journal/gates. A reference that reimplements a verb
  instead of calling it has left the substrate.

## Judgment calls needing the user (flagged — you asked to be hammered with these)

1. **⚑ T6 is the big one: is the install-surface abstraction worth the churn?**
   `agent_assets.py` is stable, tested, and Claude-shaped by necessity. Refactoring
   it behind a `CapabilityProviderInstaller` Protocol buys a second harness "somewhere
   to plug in" but touches the single most load-bearing install file for a benefit no
   external harness has yet asked for. Options: (a) do it now (proactive seam);
   (b) defer to when a real second harness needs it (YAGNI, but then the abstraction
   is designed under delivery pressure); (c) doc-only — state the seam in the contract
   without refactoring, and let the first real second harness drive the extraction.
   **My lean: (c) then (b)** — the other units deliver portability proof without it.
2. **⚑ T3 GenAI schema stability.** The OTel GenAI semantic conventions for
   agent/LLM output events are still evolving upstream. A reference reader pinned to
   a moving schema will rot. Options: (a) implement against the current convention
   and accept a drift-log burden; (b) implement the SEAM only, with the GenAI reader
   as a documented stub + a fixture, deferring the live parse. **My lean: (b)** — the
   seam is the durable win; the concrete reader can follow the convention's freeze.
3. **⚑ T5 placement: kit vs examples.** Does the elicitation-client proof belong IN
   the conformance kit (a third certified adapter, shipped in the wheel) or in the
   examples lane (unpublished reference)? The notebook-render precedent is a shipped
   adapter; but an elicitation client is closer to a live-harness integration. **My
   lean: examples-lane reference + a conformance LEG that drives it via FakeMcpClient**
   (offline), mirroring how the notebook adapter drives the plugin.
4. **⚑ T9: remove vs relabel the interview permission-writer.** Removing
   `_maybe_write_claude_permissions` is the honest "the worker is gone" move but
   deletes a convenience for any `claude` launched from an experiment dir and changes
   an artifacts-list output (a visible behavior change). Relabeling keeps a Claude-Code
   project-settings write with no live consumer. **My lean: relabel to a
   harness-neutral grant + delete the `claude -p` prose**, unless you want it gone
   entirely.
5. **⚑ Scope: is T4 (response gateway) in or out?** It is the most "productizing"
   unit — a real proxy in front of a model. If the goal is PROOF-of-portability, the
   outcome-shaped conformance adapter suffices without a runnable gateway example.
   **My lean: build the conformance adapter (proof) now; make the runnable gateway
   example optional / a follow-up.**

## Related docs

- `docs/internals/harness-contract.md` — the normative skeleton this plan builds
  out; the three capabilities, the write API, the sha canon, INSPECT/ACT, the
  honest trust limit.
- `docs/design/conformance-kit.md` — the certification mechanism (LANDED; T1 flips
  its status); the adapter Protocol, the reference adapters, the CI self-conformance
  job, `export-attestations`.
- `docs/design/mcp-elicitation.md` — the elicitation channel + the dark-leg
  degradation T5 proves a second client against.
- `docs/design/notebook-audit.md` — the second-conforming-harness precedent
  (notebook render) and the hand-off pattern this document follows.
- `docs/design/live-conformance.md` / `docs/design/evidence-memory.md` — the
  attestation-projection siblings whose house structure this doc mirrors.
- `docs/internals/engineering-principles.md` — the Q1 boundary the flags patrol;
  the enforcement maps the rows accrue to.

## Drift log

- **Same-day rulings + Phase 0 execution (2026-07-09):** T1 (conformance-kit
  status flip + 7 more stale docs — the sweep found the class was wider than
  the one doc) and T2 (contract addendum: detection asymmetry + auth boundary,
  one doc-prose pin each) landed the same session this plan merged. USER RULED:
  ⚑T9 = REMOVE (excised same session — interview no longer writes
  `.claude/settings.json`; the three tests replaced by a does-not-write pin;
  `side_effects` narrowed); ⚑T3/⚑T5 and ⚑T4 = DEFERRED until after run #12
  (revisit with campaign evidence); ⚑T6 stays open (the plan's doc-only lean
  stands until an external harness asks).
- (Populate per deviation, each with its recorded reason, when implementation
  lands. The `docs/design/notebook-audit.md` drift log is the form to follow.)
- **Ruling records (2026-07-10 user, recorded from session):** (a) raw-ssh
  DENY rule — the agent-facing environment gains a deny on raw `ssh`/`scp`
  invocation against cluster hosts (the sanctioned verbs are the only dial
  path; the improvisation class dies at the permission layer, not in conduct
  prose). **SHIPPED (2026-07-10):** `agent_assets.py::_merge_deny_rules` writes
  the deny into `~/.claude/settings.json`'s `permissions.deny`, wired into
  `install_agent_assets` as the `settings_deny` result key.
  **NARROWED (2026-07-10, same day, user):** the first cut wrote a BLANKET
  `Bash(ssh:*)` + `Bash(scp:*)` into the user-GLOBAL settings, blocking ALL
  ssh/scp in EVERY project on the box — over-broad. User ruling: *"hpc-agent
  should be a TOOL and not something that takes over the user's entire
  workspace,"* and the original ruling text already said *"against cluster
  hosts."* So the deny is now HOST-SCOPED: at install time
  `_configured_cluster_hosts()` derives the host list from the clusters config
  the install can see (`load_clusters_config`: packaged default + user
  overrides, skipping `<...>` placeholders) and `_raw_ssh_deny_rules()` emits
  `Bash(ssh *<host>*)` + `Bash(scp *<host>*)` per host. Rule form uses the
  `Bash(<pat>)` `*`-glob-anywhere matcher (Claude Code settings docs; their own
  deny example is `Bash(curl *)`). Only cluster ssh is denied; ssh to any other
  host (a colleague's box, a git remote) is untouched. No resolvable hosts →
  NO deny rules installed (the user-side cluster-ssh confirm-guard hook is the
  backstop). MIGRATION: `_merge_deny_rules` also REMOVES the two blanket rules
  (`_BLANKET_SSH_DENY_RULES`, exact-string match — never any other `deny`
  entry) on every run, so an upgrade self-heals the over-reach. A raw ssh/scp to
  a cluster the model authors AT RUN TIME dies at the permission layer; the
  sanctioned hpc-agent verbs dial ssh inside their OWN subprocesses (never via
  the agent's Bash tool) and are unaffected — as are `ssh-keygen` / `ssh-add`
  (distinct command tokens with no cluster host) and the `ssh_run` /
  `ssh_target` identifier forms. Complements `scripts/lint_no_raw_ssh.py` (which
  removes the raw-ssh affordance from agent-facing PROSE); this closes the
  runtime side. Tests: `tests/cli/test_agent_assets_settings_deny.py` (host-
  scoped rules written + placeholder skipped, blanket removed on reinstall,
  no-hosts → no add, no-hosts still removes stale blanket, idempotent, partial
  overlap, other entries preserved, unparseable skip, dry-run); two sibling
  permission/hook tests updated (blanket rule asserted ABSENT). (b) the MCP
  elicitation display-receipt gap is to be FILED UPSTREAM
  (spec issue: a client-side receipt that the elicitation was actually
  displayed), per this doc's honest-trust-limit note. Both = post-run-#12
  batch item 8 riders.
- **T6 REFRAMED (2026-07-10, user):** the install-surface question was a
  proxy — the actual want is a fleshed-out EXTERNAL-HARNESS READINESS plan
  (how a non-Claude harness drives the stack end-to-end: the lockout
  inventory turned into dispatch-ready work items, with the harness
  contract / second-harness plugin / conformance kit as the existing
  skeleton). T6's install-surface answer folds into that plan rather than
  standing alone. Plan doc = the Fable-endgame handoff bank.
