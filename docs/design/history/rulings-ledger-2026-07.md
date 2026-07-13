# Rulings ledger — 2026-07-07 → 07-12 (INDEX)

An index for double-checking: one line of essence per user ruling + a pointer
to the CANONICAL record (the owning doc's drift log / section — canon lives
THERE; this page is navigation, not restatement; if a line here disagrees
with the pointed record, the record wins).

## 2026-07-12
- **#362 async-refill Phase-1 BUILD ordered (user):** land the opt-in
  continuous-async campaign refill on the block-drive architecture, with
  `campaign-run` as the per-iteration spine and the new `campaign-refill`
  primitive as the actor (re-homed off the deleted `deterministic_resolver.py`
  refill arm) → `campaign-async-refill.md` drift log (v1). Default off is
  byte-identical; the Phase-2 live-verify gate
  (`scripts/campaign_async_live_verify.py`) is UNCHANGED and still gates
  non-experimental status — green unit tests are not "done."

## 2026-07-10
- Three-tier pack distribution (upstream-in-core-repo / lab fork / per-
  experiment .hpc pin) → `domain-packs.md` drift log
- Program templates = derivatives of domain skeletons (`derived_from` at
  template granularity) → `domain-packs.md` drift log
- Unified render: popup = THE default read-and-sign surface, one composer,
  embed the render bytes, chunk-never-truncate → `mcp-elicitation.md` drift log
- Overnight-repair A/B/C1/C2 taxonomy + standing rules → `overnight-repair.md`
- Stop-hook completer (omission/violation/judgment) → `stop-hook-completer.md`
- Ship pack v0.2.0 now; quant/rv two-layer split → harxhar-clean
  `packs/README.md`
- CI on GitHub, never local full suites (standing conduct directive)

## 2026-07-09
- E-render PRIMARY firing + digest v2 (supersedes same-day retry-only + v1)
  → `mcp-elicitation.md` §E-render amendment
- B3 RESOLVED as B3-LEAN → `data-trace.md` Amendment 16
- AVL T9 REMOVE / T3-T5 DEFER post-run-#12 / T6 open →
  `anti-vendor-lockout.md` drift log
- Overnight self-heal (supersedes the wake-liveness defer) →
  `notebook-audit.md` item 8 + `ops/overnight.py`
- Sentinel-ack transport → `connection-broker.md` ruling record
- Bounded auto-prune → `data-manifest.md` ruling record
- Poka-yoke conversions (wake auto-arm / cap defaults / draft-at-pass) →
  `notebook-audit.md` ruling record
- Raw-ssh deny rule; MCP display-receipt upstream filing →
  `anti-vendor-lockout.md` ruling records
- **hatch-vcs versioning (recorded HERE — no owning doc):** the package
  version derives from git via hatch-vcs instead of the static `0.11.0`
  (the "never check version strings, verify by import" trap dies at the
  root). Post-run-#12 batch item.

