---
status: premortem
created: 2026-07-17
reviews: harness-activation-2026-07-17.md (committed 6f6a945b)
baseline: main @ c893d2fa (source verified against this tree)
style: daemon-engineering-2026-07-16/premortems/ (verdict + numbered binding deltas)
---

# Harness ACTIVATION — adversarial premortem

**Anchors verified against source** (`main @ c893d2fa`): the read/write asymmetry
(`ops/harness_capabilities.py::_claude_dir` L138-150 reads `CLAUDE_CONFIG_DIR`;
`agent_assets.py::DEFAULT_CLAUDE_DIR` L424-426 is hardcoded `Path.home()/".claude"`,
no env; `install_agent_assets` target `= (claude_dir or DEFAULT_CLAUDE_DIR()).expanduser()`
L1265; `cli/setup.py` passes `args.claude_dir or None` L34/89/98/228). The merge
core (`_merge_json` + `_load_json_object`'s `_UNPARSEABLE` refusal + `atomic_write_text`
mkstemp→fsync→`os.replace` L180-210). `_mcp_config_path = claude_dir.parent/".claude.json"`
L249-261. The ten needles + `_HOOK_SPECS` + `_merge_stop_multiplex_hook`. The
manifest writer stamps `__version__` (L1127/1131) and the deny rules derive from
`_configured_cluster_hosts()` (L763-794). The journal-home resolvers
(`state/_homedir.py::journal_homedir` L42-55, `state/run_record.py::current_homedir`)
key on `HPC_JOURNAL_DIR`, default `Path.home()/".claude"/"hpc"` — **NOT** `CLAUDE_CONFIG_DIR`.
The conformance adapter (`conformance/adapters/claude_code.py` L256-258) HAND-WRITES
settings.json into a `CLAUDE_CONFIG_DIR` temp dir (it does not call `install_agent_assets`).

---

## VERDICTS

- **Wave 1 (U-ENV) — GO-WITH-CHANGES.** The asymmetry is genuinely live on the
  DEFAULT install path (not a corner case), and closing it is a real correctness
  fix. But the plan under-states two things that bind the coordinator's
  integration: the resolver's blast radius must be FENCED off the journal home
  (a naive one-definition merge would relocate every existing user's runs), and
  the "MCP follows for free" claim is an UNVERIFIED Claude-Code invariant, not a
  fact. Four binding deltas (D1-D4). No STOP: the merge machinery is already
  torn-write-safe and clobber-refusing, so the new write-redirect exposure is
  contained.
- **Wave 2 (U-PROFILE + U-PROFILE-VERB) — GO-WITH-CHANGES.** The refactor is
  sound and the doctrine posture (profile = mechanism, never authorization) is
  correct — but the plan defends the load-bearing trust boundary with PROSE where
  this repo's own doctrine demands a structural pin (the daemon premortem's
  F6/F8 precedent: "prose, not pins" is a finding, not a pass), and the golden
  byte-identity pin as specified (§5-item-5, "captured from the REAL pre-refactor
  output") is FLAKY on three independent nondeterminism sources. Five binding
  deltas (D5-D9). No STOP: every delta is a spec-tightening, none structural.

---

## Wave 1 — U-ENV binding deltas

### D1 — FENCE `resolve_claude_dir()` to the harness-config surface; it must NEVER reach the journal home. (blast radius / one-definition)

There are not two `.claude` resolvers — there are **three**, on **two different
axes**:

| resolver | axis | env knob | default |
|---|---|---|---|
| `harness_capabilities._claude_dir` (read) | harness CONFIG dir | `CLAUDE_CONFIG_DIR` | `~/.claude` |
| `agent_assets.DEFAULT_CLAUDE_DIR` (write) | harness CONFIG dir | *(none — the bug)* | `~/.claude` |
| `state/_homedir.journal_homedir` + `run_record.current_homedir` + `_kernel/contract/layout.py` | JOURNAL home | `HPC_JOURNAL_DIR` | `~/.claude/hpc` |

The plan's "collapse to ONE resolver (one-definition doctrine)" language (§2a,
§4 U-ENV) names only the two CONFIG-dir definitions — correct — but nothing in
the spec FENCES the merge off the journal home, which also lives under `.claude`.
If the coordinator (or a later reader chasing "one-definition") points
`journal_homedir`/`current_homedir` at `resolve_claude_dir()`, then **every
existing user who has ever set `CLAUDE_CONFIG_DIR` has their entire journal —
runs, RunRecords, submit-locks, monitor sidecars — relocated on upgrade**, i.e.
their live-run history vanishes from where every reader looks. This is the
single highest-consequence latent move in the whole program and the plan is
silent on it.

