# The harness contract

The normative specification a **conforming harness** implements so hpc-agent's
authorship, relay, and backgrounding guarantees hold. Claude Code is ONE
implementation (hooks over `~/.claude/settings.json`); the scheduled v1.5
jupytext render is intended to be a SECOND. This page is the vendor-lock-in
defense: the audit loop is defined against these three capabilities, not
against Claude Code, and implementations compete under the contract
(`docs/design/notebook-audit.md`, "THE HARNESS CONTRACT").

This page is **normative** for the write API in §2 (the frozen record schema,
the no-scaffold precondition, the provenance contract, fail-open) — the
PLUGIN wave and any second harness implement against that bullet list. The
rest is descriptive: wherever a line is mechanizable, the source of truth is
the cited code seam and the test that pins it (see the enforcement pointers),
not this prose.

The load-bearing principle throughout: **a guard the LLM itself satisfies is
not a guard** (`engineering-principles.md`). Every capability below exists to
move a trust anchor OUT of the model's reach; where a harness cannot provide
it, the machinery degrades HONESTLY to a weaker, named tier rather than
pretending the guarantee still holds.

## Contract version

Contract version: 1.2.0

This is the SemVer of the contract this page specifies. It has ONE home in
code — `HARNESS_CONTRACT_VERSION` in `ops/harness_capabilities.py` — and this
line, that constant, and the conformance kit's stamped verdict
(`hpc_agent.conformance.report.CONTRACT_VERSION`) are pinned mutually equal by
`tests/contracts/test_harness_contract.py`. The `harness-capabilities` verb
reports it to a negotiating harness as its `harness_contract_version` result
field. The conformance verdict line names only the MAJOR — `conforming:
harness contract v1 (kit hpc-agent X.Y.Z)` (`docs/design/conformance-kit.md`
D-K6).

