---
status: reference
---
# MCP elicitation — researched facts (2026-07-10, for the chunked-render deliberation)

Sourced from the MCP spec (2025-06-18 + 2025-11-25) and Claude Code docs;
each fact tagged SPEC-GUARANTEED / CLIENT-DISCRETION / GAP. Full citations in
the session research pass; spec URLs:
modelcontextprotocol.io/specification/2025-11-25/client/elicitation.md.

1. **Schema (SPEC):** `elicitation/create` = free-text `message` +
   `requestedSchema` (FLAT PRIMITIVES ONLY — no nesting) in form mode;
   2025-11-25 adds **URL mode** (`url` + `elicitationId`, out-of-band browser
   interaction with a `notifications/elicitation/complete` completion leg).
   Response actions: accept / decline / cancel.
2. **Rendering (CLIENT-DISCRETION):** NO spec guarantee that the message
   renders markdown or code blocks, and NO documented length limit — Claude
   Code's exact rendering (truncation, monospace, highlighting) is
   UNDOCUMENTED. ⇒ the popup-as-THE-render-surface has an unverified
   rendering leg: an empirical rendering probe is a build prerequisite.
3. **Sequential elicitations (UNGUIDED-VALID):** one blocking
   `elicitation/create` per chunk, back-to-back, is protocol-valid; no
   progress semantics (the [k/n] disclosure rides the message text).
4. **Display receipt (GAP — CONFIRMED):** no ack that the dialog was SHOWN
   (vs answered); "never displayed" is indistinguishable from silence.
   Upstream filing stands. TWO local mitigations discovered:
   a. **Claude Code ≥2.1.76 ships `Elicitation` / `ElicitationResult`
      HOOKS** — a client-side seam WE can install (agent-assets) to journal
      display/answer events locally: a harness-specific display receipt
      that closes declared-but-dark on Claude Code without waiting for the
      spec.
   b. **URL mode** gives out-of-band display with an engagement callback —
      spec REQUIRES the client show the full URL; phishing-mitigation
      identity binding is specified.
5. **Security (SPEC):** never put secrets in the message; clients must show
   which server asks; rate-limiting recommended; URL-mode identity binding
   (same user completes as initiated) is MUST.

## Options for the across-whole-code concern (deliberation input)
- **O1 — chunks only:** per-chunk header + ack; whole-code properties are
  invisible to the human. Weakest.
- **O2 — hybrid URL mode:** full render served out-of-band (URL mode, local
  page) for whole-code READING + form-mode chunk acks for per-chunk
  ENGAGEMENT; completion notification = engagement evidence. Heaviest;
  adds a local HTTP surface.
- **O3 — chunks for engagement, code for whole-code properties:** the
  chunked popups carry per-chunk headers (what the chunk does, how the data
  passes through); ACROSS-chunk properties are exactly what mechanical
  checks (lints, diff passes, section contracts, trace joins) already
  patrol — the human is not the whole-code scanner, code is; the Read pane
  stays the optional whole-view convenience (already ruled demoted).
  Cheapest, most consistent with the tiering doctrine.