**Binding:** U-ENV's spec must state that `resolve_claude_dir()` collapses
`DEFAULT_CLAUDE_DIR` + `_claude_dir` ONLY, and that the journal-home resolver is
a SEPARATE axis keyed on `HPC_JOURNAL_DIR` that MUST NOT delegate to it. Add a
fire-test/lint: `journal_homedir()` does not read `CLAUDE_CONFIG_DIR` (set
`CLAUDE_CONFIG_DIR`, assert the journal home is unchanged). The `~/.claude/hpc`
default staying literal-home under a relocated config is INTENTIONAL, not a bug
to "heal."

### D2 — "MCP registration follows for free" is an UNVERIFIED assumption; downgrade it to best-effort. (correctness of the asymmetry-close)

`_mcp_config_path` writes `.claude.json` to `claude_dir.PARENT/".claude.json"`.
For the default (`~/.claude` → `~/.claude.json`) that sibling relationship is
correct. The plan asserts it "already derives correctly from a relocated dir …
so MCP registration follows for free" (§2a). That rests on an ASSUMED Claude Code
invariant — that under `CLAUDE_CONFIG_DIR=/custom/claude` the MCP config lives at
`/custom/.claude.json` (parent-sibling) rather than INSIDE the relocated dir. The
source proves only the default case; the relocated case is unverified here, and
real Claude Code may store the relocated user config inside the config dir. Under
U-ENV the installer would then write `.claude.json` to `/custom/.claude.json`
while Claude Code reads elsewhere — a NEW asymmetry the close would introduce for
the MCP surface specifically.

Because **MCP is ruled NON-load-bearing** (Surface 2, MCP-is-projection), a wrong
location degrades honestly to "MCP server absent → drive the CLI, no guarantee
lost." So this is not a STOP — but the plan must not claim it "follows for free."

**Binding:** downgrade the claim to "MCP registration under `CLAUDE_CONFIG_DIR`
is best-effort; a mislocated `.claude.json` degrades to MCP-absent (CLI-only),
consistent with MCP-is-projection." Do NOT ship a green MCP-relocation test that
merely asserts the code's own assumption — either verify the real Claude Code
relocated `.claude.json` location first, or assert only the honest degradation.

### D3 — State the upgrade orphan-litter (it is a HEAL, but the plan must say so). (blast radius / user-harm on upgrade)

The population U-ENV changes is exactly "users with `CLAUDE_CONFIG_DIR` set and no
`--claude-dir`." Today those users have a **silently broken install**: assets
written to `~/.claude`, Claude Code reading `$CLAUDE_CONFIG_DIR` — the hooks never
fired, capabilities absent. U-ENV **heals** this (assets now land where Claude
Code reads). The task asks: is it a heal or a surprise, and what of the stale
files? Answer, verified against the merge code:

- **No double-fire.** The old `~/.claude/settings.json` hook entries are ORPHANED
  but INERT — Claude Code follows `CLAUDE_CONFIG_DIR`, so it never reads the old
  location; the orphaned hooks do not fire. Not a hazard.
- **Litter, not harm.** The old `commands/`, `skills/`, and `.hpc-agent-manifest.json`
  remain. Manifest-prune (`_prune_stale_assets`) reads the manifest at the NEW
  location (absent on first post-upgrade install → prunes nothing), so the old
  location is never cleaned. Inert cruft.
- **The one honest exception:** a Claude Code FORK that reads `~/.claude`
  UNCONDITIONALLY while the user also set `CLAUDE_CONFIG_DIR` for another harness
  could see BOTH locations — genuinely ambiguous multi-harness config the
  installer cannot adjudicate.

**Binding:** the plan must state this as a known, inert residual (a heal for the
broken common case; harmless litter at the old dir; the fork/multi-harness case
degrades honestly). Optional: an install-time note pointing at the old dir when a
relocation is detected. Do not silently omit it — "the write target moved" is a
user-visible fact.

### D4 — The owed fire-test must assert NOTHING is written outside the resolved tree. (§5-item-1, mechanized)

§5-item-1's premortem target ("does any code path mkdir or write OUTSIDE the
resolved dir?") is the right question; the plan's owed test only asserts the
write LANDS in the resolved dir. It must also assert the NEGATIVE: with
`CLAUDE_CONFIG_DIR` set to a tmp dir and `--claude-dir` unset, NO write touches
`~/.claude` (guard against a missed call site still calling `DEFAULT_CLAUDE_DIR`).
Note the ONE intentional out-of-tree write is `.claude.json` at the parent
(D2) — the test must pin exactly that single out-of-tree path and nothing else.