**Deprecation posture.** Within major 1 the contract is **ADDITIVE-ONLY**: a
new capability, a new assertion, or a new conforming implementation SHAPE (as
capability 2's ACT gained the response-gateway alongside the Stop hook) may
land as a MINOR bump, but only if both reference adapters stay conforming — a
previously-conforming harness failing a minor is the definition of a breaking
change and forces a MAJOR. Capability REMOVAL never happens. The sha
canonicalization (below) is the canonical MAJOR trigger: changing it
drift-revokes every stored attestation, so it can only ever ship as v2 with a
`canon_version` field on new records, never silently.

## The three capabilities

A conforming harness MUST provide all three. Each names what it provides, the
trust property it earns, and what degrades — to which exact seam — when it is
absent.

### Capability 1 — the attributed utterance log (the out-of-band human-utterance log)

**Provides.** An append-only log of the text a human verifiably TYPED, written
by the harness at the moment of input, through the write API in §2. The model
never mediates the write: the harness is the writer, the log is the reader's
trust anchor. When the harness knows WHOSE session it is, it MAY ATTRIBUTE the
write — appending through the actor-suffixed locator (§2) so the record carries
harness-asserted authorship — while the record schema, provenance contract,
no-scaffold rule, and fail-open semantics are unchanged (attribution rides the
locator, never a fourth record field). Core never verifies the attribution: the
claim is harness-asserted, the tier is named ("The honest trust limit", below).

**Trust property earned.** The FULL-STRENGTH authorship tier. With the log
present, the human-authorship gate
(`ops/decision/journal.py::_assert_human_authorship`) and the scope-unlock gate
(`::_assert_unlock_authorship`) and the notebook sign-off gate
(`::_assert_signoff_authorship`) require every REQUIRED_CALLER value token — and
every unlock rationale, and every sign-off — to DERIVE from this log. Journal
`response` fields, which the driving agent itself authors, carry no authorship
weight in this mode. This is the lock the verb-only v1 could only stage: the
same model that fabricates a value can fabricate a quote, but it cannot forge
a log the harness wrote before the model saw the prompt.

**Degrades when absent.** The tier machinery falls back to the
JOURNAL-RESPONSE FRICTION TIER at the exact seam
`ops/decision/journal.py::_harness_human_texts` **returning `None`** (no log /
older session / hook not installed). In that tier the evidence source is the
journal `response` fields — agent-authored — so the gate mechanically kills the
OBSERVED rationalization class (hand-injected fields, bare-`y` laundering) but
a determined agent could still fabricate a human quote. The fallback is
deliberate back-compat: refusing it outright would break every pre-hook
install. The honesty is that the two tiers are NAMED and different, not a
uniform claim.

**Attribution degrades the same way (multi-actor, MH2).** The full-strength tier
becomes ATTRIBUTION-CONDITIONAL when interview.json declares MORE THAN ONE actor.
Each authorship gate then draws its evidence from the SESSION ACTOR'S own log
only — `ops/decision/journal.py::_harness_human_texts` passes the actor through
to `state/utterances.py::read_utterances(experiment_dir, actor=<slug>)`, an
actor-scoped read that deliberately EXCLUDES the unsuffixed log (anonymous text
satisfying an actor-specific check is the cross-actor laundering channel). So a
harness that fully honors §2 v1 but writes UNATTRIBUTED — into the unsuffixed
log — no longer earns the full-strength tier in a declared-multi-actor
experiment: its writes land where the actor-scoped evidence pool cannot see
them, and the gate falls to the JOURNAL-RESPONSE FRICTION TIER at that same seam.
This is disclosed, not accidental. Zero or one declared actor → no scoped read,
no degradation, byte-identical to today.

### Capability 2 — the relay/verbatim enforcement point

**Provides.** A seam at which the harness can inspect the FINAL agent-visible
message and force the agent to continue (re-answer) instead of ending the turn.

**Trust property earned.** Conduct rule 10 becomes a seatbelt, not a
suggestion: numbers and state the durable journal does not support cannot reach
the human unchallenged. `verify-relay` mechanized the audit as a pure verb, but
nothing made a driving agent RUN it; this capability is what runs it at the one
sound moment — the outgoing message is final and the transcript is on disk.

**Degrades when absent.** The relay audit reverts to the VERB-ONLY posture: an
agent (or a human) may still invoke `hpc-agent verify-relay`, but an unaudited
relay reaches the human (the proving-run-#3 failure: "running" relayed while
the journal said "failed"). No exception, no wedge — just the weaker guarantee.

### Capability 3 — backgrounding / wake

**Provides.** The ability to detach a long-running block into a worker that
survives the turn, and to wake / re-invoke the driving agent when the worker
reaches a terminal or an anomaly.

**Trust property earned.** The detached-worker machinery (S2/S3/S4 detach,
campaign reconcile self-chaining, the driver watchdog) can run cluster waits —
staging, canary polls, harvest — outside the synchronous chat turn without the
human idle-blocking, while the journal remains the durable rendezvous the woken
agent reads to resume.

**Degrades when absent.** The blocks collapse to synchronous, in-turn execution:
a submit that would detach at S2 instead blocks the turn for the whole canary
poll. Correctness is unaffected (the journal is still the source of truth); only
the wall-clock ergonomics degrade. A harness that cannot background at all still
runs the full pipeline, just without the detach optimisation.

## The utterance-log WRITE API (normative)

The one capability a second harness MUST implement byte-for-byte. The reference
implementation is `state/utterances.py::append_utterance` (the SOLE writer); a
conforming harness writes records the reader
(`state/utterances.py::read_utterances`, consumed by
`ops/decision/journal.py::_harness_human_texts`) accepts. The obligations, as
the bullet list the PLUGIN wave implements against:

- **Storage locator.** `<journal home>/<repo_hash>/utterances.jsonl`, derived
  exactly as `state/utterances.py::utterances_path`:
  `current_homedir() / repo_hash(experiment_dir) / "utterances.jsonl"`.
  `current_homedir()` (`state/run_record.py`) resolves the journal home:
  `HPC_JOURNAL_DIR` env if set-and-non-empty, else the module `HPC_HOMEDIR`
  attribute, else `~/.claude/hpc`. `repo_hash(experiment_dir)`
  (`state/run_record.py`) is the path-form-invariant
  `sha256(canonicalized resolved dir)[:12]`. The locator MUST reuse these two
  derivations, never re-implement the hash — a divergent hash writes into a
  namespace the reader never looks up.
  **Attributed variant (additive, MH2).** When the harness knows the session's
  actor, it writes instead to `<journal home>/<repo_hash>/utterances.<actor>.jsonl`
  — the SAME locator with an actor-slug segment, produced by the SAME
  `state/utterances.py::utterances_path(experiment_dir, actor=<slug>)` (no
  re-derived path). The slug rides into the filename, so it is validated by the
  shared filesystem-safe tag class (`state/utterances.py::_actor_utterances_name`
  → `state/scopes.py::validate_tag`); an invalid slug FAILS OPEN to the
  unsuffixed log. Reads are UNION by default —
  `state/utterances.py::read_utterances(experiment_dir)` merges the unsuffixed
  log and every `utterances.<actor>.jsonl` oldest-first by `ts`, so every
  identity-less consumer still sees all human text; an actor-scoped read
  (`read_utterances(experiment_dir, actor=<slug>)`) returns that actor's file
  ONLY, never the unsuffixed log. Attribution rides the LOCATOR, never a fourth
  record field — the frozen schema below is UNCHANGED, holds PER FILE, and the
  single-actor world stays byte-identical (no actor configured → no suffixed
  file is ever created).

- **Frozen record schema.** One JSON object per line, sorted keys, append-only,
  oldest-first. Exactly three fields:
  - `ts` — ISO-8601 UTC timestamp of the write.
  - `sha256` — the SHA-256 hex digest of the FULL raw text, computed BEFORE any
    capping, so a capped entry still carries a verifiable fingerprint of the
    whole utterance.
  - `text` — the raw text, capped at `MAX_UTTERANCE_BYTES` (4096) UTF-8 bytes,
    truncated on a CODEPOINT boundary (never mid-codepoint —
    `raw[:max].decode("utf-8", errors="ignore")`).
  Serialize with `json.dumps(record, sort_keys=True)` + `"\n"`. No other fields;
  the reader tolerates unknown keys but the writer MUST NOT add them.

- **No-scaffold precondition.** Write ONLY when the namespace directory
  (`<journal home>/<repo_hash>/`) ALREADY EXISTS — i.e. some prior hpc-agent
  state write already claimed this cwd as an experiment repo. NEVER create the
  namespace. The capture writer is installed user-globally and fires in ANY repo
  the human works in; scaffolding a `<repo_hash>/` dir for an arbitrary cwd
  leaks namespace directories across the user's whole filesystem (proving-run-#3
  finding g). Both the writer and the reader are non-creating; `utterances_path`
  is deliberately NOT `journal_dir()` (which mkdirs + writes `repo.json`).

- **Provenance contract.** Only HUMAN-TYPED text may enter the log — this is the
  entire trust value; admitting anything else hands the authorship gate's anchor
  back to the model. Two obligations:
  - The writer MUST run OUT-OF-BAND relative to the LLM's tool/CLI surface — it
    fires from the harness input path, before the model sees the input, NOT as a
    verb the model can call. (In Claude Code: a `UserPromptSubmit` command hook,
    `_kernel/hooks/utterance_capture.py`; and a `PostToolUse` hook for typed
    AskUserQuestion answers, `_kernel/hooks/answer_capture.py`.)
  - The writer MUST FILTER harness-injected and agent-authored text. Reference
    filters:
    - `state/utterances.py::is_harness_injected` (the PUBLIC write-API symbol;
      `HARNESS_INJECTION_RE` is the regex form — one definition every
      conforming writer imports, never a re-derived copy) — a prompt that
      OPENS with a harness-injection tag (`<task-notification>`,
      `<system-reminder>`, `<local-command-caveat>`, `<command-name>`,
      `<command-message>`, `<local-command-stdout>`) is dropped: notification
      text is agent-influenced, so admitting it is a laundering channel into the
      trust anchor. (A human merely quoting a tag mid-text still lands.)
    - `_kernel/hooks/answer_capture.py::_is_clicked` (over `_typed_texts`) — a
      CLICK on an agent-authored option label is NOT captured (the agent wrote
      that text); only the free-text "Other" residue and typed annotation notes
      are. A multi-select composed entirely of offered labels is skipped; if any
      part was typed, the whole answer is captured.

