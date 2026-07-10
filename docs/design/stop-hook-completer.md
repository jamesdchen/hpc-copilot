---
status: implemented (dark — capability-gated; see drift log 2026-07-10)
---
# Stop-hook completer — rejector → completer (design)

**Status: DESIGN (2026-07-10, ruling user-ratified 2026-07-10). Build is
post-run-#12 batch item 1** — the flagship of the refusal + hook-bounce
inventory (Day-2 audit feeds the class mapping below before code lands).
Cite `path::symbol`, never line numbers. Drift log at the foot.

## Why now

`_kernel/hooks/relay_audit_stop.py` is a REJECTOR: every finding — a
contradicted number, an undischarged relay-due marker, an unjournaled
decision-state claim — blocks the stop once and forces a full extra model
turn to re-say what deterministic code already knows verbatim. That bounce
is the single most common source of extra-model-turn latency in proving
runs, and it is also a *trust downgrade*: the forced re-relay is model-typed
text, which is exactly the surface the paraphrase pass (G1) exists to police.
Code already holds the artifact (the render file, the journal value, the
marker's key tokens). The ruling (2026-07-10): **when code can complete the
relay itself, it appends the artifact directly — a code-appended render is
model-untouched and therefore MORE trusted than an LLM relay — and the
bounce survives only where judgment is genuinely required.**

This is the poka-yoke doctrine applied to the hook itself: compose/default/
auto-remedy what code can; refuse only at trust boundaries; a converted
refusal becomes a never-fires assertion.

## The three classes (the ruling)

### 1. Omission class → COMPLETE (no bounce)

The finding is that something owed was not said, and code holds the exact
content owed. Members (today's passes):

* **relay-due markers** — `notebook-status` terminal verdicts and
  `notebook-audit-view` per-section `view_sha12` markers
  (`state/notebook_audit.py::read_undischarged_relay_markers`), plus the
  campaign-scope markers the same discharge pass scans;
* **brief surfacing** — a computed brief (morning brief, park brief) that
  reached disk but not the human.

Completer behavior: code APPENDS the owed artifact to the turn's visible
output (channel: D1), records the discharge
(`state/notebook_audit.py::record_relay_discharge`) with completer
provenance (D3), journals the append — and the stop PROCEEDS. Zero extra
model turns. The appended render for an audit-view marker is the render
file's own content via the ONE composer (the unified-render ruling,
`docs/design/mcp-elicitation.md` drift log 2026-07-10) — verbatim by
construction, so the G1 paraphrase class cannot exist for appended content.

### 2. Violation class → APPEND CORRECTION; bounce only on a poisoned decision

The finding is that something said contradicts the durable record. Members:
the rule-10 contradiction pass (`number`/`state`/`run_id` mismatches from
`ops/decision/verify_relay.py::verify_relay` / `verify_notebook_relay`), the
decision-state-claims pass (`_decision_state_findings`), and the paraphrase
pass (`_paraphrase_findings` — the correction is the verbatim render lines).

Completer behavior: code appends a correction UNDER the claim — a labeled,
code-authored block quoting the claim and the journal's actual value
(`journal: <value>` — the same `nearest_source_value` the reason string
carries today). The human reads the correct value in the same turn; the
model's error is visible but neutralized. The stop proceeds, EXCEPT:

**The poisoned-decision test.** Bounce (today's block-once) survives when
the contradicted claim feeds a PENDING decision — an unresolved decision
brief for the same scope whose proposal the human is about to `y` on the
strength of the wrong number/state. Test, mechanically: the scope's LATEST
persisted brief (`state/decision_briefs.py::read_briefs` /
`latest_brief_for_block` — the brief store persists in BOTH driving modes)
has no subsequent committed `y` in the decision journal, and the finding's
claim tokens intersect that brief's content. Deliberately NOT keyed on the
run journal's `pending_decision` marker — that marker exists only under
block-drive (`state/decision_briefs.py` records this verbatim), so a test
built on it would silently never fire for MCP-direct-driven runs, which is
the default skill path. And NOT keyed on
`is_latest_committed_greenlight` being false — that is true of essentially
every mid-flight run and discriminates nothing. A correction footnote under
a poisoned proposal is not enough — the proposal itself must be re-relayed,
which is model work, hence the bounce stays.

Audit-scope violations (`verify_notebook_relay` findings) ALWAYS take the
append path: there is no per-audit brief store, so no poisoned-decision
test applies to them (the sign-off boundary has its own gates).

**Sign-off echo** (`_sign_off_echo_findings`, laundered authorship) —
RULED (2026-07-10, user: "the completer just does its job"): violation
class, append-only. The correction is a code-appended disclosure under the
attestation ("this sign-off wording echoes a model-drafted line — the
attestation stands only if the human re-affirms in their own words"), and
the completer NEVER bounces for an echo — the model cannot repair
authorship, so a forced model turn produces nothing the disclosure doesn't.
The disclosure itself carries the re-attestation request when the sign-off
is load-bearing.

### 3. Judgment class → BOUNCE (unchanged)

The finding requires the model to produce something code cannot: an
unanswered direct question from the human, a driver continuation the agent
abandoned mid-chain (ending the turn with work parked that only the model
can resume). These keep today's block-once seam verbatim
(`stop_hook_active`, never loops, never hard-blocks).

## Settled decisions

### D1 — The append channel: hook `systemMessage`, capability-gated

The Claude Code hook output contract carries a `systemMessage` field —
code-authored text the harness displays to the user — alongside (or instead
of) a `decision`. The completer emits
`{"systemMessage": "<owed artifact / correction>"}` with NO block decision:
the stop proceeds and the human sees the code-appended content in the same
turn. This is the model-untouched property the ruling values: the text never
passes through the model at all.

Two hard caveats, both build-time obligations:

* **Verify display, don't assume it.** A `systemMessage` the harness
  swallows is the declared-but-dark class. `systemMessage` has ZERO evidence
  in this repo today — `docs/internals/harness-contract.md`'s
  relay-enforcement section knows only `decision`/`reason` — so it is an
  unverified external dependency until probed. Build step 1 is a
  conformance probe (the `conformance/` kit): a NEW capability key
  (`stop-hook-append`) with its own detection seam added to
  `ops/harness_capabilities.py`'s result model, plus the harness-contract
  version bump — the existing four-capability model has no append seam
  (`trusted_display` sits at `"unknown"` for exactly this no-seam reason).
  The probe MUST cover both output shapes: `systemMessage` alone
  (stop proceeds) AND `systemMessage` combined with `decision: "block"`
  (the D2 mixed-class output) — display behavior may differ between them.
  Where the capability is absent, the completer DEGRADES TO THE REJECTOR —
  today's block-once bounce — never to silence. This mirrors the MCP
  elicitation display-receipt gap (`docs/design/anti-vendor-lockout.md`):
  the harness contract gains the append channel as a declared capability so
  a second harness can conform.
* **The append is display, not transcript.** A `systemMessage` does not
  join the assistant transcript, so a LATER stop's mention scan will not see
  it. Discharge must therefore be keyed to the append event itself (D3),
  never re-derived from transcript text.

### D2 — Completion never blocks; classes compose in one output

One stop can carry findings from all three classes. Composition rule:
completions and corrections are gathered into one `systemMessage`;
if ANY judgment-class finding (or poisoned-decision violation) exists, the
output ALSO carries `{"decision": "block", "reason": ...}` for those
findings only — the completed/corrected findings are already discharged and
MUST NOT be re-stated in the reason (or the model re-relays what code just
appended, resurrecting the latency this design kills). On a
`stop_hook_active` forced continuation, completions still run (they never
block, so they are loop-safe by construction — a strict widening of today's
"discharges still land on forced stops" behavior); blocks never re-fire.

**Discharge is gated on confirmed display.** A completion whose
`systemMessage` rides a BLOCKED output is completer-discharged ONLY where
the conformance probe has confirmed the harness displays `systemMessage` on
a blocked stop for that harness (D1). Where unconfirmed, mixed-class stops
DEFER completions to the post-continuation stop (which is never blocked —
the block-once seam — so the plain append path applies). Otherwise a
swallowed systemMessage plus an already-recorded discharge would silently
and permanently lose the owed verdict — the exact failure the marker
exists to prevent.

### D3 — Discharge provenance: `discharged_by`

`record_relay_discharge` records gain `discharged_by: "completer" |
"relay"` in `resolved` (append-only; existing records read as `"relay"`).
This is the automatability metric's data: the journal-derived count of
completer discharges vs model-relayed discharges IS the measure of latency
killed, and it keeps the audit trail honest about which artifacts the human
saw as code-appended text vs model prose.

### D4 — The completer composes from files, never from model text

Appended content sources, in order of preference: the trusted render file
for view markers — selected BY the marker's `view_sha12`, which is embedded
in the trusted render's filename (`state/notebook_audit.py::`
`RENDER_RELAY_DUE_RECORD_KIND` records this), so the mapping is
`view_sha12 → the .hpc/renders/<audit_id>/*.md file whose name carries it`,
never a glob-all (the `_paraphrase_findings` corpus glob is NOT the model
here); a marker whose sha matches no on-disk render degrades to the
token-level floor below; the journal
record's own fields for status/decision-state corrections; the marker's
`key_tokens` verbatim as the floor. The completer NEVER quotes the model's
final text back except to label the claim being corrected. Caps ride along
(the `_MAX_*` posture): total appended bytes bounded; over-cap content
degrades to the token-level floor plus a file reference.

## What this kills / what it keeps

* Kills: the extra model turn per omission (the most common bounce), the
  paraphrase risk on re-relays, the "corrected relay forgets a second
  marker" re-bounce chain.
* Keeps: block-once loop safety, fail-open-everywhere (every completer pass
  wraps like today's passes — a completer crash degrades to the rejector,
  then to silence, never a wedge), the no-scaffold discovery probe, and the
  verb-level `unverifiable` policy (still not a hook concern).

## Test plan (sketch)

* Unit: class routing (each existing finding kind → its class), the
  poisoned-decision test (pending proposal + token intersection), D2
  composition (mixed-class stop), D3 provenance on discharge records,
  D4 render-sourced append with cap degradation.
* Conformance: `stop-hook-append` probe present/absent → completer/rejector;
  the relay-triples suite (`tests/conformance_kit/test_relay_triples.py`
  pattern) gains the completer leg.
* Regression: forced-stop completions still discharge; a completed marker
  never re-blocks a later stop.

## Open questions

* Whether `systemMessage` renders in ALL Claude Code surfaces this project
  drives (CLI, VS Code, web) — the conformance probe answers per-harness,
  but the v1 gate should be verified on the primary CLI surface first.
* ~~Echo-class placement~~ — RULED 2026-07-10 (violation class, append-only,
  never bounces; see §2).

## Drift log

* **2026-07-10 — BUILT (rejector → completer, capability-gated dark).** The
  completer path is fully implemented and tested but DARK by default (D1's safe
  landing): with no harness declaring the capability, `build_hook_output`
  degrades to the REJECTOR byte-for-byte (`_rejector_output`), so every
  pre-existing test still passes unchanged.
  * **Capability 5 `stop-hook-append`** — added to `ops/harness_capabilities.py`
    (`detect_stop_hook_append` / `detect_stop_hook_append_on_block`, the ONE
    detection home the hook imports) and reported by the `harness-capabilities`
    verb as a fifth `CapabilityEntry` + tier consequence. Tri-state like
    `trusted_display`: no passive install seam exists, so it reads `"unknown"`
    until a conforming harness activates it. **Deviation from D1 as written:** the
    activation seam is TWO env markers (`HPC_STOP_HOOK_APPEND`,
    `HPC_STOP_HOOK_APPEND_ON_BLOCK`), NOT the `conformance/` probe. Reason: the
    doc names the conformance probe as "build step 1 … the systemMessage-display
    conformance probe is follow-up" — the mechanize-now order is the completer
    machinery FIRST behind an explicit opt-in, the passive probe seam SECOND. The
    two markers cover D1's two required output shapes (proceeding vs blocked). The
    harness contract bumped 1.0.0 → **1.1.0** (additive minor — a new capability;
    doc line + `HARNESS_CONTRACT_VERSION` + kit `CONTRACT_VERSION` stay three-way
    equal).
  * **D3** — `state/notebook_audit.py::record_relay_discharge` gained
    `discharged_by` (`DISCHARGED_BY_RELAY` default / `DISCHARGED_BY_COMPLETER`);
    the field is additive (pre-D3 records read `"relay"`) and NOT part of
    `_marker_key`, so it never changes which marker a discharge closes.
  * **D4** — `_compose_owed_artifact` sources a render view-marker's owed content
    from the trusted render file selected BY `view_sha12` in its filename
    (`_render_by_view_sha`, the ONE composer — verbatim by construction), degrades
    to the token floor + a file reference over the append cap
    (`_MAX_APPEND_ARTIFACT_BYTES`), and falls to the token floor for a
    `notebook-status` terminal (no render file). Never quotes model text.
  * **Poisoned-decision test** — `_is_poisoned_decision` keys on
    `state/decision_briefs.py::read_briefs` (latest brief with no subsequent
    committed `y` + claim-token intersection with the brief content); explicitly
    NOT on `pending_decision` and NOT on `is_latest_committed_greenlight`. Bias to
    the append path everywhere (any error → not poisoned). It is itself
    block-once (never fires on a `stop_hook_active` forced continuation).
  * **Judgment class — no members here (scope note, not a deviation).** §3's
    judgment bounces (unanswered question, abandoned continuation) live in the
    SIBLING Stop guards (`decision_rendezvous_stop_guard`,
    `skill_return_stop_guard`), not this hook, so the only surviving bounce in the
    completer is the poisoned-decision one.
  * **Decision-state / paraphrase violations** carry an empty/verb-category
    `claim` and take the APPEND path in practice (a paraphrase/audit-scope finding
    never poisons — no per-claim value token to intersect a brief; the sign-off
    boundary has its own gates), matching §2's "audit-scope violations ALWAYS take
    the append path."