## The post-run-#12 batch (ratified 2026-07-09/10)
1 stop-hook completer · 2 overnight-repair build · 3 unified render build ·
4 pack v0.2.0 (DONE 2026-07-10) · 5 sentinel-ack · 6 bounded auto-prune ·
7 B3-LEAN section join + trace corpus into verify-relay · 8 poka-yoke
conversions + raw-ssh deny + surprise-tiered briefs (needs run-#12 evidence)
+ draft-at-pass + stale-render auto-regen + hatch-vcs + AVL T3/T5 + pack
12-slug growth (post-signature) + MCP display-receipt filing.

## 2026-07-07/08 (long-settled; owning docs)
Four-layer hierarchy + attempt-vs-run boundary · never-"harxhar" naming ·
sweep-docs-at-build · data-manifest 0a/0b + attention contract ·
attention-queue D8 · onboard-by-reproduction 6a/6b/6c · scope doctrine
(observe/judge/route, never actuate) · data-trace consolidated ruling +
Amendment 14 (G-a) · hyper-palatable sign-off (notebook-audit amendment).

## 2026-07-10 late-session dispositions (user)
- Verification-at-scale synthesis: a TOY idea — banked for the very end,
  after everything else; not a Day-3 gating item.
- AVL T6 → reframed into the external-harness readiness plan (see
  `anti-vendor-lockout.md` drift log).
- Completer echo-class: RULED violation/append-only, never bounces (see
  `stop-hook-completer.md` §2).
- TRADING-REPO SPLIT (new direction, supersedes "sign audit_template_rv.py
  now"): harxhar-clean gets a COPY for building actual trades from the
  research; the 12-slug template lives in that copy, and the trading
  threads (stochastic control / P(pass) work) move with it; the pipeline-v2
  doc + its three signature slots govern the trading side and travel too.
  Signing is deferred to the split repo. NOT during run #12.

## 2026-07-10 mechanize-now rulings (user)
- Pack remedy loop: mechanize NOW (not post-run-#12) — minimal rebuild +
  gate auto-remedy; latency obliterated; seal serves the archive (journal),
  not friction → `domain-packs.md` drift log corrections entry.
- Architecture correction: program templates are CREATED at program init by
  consuming the domain skeleton; programs carry the pinned copy; experiments
  modify variable sections only → same entry.
- The post-run-#12 batch is SPLIT: items that affect future runs move to
  BUILD NOW; only evidence-gated / user-gated / external items stay deferred
  (the split table lives in the session relay + battle plan).

## 2026-07-10 evening rulings + reprioritizations (user)
- PROSE CANNOT BE LOAD-BEARING (standing conduct rule, reapplied): wave-1's
  two prose seats convert to CODE seats — (a) the gate auto-remedy RUNS the
  caller-authored check command itself (subprocess, the executor precedent;
  DP2 = never import/interpret pack logic, subprocessing caller-declared
  commands is already core's executor posture), zero agent turns; (b) the
  on-ramp template default is composed IN CODE and NOT brought to human
  attention at all (silent, disclosed in the record — supersedes
  "confirm-default").
- Unified render: BUILD PARKED pending MCP-documentation research + the
  per-chunk-header deliberation (see mcp-elicitation.md).
- Overnight-repair standing-rules deliberation PARKED (nice-to-have).
- Echo/laundering RE-OPENED at the philosophy level: LLM suggestions are
  DESIRED (human amplification — the helping hand in decisions); the hazard
  is Y-ACK EASE (low-engagement attestation), not model-drafted wording per
  se. Resolution direction to rule: move the guard from wording-originality
  to ENGAGEMENT EVIDENCE at attestation time. OPEN.
- Slots 3.1/3.2/3.3: deferred INTO the trading-repo split as that repo's
  design decisions.
- Trading-repo split data call: vendor parquets COPY + GITIGNORE (local data
  fine; an upstream copy exists with the professor).
- Run #12 NOT yet kicked off → envs SAFE TO REINSTALL; plan: land the
  mechanize-now waves, CI green, fresh wheel + three-env refresh, THEN
  kickoff.

## 2026-07-10 night rulings (user)
- **O3+ ADOPTED as unified-render v1** ("sounds lit as a version 1; update it
  when users start using it"): chunked popups with per-chunk headers +
  terminal SYNTHESIS chunk (code-computed whole-view evidence + final
  sign-off), one composer embedding render bytes, chunk-never-truncate,
  Elicitation-hook display receipts, empirical rendering probe. BUILD NOW.
- **Echo nag-line: REMOVED.** The code-appended "re-affirm in your own
  words" disclosure does NOT survive; echo detection demotes to journal-only
  provenance (no surfaced nag, no bounce). Engagement rides the existing
  digest-read/tiered sign-off gates.
- **Raw-ssh deny: cluster-hosts-only CONFIRMED** + new direction: the deny
  seat should TRANSLATE — when the agent reaches for raw ssh, the
  deny/guard should hint the sanctioned verb for the detected intent
  (ls→dir-digest, tail/watch→status-watch, connectivity→net-triage), "the
  code should translate and invoke what the agent wants to do."
- **B3 gaps: parked for problem understanding** — no cosmetic patches (the
  unchanged-count line withdrawn); the unchanged-observable/composition
  gaps are upstream questions; solve B3 fully when run evidence shows what
  the problem actually is.
- **MCP display-receipt upstream filing: DROPPED** (local Elicitation-hook
  receipts suffice).
- **Trading-repo split: the SPLIT ITSELF IS NOT DEFERRED** (correction of my
  earlier recording) — only the build-out of the trading content is
  post-split; the copy can be created now (copy + gitignore parquets).