- **Fail-open semantics.** Any error — missing namespace, unwritable log,
  encoding error, malformed payload, empty text — degrades to a CLEAN NO-OP,
  never an exception. A broken capture channel MUST degrade to the friction tier
  (capability 1 absent), never wedge input submission or the harness. The
  reference writer catches `(OSError, ValueError, UnicodeError)` and returns
  `None`; the reader skips blank/corrupt lines so one bad line never strands the
  trail.

**The LLM must never gain a sanctioned write call.** This API is importable by
HARNESS-SIDE code only. There is NO CLI verb, NO MCP tool, NO primitive, NO
chain/next_block/skill affordance that writes an utterance — appending one is
the harness's exclusive act. A write verb would let the model author its own
authorship evidence, which is precisely the lock-1 posture this API exists to
deny. The contract test in `tests/contracts/` pins the absence of any such verb
in the operations registry.

## The sha canonicalization (normative)

Every content/view/story sha in the system is computed the same way, and a
second implementation MUST reproduce it byte-for-byte or every recompute
lock reads drift:

- **Payload form**: JSON via Python-`json.dumps` semantics with
  `sort_keys=True` (keys ordered by Unicode CODE POINT — note this differs
  from RFC 8785/JCS, which orders by UTF-16 code units), compact separators
  `(",", ":")`, `ensure_ascii=False`, UTF-8 encoded.
- **Digest**: SHA-256 over that encoding, lowercase hex.
- **Source-text shas** (`section_sha`, `module_sha`, linked-source shas) are
  SHA-256 over NORMALIZED source text instead: newlines unified to `\n`
  (CRLF and lone CR), trailing whitespace stripped per line — nothing else
  (`state/audit_source.py::normalize_source` is the reference).

This form is deliberately NOT RFC 8785: it predates any cross-language
consumer, and changing it would move every stored sha (drift-revoking all
existing attestations). The recorded escape hatch: if a non-Python
implementation ever needs to recompute these shas, adopt JCS under a
`canon_version` field on NEW records — never silently.

## The relay-enforcement capability (detail)

The reference implementation is the Stop hook `_kernel/hooks/relay_audit_stop.py`.
On the turn-final event a conforming harness MUST be able to:

1. resolve the cwd repo's journal namespace WITHOUT creating it (no-scaffold) —
   no namespace → not an hpc repo → silent pass;
2. read the final agent-visible message text (the trailing run of assistant
   entries in the transcript);
3. find which journaled run ids that text actually NAMES (a claim is only
   attributable to a run the relay mentions);
4. run `verify-relay` per mentioned run;
5. on a CONTRADICTION mismatch (`number` / `state` / `run_id` — NOT the
   `unverifiable` kind, which a final message legitimately carries) BLOCK the
   stop ONCE with the itemized summary, forcing the agent to correct the relay
   to match the journal.

Loop-safety is mandatory: block a given stop AT MOST ONCE
(`stop_hook_active` → no-op), never hard-block a session. Fail-open everywhere
(missing transcript, no run mentions, per-run audit error, any exception →
silent pass). The harness need not use a "Stop hook" specifically — it needs the
CAPABILITY: inspect the final agent-visible message and force one continuation.

## The honest trust limit

From `ops/decision/journal.py`'s own contract: the utterance log is out-of-band
relative to the MODEL's tool/CLI surface — that is the whole guarantee and the
whole boundary. It defeats the model fabricating its own authorship evidence. It
does NOT defend against filesystem-level attacks (a process editing
`utterances.jsonl` directly) or harness-config-level attacks (disabling the
capture hook) — those are OUT OF SCOPE, and a harness that cannot keep its own
config honest cannot be made honest by this contract. The SAME boundary covers
ATTRIBUTION (MH2): the actor an attributed write is stamped with is
harness-asserted, never verified — core runs no credential check, no signature
verification, no OS-user probe. Impersonating another actor — exporting someone
else's session-actor env, editing a `utterances.<actor>.jsonl` file directly, or
forging the harness config that supplies the slug — is the same class of
env/filesystem/harness-config attack as disabling the capture hook, OUT OF SCOPE
exactly as today. Attributed ≠ verified. The tier is stated, not
overclaimed: full-strength when the harness holds up its end, named friction
when it does not.

The AUTH boundary is the same class (anti-vendor-lockout T2): how a harness's
own processes authenticate to their model provider — e.g. the
`ANTHROPIC_API_KEY` a subscription user must export before any spawned worker
can run — is a harness-side concern, OUT OF SCOPE of this contract exactly as
disabling a capture hook is. Core neither checks, stores, nor forwards
provider credentials; a conforming harness earns no extra trust by naming its
provider, and an auth failure inside the harness is the harness's own named
friction, not a contract violation.

## The CLI is the invariant substrate

The `hpc-agent` CLI stays the invariant substrate under every harness (the
block-drive doctrine); MCP tools and skills are PROJECTIONS of it. A conforming
harness supplies the three capabilities AROUND that CLI — it does not replace
or fork the verb surface. This is what lets a second harness be additive: the
audit records, gates, and journal are identical; only the capability providers
differ.

