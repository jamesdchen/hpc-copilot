---
status: planned
---
# Unified render v1 — O3+ chunked read-and-sign (build spec)

**Status: PLANNED, user-ruled 2026-07-10 ("sounds lit as a version 1 — we'll
update it when users start using it").** The build spec for the unified-render
ruling (`docs/design/mcp-elicitation.md` drift log 2026-07-10: popup = THE
default read-and-sign surface, one composer, embed the render's own bytes,
chunk-never-truncate) in the O3+ shape. Grounded in the researched protocol
facts (`docs/design/mcp-elicitation-facts.md`). Cite `path::symbol`, never
line numbers. Drift log at the foot.

## The shape (O3+)

Three concerns, separated:

* **READING** — the full render file remains the whole-view convenience
  (Read pane / editor), already ruled demoted to optional.
* **ENGAGEMENT** — sequential popup CHUNKS, each carrying its own header
  (what this chunk does + how the experiment data passes through it), each
  individually acknowledged. Chunked reading forces per-chunk processing —
  a feature, not a limitation (user 2026-07-10).
* **WHOLE-CODE PROPERTIES** — code's job, not the human's scroll: the
  mechanical checks (lint, section contracts, diff coverage, B3 evidence)
  feed a TERMINAL SYNTHESIS CHUNK; the human signs the whole only after the
  chunk walk AND code's whole-view report.

## Settled decisions

### D1 — Chunking is section-aligned, disclosed, never truncating
The composer splits the render at SECTION boundaries into chunks under the
per-elicitation budget (set empirically by D6's probe — the spec guarantees
no size limit, but the client's rendering is undocumented). A single section
over budget splits at hunk boundaries with a disclosed continuation marker.
Every chunk's message carries `[k/n]` (protocol has no progress semantics —
`mcp-elicitation-facts.md` §3 — so the disclosure rides the message text).
Sequential `elicitation/create` per chunk is protocol-valid (§3).

### D2 — Per-chunk headers live INSIDE the one artifact
The header is composed by the ONE composer into the render file itself (the
render's own bytes — nothing to drift against; the unification rule). Per
chunk: the section slugs it covers, the section titles/prose already in the
render, and the section's B3 runtime-evidence lines when present
(`ops/notebook/audit_view.py` section join — "how the data passes through
this chunk" IS the first→last-per-changed-observable block). Popup chunks
are byte-slices of that one artifact; the Read-pane view shows the SAME
bytes.

### D3 — The terminal synthesis chunk
The last chunk is code's whole-view report + the sign-off:

* section inventory (slug, status, section_sha, view_sha12 each);
* lint flag totals; declared-assertion counts (static-audit honesty note
  applies — declared, unverified, stated not fabricated);
* per-section evidence map (runtime evidence present / stale-elided / none);
* chunk-ack coverage (k of n acknowledged — D7);
* the typed sign-off form, binding the module `view_sha`.

DELIBERATELY NOT in v1: cross-section conservation (B3 gaps are parked for
problem understanding — user 2026-07-10; the synthesis chunk gains it when
B3's upstream question is answered). Projections per surface remain UX
notes, not design commitments.

### D4 — Sequencing protocol
Ack-only elicitations for chunks 1..n-1 (flat `requestedSchema`, one enum:
acknowledge / park; free-text nudge optional). The SIGN-OFF form appears
ONLY on the terminal chunk. decline/cancel at ANY chunk parks the audit
(no sign-off, no partial attestation); re-entry resumes at chunk 1 with the
prior acks shown as already-covered (acks are per-`view_sha`, so a source
edit voids them by construction).

### D5 — Display receipts via the harness's Elicitation hooks
`agent_assets.py` installs the Claude Code `Elicitation` /
`ElicitationResult` hooks (≥2.1.76) to journal display/answer events —
the harness-LOCAL display receipt closing declared-but-dark on our primary
harness (upstream spec filing DROPPED per user ruling;
`mcp-elicitation-facts.md` §4a). `ops/harness_capabilities.py` gains the
detection seam (`elicitation-display-receipt`). The existing
timeout-undisplayed → hook-path degradation stays.

### D6 — The rendering probe (one-time per harness version)
The spec does NOT guarantee markdown/code rendering in the popup (§2 —
client discretion, Claude Code undocumented). A calibration elicitation
carries a markdown + code + diff sample; the human's `y` ("renders
readably") mints a journaled capability record. Unconfirmed → the composer
emits the conservative preformatted-plain form. The record keys on harness
name+version (re-probe on version change).

### D7 — Engagement ledger (the y-ack-ease guard, v1 = visible not blocking)
Chunk acks journal to a code-authored per-audit ledger (the
overnight-consumption-ledger idiom — its own jsonl beside the audit journal,
NEVER the decision journal). The terminal sign-off record carries ack
coverage (`chunks_acked: k/n`) — the journal shows exactly how much of the
render was walked before the attestation. v1 makes this VISIBLE, not
blocking; whether coverage gates the sign-off is a later ruling with usage
evidence ("we'll update it when users start using it").

## Build waves (file-disjoint, dispatch-ready)

| Wave | Files | Work |
|---|---|---|
| W1 composer | `ops/notebook/render_store.py`, `audit_view.py` | header-annotated render + chunk index (section-aligned split points); digest v2's three jobs fold into the header of chunk 1 (the ruling's "summary = the render's own header") |
| W2 server loop | `_kernel/extension/mcp_server.py` | sequential elicitation walk (generalize `_elicit_then_retry`), ack collection, terminal sign-off form, park-on-decline; caps + [k/n] |
| W3 receipts | `agent_assets.py`, `ops/harness_capabilities.py` | Elicitation-hook install (additive/idempotent, the `_merge_hook_entry` form), display-receipt journal, capability seam, D6 probe flow |
| W4 ledger + tests | `state/notebook_audit.py` (ack ledger fns), `tests/` | engagement ledger, conformance additions, dark-harness degradation pins (no elicitation → today's path byte-identical) |

## Degradations (all disclosed, never silent)
No elicitation capability → today's digest-v2 + Read-pane + hook path,
byte-identical. Rendering unconfirmed → plain preformatted form. Oversize →
more chunks, never truncation. Hooks uninstallable → receipts absent, the
capability reads unknown, timeout-undisplayed rule carries the risk.

## Test plan (sketch)
Section-aligned split (never mid-section without the continuation marker);
[k/n] present in every message; ack ledger append + view_sha-voiding;
decline parks with no attestation; terminal chunk carries the full
inventory + ack coverage; D6 unconfirmed → plain form; dark harness →
byte-identical today-path.

## Drift log

- **2026-07-11 (run #12, PRIORITY PROMOTION — user-flagged): on Claude Code
  the chunked popup is the SOLE review channel, not a UX improvement.** The
  live popup exercise established that Claude Code has NO out-of-band render
  review surface: terminal `[file]` links are not a relay (run #11), and the
  expanded Read result pane truncates long renders — fidelity, not
  reviewability (run #12 finding 11 addendum). Every fallback rung below the
  popup is either dead or the untrusted model-retyped copy. Interim shipped:
  the popup embeds the bounded diff body (6 KB, disclosed truncation —
  `1bd16c33`); this plan's chunking is what removes the bound. The rendering
  probe (wave 4) should run EARLY to size real client limits — the 6 KB
  budget is a guess.
