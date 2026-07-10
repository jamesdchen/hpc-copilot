---
status: shipped
---
# MCP elicitation — the bidirectional protocol upgrade (design + implementation plan)

**Status: IMPLEMENTED (2026-07-08; E1–E7 landed, drift log at foot).** This plan settles how
`src/hpc_agent/_kernel/extension/mcp_server.py` gains the server-initiated
`elicitation/create` exchange that `docs/internals/harness-contract.md`
("MCP elicitation as a second capability-1 channel") already specifies
normatively. The contract section is the SPEC this plan implements against;
nothing here re-decides the binding — it decides the *machinery*. Cite
`path::symbol`, never line numbers. Record implementation drift in a drift
log appended to this document (the `docs/design/notebook-audit.md` pattern).

## Why now

The harness contract specifies elicitation as the second conforming
capability-1 channel: the typed response travels client → server with the
model never touching it (out-of-band satisfied), filtered server-side
(free-text only, per the clicked-option hazard), then
`state/utterances.py::append_utterance`. It is recorded honest-false today:
`mcp_server.ELICITATION_SUPPORTED = False`, because the server is a
hand-rolled JSON-RPC 2.0 **request → response pump** (`McpServer.serve`) with
no outbound-request path, no id-correlation for a server-originated request,
and no way for a tool handler to block awaiting a client reply. The user has
ordered the protocol upgrade planned properly. This plan is that upgrade.

## Settled decisions

### D1 — Bounded bidirectional extension of the hand-rolled pump (not SDK adoption)

**Decision: extend the existing pump with exactly the bidirectional minimum —
option (a). The official MCP Python SDK is NOT adopted, neither as a core dep
nor as an optional extra. Recorded revisit trigger below.**

What the pump lacks, enumerated (the implementation-agent finding, confirmed
by reading `McpServer.serve` / `McpServer.handle`):

1. **A server-originated request id namespace.** Outbound requests need ids
   that can never collide with client-chosen ids — a distinct string
   namespace (`"hpc-srv-<n>"`, monotonic counter on the instance).
2. **A pending-responses table.** `{outbound_id: <slot>}` so an incoming
   RESPONSE finds its waiter. Size ≤ 1 in v1 (see D3 re-entrancy).
3. **Message-kind dispatch in the read loop.** Today every stdin line is
   assumed to be a request. The loop must classify: has `"method"` →
   request/notification (existing `handle` path); has `"id"` + `"result"` or
   `"error"` and no `"method"` → a response to a server-originated request →
   route to the pending table, emit nothing.
4. **A blocking-with-timeout wait primitive callable from a tool handler.**
   The handler pumps inbound messages while blocked: a
   `_request_from_client(method, params, timeout_s)` that writes the outbound
   request, then consumes/classifies messages until the matching response
   arrives or the deadline expires — servicing interleaved client REQUESTS
   inline while waiting (see D3), so the wait does not head-of-line-block the
   rest of the session (the conduct rule 11 lesson,
   `_refuse_blocking_over_mcp`).

5. **Stream plumbing the current structure lacks** (pre-implementation
   verification 2026-07-07): the tool handler is reached via
   `McpServer.handle → _dispatch → call_tool`, NONE of which see the
   transport — `serve(stdin, stdout)` alone owns the streams, and
   `handle()` is deliberately transport-free for unit tests. The wait
   primitive therefore needs the duplex threaded onto the instance (set by
   `serve` before the loop; `None` otherwise). When the transport is absent
   (direct-`handle` tests, any embedding that never calls `serve`),
   elicitation is structurally unavailable and the flow takes the degrade
   path — which keeps every existing `handle()`-level test valid unchanged.

6. **The timeout needs ONE dedicated stdin-reader thread** (pre-implementation
   verification 2026-07-07 — this corrects the earlier "no threads" claim).
   A single-threaded blocking `readline` has no deadline: `select()` does not
   work on pipes/console stdin on Windows, and while the human deliberates a
   conforming client sends NOTHING on the wire — so a readline-loop
   implementation of the 300 s timeout can never fire and the elicitation
   call wedges the whole server indefinitely, exactly the head-of-line class
   D3 exists to prevent. The correct portable shape: one daemon thread is the
   SOLE stdin reader, pushing parsed lines (and an EOF sentinel) onto a
   `queue.Queue`; `serve`'s loop and `_request_from_client` both consume from
   the queue — `Queue.get(timeout=…)` is what makes the deadline real.
   Dispatch stays single-threaded (the reader thread only reads and
   enqueues; it never touches handlers or the registry), so the state-leak
   audit and the re-entrancy analysis (D3) are unaffected. EOF on stdin
   during a wait = decline-equivalent, then normal shutdown.

Estimated size: ~180–260 lines in `mcp_server.py` plus a fake-client duplex
test harness. One daemon reader thread (reads/enqueues only — never
dispatches), no asyncio, no new dependency.

**Why the alternatives lose:**