**The harness-neutral runbook.** A foreign harness drives the four workflows
from `docs/generated/harness-runbook.md` — a prose-neutral projection of the
code-homed procedure (block sequences, decision points tagged code-vs-judgement
with their backing verbs, and the park → typed `y` → `append-decision` consent
boundaries), stated in CLI-verb vocabulary with none of the Claude-Code SKILL.md
idioms. That page is GENERATED (`scripts/build_harness_runbook.py`, a
`regen_all` step) from `_wire/spawn_contract.py::DECISION_POINTS` +
`infra/block_chain.py` — **edit `DECISION_POINTS`, never the runbook prose**
(anti-vendor-lockout T5/R5). It is a translation aid, NOT a trust surface: the
trust anchor stays the journal + the utterance log.

## The second-conforming-harness sketch

The ceiling this contract earns. A jupytext notebook render (scheduled v1.5,
`docs/design/notebook-audit.md`) is a projection over SEALED records (source +
template + execution receipt), built in the plugin/tools lane (jupytext never
enters core). It plays two roles, in order:

1. **The portability artifact** — audits readable anywhere, with no harness.

2. **A SECOND CONFORMING HARNESS** — a human typing into a notebook sign-off
   cell IS out-of-band from the LLM. A render that writes that typed text
   through THIS write API (§2 — same locator, same frozen schema, same
   no-scaffold, same provenance filter, same fail-open) provides the
   full-strength authorship tier with NO Claude Code anywhere in the loop. The
   sign-off is then an ordinary `append-decision` under the `notebook` scope,
   its value tokens deriving from a log the notebook harness wrote — the gate
   cannot tell, and need not tell, which conforming harness produced the
   utterance. The execution-receipt emitter that feeds the render remains ~30
   lines of caller-side convention.

This is the product claim the contract secures: "we also export notebooks," not
"we are one notebook tool" — every trusted thing (approval, sign-off, receipt)
is an attestation over a journal a conforming harness fed, and the harness is
swappable.

**The ingest verb's trust boundary (adversarial review F1).** The
`notebook-ingest-signoffs` verb that lands those sign-offs reads a human-edited
`.ipynb` — a file the AGENT can also author. If it wrote the tier-1 utterance log
on the agent's behalf, the agent's own words would become tier-1 human evidence
and defeat the authorship gate for every audit (the core no-utterance-writer pin
cannot see plugin primitives). So the boundary is enforced two ways: the verb is
`agent_facing=False` (a HUMAN-invoked CLI verb, never an agent tool), and the
utterance-log write is OFF by default — a sign-off still lands and the gate judges
it at whatever tier the existing log supports (the `_harness_human_texts` friction
tier when no hook-captured utterance matches). The full-strength authorship
channel is restored only by an explicit `write_utterance_log=True`, documented
HUMAN-INVOKED-ONLY: a human who typed in Jupyter then ran the ingest is a genuine
second harness; an agent setting that flag is violating the documented contract,
the same class as editing harness config. (The utterance log is append-only; a
concurrent doc edit is expected — re-read/retry.)

## Capability negotiation

A conforming harness's capability set is not a self-asserted manifest — it is
**DETECTED**, and the declaration IS what code can verify. This is the LSP-style
negotiation posture: a harness advertises what it can do, but here "advertise"
means "expose the seams code reads", never "claim in prose". The read-only
`harness-capabilities` verb (`ops/harness_capabilities.py`) is the negotiation
surface; it reports the capabilities as code OBSERVES them, each against a
named seam:

- **Capability 1 (utterance log)** — detected from the installed input-capture
  hooks in `~/.claude/settings.json` (honoring `CLAUDE_CONFIG_DIR`), matched by
  their module-path needles in `agent_assets.py`
  (`_UTTERANCE_CAPTURE_NEEDLE`, `_ANSWER_CAPTURE_NEEDLE`) through the ONE canonical
  entry-matcher `agent_assets._find_hook_entry_index` — never a re-derived scan —
  plus the repo's utterance-log presence (`state/utterances.py::utterances_path`,
  non-creating) and the MCP elicitation server flag (below; the client-side bit
  is negotiated per session at `initialize`, never probe-asserted).
- **Capability 2 (relay enforcement)** — detected from the relay-audit Stop hook's
  needle (`_RELAY_AUDIT_NEEDLE`).
- **Capability 3 (backgrounding)** — always present (the detached-worker machinery
  is core-side); the watchdog alert-delivery hook (`_ALERT_COUNT_NEEDLE`) is
  reported honestly.
- **Capability 4 (trusted display)** — reported `"unknown"`: no detection seam
  exists yet for a trusted-render surface, so the verb asserts nothing.
- **Capability 5 (Stop-hook append channel)** — a hook `systemMessage` the
  harness DISPLAYS to the human (added v1.1.0, additive; `docs/design/stop-hook-completer.md`,
  D1). It is what lets the relay-audit Stop hook COMPLETE — code-append the owed
  render / verdict / correction and PROCEED — instead of bouncing the model into
  re-relaying what deterministic code already holds verbatim. Reported tri-state
  like capability 4: there is no passive install seam, so it reads `"unknown"`
  until a conforming harness's conformance probe confirms `systemMessage` display
  (the probe MUST cover BOTH output shapes — a bare `systemMessage` on a
  proceeding stop AND a `systemMessage` combined with `decision:"block"`, since
  display may differ) and activates it via `detect_stop_hook_append` /
  `detect_stop_hook_append_on_block` (`ops/harness_capabilities.py`). **Degrades
  when absent/unknown** (the default — no harness declares it yet): the Stop hook
  stays the REJECTOR (block-once bounce, `_kernel/hooks/relay_audit_stop.py`), an
  owed verdict or contradicted claim re-relayed by the MODEL, never code-appended.
  No wedge — the completer path is dark until the append channel is confirmed.
