# Ruling triage — 2026-07-20

Supersedes the question lists in the four banked memos (`banked-program-repro-phase2.md`,
`banked-f6-contributor-gate.md`, `banked-completeness-verb.md`,
`banked-wsl-multiplexing-spike.md`). Audit verdict: of 17 relayed questions, **6 remain
genuinely the user's**; 7 are withdrawn as doctrine-answered, 2 are parked pending spike
data, 2 ride a parent question. Each withdrawal cites the doctrine that answers it; any
user veto flips that question back open.

## Withdrawn (doctrine-answered — proceed on the cited ground unless vetoed)

| Q | Resolution | Ground |
|---|---|---|
| P1 | Doc-first (`docs/design/program-reproduction-phase2.md` before build) | The banked item itself demands "a user-facing design sentence"; every landed phase shipped doc-first. *Soft withdrawal — veto = build-then-doc.* |
| P4 | Finding-only disclosure, full k/N denominator | No-silent-caps standing posture; (c) contradicts the settled drift-refusal decision (`reproduction-receipt.md`). |
| P6 | No claim-check→recorded-original upgrade path | Rulings 6a (no memory record) + 6b (naming lock) + `_assert_receipt_kind_matches_baseline` already settle it. |
| F4 | Bare-campaign = require-anchor + disclosure (status quo) | It is the status quo AND `dead-end-disambiguation.md`'s own recommendation; answering changes nothing. |
| C3 | Build completeness conformance triples now | Strictly dominant: independent of the verb ruling, cheap, and the hook logic shipped unpinned. Zero tradeoff — queued as a build, no ruling needed. |
| W3 | Key material decided at daemon-design, not now | The spike memo itself defers it (keyed off E3.2 reliability ≥ 9/10). |
| W4 | Daemon stays opt-in accelerator; dead daemon → byte-identical inline | Doctrine-forced: R4 opt-in grant + guard-integrity law + the landed fail-open rule. |

## Parked (unaskable until the WSL spike runs — E1–E3, human, cluster creds, <30 min)

- **W1** daemon host (WSL-hosted vs asyncssh vs both) — depends on unmeasured E1 failure
  signature + E3.1 socket-filesystem result.
- **W2** caller↔daemon RPC — E3.4 measures exactly this.

## Conditional (ride F1)

- **F2** gate unit (settle-aggregate vs per-run) — moot if F1 = disclose-only; if it
  arises, (a) is the pre-designed clean-repro #2.
- **F3** declaration seat — moot if F1 = disclose-only; if it arises, (a)
  `append-decision` + MH8 is forced by the one-authorship-substrate doctrine.

## Remaining for the user (6)

| Q | Question | ★ |
|---|---|---|
| P2 | Fan-out shape: full blocking chain / **thin minting verb** / planning projection only | b |
| P3 | Cross-run reducer: **receipt-counts rollup** / pooled envelope (forbidden) / `evidence_meets` reuse | a |
| P5 | Sealed artifact: **extend `export-bundle`** / new replay artifact / none | a |
| F1 | Contributor status: gate / **keep disclose-only, revisit at second lab user** / non-binding annotation | b |
| C1 | Build a `completeness` query verb? **yes** / no / fold into status-snapshot | a |
| C2 | Verb-backed capability triple: **relay-enforcement + completeness + omission-gate** / detection-seam trio / defer | a |

All-★ answer line: `P2:b P3:a P5:a F1:b C1:a C2:a`. Each answer fires a builder.