- **(b) Official MCP Python SDK as an optional extra `hpc-agent[mcp]`, pump
  as fallback.** Two server implementations of the same curated catalog =
  a permanent drift surface: every catalog/envelope/refusal behavior must be
  built and pinned twice, and the conformance kit
  (`docs/design/conformance-kit.md`) would have to run its full assertion set
  against both to keep them honest — doubling the matrix to buy exactly one
  feature v1 uses in exactly one place (D4). The `[ssh]`/`[s3]` extras
  precedent (`pyproject.toml`) does not carry: those extras swap an *engine
  behind one seam* (one lazy import, one call surface); an SDK-backed server
  is not a seam swap, it is a second implementation of the whole projection
  layer (tool listing, envelope passthrough, curated derivation, blocking
  refusal, version-skew instructions).
- **(c) Full SDK migration.** The official SDK is an async (anyio) framework
  with its own transport, session, and typed-model stack. Migration breaks,
  and would force reimplementation of, everything the hand-rolled server was
  built to carry: the registry-projected curated catalog
  (`McpServer._curated_metas`, derived from `next_block` Result fields), the
  CLI-envelope passthrough in `structuredContent` (`_tool_result` — the
  failure contract the doc's "Addressing the trade-offs" section exists for),
  the injectable `CliRunner` + subprocess parity oracle
  (`_in_process_cli_runner` / `_subprocess_cli_runner`,
  `tests/test_mcp_server.py`), the synchronous in-process dispatch whose
  state-leak audit is written against `contextlib.redirect_stdout`, and the
  blocking-invocation refusal seam. It also violates the zero-new-core-deps
  posture for the base install (anyio + httpx + more).

**Why the library-knowledge outsourcing lesson does NOT flip this one.** The
filelock/psutil precedent (`pyproject.toml` dep comments;
`docs/internals/engineering-principles.md` four-question boundary test)
outsourced substrate where hand-rolled platform branches had *diverged and
caused real incidents* (two win32 serialization losses; two byte-divergent
PID probes). The hand-rolled pump has the opposite record: correct and
battle-tested across proving runs #3–#9, with its two real defects (the
head-of-line wedge, the blocking-watch class) fixed at seams the SDK would
not have provided. The delta needed here is small, bounded, and fully
testable offline; the SDK is not a tiny canonical primitive but a framework
adoption. The honest reading of the boundary test: JSON-RPC framing is
substrate we already own correctly; elicitation adds one outbound request
type, not a new protocol.

**Recorded revisit trigger — TIGHTENED (user review, 2026-07-07)**: the
trigger is **any further concurrency requirement**, not merely a second
server-initiated feature: parallel elicitations, sampling, roots,
progress-with-cancellation, subscriptions — ANY of them flips the calculus
to SDK adoption (path (c), done once, the parity oracle retired
deliberately). Rationale from the verification pass: the original
no-threads plan was UNIMPLEMENTABLE (the Windows stdin-deadline problem),
and the amended one-reader-thread facade is defensible only because it is
the platform's own canonical shape for exactly this problem —
CPython's `subprocess.communicate(timeout=)` spawns reader threads on
Windows, `jupyter_client`'s blocking channels are thread-fed queues. One
sync facade thread is the LAST cheap step; hand-rolled *async
coordination* beyond it is precisely the divergence zone the ssh lesson
names. One outbound request type is a bounded extension; the next one is
a protocol re-implementation.

**Risk bound:** elicitation is a *degradable* capability by contract — every
failure mode (protocol bug, client quirk, timeout) degrades to the hook tier,
never to a wrong answer. A subtle pump bug costs friction, not integrity.
That asymmetry is load-bearing for choosing the smaller machine.

### D2 — Capability negotiation: per-session detection, the static flag retires

Elicitation support is **detected from the client's declared capabilities at
`initialize`** (`params["capabilities"]["elicitation"]` per the 2025-06-18
revision) — never assumed. `McpServer._initialize` currently discards client
capabilities; it will store `self._client_elicitation: bool` on the instance.
Elicitation fires only when that per-session bit is true; absent client
support → the documented degrade-to-hook path, **silently and honestly** (no
error, no warning to the model — the gate's normal refusal with the existing
hook-path remediation is the surfaced behavior).

The module-level `ELICITATION_SUPPORTED` static-False retires. It is replaced
by **`ELICITATION_SERVER_IMPLEMENTED: bool = True`** — the honest thing a
*separate-process probe* can report. `ops/harness_capabilities.py` is a CLI
verb; it cannot observe a live MCP session's negotiation, so its evidence
reshapes from the single `"elicitation_channel"` bool to:

```
"elicitation_server": True,            # code capability, verifiable
"elicitation_client": "per-session"    # negotiated at initialize; unknown
                                       # from this probe (say unknown, not yes)
```

This keeps the contract's detection-as-negotiation posture: every `present`
bit is something code verified; client support stays "unknown from this
probe" exactly as the contract's client-support reality-check section
demands. The harness-contract doc's capability-1 detection paragraph and
`docs/primitives/harness-capabilities.md` update in the same task.