- **Capability 6 (scheduler-write fence)** — detected from the fence hook's needle
  in `hooks.PreToolUse` (`_SCHEDULER_WRITE_FENCE_NEEDLE`, added v1.2.0, additive),
  a real bool like capabilities 1–2. The behaved leg is a kit battery over the
  real fence core; a FOREIGN provider's proof is owed (Wave C). See "Capability 6 —
  the scheduler-write fence" for the full HONEST STATUS.
- **Capability 7 (decision-rendezvous commit-then-continue)** — detected from the
  rendezvous guard's needle (`_DECISION_RENDEZVOUS_STOP_NEEDLE`), carried inside
  the fused `stop_multiplex` Stop entry (added v1.2.0, additive), a real bool. The
  behaved leg is a kit battery over the real rendezvous core; a FOREIGN provider's
  proof is owed (Wave C). See "Capability 7 — the decision-rendezvous
  commit-then-continue" for the full HONEST STATUS.

**The detection asymmetry (D-K3; anti-vendor-lockout T2).** The needles above
detect OUR reference providers — the Claude Code hooks this repo installs;
they are not the definition of the capability. A foreign harness is detected
BY BEHAVIOR: it proves each capability by running the conformance kit against
its own providers (declared == detected == behaved), and `harness-capabilities`
reporting a needle absent means only that the REFERENCE provider is absent —
never that a conforming foreign provider is. The needle is a fast path for our
own install; the kit verdict is the portable claim.

**Tiers key off detection, not declaration** — this is existing behavior, not a
new rule. The gate already reads the DETECTED presence of the log (capability 1)
at `ops/decision/journal.py::_harness_human_texts` and picks the full-strength or
the friction tier accordingly; `harness-capabilities` merely surfaces the same
observation the gate acts on. So a harness cannot "declare" a capability into
existence: if the seam is absent, both the verb's report and the gate's behavior
degrade together.

The conformance kit (planned separately, `docs/design/conformance-kit.md`) asserts
the three stay aligned: **declared == detected == behaved** — the reported
capability set matches what the seams observe, and what the seams observe matches
how the gates actually behave. A drift between any two is the bug the kit catches.

## MCP elicitation as a second capability-1 channel (implemented 2026-07-08)

The 2025-06-18 MCP revision adds server-initiated **elicitation**: the server sends
an `elicitation/create` request, the client renders a form, the human types a
response, and the client returns it. This is a natural SECOND conforming channel
for capability 1 — the typed response travels client -> server with the model never
touching it (out-of-band satisfied), exactly like the `UserPromptSubmit` hook.

**The specified binding** (a second harness that implements elicitation MUST honor
it, byte-for-byte with §2):

- The server-side handler, on receiving an elicitation response, applies the
  reference provenance filter (`state/utterances.py::is_harness_injected`, typed-
  only) and then `append_utterance`s — the same write API §2 pins.
- **The clicked-option hazard.** An elicitation form may offer server-authored
  OPTIONS. A CLICK on an offered option is NOT typed text — it is the elicitation
  analogue of the `answer_capture._is_clicked` case — so only FREE-TEXT fields of
  the response qualify for the log. A response composed entirely of clicked options
  is dropped; a typed field lands. This mirrors `answer_capture`'s `_typed_texts`
  posture exactly.
- **CRITICAL provenance rule.** The elicitation PROMPT shown to the human MUST be
  CODE-RENDERED (the `relay_render` posture), NEVER LLM-authored text passed
  through a spec. If the model could author the prompt, it would bait the human's
  reply into the trust anchor — the same laundering channel the authorship gate
  exists to close. A conforming elicitation prompt is built by code, from the
  journal, like every other trusted projection.

