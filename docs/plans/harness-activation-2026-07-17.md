---
status: plan
created: 2026-07-17
program: harness-activation
authority: this file (design of record) — folds the anti-vendor-lockout memo's R1/T4 grant
baseline: main @ c893d2fa
supersedes: anti-vendor-lockout-2026-07-17.md §3 T4 (which reserved this design); T4 is now DECOMPOSED here
---

# Harness ACTIVATION — the engineering plan (design of record)

**User ruling (2026-07-17):** anti-lockout **R1 = "let's plan this out properly
now."** Activation is the single biggest concrete lockout item on the standing
board (`anti-vendor-lockout-2026-07-17.md` §1(1), sized **L**, "the single
biggest concrete lockout"). This document is the plan of record for it, to the
house planning standard (`daemon-engineering-2026-07-16/` is the precedent:
settled-design table + wave plan + file-disjoint unit decomposition +
residual-risk register written for the premortem to attack). A premortem agent
reviews this next; §5 is written for that lens, and the `unit-specs` twin is
embedded in §4 (this program ships as ONE file by dispatch instruction — the
machine-readable unit table is folded in, not a sibling `.json`).

**The bounding rulings (honored verbatim, not re-litigated).** Three 2026-07-17
rulings fence this design and NOTHING here may cross them:

- **MCP is a PROJECTION, never load-bearing on trust** — the MCP server the
  installer registers is a convenience surface over the CLI; a harness that
  never registers it loses no guarantee (`harness-contract.md`, "The CLI is the
  invariant substrate").
- **Bare `y` stands; no gate tightening** — activation adds capability
  *providers*, never friction (`human-amplification-blocks.md` §2).
- **Trust never moves into a harness surface** — an activation profile is a
  MECHANISM DESCRIPTION (which providers to wire), never an AUTHORIZATION
  (installing it grants zero trust; the gate still reads the DETECTED seam and
  the kit still proves by behavior). This is the load-bearing guardrail of the
  whole plan; §5-R3 is the premortem's sharpest target.

Two upstream rulings already recorded in the memo's §5 are consumed here as
settled inputs: **R2 (negotiation version-gating) = NO, report-only** and **R4
(the two unnamed enforcement behaviors) = promote to NAMED capabilities 6/7**.

---

## 1. The target, stated honestly — the activation surfaces

"A non-Claude harness activates the stack" concretely means: a foreign harness
can materialize its OWN capability providers (the things that make hpc-agent's
authorship / relay / backgrounding guarantees hold) through a SUPPORTED SEAM,
instead of hand-wiring them with no contract. Activation is the INSTALL side of
the harness contract — the contract already specifies what a conforming provider
must *do* (`harness-contract.md` §2, the capabilities); it says nothing about how
a harness *installs* one. `install_agent_assets` is that install path, and it is
Claude-Code-only.

The complete inventory of activation surfaces, each rated Claude-Code-specific
(the lockout) vs already-portable, cited `file::symbol`:

### Surface 1 — hook wiring (the load-bearing lockout)

`agent_assets.py::_HOOK_SPECS` (the six non-Stop hooks) +
`agent_assets.py::_merge_stop_multiplex_hook` (the fused Stop entry). Ten hook
needles total (`_UTTERANCE_CAPTURE_NEEDLE`, `_ANSWER_CAPTURE_NEEDLE`,
`_RELAY_AUDIT_NEEDLE`, `_ALERT_COUNT_NEEDLE`, `_STOP_MULTIPLEX_NEEDLE` +
the three guard needles, `skill_return_autofetch`,
`decision_rendezvous_autofetch`, `scheduler_write_fence`).

- **Claude-Code-specific:** the `settings.json` `hooks.<event>` array layout;
  the `bash -c` command shape (`agent_assets.py::_hook_command`, incl. the
  Windows forward-slash + `shlex.quote` fixes in `_hook_python`); the event
  vocabulary (`PostToolUse` / `PreToolUse` / `UserPromptSubmit` / `SessionStart`
  / `Stop`); the `matcher` semantics (`Bash`, `AskUserQuestion`); the merge/heal
  machinery keyed on the module-path needle (`_find_hook_entry_index`).
- **Already portable:** the hook BODIES. Every hook is `python -m
  hpc_agent._kernel.hooks.<module>` reading a **payload dict** — pure functions,
  no Claude Code in the loop (confirmed by the conformance kit's own driving:
  `utterance_capture.capture(payload)`, `answer_capture.capture(payload)`,
  `relay_audit_stop.build_hook_output(payload)` are payload-pure — conformance-kit
  drift-log #4). A foreign harness that can call a subprocess with a JSON payload
  on its own turn-boundary events already has everything the bodies need; what
  it lacks is a SEAM that tells it *which* body binds to *which* event with
  *which* pre-filter.

### Surface 2 — MCP server registration

`agent_assets.py::_register_mcp_server` writes the `hpc-agent` stdio server into
`.claude.json`'s `mcpServers` (`_MCP_SERVER_ENTRY`, `_mcp_config_path`).

- **Claude-Code-specific:** the `.claude.json` sibling-of-`.claude` discovery
  path; the stdio-server entry shape Claude Code reads.
- **Already portable:** `hpc-agent mcp-serve --allow-mutations --catalog curated`
  is a standard stdio MCP server any MCP client registers its own way. **Per the
  MCP-is-projection ruling this surface is explicitly NON-LOAD-BEARING** — a
  harness that never registers it drives the identical CLI and loses no
  guarantee. It is the LOWEST-priority activation surface, and the plan does not
  chase MCP-client-specific registration.

### Surface 3 — permission / settings layout

`agent_assets.py::_merge_skill_permissions` (`Skill(<name>)` allow rules),
`_merge_deny_rules` (host-scoped `Bash(ssh *<host>*)` cluster-ssh deny),
`_prune_skill_permissions`.

- **Claude-Code-specific:** the entire `permissions.allow`/`deny` schema is
  Claude Code's auto-mode classifier surface; the `Skill(...)` / `Bash(...)`
  matcher grammar. These exist to satisfy Claude Code's classifier
  (`_merge_skill_permissions` docstring: "Denied by auto mode classifier"). A
  foreign harness has its own permission model or none.
- **Already portable:** nothing here is load-bearing on a guarantee — it is
  ergonomic (stops Claude Code silently denying the first `Skill()` call) and
  defensive (the cluster-ssh deny backstops conduct rule 7, whose REAL
  enforcement is `scheduler_write_fence` + the verbs dialing ssh inside their own
  subprocesses). A harness without this layout degrades to "the classifier
  prompt fires" / "the confirm-guard hook is the backstop", never a trust loss.

### Surface 4 — skill / command / agent prose distribution

`agent_assets.py::_install_tree` copies `commands/*.md`, `skills/<name>/SKILL.md`,
`agents/*.md` into `<claude_dir>/`; `_prune_stale_assets` +
`_write_asset_manifest` handle removal/versioning.

- **Claude-Code-specific:** the `.claude/commands` + `.claude/skills` discovery
  convention; the prose idioms inside the assets (`$ARGUMENTS`, `Skill(...)`,
  "your final action MUST be a tool call") — this is the **T5 prose lockout**,
  owned by the runbook unit, NOT this activation plan (see §3).
- **Already portable:** each command maps 1:1 to a CLI entry; each SKILL.md wraps
  a harness-neutral CLI spine (`block-drive` / `append-decision` / …). The
  DISTRIBUTION mechanism (copy a tree into a config dir) is trivially
  generalizable; the PROSE neutrality is T5's problem.

### Surface 5 — environment variables

- **`CLAUDE_CONFIG_DIR`** — honored in the READ/probe path
  (`ops/harness_capabilities.py::_claude_dir`) but **NOT the WRITE path**:
  `agent_assets.py::DEFAULT_CLAUDE_DIR()` is hardcoded `Path.home()/".claude"`
  and `install_agent_assets` honors ONLY the `claude_dir` kwarg
  (`cli/setup.py::_emit_install_commands` passes `args.claude_dir` or `None`).
  **This asymmetry is a latent bug, not just a lockout**: a user who relocates
  their config with `CLAUDE_CONFIG_DIR` gets a probe that reads the new dir and
  an installer that writes the OLD one — capabilities land where the probe never
  looks. Closing it is the MINIMAL floor (Surface-5 is the cheapest, highest-ROI
  activation fix).
- **`HPC_STOP_HOOK_APPEND` / `HPC_STOP_HOOK_APPEND_ON_BLOCK`** — capability-5
  activation markers (`ops/harness_capabilities.py`), already env-declared and
  harness-neutral (a foreign harness sets them after its own conformance probe).
- **`HPC_JOURNAL_DIR`, `HPC_ACTOR`, `HPC_CLUSTERS_CONFIG`** — already
  harness-neutral; no lockout.

**Net honest target.** The capabilities are portable; the ACTIVATION of them is
Claude-Code-shaped in Surfaces 1, 3, 4 and half-portable in 2, 5. The residual
lockout is concentrated in **Surface 1 (hook wiring)** — everything else either
already degrades honestly (2, 3), is another plan's problem (4-prose = T5), or is
a one-line env fix (5). The plan attacks Surface 1 as the real work, Surface 5 as
the free floor, and explicitly declines Surfaces 2/3/4-prose.

---

## 2. The two R1 options, to decision-depth

### Option (a) — MINIMAL: make the existing install env-overridable

**Shape.** One resolver for the harness-config target, shared by the write and
read paths. Today `agent_assets.DEFAULT_CLAUDE_DIR()` and
`harness_capabilities._claude_dir()` are TWO definitions that disagree (the
latter honors `CLAUDE_CONFIG_DIR`, the former does not). Collapse to one
`resolve_claude_dir()` (one-definition doctrine) honoring `CLAUDE_CONFIG_DIR` →
`~/.claude`, used by `install_agent_assets`, `setup`, and the probe. The
`.claude.json` sibling already derives correctly from a relocated dir
(`_mcp_config_path` docstring already handles this), so MCP registration follows
for free.

- **What it buys.** (1) Closes the latent read/write asymmetry bug — a real
  correctness fix independent of portability. (2) Unblocks **Claude-Code
  variants and forks**: any harness that reads a `~/.claude`-SHAPED config
  (settings.json hooks, the same event/matcher vocabulary) from a RELOCATABLE dir
  now activates through the supported installer — a fork that renames the dir,
  ships in a sandbox, or multi-tenants config dirs all work.
- **What it CANNOT buy.** A harness whose config is NOT `.claude`-shaped —
  different hook schema, no `hooks.<event>` array, no `bash -c` command strings,
  no `Skill()`/`Bash()` grammar — still cannot be activated. The DIRECTORY is
  parameterized; the LAYOUT is still hardcoded. **What degrades and how the kit
  catches it:** such a harness that points `CLAUDE_CONFIG_DIR` at its own dir gets
  a settings.json it does not read → its hooks never fire → capabilities 1 & 2
  are ABSENT → the gates fall to the named friction tiers
  (`journal.py::_harness_human_texts` → None; relay reverts to verb-only). The
  conformance kit detects this by BEHAVIOR: `detect_capabilities` runs
  `harness-capabilities`, sees the needles absent (or, for a foreign provider,
  runs the behavior modules), and `test_negotiation.py`'s `declared == detected
  == behaved` reports the harness PARTIAL — never falsely conforming. Honest
  degradation, kit-verified.
- **Size:** S. **Files:** `agent_assets.py`, `ops/harness_capabilities.py`
  (collapse to the shared resolver), `cli/setup.py` (surface a note), one test.
  **No regen** (no primitive/CliShape/wire change — `--claude-dir` already
  exists; the env is read inside the resolver).

### Option (b) — FULL: a harness-neutral activation profile

**Shape.** A declarative `HarnessProfile` describing WHAT to wire, independent of
HOW a given harness's config renders it:

- a list of **hook descriptors** — `{needle (module path), event-semantics
  (turn-final / pre-tool / on-prompt / on-answer / session-start), pre-filter
  trigger verbs, matcher-intent}` — event-semantics stated NEUTRALLY (not Claude
  Code's `Stop`/`PreToolUse` strings), so a foreign consumer maps them to its own
  event model;
- the **asset trees** (commands/skills/agents) to distribute;
- the **MCP server** invocation (`hpc-agent mcp-serve …`) as data;
- the **grants** intent (which skills need an auto-invoke allowance) — advisory,
  since permission models differ.

`ClaudeCodeProfile` is the FIRST profile: it renders the descriptors into today's
exact settings.json/`.claude.json` layout (the current `install_agent_assets`
body becomes `ClaudeCodeProfile.render()` — a pure refactor, byte-identical
output pinned by a golden test). A foreign harness ships its OWN profile consumer
(harness-side code, the plugin lane) that reads the SAME descriptor list and
wires its own config. Core exposes the descriptor list through a read verb
(`harness-activation-profile`, a `query`, `agent_facing=False`) so a foreign
installer consumes DATA, never re-derives our hook inventory.

- **What it buys.** Genuine foreign activation: a harness with hooks-of-some-kind
  gets the canonical, versioned list of providers-to-wire and binds them to its
  own event model through one supported seam — no hand-wiring, no needle
  guessing. It also makes the hook inventory a SINGLE SOURCE OF TRUTH (the
  descriptor list) instead of the `_HOOK_SPECS`-tuple-plus-Stop-special-case
  split, retiring a class of drift.
- **What it CANNOT buy.** A harness with **NO hook capability at all** — no
  turn-boundary subprocess seam — cannot run the relay classifiers *regardless of
  profile*. The profile describes the wiring; it cannot manufacture a capability
  the harness lacks. **What degrades and how the kit catches it:** such a harness
  installs the profile, the descriptors have nowhere to bind, capabilities 1 & 2
  are absent → same named friction tiers as (a). The profile install is NOT
  evidence of the capability — the kit's `declared == detected == behaved` is what
  proves it, and a profile-installed-but-not-behaved harness is reported PARTIAL.
  **This is the doctrine-critical property (§5-R3):** installing a profile grants
  no trust; only behaving the capability does.
- **Size:** L (touches the hottest core file; introduces the profile
  abstraction; a foreign-consumer contract to pin). **Files:** `agent_assets/`
  becomes a package — `agent_assets/__init__.py` (the shim), `agent_assets/
  profile.py` (`HarnessProfile` + descriptor types), `agent_assets/
  claude_code_profile.py` (the rendering), `agent_assets/install.py` (the merge
  machinery, unchanged, now profile-driven); a new read verb wire model; a
  golden byte-identity test; `pyproject.toml` (package move). **Regen:** yes (new
  verb → `bake_operations_json.py --write` + schema regen + registry-count pins).

### Recommendation: (a) NOW, (b) STAGED behind it — they COMPOSE, not compete

(a) is not an ALTERNATIVE to (b); it is (b)'s first step. In (b), "resolve the
Claude Code profile's target dir" IS the `CLAUDE_CONFIG_DIR`-overridable resolver
(a) builds. So:

- **Ship (a) as Wave 1, unconditionally.** It is the memo's own stated "at
  minimum" floor for T4, it fixes a latent correctness bug, it is strictly
  additive (S-sized, no abstraction, no regen), and it unblocks the largest
  concrete population today (Claude Code forks/variants/sandboxes).
- **Recommend R1 GRANT the FULL profile (b), sequenced as Wave 2**, with the
  MINIMAL install as its first consumer — mirroring the daemon package's
  MEASURE-THEN-DECIDE discipline (`daemon-engineering` §2a): land the cheap
  correctness floor, then build the L-sized abstraction once a concrete foreign
  harness (or the Wave-C adapters, §3) demonstrates it is exercised rather than
  speculative. The profile extraction is a pure refactor over (a) plus the
  descriptor list, so nothing in Wave 1 is rework.

The honest cut-line: if no foreign harness materializes and the Wave-C adapters
stay reserved (R3), (b) stands as design-of-record, shelved — (a) still shipped
the correctness fix and the fork-unblock. **This document is that design of
record.**

---

## 3. Sequencing with the standing anti-lockout waves

| Memo unit | Relationship to activation | Sequencing |
|---|---|---|
| **T1** (status/claim reconciliation) | Orthogonal; THIS doc is part of T1's living-doc output (the activation design the inventory reserved). | Wave A, parallel. |
| **T2** (name caps 6/7, R4-ruled) | Orthogonal — docs-only, names `scheduler_write_fence` (rule 7) + rendezvous commit-then-continue. Activation WIRES those hooks; naming them does not gate the wiring. | Wave A, parallel. |
| **T3** (cap 4/5 seam audit) | Orthogonal; preserves the elicitation-non-load-bearing posture (G3). | Wave A, parallel. |
| **T4** (activation seam) | **= this plan.** Wave 1 = (a) MINIMAL; Wave 2 = (b) FULL profile. | Sequential (hot file). |
| **T5** (prose-neutral runbook, R5) | ADJACENT, disjoint files (`harness-runbook.md` + light SKILL.md front-matter vs `agent_assets`). Shares the "harness-neutral projection" theme; the FULL profile's descriptor list is a natural INPUT the runbook references (the wiring the runbook's reader must perform). | After Wave 1; parallel with Wave 2 (file-disjoint). |

**Which capability second-harness proofs activation unblocks (the real
sequencing insight):**

- **Cap 1 (utterance log)** is ALREADY proven by `notebook_render.py` — and it
  needs NO activation seam (it activates through the `ingest-signoffs` path, not
  a hook install). **Orthogonal** to this plan.
- **Caps 2 & 3 (relay, backgrounding)** foreign adapters (memo T6/T7/T8, Wave C)
  are where activation matters: you cannot run `test_capability_relay.py` /
  `test_capability_backgrounding.py` against a FOREIGN harness's providers unless
  that harness can INSTALL its relay/background provider through a supported path.
  **(a) MINIMAL does NOT unblock these** (a foreign provider is not
  `.claude`-shaped). **(b) FULL DOES** — the profile is exactly the seam a foreign
  adapter's `detect_capabilities` install step consumes. So **FULL activation is
  a soft prerequisite of the Wave-C foreign adapters**; this is the concrete
  "exercised, not speculative" signal that should trigger building (b) under the
  MEASURE-THEN-DECIDE cut-line.
- **Caps 4 & 5 (trusted display, stop-hook append)** — Wave D, gated on R4
  outcomes (already ruled: name them 6/7). Cap 5 already activates by env marker
  (`HPC_STOP_HOOK_APPEND`), harness-neutral today. **Orthogonal** to the profile
  work.

---

## 4. Unit decomposition (dispatch-depth; file-disjoint)

Waves group file-disjoint units. The machine-readable twin is folded in as the
table below (this program ships as one file). Every unit that touches a trust
seam carries its §5 guardrail check.

### Wave 1 — MINIMAL activation floor (ships now, no ruling needed)

**U-ENV — collapse the config-dir resolver; honor `CLAUDE_CONFIG_DIR` in the
write path.**
- **Spec:** Introduce one `resolve_claude_dir()` (`CLAUDE_CONFIG_DIR` non-empty →
  `expanduser`, else `~/.claude`). `agent_assets.DEFAULT_CLAUDE_DIR` and
  `harness_capabilities._claude_dir` both delegate to it (retire the duplicate).
  `install_agent_assets`'s target becomes `claude_dir or resolve_claude_dir()`.
  The explicit `--claude-dir` kwarg still wins over the env (test-hermetic).
- **Files (exclusive):** `agent_assets.py`, `ops/harness_capabilities.py`,
  `cli/setup.py`, `tests/.../test_install_config_dir.py (new)`.
- **Tests/conformance owed:** fire test — set `CLAUDE_CONFIG_DIR` to a tmp dir;
  assert `install_agent_assets()` writes settings.json THERE and
  `harness-capabilities` reads it back from the SAME dir (the asymmetry, seeded
  red by reverting the resolver on the write side). Kit: none new — the existing
  `test_negotiation.py` already exercises the read side.
- **Size:** S. **Regen:** no. **Merge risk:** LOW (additive resolver; the merge
  machinery is untouched).
- **Guardrail check:** G2 (detection-only) — the resolver changes only WHERE we
  write, never WHAT the gate reads to grant trust. G4 (CLI invariant) — no verb
  surface change.
- **Ruling:** NONE — memo's stated "at minimum," a latent-bug fix.

### Wave 2 — FULL harness profile (gated on R1 = grant-staged)

**U-PROFILE — extract `HarnessProfile` + descriptor types; `ClaudeCodeProfile`
as first renderer.**
- **Spec:** `agent_assets/` becomes a package. `profile.py` declares
  `HarnessProfile` (frozen: hook descriptors with NEUTRAL event-semantics, asset
  trees, MCP invocation, grant intents). `claude_code_profile.py` renders it into
  today's exact settings.json/`.claude.json` layout — the current
  `install_agent_assets` body moves here UNCHANGED behind the profile. The hook
  needle constants stay the SINGLE definition, imported by both the renderer and
  `harness_capabilities` (unchanged).
- **Files (exclusive):** `agent_assets/__init__.py (new — re-exports the public
  API byte-stable)`, `agent_assets/profile.py (new)`, `agent_assets/
  claude_code_profile.py (new)`, `agent_assets/install.py (new — the merge
  machinery, moved verbatim)`, delete `agent_assets.py`, `pyproject.toml`
  (package data path), `tests/.../test_profile_golden.py (new)`.
- **Forbidden:** `ops/harness_capabilities.py` (U-ENV/U-PROBE own the resolver;
  this unit imports the unchanged needle constants only).
- **Tests/conformance owed:** GOLDEN byte-identity test — `ClaudeCodeProfile`
  rendered install output is byte-for-byte the pre-refactor
  `install_agent_assets` output (a captured fixture), so the refactor is provably
  inert. Needle-embed pin: every hook descriptor's rendered command MENTIONS its
  needle (else `_find_hook_entry_index` orphans it — §5 drift risk).
- **Size:** L. **Regen:** no (no verb yet — that is U-PROFILE-VERB).
- **Merge risk:** HIGH (hottest core file; the settings.json write path — §5-R1
  blast radius). Merges alone, last in its wave, full-CI gate.
- **Guardrail check:** G1 (no utterance-write affordance added — the profile
  wires the OUT-OF-BAND capture hook, never a model-callable write). G2
  (installing a profile grants NO trust; detection stays by-behavior). G4 (the
  profile wires providers AROUND the CLI; the verb surface is untouched).

**U-PROFILE-VERB — expose the descriptor list as a read verb for foreign
consumers.**
- **Spec:** `harness-activation-profile` (`query`, `agent_facing=False`,
  idempotent) returns the `ClaudeCodeProfile` descriptor list as DATA (hook
  needles + neutral event-semantics + pre-filters + asset-tree names + MCP
  invocation), so a foreign installer wires itself from the canonical inventory
  instead of re-deriving it. `agent_facing=False` because it is a
  harness-installer surface, not a model tool — and a read verb, never a write.
- **Files (exclusive):** `ops/harness_activation_profile.py (new)`,
  `_wire/queries/harness_activation_profile.py (new)`,
  `schemas/harness_activation_profile.output.json (new)`,
  `tests/.../test_harness_activation_profile.py (new)`.
- **Forbidden:** `agent_assets/**` (U-PROFILE owns it; this verb READS the
  profile object).
- **Tests/conformance owed:** the verb's descriptor list equals the profile's
  own hook inventory (one-definition pin — the verb never hand-lists needles).
  Boundary: no write side-effect (`side_effects=[]`). Registry-count pins move.
- **Size:** M. **Regen:** YES (new primitive → `bake_operations_json.py --write`
  + schema regen + registry-count contract-test bump; MCP curated-catalog
  decision — recommend NOT curated, it is installer-facing not block-facing).
- **Merge risk:** MEDIUM (regen-forcing; merges after U-PROFILE lands).
- **Guardrail check:** G2 (the verb REPORTS mechanism; it asserts nothing about
  trust — reading it is not activating anything). G5 (report-only, consistent
  with R2's no-gating ruling — the profile carries a version but the verb never
  gates on it).

**What needs NO further ruling** (build directly once R1 grants staging):
U-ENV (Wave 1, no ruling at all), U-PROFILE and U-PROFILE-VERB (the mechanics of
the grant — the refactor and the read verb are pure engineering under G1/G2/G4).

**Genuine residual decisions (with recommendations):**
- **RD-1: Does core ship any FOREIGN profile, or only `ClaudeCodeProfile`?**
  *Recommend: only `ClaudeCodeProfile` in core.* A foreign profile is the
  harness's own code (the plugin lane, `slash_command_assets` precedent). Core
  shipping a foreign layout would be core WRITING a config it cannot verify —
  and detection-only (G2) means core never needs to. This keeps the blast radius
  (§5-R1) to the one layout we own.
- **RD-2: Where does the profile package live — `agent_assets/` vs
  `_kernel/`?** *Recommend: `agent_assets/` package.* It is install machinery,
  not kernel; keeping the public `install_agent_assets` / `DEFAULT_CLAUDE_DIR`
  names stable (re-exported from `__init__`) means every existing importer
  (`cli/setup.py`, `harness_capabilities`) is untouched.
- **RD-3: Is `harness-activation-profile` MCP-curated?** *Recommend: no.* It is
  installer-facing; the MCP catalog is block-facing (returns `next_block`). Per
  the MCP-is-projection ruling, a foreign installer reads the CLI verb directly.

---

## 5. Residual-risk register (written for the premortem to attack)

1. **The settings.json write path touches a user's REAL `~/.claude` — blast
   radius.** The merge is already well-defended: additive + idempotent +
   skip-unparseable + atomic-write (`_merge_json`, `_load_json_object`'s
   `_UNPARSEABLE` refusal, `atomic_write_text`). The NEW exposure U-ENV adds is
   that a MISTAKEN `CLAUDE_CONFIG_DIR` now redirects the WRITE (today it silently
   redirects only the read). *Containment:* `expanduser` + no parent-mkdir beyond
   the target dir; the explicit `--claude-dir` kwarg still overrides the env so
   tests never touch a real home; the write path stays skip-unparseable (a
   foreign file at the redirected path is refused, never clobbered). U-PROFILE
   MULTIPLIES this only if core rendered foreign layouts — RD-1 forbids that, so
   core still only ever writes the ONE layout it owns. **Premortem target:** does
   any code path mkdir or write OUTSIDE the resolved dir? (It must not.)
2. **Hook-needle drift between harnesses.** The needle (module path) is
   load-bearing: it is written INTO the command AND used to re-find/heal our entry
   (`_find_hook_entry_index`) AND probed by `harness-capabilities`
   (`_needle_installed`). A profile that renders a different command shape must
   STILL embed the same needle or the probe/re-find silently orphans the hook.
   *Containment:* the needle constants stay the single definition imported by
   renderer + probe; U-PROFILE's needle-embed pin asserts every descriptor's
   rendered command mentions its needle; a renamed needle is already known to
   orphan installs (module docstring warns it). **Premortem target:** a foreign
   profile that renders a DIFFERENT invocation (not `python -m …`) — does the
   needle survive? (The descriptor carries the needle as data; the foreign
   renderer must embed it — the contract must state this obligation.)
3. **The activation profile becoming a TRUST surface (the central doctrine
   risk).** A profile MUST stay MECHANISM-DESCRIPTION, never AUTHORIZATION.
   Installing a profile must grant ZERO trust; the gate must still read the
   DETECTED seam (`journal.py::_harness_human_texts`) and the kit must still prove
   `declared == detected == behaved`. The failure shape to guard: a
   `capabilities:` field the profile SELF-ASSERTS ("I provide relay enforcement")
   that any code reads as truth — the exact guard-the-LLM-satisfies failure one
   level up (conformance-kit boundary flag: "No self-asserted capability
   manifests"). *Containment:* the profile describes PROVIDERS TO WIRE, never
   CAPABILITIES CLAIMED; `harness-activation-profile` is a read verb that reports
   mechanism and asserts nothing; no gate anywhere reads "profile installed."
   **Premortem target:** trace every consumer of the profile/verb — does any of
   them treat profile-presence as capability-presence? (None may. This is the
   guardrail the whole plan lives or dies on.)
4. **Version skew between profile and wheel.** A profile rendered by an OLD wheel
   into a harness, then the wheel upgrades — stale needles/commands. *Containment:*
   `_write_asset_manifest` already stamps the wheel `version`; the merge machinery
   already SELF-HEALS a stale entry (`updated` on a moved venv / changed shape).
   For FULL, the profile carries a schema version; per R2 (ruled report-only) skew
   is REPORTED, NEVER GATED — a version mismatch surfaces in the verb's output,
   never blocks activation (gating would move trust into a self-declared version
   string, the exact forbidden move). **Premortem target:** does anything gate on
   the profile version? (It must only report — R2.)
5. **The refactor (U-PROFILE) silently changing install output.** Moving the
   `install_agent_assets` body behind `ClaudeCodeProfile.render()` risks a
   byte-drift in the written settings.json that no test catches → every existing
   install heals-then-reheals or orphans. *Containment:* the GOLDEN byte-identity
   test pins the rendered output equal to a captured pre-refactor fixture; the
   refactor is provably inert or the test is red. **Premortem target:** is the
   golden fixture captured from the REAL pre-refactor output (not hand-written)?

---

## 6. Guardrail conformance (the plan may not cross these — restated from the memo §4)

- **G1** — no unit adds a sanctioned utterance-write call; the profile wires the
  out-of-band capture hook, never a model-callable write. (U-PROFILE checked.)
- **G2** — detection-only: installing a profile grants no trust; the gate reads
  the detected seam, the kit proves by behavior. (§5-R3 is the enforcement.)
- **G3** — elicitation stays non-load-bearing; untouched (activation does not
  alter the MCP elicitation path).
- **G4** — the CLI stays the invariant substrate; the profile wires providers
  AROUND the verb surface, never forks it.
- **G5** — amplification doctrine: bare `y` stands; activation adds providers,
  never friction; skew is reported, never gated.

## 7. Ruling docket

- **R1 (activation scope) — the open ruling this plan answers.** Recommend:
  **GRANT the FULL profile (b), STAGED** behind the MINIMAL floor (a). (a) ships
  Wave 1 unconditionally (latent-bug fix + fork unblock, no ruling needed); (b)
  is Wave 2, built when a foreign harness or the Wave-C adapters exercise it
  (MEASURE-THEN-DECIDE cut-line). RD-1/2/3 are the constraints OF the grant
  (core ships only `ClaudeCodeProfile`; `agent_assets/` package; verb not
  MCP-curated) — all recommended, none blocking.
- **R2, R4** — consumed as settled (report-only; name caps 6/7). Not reopened.
- **R5 (prose runbook)** — adjacent (T5), not this plan; the FULL profile's
  descriptor list is a natural input the runbook references.

---

## Drift log

- **Created 2026-07-17 (plan of record).** Chartered by the R1 ruling
  ("let's plan this out properly now", 2026-07-17) recorded in
  `anti-vendor-lockout-2026-07-17.md` §5-R1. DECOMPOSES that memo's T4 (Wave B)
  to dispatch depth; supersedes T4's reservation. Inventory (§1) verified against
  code at `main @ c893d2fa`: `agent_assets.py` (the ten needles, `_HOOK_SPECS`,
  `_merge_stop_multiplex_hook`, `_register_mcp_server`, `DEFAULT_CLAUDE_DIR`),
  `ops/harness_capabilities.py::_claude_dir` (the `CLAUDE_CONFIG_DIR` read the
  write path lacks — the latent asymmetry), `cli/setup.py` (the `--claude-dir`
  kwarg / `None` default), `_wire/spawn_contract.py::DECISION_POINTS` (the
  code-homed procedure R5 projects), `docs/internals/harness-contract.md`
  (capabilities + the CLI-invariant + detection-asymmetry D-K3),
  `docs/design/conformance-kit.md` (K1-K10 shipped; `declared==detected==behaved`;
  drift-log #4 confirms the hook bodies are payload-pure).
- **Bounding rulings honored (2026-07-17):** MCP-is-projection (Surface 2
  declined as non-load-bearing), amplification (G5, skew report-only), trust-never-
  in-a-harness-surface (§5-R3, the profile-is-mechanism-not-authorization
  guardrail). R2 (report-only) and R4 (name caps 6/7) consumed as settled inputs.
- **Recommendation recorded:** (a) and (b) COMPOSE — (a) is (b)'s first step, not
  a competing option; ship (a) now, stage (b). The honest cut-line: if unexercised,
  (b) stands as design-of-record here, shelved (the daemon-package precedent).

- **2026-07-17 — U-ENV BUILT (Wave 1), with D1–D4 applied.** One shared
  `agent_assets.resolve_claude_dir()` now honors `CLAUDE_CONFIG_DIR` on both the
  install WRITE path (`install_agent_assets`) and the capability READ probe
  (`harness_capabilities._claude_dir` delegates), closing the latent read/write
  asymmetry. D1: resolver FENCED to the config-dir surface; the journal home
  (`HPC_JOURNAL_DIR`, default `~/.claude/hpc`) is untouched, with a fire-test
  asserting it ignores `CLAUDE_CONFIG_DIR`. D2: MCP-follows-resolved-dir
  downgraded to best-effort in `_mcp_config_path`. D3: upgrade HEAL/orphan-litter
  semantics documented at the resolver. D4: fire-test pins `.claude.json` as the
  sole out-of-tree write and guards the default `~/.claude` is never touched.
  Stale `interview.py #190` comment corrected. Built in an isolated worktree,
  integrated by the coordinator.