### D3 — The exchange: shapes, timeout, re-entrancy

**Request** (spec-conformant `elicitation/create`):

```json
{"jsonrpc": "2.0", "id": "hpc-srv-1", "method": "elicitation/create",
 "params": {
   "message": "<CODE-RENDERED prompt — see D5>",
   "requestedSchema": {
     "type": "object",
     "properties": {"utterance": {"type": "string",
       "description": "Type the sign-off in your own words."}},
     "required": ["utterance"]}}}
```

**The free-text constraint is structural, then filtered.** The
`requestedSchema` we emit contains string fields ONLY — never `enum`, never
option lists — so there is nothing to click: the clicked-option hazard
(`answer_capture._is_clicked`) is closed *by construction* on the send side.
Defense-in-depth on the receive side anyway (a nonconforming client could
still return canned text): each returned string field passes
`state/utterances.py::is_harness_injected` (refuse) and non-empty checks
before `append_utterance` — mirroring `answer_capture._typed_texts` posture.
A response with no qualifying free text is treated as decline.

**Decline / cancel / malformed → the degraded tier, never a JSON-RPC error.**
`{"action": "decline"}` or `"cancel"` (or a shape the filter rejects) means:
no utterance appended, and the tool call returns the gate's ordinary refusal
envelope (`ok:false`, the existing remediation text naming the type-it-in-
the-chat hook path). The human saying no is a valid outcome, not a fault.

**Timeout: 300 s, then decline-equivalent.** A human may walk away
mid-elicitation; the tool call must not wedge. 300 s is generous for an
at-the-keyboard typed sentence while staying far under the runner ceiling
(`_SUBPROCESS_RUNNER_TIMEOUT_SEC` = 3600 s). On expiry the pending slot is
cleared, a late response for that id is dropped silently (logged to stderr
telemetry, the `[mcp]` line convention), and the flow proceeds as decline.
During the wait the pump keeps servicing interleaved client requests, so
other tool calls do not queue behind the elicitation (the run-#3 head-of-line
lesson applies to elicitation exactly as to blocking watches).

**Re-entrancy: exactly ONE elicitation in flight, depth-capped.** The pending
table holds at most one entry. While awaiting a response, interleaved client
REQUESTS are dispatched inline with elicitation *suppressed* (a nested tool
call that would elicit instead takes the degrade path). A second concurrent
elicitation cannot arise otherwise (dispatch is single-threaded — the D1
reader thread only enqueues), so the cap is an
invariant assertion, not a queue. Nested in-process CLI dispatch during the
wait is safe: `contextlib.redirect_stdout`/`redirect_stderr` nest, and
journal writes are already flock-guarded against concurrent CLI processes.

### D4 — Where it fires v1: the MCP layer, wrapping `append-decision`, retry-once

Per the contract: **no new verb** (lock 1 — appending an utterance stays the
harness's exclusive out-of-band act; no sign-off verb, no generic
ask-the-user tool). The one v1 firing site is the sign-off path over MCP:

1. `McpServer.call_tool("append-decision", …)` runs the CLI exactly as today.
2. The envelope comes back `ok:false` with the **authorship-evidence marker**
   (E2 below — a machine-readable discriminator on the authorship/sign-off
   refusal, not message-text sniffing) AND `self._client_elicitation` is true
   AND no elicitation is already in flight.
3. The server code-renders the prompt (D5), sends `elicitation/create`,
   awaits per D3.