**How it is implemented here** (`docs/design/mcp-elicitation.md` is the plan of
record; the pump stays hand-rolled — no MCP SDK dependency). The
`_kernel/extension/mcp_server.py` pump is bidirectional: one daemon stdin-reader
thread feeds a message queue (the portable Windows-safe deadline shape), and
`McpServer._request_from_client` is the blocking-with-timeout wait a tool handler
uses — servicing interleaved client requests inline so a waiting elicitation
never head-of-line-blocks the session. The one firing site wraps
`append-decision`: on an authorship refusal carrying the machine-readable
`failure_features.authorship_evidence` marker
(`ops/decision/journal.py::_refuse_missing_authorship`), the server sends
`elicitation/create` with a CODE-RENDERED prompt
(`mcp_server._render_elicitation_prompt` — built from code-selected identifiers
only, never the model's free text and never the refusal message), filters the
response (`mcp_server._accepted_utterance`: free-text-only,
`is_harness_injected` refused), `append_utterance`s harness-side, and re-runs
the identical invocation exactly once (`McpServer._elicit_then_retry`). The
model receives `{elicitation: "captured", sha256}` — the fingerprint, never the
text. The send-side `requestedSchema` is string-fields-only by construction, so
the clicked-option hazard is closed before the receive-side filter ever runs.
The server capability is recorded by the honest flag
`mcp_server.ELICITATION_SERVER_IMPLEMENTED = True` — what a separate-process
probe can verify — read by `harness-capabilities` as `elicitation_server`.

**Client support reality-check — per-session negotiation.** Client support is
never assumed: it is DETECTED at `initialize` from the client's declared
`capabilities.elicitation` (stored per-session as
`McpServer._client_elicitation`). `harness-capabilities` reports
`elicitation_client: "per-session"` — unknown from a separate-process probe, by
design (say unknown, not yes). When a session's client does not declare the
capability, the elicitation channel **degrades to the hook path** silently and
honestly: capability 1's `UserPromptSubmit` utterance-capture remains the
working channel, and the gate behaves identically. Decline, cancel, timeout
(300 s), and malformed responses all take the same degrade path — the original
refusal envelope returns unchanged. No sign-off VERB is introduced (lock 1):
appending an utterance stays the harness's exclusive out-of-band act, whichever
channel captured it; the authorship BAR is unchanged — elicitation is a
channel, never a waiver.

## Capability 2, split: INSPECT vs ACT

Capability 2 (relay/verbatim enforcement) decomposes into two conforming halves,
so a harness can provide each independently:

- **INSPECT** — the ability to OBSERVE the final agent-visible message. The
  emerging portable ride for this is the **OpenTelemetry GenAI semantic
  conventions** (the standardized observable-output spans/events for LLM turns): a
  harness that emits GenAI-conformant telemetry exposes the final message as an
  observable output the audit can read, with no Claude-Code-specific hook.
- **ACT** — the ability to FORCE a continuation (re-answer) instead of ending the
  turn. Two named conforming implementations:
  1. **Harness hooks** à la Claude Code — the reference `Stop` hook
     (`relay_audit_stop.py`) blocks the stop once and makes the agent correct the
     relay.
  2. **A RESPONSE GATEWAY** — an LLM proxy that applies `verify_relay` to the
     outgoing message BEFORE delivery, holding back or amending a contradicted
     relay. The gateway sits in front of any model, so it provides the ACT half
     with no harness hooks at all.

A harness that provides INSPECT (e.g. via OTel GenAI) plus either ACT
implementation earns capability 2's trust property; absent both, the relay audit
degrades to the VERB-ONLY posture named above.

## Capabilities 6 & 7 — the code-backstopped enforcement behaviors (RULED 2026-07-17)

Two guarantees the reference harness ALREADY enforces mapped to NO named
contract capability until now. R4 (`docs/plans/anti-vendor-lockout-2026-07-17.md`
§5) ruled them PROMOTED to named capabilities 6 and 7: recording an
already-enforced behavior is docs-only bookkeeping with no downside, and an
UNNAMED capability can never acquire a second-harness proof. They are named here.
They are, TODAY, **code-backstopped-only** — there is no negotiation seam in the
`harness-capabilities` verb for either, and no conformance-kit behavioral
assertion for either. The seams are NAMED FOLLOW-ONS below, never silently
implied. Per the doctrine guardrail, each bullet describes the MECHANISM the
behavior enforces, never an authorization the harness grants — installing a
provider grants no trust; the behavior is what a foreign harness must reproduce.

### Capability 6 — the scheduler-write fence

**What it IS.** A pre-execution seam at which the harness INSPECTS a command the
agent is about to run and BLOCKS it when the command would EXECUTE a mutating
scheduler verb (`qsub`, `sbatch`, `qdel`, `scancel`, `qmod`, `qalter`) — in
command position, including inside `ssh` / `bash -c` / `eval` / `xargs` / a
command substitution — while letting every read-only probe (`qstat` / `squeue` /
`qacct`, plain `ssh`, a `grep qsub log`) through. Conduct rule 7 mechanized:
"consequences are gated, curiosity isn't." Mutating cluster actions belong to
block CODE, which runs them remotely through `ssh_run` inside Python and gates
each on a journaled human greenlight; the agent's own shell surface never carries
them.

**Trust property earned.** The agent cannot improvise a cluster mutation. Before
this seam, prose was the only guard, and prose drifts with every model/harness
update. The fence moves the guarantee OUT of the model's reach: even a model that
decides to `qdel` a job is stopped at the seam, not trusted to refrain.

**A conforming harness MUST / MUST NEVER.** MUST intercept the agent's
shell/tool-execution surface BEFORE the command runs and refuse — surfacing the
reason to the agent, never silently dropping — any command whose command-position
analysis finds a fenced verb. MUST let read-only probes AND any command that
merely NAMES a fenced verb as an argument (`grep qsub log`, a help string that
prints `qsub`) through: the fence blocks EXECUTION, not mention. MUST NEVER fence
the `hpc-agent` CLI itself — the blocks' command line is `hpc-agent <verb> …`,
which carries no fenced token; they dial the scheduler remotely inside Python.
MUST NEVER treat the fence as authorization to run mutations from block code
WITHOUT a journaled greenlight — the fence is one layer; the greenlight gate is
the other.

**Detection (declared == detected == behaved).** HONEST STATUS (as of v1.2.0):
the declared == detected legs are CLOSED and the behaved leg is closed FOR THE
REFERENCE ADAPTER; a FOREIGN proof remains owed (Wave C). `harness-capabilities`
now REPORTS capability 6 as `scheduler_write_fence`, detected by the same
canonical matcher the other needles use
(`agent_assets._find_hook_entry_index` over the `_SCHEDULER_WRITE_FENCE_NEEDLE` in
`hooks.PreToolUse`, pinned by `tests/cli/test_agent_assets_settings_hook.py` +
`tests/ops/test_harness_capabilities.py`) — a real bool like capabilities 1–2,
never asserted, with the tier its absence degrades to named. The conformance kit
carries a BEHAVIORAL battery (`conformance/test_capability_scheduler_fence.py`)
that drives the REAL fence core in-process as the reference: it MUST block a
fenced verb in command position (including `bash -c 'qsub …'`, `ssh host qdel`,
and other wrapped/transport forms) and MUST pass a mere mention (`grep qsub`), a
read-only probe (`qstat`), and the `hpc-agent` CLI itself — a planted fence that
misses `bash -c 'qsub …'` is FAILED by the battery (guard-can-fire). What is NOT
yet closed: no FOREIGN PreToolUse-equivalent provider has run the battery — the
adapter seam (`run_scheduler_fence`) exists so one can (Wave C), but the reference
core is the only thing proven to behave. The needle detects OUR reference
provider only (the D-K3 detection asymmetry); a conforming foreign fence is
detected BY BEHAVIOR (the kit verdict), never by the needle.

**Enforcing code.** `_kernel/hooks/scheduler_write_fence.py` — a `PreToolUse(Bash)`
command hook, **exit 2 blocks the tool call** with the reason on stderr;
`_fenced_in_command` → `_analyze_tokens` → `_first_real_token` do the
command-position analysis (subshell/redirect/wrapper skipping, transport
recursion, command-substitution scan). Installed by `install_agent_assets` into
`hooks.PreToolUse` (`agent_assets.py`, the `settings_write_fence_hook` spec).

**Follow-on seam (owed, Wave C).** A FOREIGN capability-6 reference adapter (a
non-Claude PreToolUse-equivalent provider) exercising the shipped battery through
the `run_scheduler_fence` adapter method, so the fence-vs-mention discrimination
is proven by something other than our own core. Size: M. The MINOR
contract-version bump this capability's seam OWNED is TAKEN by this landing
(`HARNESS_CONTRACT_VERSION` 1.1.0 → 1.2.0 in `ops/harness_capabilities.py` and the
three-way pin) — the report + reference battery are the seam.

### Capability 7 — the decision-rendezvous commit-then-continue

**What it IS.** A turn-final seam at which the harness DETECTS that a human
greenlight (`response == "y"`) has been committed to the decision journal for a
parked block boundary while the `block-drive` driver has NOT advanced past it, and
FORCES the agent to continue (advance the driver) instead of ending the turn. It
fires ONLY on committed-but-unadvanced: while the driver is merely waiting for the
human (no committed `y` targeting this boundary) the stop is VALID and the seam
stays silent — forcing a continuation there would loop the harness into a void.
Self-healing: the next `block-drive` tick consumes the approved spec and clears
the marker, after which there is nothing to force.

**Trust property earned.** A committed approval always ADVANCES the workflow. The
failure this closes: the model commits the `y` and then ends its turn ("recorded,
done"), stranding the driver un-advanced. The guarantee — an approval never dies
in the journal — moves out of the model's diligence into a code seam.

**A conforming harness MUST / MUST NEVER.** MUST provide the same primitive
capability 2 names — inspect the turn-final state and force ONE continuation — and
at that seam read the journal to distinguish committed-but-unadvanced from
merely-awaiting. MUST block AT MOST ONCE per stop (loop-safe; a
`stop_hook_active`-equivalent re-entry passes straight through). MUST fail-open: a
malformed payload, an unreadable journal, or no committed-unadvanced decision is a
clean no-op, never a wedge. MUST NEVER force continuation while the driver is only
waiting for the human. When the harness ALSO declares the `stop-hook-append`
capability (capability 5) AND the parked next verb is MECHANICAL AND transport is
healthy, it MAY complete the mechanical tick in code rather than bouncing — but
absent any of those it degrades to the byte-identical bounce.

**Detection (declared == detected == behaved).** HONEST STATUS (as of v1.2.0):
like capability 6, the declared == detected legs are CLOSED and the behaved leg is
closed FOR THE REFERENCE ADAPTER; a FOREIGN proof remains owed (Wave C).
`harness-capabilities` now REPORTS capability 7 as `decision_rendezvous`, detected
via the rendezvous guard's needle (`_DECISION_RENDEZVOUS_STOP_NEEDLE`), which the
fused `stop_multiplex` Stop entry's command mentions as an argument — so the SAME
canonical matcher (`agent_assets._find_hook_entry_index`) resolves it against the
one fused Stop entry (pinned by `tests/ops/test_harness_capabilities.py`). The
conformance kit carries a BEHAVIORAL battery
(`conformance/test_capability_decision_rendezvous.py`) that drives the REAL
rendezvous core (`decision_rendezvous_stop_guard.build_hook_output`) in-process as
the reference: it MUST force exactly-one continuation on a committed-but-unadvanced
greenlight, MUST stay SILENT while merely awaiting the human (a trailing nudge / no
committed `y`), and MUST be loop-safe (`stop_hook_active` re-entry passes through
without a second block) — a planted rendezvous that FIRES while merely awaiting is
FAILED by the battery (guard-can-fire). What is NOT yet closed: no FOREIGN
turn-final provider has run the battery — the adapter seam
(`run_decision_rendezvous`) exists so one can (Wave C), but the reference core is
the only thing proven to behave.

**Enforcing code.** `_kernel/hooks/decision_rendezvous_stop_guard.py` —
`build_hook_output(payload)` → `find_committed_unadvanced` (the
committed-`y`-targets-this-boundary predicate, via
`block_drive.committed_greenlight_for_boundary`); `_rejector_output` is the
block-once bounce, `_completer_output` the capability-5-gated in-code advance.
Fused into the one Stop entry by the `stop_multiplex` dispatcher; installed by
`install_agent_assets`.

**Follow-on seam (owed, Wave C).** A FOREIGN capability-7 turn-final provider
exercising the shipped battery through the `run_decision_rendezvous` adapter
method, so exactly-one-continuation-on-committed-unadvanced + silent-while-awaiting
is proven by something other than our own core. Size: M. The MINOR version bump
this seam shared with capability 6 is TAKEN by this landing (v1.2.0).

## Capabilities 4 & 5 — the detection-seam audit (anti-vendor-lockout T3)

Both capability 4 (trusted display) and capability 5 (Stop-hook append channel)
are reported `"unknown"` by `harness-capabilities` — honest non-answers, never
`False` (a caller distinguishes "declared absent" from "never probed"). This
section records, per capability, what a SECOND harness would need to PROVE
conformance and what seam is MISSING, per the T3 charter. Guardrail G3 is
restated and unchanged: the elicitation path stays NON-LOAD-BEARING — nothing here
makes a client-render capability a trust precondition; the MCP elicitation channel
degrades to the hook path silently and introduces no sign-off verb, and the
authorship BAR is untouched.

### Capability 4 — trusted display

- **What conformance would require.** A render surface the harness DISPLAYS to the
  human that code can PROVE the human saw VERBATIM — the trusted projection of a
  relay / verdict / brief, uncorrupted by the model. A second harness proves it
  with a conformance probe that emits a known code-rendered payload and confirms
  the surface displayed it byte-for-byte, with no model-authored substitution.
- **What seam is MISSING.** There is NO code-observable install marker for a
  trusted-render surface — `harness_capabilities` asserts nothing and reads
  `"unknown"` (`ops/harness_capabilities.py`, the `trusted_display`
  `CapabilityEntry`). There is no second render surface and no kit noun. A
  negotiated report needs EITHER a passive detection seam OR a conformance probe
  in the shape capability 5 already sketches.
- **Verdict.** KEEP `"unknown"` in Wave A. Naming/probing is deferred.
- **Follow-on unit.** T9 (plan Wave D) — a trusted-display detection seam + a
  second render surface. Files: `ops/harness_capabilities.py` + a kit noun. Size: M.

### Capability 5 — the Stop-hook append channel

- **What conformance would require.** The harness DISPLAYS a hook `systemMessage`
  to the human — the channel that lets the relay-audit Stop hook COMPLETE
  (code-append the owed render / verdict / correction and PROCEED) instead of
  bouncing the model into re-relaying what code already holds verbatim. A
  conforming prober MUST confirm BOTH output shapes display (D1): a bare
  `systemMessage` on a PROCEEDING stop, AND a `systemMessage` combined with
  `decision:"block"` — display may differ between them.
- **What seam is MISSING.** There is NO passive install seam — a hook
  `systemMessage` leaves zero evidence in `settings.json` — so the channel is
  ENV-DECLARED only (`HPC_STOP_HOOK_APPEND` / `HPC_STOP_HOOK_APPEND_ON_BLOCK`, read
  tri-state by `detect_stop_hook_append` / `detect_stop_hook_append_on_block` in
  `ops/harness_capabilities.py`) and reads `"unknown"` until a conforming harness's
  probe confirms `systemMessage` display. No conforming prober exists yet.
  Env-declared activation is trust-by-self-assertion's next-door risk (guardrail
  G2), which is exactly why the completer path is gated on a PROBE-confirmed bit,
  not on the env marker alone.
- **Verdict.** KEEP `"unknown"` in Wave A. Activation stays PROBE-gated.
- **Follow-on unit.** T10 (plan Wave D) — a stop-hook-append conformance prober
  (the D1 two-shape probe) so a foreign harness activates capability 5 by
  BEHAVIOR, not just env markers. Size: M.

## Drift log

- **2026-07-17 (anti-vendor-lockout, capability 6/7 negotiation seams — the
  Wave-A-owed M follow-on).** Closed the declared == detected legs and the
  behaved-for-the-reference-adapter leg for capabilities 6 (scheduler-write fence)
  and 7 (decision-rendezvous commit-then-continue); a FOREIGN proof remains owed
  (Wave C). (1) `harness-capabilities` now REPORTS both — `scheduler_write_fence`
  detected from `_SCHEDULER_WRITE_FENCE_NEEDLE` in `hooks.PreToolUse`,
  `decision_rendezvous` from `_DECISION_RENDEZVOUS_STOP_NEEDLE` inside the fused
  `stop_multiplex` Stop entry — both via the ONE canonical matcher
  `agent_assets._find_hook_entry_index`, real bools with named tier consequences.
  (2) `HARNESS_CONTRACT_VERSION` bumped 1.1.0 → **1.2.0** (additive MINOR; both
  reference adapters stay conforming) and the three-way pin (doc line == constant
  == kit stamp) updated consistently. (3) Kit behavioral batteries land as
  reference-core-driven modules: `conformance/test_capability_scheduler_fence.py`
  (blocks fenced verbs incl. `bash -c 'qsub …'` / `ssh host qdel` / wrapped forms;
  passes `grep qsub`, `qstat`, and the `hpc-agent` CLI itself) and
  `conformance/test_capability_decision_rendezvous.py` (forces exactly-one
  continuation on a committed-unadvanced greenlight; silent while merely awaiting;
  loop-safe re-entry). Both carry the adapter-driven shape (`run_scheduler_fence` /
  `run_decision_rendezvous` optional adapter methods) so a FOREIGN provider can run
  the identical assertions in Wave C. The top-level `conforming: harness contract
  v1` verdict is UNCHANGED (the three core capabilities); 6/7 are additive
  reference-behaved batteries, not verdict gates, per the amplification/skips-honest
  doctrine. Updates the capability 6/7 HONEST STATUS + follow-on paragraphs and the
  negotiation-section bullets. Enforcing hooks unchanged.
- **2026-07-17 (anti-vendor-lockout Wave A, T2 + T3).** Named the two
  previously-unnamed enforcement behaviors as **capabilities 6 (scheduler-write
  fence) and 7 (decision-rendezvous commit-then-continue)** — R4 RULED
  (`docs/plans/anti-vendor-lockout-2026-07-17.md` §5). Recorded them
  code-backstopped-only: NO negotiation seam and NO kit assertion for either, so
  the declared == detected == behaved triangle is UNCLOSED for both; the seams are
  named follow-ons that OWN the MINOR contract-version bump. Contract version left
  at 1.1.0 deliberately — this landing is docs-only and adds no reported
  capability. Added the **capability 4/5 detection-seam audit (T3)**: both stay
  `"unknown"` in Wave A, with the missing seam and the Wave-D follow-on unit
  (T9/T10) recorded per capability; the elicitation-non-load-bearing posture (G3)
  restated unchanged. No code touched; the enforcing hooks
  (`_kernel/hooks/scheduler_write_fence.py`,
  `_kernel/hooks/decision_rendezvous_stop_guard.py`) predate this record.
