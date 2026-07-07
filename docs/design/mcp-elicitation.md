# MCP elicitation — the bidirectional protocol upgrade (design + implementation plan)

**Status: PLANNED (2026-07-07), not yet implemented.** This plan settles how
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
   The server is synchronous and single-threaded, so "waiting" means the
   handler itself pumps stdin: a `_request_from_client(method, params,
   timeout_s)` that writes the outbound request, then reads/classifies
   messages until the matching response arrives or the deadline expires —
   servicing interleaved client REQUESTS inline while waiting (see D3), so
   the wait does not head-of-line-block the rest of the session (the conduct
   rule 11 lesson, `_refuse_blocking_over_mcp`).

Estimated size: ~120–180 lines in `mcp_server.py` plus a fake-client duplex
test harness. No threads, no asyncio, no new dependency.

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

**Recorded revisit trigger** (the scope-by-constraint discipline): the moment
a SECOND server-initiated MCP feature is wanted (sampling, roots,
progress-with-cancellation, subscriptions), the calculus flips — a growing
hand-rolled bidirectional surface is exactly the divergence risk the
outsourcing doctrine names, and SDK adoption (likely path (c), done once,
with the parity oracle retired deliberately) becomes the default. One
outbound request type is a bounded extension; three is a protocol
re-implementation.

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
elicitation cannot arise otherwise (single-threaded server), so the cap is an
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
(generic). The elicitation hook must not parse prose. Smallest correct
change: the authorship-gate refusals carry a structured discriminator the
envelope preserves (e.g. `data.authorship_evidence = "missing"` or a distinct
`error_code: authorship_evidence_missing` — implementation task decides after
checking the error-envelope contract's compatibility rules; a `data` field is
additive and back-compatible, so it is the default). The MCP layer keys on
that, nothing else.

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
  run.
- No SDK, no new dependency, no change to `pyproject.toml`.

## Task waves (file-disjoint, Opus-sized; tests ride each task)

**Wave 1 (parallel):**

- **E1 — the bidirectional pump.**
  `src/hpc_agent/_kernel/extension/mcp_server.py`: outbound id counter +
  namespace, pending-response slot, message-kind classification in `serve`
  (request vs notification vs incoming response), the
  `_request_from_client(method, params, timeout_s)` wait primitive that
  pumps interleaved requests inline (elicitation-suppressed nested dispatch,
  depth cap asserted), late-response drop, stderr telemetry line. Store
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

(Empty — populated during implementation, the notebook-audit.md convention.)