4. On an accepted, filtered, typed response:
   `state/utterances.py::append_utterance` (the write API §2 pins — the MCP
   server process is harness-side code, the contract's specified handler),
   then **re-run the identical CLI invocation exactly once**. The gate
   re-checks against the now-present utterance. Second refusal stands —
   never loop.
5. Any other outcome: the original refusal envelope is returned unchanged.

**Settled: this lives in the MCP layer, not inside the gate.**
`ops/decision/journal.py::_assert_signoff_authorship` (and the sibling
authorship gates) stay 100% harness-agnostic — they read evidence tiers and
refuse; they never know a transport exists. A retry-after-elicit seam inside
the gate would thread harness knowledge into `ops/` (the layer the
subject-import discipline and the harness contract both keep clean) and
would fire identically for CLI callers who have no elicitation channel. The
MCP layer already owns per-transport posture (`_refuse_blocking_over_mcp` is
the precedent: transport-specific behavior belongs at the transport seam).
Recorded alternative: the gate-seam design was rejected for exactly that
layering reason, plus testability — the MCP-layer design is fully exercised
by the fake-client harness with zero gate changes.

**E2 prerequisite — the machine-readable trigger.** The refusal envelopes
from the authorship/sign-off gates surface as `errors.SpecInvalid`
(generic). The elicitation hook must not parse prose. **Seam settled by
pre-implementation verification (2026-07-07): the marker rides
`failure_features`, not a `data` field and not a new `error_code`.** As
coded, `ok:false` envelopes carry NO `data` field at all
(`cli/_helpers.py::_err` builds exactly `{ok, error_code, message, category,
retry_safe[, remediation][, failure_features][, escalation]}`), so
"additive `data`" would itself be a shared-envelope change; and a new
`error_code` is a breaking wire change by `errors.py`'s own header doctrine
("Adding new error_code values is a breaking change"). The additive seam
ALREADY EXISTS: `cli/_helpers.py::_err_from_hpc` lifts
`getattr(exc, "failure_features", None)` verbatim into the envelope, the
`failure_features` block is contractually open/ungoverned, and it rides the
MCP `structuredContent` untouched (`mcp_server.py::_tool_result` copies the
whole envelope). So E2 = the gate's raise sites attach
`exc.failure_features = {..., "authorship_evidence": "missing"}` — zero
envelope-machinery change. One trap: `_err_from_hpc` SYNTHESIZES a default
`failure_features` for every `spec_invalid` envelope
(`_spec_invalid_failure_features`), so the MCP trigger must key on the
distinct KEY (`authorship_evidence`), never on the mere presence of a
`failure_features` block. The MCP layer keys on that, nothing else.

### D5 — Trust and provenance pins

- **The prompt is CODE-RENDERED** (the `ops/relay_render.py` posture, the
  contract's CRITICAL provenance rule). A pure function
  (`_render_elicitation_prompt` in the MCP layer) builds it from the refusal
  envelope + journal-derived identifiers only: the block name, scope kind/id,
  and (for a notebook sign-off) the section slug — identifiers the gate
  itself will token-match, never the model's `proposal`/`response` free text,
  never any string the model authored in the tool arguments beyond those
  code-selected identifiers. If the model could author the prompt it would
  bait the human's reply into the trust anchor — the laundering channel the
  gate exists to close.
- **The response text never passes through the model.** It travels client →
  server stdin → filter → `append_utterance`. The tool RESULT returned to the
  model carries `{elicitation: "captured", sha256: <digest>}` — the
  fingerprint, not the text. The model learns the gate's verdict from the
  retried envelope, not the human's words (which it will see only if the
  human also says them in chat).
- **The gate still judges.** Elicitation only *carries text into the log*;
  the authorship BAR is unchanged — bare acks still refused, token-exact
  naming still required, the recompute lock (`state.attestation.bind`) still
  un-fakeable. Elicitation is a channel, never a waiver.
- **Stop-hook / relay machinery: nothing needed — verified.** The relay-audit
  Stop hook audits the model's final agent-visible message; the elicitation
  exchange happens inside a tool call, out of that surface. The
  `UserPromptSubmit`/`PostToolUse` capture hooks are client-side Claude-Code
  hooks; the elicitation response never traverses those events, which is why
  the server-side handler does the appending. No hook change, no
  `agent_assets.py` needle change.
- **Enforcement rows + conformance reservation.** New rows in the
  engineering-principles enforcement map for (i) send-side schema is
  free-text-only and (ii) the prompt-renderer never embeds model free text —
  each named to its pinning test. The conformance kit's adapter already
  reserves `answer_question` as "the AskUserQuestion / MCP-elicitation
  analog" exercising `_is_clicked`; the kit gains an elicitation-channel
  assertion (declared == detected == behaved for the per-session capability)
  when it lands — reserved here, built there
  (`docs/design/conformance-kit.md` K6/K10 coordination note applies).

### D6 — What this deliberately does not build

- No generic ask-the-user MCP tool the model can drive (an
  elicitation-on-demand verb would be a model-authored prompt channel — the
  exact bait vector D5 closes).
- No sensitive-data elicitation, ever: the MCP spec's guidance is normative
  here — the only thing this server elicits is a sign-off utterance about
  work the human is already reviewing. The prompt renderer is the enforcement
  point (it has no input slot that could carry a secret request).
- No second firing site. `scope-unlock` and the plain greenlight authorship
  gate keep the hook-tier flow in v1; extending elicitation to them is a
  one-line trigger addition *after* the sign-off path has survived a proving
  run. **Amended 2026-07-09 (see the "E-render amendment" section):** the ONE
  firing site (the `append-decision` authorship refusal) is PROMOTED from a
  retry-only fallback to the PRIMARY read-and-sign channel — still one site,
  still append-decision-or-nothing (D5's channel lock intact), still no new verb;
  only the ordering/framing changed, orchestrated in the server around an
  untouched gate.
- No SDK, no new dependency, no change to `pyproject.toml`.

## Task waves (file-disjoint, Opus-sized; tests ride each task)

**Wave 1 (parallel):**

- **E1 — the bidirectional pump.**
  `src/hpc_agent/_kernel/extension/mcp_server.py`: outbound id counter +
  namespace, pending-response slot, the single daemon stdin-reader thread +
  message queue (D1 item 6 — the reader only reads/enqueues; `serve` becomes
  a queue consumer), message-kind classification on dequeue (request vs
  notification vs incoming response), the transport threaded onto the
  instance by `serve` (D1 item 5 — absent transport ⇒ elicitation
  structurally unavailable, existing direct-`handle` tests unchanged), the
  `_request_from_client(method, params, timeout_s)` wait primitive that
  consumes the queue with a real `get(timeout=…)` deadline and dispatches
  interleaved requests inline (elicitation-suppressed nested dispatch,
  depth cap asserted), late-response drop, EOF-sentinel → decline +
  shutdown, stderr telemetry line. Store
  client capabilities at `_initialize` → `self._client_elicitation`.
  `ELICITATION_SUPPORTED` → `ELICITATION_SERVER_IMPLEMENTED = True` (E3
  consumes). Tests: a fake-client duplex harness (paired in-memory streams —
  the plugins-CI offline posture, no real stdio, no network) driving:
  correlation, interleaved-request servicing during a wait, timeout →
  decline-equivalent, late response dropped, id-namespace non-collision,
  depth cap.
- **E2 — the authorship-refusal marker.**
  `src/hpc_agent/ops/decision/journal.py` (+ `errors.py` if a code is chosen
  over a `data` field): the additive machine-readable discriminator on
  authorship/sign-off refusals, preserved through the CLI envelope. Tests:
  refusal envelopes carry the marker; success envelopes don't; envelope
  schema unchanged otherwise.
- **E3-a — `ops/harness_capabilities.py` reshape** (D2 evidence keys, import
  the renamed flag) + `tests/ops/test_harness_capabilities.py` +
  `docs/primitives/harness-capabilities.md`. Depends only on the flag rename
  landing in E1; coordinate the one-symbol rename, otherwise file-disjoint.
  The verb's result will additionally gain a `HARNESS_CONTRACT_VERSION`
  field, but that field is OWNED by the conformance kit's K10, not this task
  (`docs/design/conformance-kit.md` D-K6/K10): E3-a reshapes the evidence
  keys and leaves the result shape OPEN for that additive field — do not pin
  it closed.

**Wave 2 (after wave 1):**

- **E4 — the elicitation handler + firing site.** In `mcp_server.py` (same
  file as E1 — sequential after it, same agent or explicit hand-off, never
  parallel): `_render_elicitation_prompt` (pure, code-rendered, D5),
  the response filter (`is_harness_injected` + typed-only + non-empty),
  `append_utterance`, the retry-once wrap of `append-decision` keyed on E2's
  marker and gated on the per-session capability + in-flight suppression.
  Tests (fake client, end-to-end): accept-typed → utterance appended
  (record's `sha256` verified) → retry succeeds; decline/cancel/timeout →
  original refusal returned, log untouched; injected-tag response refused;
  client-without-capability → no elicitation attempted; prompt contains the
  code-selected identifiers and none of the model's free text; result echoes
  sha, never text; no elicitation on non-append-decision tools.
- **E5 — contract + doc alignment.**
  `docs/internals/harness-contract.md`: the "specified, not implemented"
  section flips to implemented-by-reference (cite the E1/E4 symbols), the
  capability-negotiation section gains the per-session posture;
  `tests/contracts/test_harness_contract.py` +
  `tests/contracts/test_authorship_elicitation_guidance.py` re-pinned
  (including the standing pin: still NO utterance-writing verb in the
  registry — elicitation must not have added one);
  enforcement-map rows (`docs/internals/engineering-principles.md`);
  `CHANGELOG.md`; drift log opened in THIS document.

**Wave 3 (tails, parallel):**

- **E6 — regen + schema sweep.** No new primitive and no wire-model change
  is expected (the evidence dict is untyped; `append-decision`'s spec is
  unchanged), but run the full regen list
  (`scripts/bake_operations_json.py --write` et al.) and the schema build to
  prove byte-stability — the 0.8.0 lesson: never assume "no regen needed".
- **E7 — conformance-kit reservation note.** One paragraph in
  `docs/design/conformance-kit.md` pointing K6/K10 at the now-real
  per-session capability and the elicitation-channel assertion shape. The
  per-session detection leg is the fake-client `initialize` seam (the
  client's declared `capabilities.elicitation` at `initialize`, D2) — NOT the
  CLI `detect_capabilities` verb, which is a separate-process probe that
  reports client support as `"unknown"` (D2's honesty posture) and so can
  never witness a live session's negotiation. No kit code (the kit is its own
  plan).

**Verification (end of every wave, parallel, mechanical):** `ruff check
--fix`, `ruff format`, `mypy --ignore-missing-imports`, full `pytest` via
`.venv/Scripts/python.exe -m pytest`.

## Boundary-drift flags (stop and re-read the contract if any is about to bend)

1. **Never elicit sensitive data** — the prompt renderer must stay a closed
   function with no free-form input slot (MCP spec guidance, D6).
2. **Never a generic ask-the-user surface the model can drive** — the firing
   trigger is a code-detected gate refusal, not a tool argument.
3. **Elicitation never substitutes for the authorship BAR** — it carries text
   into the log; `_assert_signoff_authorship`'s recompute + token bar judge
   it unchanged. If a task finds itself weakening a bar "because the channel
   is trusted now", that is the drift.
4. **The gate stays harness-agnostic** — any change to `ops/decision/` beyond
   E2's additive marker is out of bounds for this plan.
5. **No LLM-authored text in the prompt; no response text echoed to the
   model** — D5's two provenance pins, each with a named test.
6. **The static-flag honesty posture survives the upgrade** — the probe
   reports what code can verify (`server: true, client: unknown-from-probe`),
   never an asserted "elicitation works".

## Drift log

- **Pre-implementation verification (2026-07-07, adversarial plan review;
  no code had landed):**
  1. *D1 re-shaped* — the original "no threads" + blocking-with-timeout
     combination was unimplementable: `handle`/`call_tool` never see the
     transport (only `serve` does), and a blocking `readline` has no
     deadline on Windows while a waiting client sends nothing, so the 300 s
     timeout could never fire (an indefinite server wedge — the exact class
     D3 prohibits). D1 gained items 5–6 (instance-threaded transport; one
     daemon stdin-reader thread + `queue.Queue`, `get(timeout=…)` as the
     real deadline), E1's task text and the size estimate were updated to
     match.
  2. *E2 seam corrected* — `ok:false` envelopes carry no `data` field
     (`cli/_helpers.py::_err`), and a new `error_code` is a breaking wire
     change per `errors.py`'s header doctrine. The marker rides the
     existing additive `failure_features` seam (`_err_from_hpc` lifts
     `exc.failure_features` verbatim; MCP `structuredContent` preserves
     it), keyed on the distinct `authorship_evidence` key — never on the
     block's presence, which `_spec_invalid_failure_features` synthesizes
     for every spec_invalid.
  3. *Verified against code, no change needed*: `_initialize(params)` does
     receive the client capability object (currently discarded — D2 is a
     store, not a plumbing change); the gate raise sites are
     `errors.SpecInvalid` routed through `_dispatch.py::_err_from_hpc`;
     `_tool_result` copies the full envelope into `structuredContent`.

- **Implementation (2026-07-08, E1–E7 landed; deviations recorded):**
  1. *E1 defensive branch* — `McpServer._consume_message` handles a malformed
     non-response dict (no `method`, not response-shaped) with a `-32600`
     error; the plan enumerated only the three conforming kinds. No behavior
     change for conforming clients.
  2. *E2 scoping* — only genuine authorship-BAR refusals carry the
     `authorship_evidence` marker (via `journal.py::_refuse_missing_authorship`);
     structural refusals (view_sha mismatch, section-not-found, unresolvable
     source, bind recompute) are deliberately UNMARKED — a re-elicited
     utterance cannot cure them, so marking them would make the D4 retry a
     guaranteed-failing round-trip. The attached block WINS over
     `_err_from_hpc`'s synthesized default (layering-clean; the misleading
     `error_class: "code_bug"` drops from human-policy refusals).
  3. *E2 heads-up* — `_wire/fixtures/failure_features.py::FailureFeatures` is
     `extra="forbid"`, but the ERROR envelope path never validates against it
     (only the `_ok` path runs `validate_output`), so the additive key rides.
     If error envelopes ever gain schema validation, the model must admit the
     key first.
  4. *E4 renderer* — `_render_elicitation_prompt` deliberately excludes the
     refusal envelope's `message` too (not just tool-argument free text): the
     gate's message can QUOTE the model's response, which would smuggle
     model-authored words into the trusted prompt.
  5. *Test rig* — the duplex harness lives at `tests/_mcp_harness.py`
     (`FakeMcpClient`, `RecordingElicitServer` with its harness-only
     `elicit-test` tool seam); the conformance kit consumes this rig per the
     cross-slate reuse ledger.
  6. *E6* — regen byte-stability verified (all six scripts, zero drift): the
     phase adds no primitive and no wire model, exactly as planned.

- **E-render (2026-07-09, SHIPPED; the "E-render" section below flipped from
  NOTED to SHIPPED):** the notebook sign-off popup now carries a code-computed
  render DIGEST (RULING 1: digest + `view_sha12`, not the full render — reading
  ergonomics; trust identical) on the EXISTING retry-only firing site (RULING 2:
  no D6 amendment, no second firing site). `read_render_digest` derives the
  digest from the on-disk render's own code-authored bytes (reusing the
  audit-view projections — diff/asserts/lint — never the notebook source);
  missing/stale/no-`view_sha` degrade to a reason-disclosing fallback line
  (fail-soft, never a crash, never an unmarked silent omission). Bounded by
  construction: counts + a capped, per-item-truncated assertion list, never the
  diff body. No new primitive, no wire-model change (regen debt: none expected —
  same class as E6; the orchestrator's central regen run confirms byte
  stability).

- **E-render amendment (2026-07-09, same-day; SUPERSEDES the E-render entry above):**
  two user rulings landed as one change set. (1) **PRIMARY firing (D6 amendment):**
  the append-decision popup is promoted from a retry-only fallback to the primary
  read-and-sign channel — for a human-required notebook sign-off the server elicits
  FIRST and the append proceeds with the typed utterance, instead of surfacing a
  refusal. D5's channel lock is intact (append-decision or nothing, no new verb);
  the reliability guard is hard (declared-but-dark / undeclared / timeout → the
  byte-for-byte pre-promotion refusal path); a valid utterance appends without a
  popup. Realized ENTIRELY in the server orchestration
  (`call_tool`/`_elicit_then_retry`/`_elicitation_applies`) — the authorship gate
  (`_assert_signoff_authorship`) is untouched and stays one-definition. (2) **DIGEST
  v2:** the popup is a SIGNING surface — every element serves BIND (identity +
  freshness; stale → do-not-sign, pointer only), WHY-YOUR-JUDGMENT (tier-trigger
  headline, declared-assertion table, lint-flag names+locations, per-hunk
  one-liners — never the diff body), or ROUTE (the on-disk path); the honesty rule
  refuses to digest (naming counts + pointer) rather than silently drop a
  judgment-critical item when oversize, and every capped list discloses its
  elision. Recorded judgment call: the trusted render is static (no per-assertion
  computed/pass-fail value exists), so the assert table shows declared expressions
  marked unverified rather than fabricating a computed value — the honest reading
  of "declared-vs-computed" under the static-audit invariant. No new primitive, no
  wire-model change (regen debt: none — same class as E6; the orchestrator's central
  regen run confirms byte stability). Symbols + tests: see the "E-render amendment"
  section body.

## E-render: the popup carries the render digest (SHIPPED 2026-07-09; AMENDED same-day — see the amendment section)

Run #11 exposed the read/sign channel split: the v1 sign-off popup showed
identifiers only, so the human had to READ the section render in some
model-adjacent channel (chat relay, file link, an expanded Read pane) before
typing into the model-untouched box. E-render closes the loop: when the
refusal is a notebook sign-off, the SERVER reads the section's
content-addressed render (`.hpc/renders/<audit_id>/<slug>.<view_sha12>.md`)
off disk and embeds a code-computed DIGEST of it in the elicitation `message`
— code-read bytes in, typed utterance out, one channel, model suspended
throughout.

Compatibility with the D5 identifiers-only rule: the rule's PURPOSE is to bar
MODEL-authored text from baiting the reply; a disk render is code-authored
(the trusted-display artifact the T8 gate already binds), so embedding a digest
of it honors the purpose while widening the letter — which is exactly why this
was a recorded design change, not a patch.

The initial E-render decisions (2026-07-09, same-day) were: (i) digest not full
render, and (ii) retry-only, no D6 amendment. **Both the firing and the digest
were AMENDED the same day by an explicit user ruling** (see "E-render amendment"
below) — the popup becomes the PRIMARY read-and-sign channel (a promoted D6
firing site) and the digest is rebuilt as a three-job signing digest (v2). The
original two decisions are recorded here as superseded; the current design is the
amendment.

**Superseded same-day decisions (kept for the drift record):** the digest (not
the full render — reading ergonomics; trust identical) STANDS. What changed is
(a) the firing FRAMING (retry-only → primary, the D6 amendment) and (b) the
digest COMPOSITION (v1 counts → v2 three-job signing digest).

### E-render amendment — PRIMARY firing + DIGEST v2 (user-ruled 2026-07-09, superseding the same-day retry-only + v1-digest rulings)

**RULING 1 — the popup is a PROMOTED D6 firing site (the primary read-and-sign
channel).** D6's original text pinned ONE firing site and framed the popup as a
retry-only fallback. This amendment PROMOTES it: for a human-required notebook
sign-off, when `append-decision` is invoked and the authorship gate would refuse
(no matching human utterance), the server ELICITS FIRST — the popup collects the
human's typed utterance and the append proceeds with it — instead of surfacing a
refusal. The pins:

- **(a) D5's channel lock is UNCHANGED.** Append-decision or nothing — no new
  verb, no skill affordance, no generic ask-the-user surface. This is an ORDERING
  change inside the one existing seam (`McpServer.call_tool` →
  `_elicit_then_retry`), not a new channel. It is realized in the SERVER
  orchestration only: the authorship gate
  (`ops/decision/journal.py::_assert_signoff_authorship`) stays one-definition and
  harness-agnostic — the server elicits AROUND it, the gate is never touched. (The
  existing pump already elicits before any refusal reaches the model, so this was
  a framing promotion + the reliability pins below, not a rewrite of the seam.)
- **(b) The reliability guard is HARD.** If the session's elicitation is
  declared-but-dark (`_client_elicitation_dark`) or the client never declared the
  capability, the primary elicitation is SKIPPED and behavior is EXACTLY the
  pre-promotion refusal → hook path, byte-for-byte
  (`_elicitation_applies` gates all of it). A timeout on the primary path flips
  the dark flag (item 12's contract) and falls back to the refusal — the popup
  must NEVER wedge a sign-off flow that used to work.
- **(c) A valid utterance appends directly.** An `append-decision` that already
  passes the gate (`ok:true`) returns straight through; the popup fires only when
  the gate would otherwise refuse. No popup nagging on already-valid appends.
- **(d) The render relay-due markers are untouched.** The popup SUPPLEMENTS the
  full-render relay; it never discharges it.

**RULING 2 — DIGEST v2: the popup is a SIGNING surface, every element serves one
of three jobs.** The v1 digest was a reading summary (classification, diff
`+/-`, an assertion count, a lint-flag count). v2 rebuilds it so each element
earns its place:

- **Job 1 — BIND.** `audit_id`, section slug, `view_sha12`, and the FRESHNESS
  state. A STALE render (the signed `view_sha` no longer addresses a readable
  on-disk render — since the render is content-addressed by `view_sha`, a drifted
  source moves the address, so stale ≡ missing here) says **STALE — do NOT sign**
  and shows nothing but the pointer. No summarizing a render the human is not
  signing.
- **Job 2 — WHY YOUR JUDGMENT.** The tier-trigger headline (which of
  diff / lint / assertions fired, with counts — derived from the render's own
  `classification` / lint-flag count / assertion count; the D-attention legs); the
  declared-assertion table; the lint-flag NAMES + locations (`rule @ Lnn` /
  `rule @ where`, not a bare count); and per-hunk one-liners from the diff (source
  line range + the first changed line, per-line truncated) — **never the diff
  body**.
- **Job 3 — ROUTE.** The on-disk render path, stated plainly.

**THE HONESTY RULE.** When the honest digest exceeds a byte budget
(`mcp_server._DIGEST_BLOCK_MAX_BYTES`, a last-ditch guard for pathologically many
hunks/flags), the composer does NOT compress harder — it emits
`too large to digest honestly: N hunks, M flags — read the render` + identity +
pointer only. Every capped list (`render_store._DIGEST_MAX_{ASSERTIONS,HUNKS,
LINT_FLAGS}`) DISCLOSES its elision (`… (K more — read the render)`); a silent
drop of a judgment-critical item is the misleading-summary class the rule
forbids, and assertions are the last thing dropped (the composer refuses before
silently trimming them).

**The static-audit honesty note (a recorded judgment call, not a gap covered
over).** The ruling asks the assert table to carry "declared-vs-computed values"
and order "failed asserts first." The trusted render is STATIC by design
(`audit_view`: "No execution ever happens here") — it carries declared assertion
EXPRESSIONS only, and no per-assertion pass/fail or computed value exists anywhere
in the audit path (receipts green a whole SECTION, not an assertion, and are not
in the render body). So v2 reports each DECLARED assertion and marks it
**unverified — static audit, no execution**: the declared side is shown, and the
absent computed side is stated rather than FABRICATED (inventing a computed value
the render does not hold would itself be the misleading-summary class the honesty
rule exists to bar). "Failed asserts first" is honored at the granularity the
data supports: in the static model every declared assertion is unverified and so a
judgment trigger — the digest surfaces them all as the never-silently-dropped
class, and the honest-refusal backstop enforces "the last thing dropped." Reusing
the audit-view producers (diff hunks / assertions / lint flags off the code-written
render), never a re-derivation from source, is the source-of-truth pin.

Edge cases (all handled + tested): a stale/missing render, an
unparseable/header-mismatched render, a sign-off with no bound `view_sha` yet, or
no experiment context on the call ⇒ a single reason-disclosing line (the do-not-sign
freshness line for a bound-but-absent view; `render digest unavailable: <reason> —
open the section render in your Read pane before signing.` for the no-context /
no-`view_sha` cases), never a crash and never an unmarked silent omission.
`read_render_digest` is fail-soft (`None`) exactly like `read_render_header`.

**Symbols:** `mcp_server._render_elicitation_prompt` (takes `experiment_dir`,
delegates the notebook block to `_render_digest_block`), `_render_digest_block`
(the three-job composer + the honesty-rule budget), `_tier_trigger_headline`;
`ops/notebook/render_store.py::{RenderDigest, read_render_digest}` (v2 fields:
`tier`, `diff_hunks`/`diff_hunk_count`, `lint_flags`/`lint_flag_count`; re-exported
through the `ops/notebook_view` facade the T8 gate uses, per the subject-import
discipline). The firing promotion lives entirely in
`McpServer.call_tool`/`_elicit_then_retry`/`_elicitation_applies` — the gate is
untouched. Tests: `tests/ops/notebook/test_render_store_digest.py` (the parser
pinned against `write_render`, incl. tier + hunk one-liners + flag names),
`tests/test_mcp_elicitation_render.py` (the three jobs, the honesty-rule oversize
refusal, disclosed elision, the stale/missing/no-`view_sha` do-not-sign,
non-notebook unchanged), `tests/test_mcp_elicitation_firing.py`
(`test_primary_popup_fires_before_any_refusal_for_notebook_signoff`,
`test_valid_utterance_append_never_pops`, plus the standing dark/timeout/undeclared
guards that pin the byte-for-byte fallback).
