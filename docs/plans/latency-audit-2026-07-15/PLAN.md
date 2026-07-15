# Latency elimination plan — 2026-07-15 (Fable fleet audit)

Status: **BANKED — ranked, nothing executed.** Produced by a 7-agent Fable
fleet (6 per-stage code censuses + a measured-evidence miner over the
run-12/13 logs and journals) + a synthesis critic. Full per-item detail
(mechanism, file:line, evidence, dedup notes) in **`census.json`** in this
directory. Maintainer standing ruling applies: proper fixes only —
eliminate the work over speeding it up.

**Headline: a typical healthy submit→canary→fleet→harvest→aggregate cycle
saves ~60–120 minutes if this plan lands; failure-shaped nights compound
far larger (run-12's 5.7 h aggregate night collapses to <1 h under ranks
2+17; the 11 h dead-watch window dies under rank 4).**

## The ranked 25 (rank · stage · cost · fix · effort)

Top tier — each independently worth minutes-to-hours per run:

1. **Canary sidecar false-negative spin** (B, ~30 min MEASURED per gated
   submit): the `-canary2` sidecar is never shipped; the verifier polls
   rc=2 for half an hour on a 97 s job (finding 7's latency face). Fix:
   mint/ship ALL canary sidecars pre-deploy; deterministic reporter errors
   (`sidecar_not_found`) escalate instead of re-polling. (M)
2. **Pull engine parity** (D, 40–90 min MEASURED double-pulls): the pull
   side gets everything the push side got tonight — server-side filtered
   `find | tar c` pull, content-hash delta, batched durable checkpoints;
   replaces `_scp_pull`. (L)
3. **`HPC_SSH_ENGINE=asyncssh` default** (E, ~2–6 min handshake/run +
   ~9 min breaker dead-waits when they fire): one persistent channel per
   host; ControlMaster is structurally broken on native Windows so every
   exec pays a 1–3 s cold dial today. Reaper + dispatched-guard defects
   already fixed. GATED on the queued post-run-13 engine-default decision
   (scope-by-constraint trigger). (S)
4. **Preamble-free liveness poll** (B/C/E/F, ~490 s dead per s3 worker +
   45–60 min of fork-dead watches over run-12's 11 h): poll scheduler
   state via `ssh_batch_scheduler_states` while queued/running; the full
   conda-activated reporter runs exactly once, on terminal. (M)
5. **Remote-side push-manifest cache** (A, ~12 min MEASURED per S2 push):
   the remote snippet re-sha256s the whole tree every push — give it the
   finding-6 (size, mtime_ns) cache via the manifest the push already
   writes. (M)
6. **Eager announce dir** (C, 20–25 min per pre-first-marker reconcile):
   dispatcher creates `.hpc/announce/<run_id>/` at submit so the
   one-readdir census owns the lifecycle from tick 1. (M)
7. **Compress the tar|ssh stream** (A, ~5–7 min per GB-scale push over
   the ~2 MB/s VPN): `tar cz`/`--zstd` default on the rsync-less path;
   fast-LAN opt-out stays as the env knob. (S)

Second tier (each ~1–20 min or failure-class killers): 8 parallel double
canary (M, dep 1) · 9 cluster-final reduce default #254 (M) · 10 canary
TTL cache on the gated path — **NEEDS RULING** (#249, S) · 11 run-scope
the `pull_summaries` pull (the finding-19 fix never reached this surface,
S one-liner) · 12 in-process dispatch for same-package children (L;
measured 1.3–7.2 s interpreter+registry per subprocess × ~6/tick) · 13
fast-path granularity: a `register_cli` plugin currently disqualifies
EVERY verb from the fast path — 7.2 s vs 1.5 s per call in the live env
(M) · 14 submit-preflight verdict TTL (S) · 15 one pruned tree walker for
the 3–4 per-submit local walks (M) · 16 batch wave combines into one exec
(M) · 17 deterministic-failure memo for aggregate re-runs (M; run-12 paid
two byte-identical ≥1800 s failures).

Third tier (post-engine / constants): 18 compound remote scripts for the
verify tail + pre-submit probes (only if rank 3 stalls) · 19 skip the
reporter walk when announce evidence suffices · 20 ThreadPool the cold
local hash (37 min → ~6–10 min) · 21 poll-loop constant hygiene · 22
event-driven terminal notice (remote long-poll; deps 3+6) · 23
fleet-level census exec (L; re-measure after 3) · 24 engine idle-recycle
cadence awareness · 25 connect-failure ladder + ENOENT classification.

## Sequencing

- **Wave L1 (S-effort, no deps):** 7, 11, 14, 20, 21, 25 — plus 10 the
  moment its ruling lands, and 3 the moment run 13 closes (both are
  one-line-ish flips with disproportionate payoff).
- **Wave L2 (M, the structural eliminations):** 1, 4, 5, 6, 9, 13, 15,
  16, 17, 19.
- **Wave L3 (L, the big rebuilds):** 2 (pull parity — highest single
  payoff), 12; then 22/23/24 re-measured after the engine flip.

## Rulings needed from the maintainer

- Rank 10: honor the canary TTL cache (#249) on the gated submit-s2 path
  (documented design concern — the cache exists, the gate ignores it).
- Rank 3: the engine default flip is already the queued post-run-13
  "engine-default decision" — this audit adds the measured case for YES.

## What no census covered (follow-ups)

Cluster-side env skew as a latency root (the py3.11→3.13 reload warning
behind run-13's sidecar_not_found class); canary QOS/express-partition
placement (queue wait is a spec choice for canaries); LLM/harness relay
round-trip latency between block boundaries; Windows Defender attribution
of the NTFS walk/hash costs; the wheel-build + three-env reinstall loop;
detached spawn cost + journal growth curves; DTN/remote-compress bulk
planes; whether mcp-serve keeps a warm registry between tool calls; local
reduce CPU at 2700-task scale.

## Drift log

- 2026-07-15: banked from the fleet audit. Nothing executed.