**Verified already-safe (no delta needed):** a mistaken `CLAUDE_CONFIG_DIR`
pointed at a dir holding a foreign settings.json is REFUSED (`_load_json_object`
→ `_UNPARSEABLE` → `skipped-unparseable`), never clobbered; crash mid-edit is
contained by `atomic_write_text` (mkstemp sibling + fsync + `os.replace` +
parent-dir fsync → previous-or-new, never torn); re-install is idempotent at
either location. The explicit `--claude-dir` kwarg still overrides the env, so
tests stay hermetic. This defense is real and the plan cites it correctly.

---

## Wave 2 — U-PROFILE / U-PROFILE-VERB binding deltas

### D5 — The golden pin is NOT byte-identity of REAL output; §5-item-5 is wrong as written. (flaky golden is worse than none)

`install_agent_assets` output is NOT deterministic across machines/commits/CI-OS.
Three independent sources, all verified:

1. **`sys.executable`** — an ABSOLUTE, machine- AND OS-specific interpreter path
   is embedded in EVERY hook command (`_hook_python`: backslash→forward-slash +
   `shlex.quote`, so the exact bytes differ Windows vs POSIX) AND in
   `_MCP_SERVER_ENTRY["command"]`.
2. **`__version__`** — the git-sha-bearing wheel version (`0.11.0+g<sha>`) is
   stamped into `.hpc-agent-manifest.json` (`_write_asset_manifest` L1127/1131) —
   it changes every commit.
3. **The ambient clusters config** — `permissions.deny` rules derive from
   `_configured_cluster_hosts()` (packaged default + `HPC_CLUSTERS_CONFIG` +
   `~/.hpc-agent/clusters.yaml`), so the deny block differs on a dev box vs CI vs
   a user with real clusters.

A golden "captured from the REAL pre-refactor output" is flaky on all three and
non-portable across the Windows/Linux CI legs. §5-item-5's own premortem target
("captured from the REAL pre-refactor output, not hand-written") is therefore the
WRONG instruction.

**Binding:** make it a golden-of-a-pure-function, not golden-of-real-bytes. Drive
BOTH the pre-refactor body and `ClaudeCodeProfile.render()` through IDENTICAL
HERMETIC inputs — a PINNED fake interpreter string, a pinned version, a pinned
`HPC_CLUSTERS_CONFIG` fixture, a fixed `claude_dir` — and assert
`render(inputs) == old_body(inputs)`. Scope the golden to the settings.json /
`.claude.json` RENDER; EXCLUDE the `__version__`-stamped manifest (or normalize
the version). That proves the refactor inert without pinning machine noise.

### D6 — The trust boundary needs a STRUCTURAL pin, not prose. (the doctrine the whole plan lives on — §5-R3)

§5-R3 is correctly identified as load-bearing, but it is CONTAINED with prose
("the profile describes PROVIDERS TO WIRE, never CAPABILITIES CLAIMED") plus
reliance on the kit's `declared == detected == behaved`. By this repo's own
mechanization axis — and the daemon premortem's explicit F6/F8 finding that
"never-X is prose, not a pin" fails review — prose is insufficient for the
guardrail the plan says it lives or dies on. The named failure shape (a
`capabilities:` field the profile self-asserts that some code reads as truth)
must be made UNREPRESENTABLE, not merely discouraged.

**Binding, two pins:**
- (a) `HarnessProfile` is a FROZEN type with a CLOSED field set, pinned by a
  shape/schema test that goes RED on adding any capability-shaped field
  (`capabilities` / `provides` / `grants` / `trust` / `conformant`). The type
  cannot carry a self-assertion.
- (b) A consumer-trace pin (grep/AST, mirroring `test_no_unlock_affordance_...`)
  asserting NO gate / `journal.py` / conformance-kit code branches on
  profile-presence or verb-output as capability evidence. "No code reads 'profile
  installed' as 'capability present'" becomes a fired test, not a sentence.

### D7 — The read verb must emit NEUTRAL, machine-independent descriptors. (trust leak + determinism, coupled)

`harness-activation-profile` must return needles + NEUTRAL event-semantics +
LOGICAL asset names + the `mcp-serve` ARGV TEMPLATE — and must NOT leak the
resolved `sys.executable`, the rendered `bash -c` command strings, or the
host-scoped `Bash(ssh *<host>*)` deny rules. Two reasons in one: (i) those carry
machine + cluster specifics, so a verb-output schema/snapshot test would be flaky
(the D5 disease, now in a query); (ii) they are host/config evidence a foreign
harness could scrape from a projection surface. A neutral descriptor list is both
deterministic and leak-free.

**Binding:** the verb's output schema forbids absolute paths / rendered
command strings / resolved deny rules; `side_effects=[]`; the payload is the
neutral inventory only. (This also keeps `agent_facing=False`
installer-facing-only, as the plan intends.)

