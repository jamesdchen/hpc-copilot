# Trainwreck audit — har_base_sweep-53c27e42, 2026-07-30 (forensic validation)

Four session transcripts mined (2,257 events). Headline: **the compute was
never the problem** — 2100/2100 tasks, 0 failures, ~44 s/task. Of ~12.4 h
from first greenlight to results: **~6.6 h permission-gating + stale-state
wedging, ~3.6 h VPN flap + route blindness, ~1 h contract round-trips.**
Longest stall: **6h01m** — the auto-mode classifier denied submit-s2 on both
CLI and MCP, and the agent "parked, honestly, one approval short of done"
under an explicit drive-to-completion directive, with nothing to wake it
(U10). 21 classifier denials, 27 spec_invalid, 13 terminal.json deletions
(2 pasted by the human), 12 human interventions, 9 re-drives.

## Validation of the 2026-07-30 program (@ faffabc6 + fleet in flight)

[A] classifier double-gate: PREVENTED on the verb path (consent hook) /
UNADDRESSED for non-verb denials (9 of 21: rm/ls/python/read-only ssh —
→ U-A below). [B] route blindness: PREVENTED (the reachability lie) +
NAMED. [C] breaker misdiagnosis: NAMED. [D] flap staging: PREVENTED+NAMED.
[E] zombie submitting: PREVENTED (but see U1 — a DIFFERENT wedge). [F]
canary wait: PREVENTED in build (P2.c). [G] contract-by-refusal: PREVENTED
for sign-off/composed-y; residue in U7/U8. [H] console flashing: PREVENTED.
[I] SLO: NAMED for S2 only (S3/S4 uncovered).

## The unaddressed docket (evidence in the mining report; owners TBD)

- **U1 terminal-record replay wedge** — `submit-sN.terminal.json` replays
  forever; 13 manual deletions, one 31-min stall. Needs a first-class
  re-drive path (supersede/invalidate verb or staleness-aware replay),
  never `rm`. (Collides with P2.c files — build after fleet lands.)
- **U2 warning-not-gate** — the axes/task_count mismatch warned at 09:16,
  detonated 11h21m later (wave planner parked the array at 600/2100 ×4).
  A preflight inconsistency between declared axes and task_count must park,
  not warn. (After P2.c.)
- **U3 operator-box env fragmentation** — hpc_agent unimportable in the
  science env, ruff off PATH, rsync ON DISK but off PATH (cost one 266MB
  re-ship). Doctor gains an operator-env section; rsync discovery uses the
  HPC_RSYNC_BINARY probe list instead of bare PATH.
- **U4 no incremental harvest** — 1741 finished tasks unreadable for 2h+
  ("why aren't you streaming the results back?"). DISPATCHED 2026-07-30.
- **U5 cluster-side combiner missing, silently** — `[combiner] ERROR: no
  _combiner/h`; human hand-launched the reduce. LANDED 2026-07-30 (deploy
  disk-check override + guard in all combine runners + redeploy-runtime).
  Named residuals, not silently closed: D1 skip-staging re-entry is
  mitigated at combine time only (submit-time probe = future work), and the
  disk-check covers the combiner only — ~15 sibling deploy artifacts ride
  the same attests-but-never-checks cache (the 2026-06-08 templates wipe
  proves the class is real).
- **U6 unsealed-tree submission** — 5 submissions against the shared base
  tree, disclosed but ungated; provenance hole. Needs a ruling (gate vs
  disclose) then a seal-on-reentry build.
- **U7 CLI/registry surface mismatch** — 27 spec_invalid; describe schema
  vs invoke surface vs registry names disagree. Mechanize: one contract
  test sweeping every verb's registry-name/CLI-name/spec surface triad.
- **U8 consent-sequencing refusals mid-crisis** — rule-9 divert +
  predecessor-not-clean refused VALID greenlights at the worst moments.
  Both need the composed-ask treatment (the refusal must render the
  corrected record ready to journal).
- **U9 sign-off voided by re-render** — freshness law working as designed
  but hostile: consent given at 09:02 voided by a sha move at 09:04.
  Ruling needed: a re-render whose diff-to-signed-view is EMPTY (byte-equal
  section, moved ingredient) should carry the sign-off forward, or the
  re-render must happen BEFORE the sitting (P2.b's chain now sequences
  this — verify it kills the class).
- **U10 no dead-man's switch** — the 6h park under a drive-to-completion
  directive. The operational-actor design (docs/design/operational-actor.md)
  + a park-with-expiry escalation (PushNotification / re-probe cadence)
  is the fix; build after P2.c.
- **U11 cross-session task-output collision** — harness-level (Claude
  Code temp dirs), not repo. Reported upstream-worthy; no repo action.
- **U12 pre-stage smoke self-disables** — the import check skips exactly
  when the local interpreter lacks the dep; the cluster canary catches it
  later at 10x the cost. Make the smoke run under the CLUSTER env probe
  (or the readiness env sensor), never silently skip.

## Numbers to beat in run 16

y→array 2h24m (post-unblock) / 10h40m (from first directive); target with
the landed program: minutes to array after one grant + sign-offs. SLO now
measures it; the morning brief renders it.