### D8 — The needle-embed obligation is a CONTRACT on the FOREIGN renderer, not just our test. (§5-R2)

§5-R2 correctly flags that a foreign profile rendering a DIFFERENT command shape
(not `python -m …`) must STILL embed the needle or `_find_hook_entry_index` /
`_needle_installed` silently orphan the hook (verified: both match on the module
path substring in the command string). But the plan's containment — "U-PROFILE's
needle-embed pin" — pins only OUR `ClaudeCodeProfile`. A foreign renderer that
drops the needle installs hooks the capability probe can never see → the harness
reports capabilities absent while its hooks are wired (a false PARTIAL, and worse,
a self-heal/re-find that never converges).

**Binding:** the profile/verb contract must STATE, as a normative obligation on
any foreign renderer, "the rendered hook command MUST contain the descriptor's
needle substring," and the conformance kit's foreign-adapter path must assert
needle-presence in the FOREIGN-rendered settings (behavioral), not just our
golden. The descriptor carries the needle as data precisely so the obligation is
checkable.

### D9 — Name the measure-then-decide trigger as a CONCRETE artifact, and resolve the chicken-and-egg. (sequencing honesty)

The cut-line trigger ("build (b) when a foreign harness or the Wave-C adapters
exercise it") is a vibe as stated — nobody can point at the moment "a foreign
harness materializes." The plan itself notes FULL is a soft PREREQUISITE of the
Wave-C adapters (you cannot run `test_capability_relay.py` against a foreign
provider it cannot install) — which means FULL cannot be gated BEHIND Wave-C
proving it; it must be the FIRST unit OF Wave-C.

**Binding (clarification, not a blocker):** state the trigger as a verifiable
artifact — "a Wave-C foreign adapter (memo T6/T7/T8) is CHARTERED and its
`detect_capabilities` install step has no supported seam (a blocked PR / a red
conformance test)" — and state that FULL is then built as the FIRST unit of that
Wave-C work, not after it. Absent that artifact, (b) stays design-of-record,
shelved (the daemon-package precedent the plan already cites). That is the honest,
firing measurement.

---

## Completeness sweep (lens 6) — what the inventory got right, and one stale pointer

- **Journal-home surface** — the real omission → D1.
- **MCP-relocation** → D2. **uv-tool hook-env / `HPC_STOP_HOOK_APPEND` activation
  markers** — correctly inventoried (Surface 5, harness-neutral today).
- **Project-scoped `.claude` write:** `agent_assets.py:680` cites
  "`ops/memory/interview.py`'s project-scoped Bash grant (#190)" — but
  `interview.py` no longer contains ANY `.claude` / settings / allow write (grep
  clean). The comment is STALE. Verified: the ONLY settings.json WRITE path in
  the tree is `install_agent_assets` (home-global). No parallel project-scoped
  write-resolver exists → no missed activation surface. Fix the stale docstring
  opportunistically; not a blocker.
- **MCP-client / claude.ai-desktop registration, keybindings, statusline** —
  correctly out of scope (declined; MCP-is-projection; `install_agent_assets`
  writes none of them). No lockout there.
- **Windows-vs-POSIX settings.json path shapes** — handled by `_hook_python`
  (the backslash→slash + `shlex.quote` fix); for the golden this is the D5
  machine-path issue, and for a foreign profile the D7 neutrality boundary.
- **Note (not a delta):** the conformance adapter (`claude_code.py`) HAND-WRITES
  settings.json into a `CLAUDE_CONFIG_DIR` temp dir rather than calling
  `install_agent_assets`, so it exercises the READ side under relocation but NOT
  the installer write side — U-ENV's new fire-test is therefore genuine net
  coverage (good), and the adapter is a latent second source-of-truth for the
  settings shape that Wave 2's golden should ideally become the reference for.

---

## Drift log

- **Created 2026-07-17.** Adversarial premortem of `harness-activation-2026-07-17.md`
  (6f6a945b) at user direction ("let's plan this out properly"). Six lenses;
  claims verified against `main @ c893d2fa`. Verdicts: Wave 1 GO-WITH-CHANGES
  (D1-D4), Wave 2 GO-WITH-CHANGES (D5-D9). Sharpest finds: the journal-home third
  resolver (D1, a data-relocation trap the plan is silent on) and the three-source
  golden nondeterminism (D5, which makes §5-item-5's instruction wrong as
  literally written). D1/D2/D3/D4 bind U-ENV and go to the coordinator for
  integration-time application (U-ENV is being BUILT concurrently — these are
  spec-tightenings, not a doc rewrite). No STOP on either wave.
