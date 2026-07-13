# Fable-pass rulings on the two skeptic severity splits (2026-07-13)

Both splits were adjudicated inline by the sweep orchestrator (Fable) after
reading the decisive code; the rulings and their reasoning are also embedded
in `verified-findings.json` under each finding's `adjudication` field.

## F12 — post-park overnight consent is never consulted → RULED **high**

The skeptics agreed on mechanism and reachability ("a natural operator flow");
the split was impact-only (fail-safe park vs lost night). Decisive evidence:
the morning brief (`ops/overnight.py:1662-1683`) renders the consent and
`consumed_count: 0` with **no field explaining why nothing was consumed** — the
lost night is silent and undiagnosable from the only surface the human reads.
The overnight mechanism's entire purpose is defeated, recurring nightly, until
a human happens to commit `y` while awake. State stays fail-safe (no wrong
action), which is what the medium vote weighted; silence + defeated purpose
outweigh it under the sweep rubric ("silently wedges a campaign").

## F37 — federated-SLURM liveness/kill builders omit `-M` → RULED **high**

The split was reachability-only ("config-gated"). Decisive: once
`slurm_cluster` targets a non-default cluster the failure is **deterministic
on the first submit** in SLURM multi-cluster mode (what `--clusters=`
implements): monitoring blind, run settled abandoned while burning allocation,
`scancel` a silent no-op with false "confirmed gone". A supported config knob
that breaks 100% of the time when used is not a rare-race mitigation. Caveat
kept: in a true federation, squeue's default federated view may partially mask
the liveness half — the F37 live-cluster check in `critic-gaps.json` stays
mandatory before the fix shape is finalized.

## Net effect

Confirmed-severity distribution moves from 18/28/9 (high/med/low) to
**20/26/9**. Also folded into `verified-findings.json` as `post_verification`
fields: the MCP smoke showed `mcp-serve` defaults `HPC_SSH_ENGINE=asyncssh` ON,
so F55/F56's "opt-in gated" reachability rating is wrong for MCP-driven usage
(default-on there), and F46 was reproduced live (see `live-smoke-notes.md`).
Line pins re-validated: `git diff fb8428c..HEAD -- src/ tests/` is empty.

## Fable hard-kill panel (2026-07-13, appended)

A final 5-agent Fable-tier panel re-judged all 20 highs + 2 WEAKs from a
votes-stripped brief (no prior verdicts shown), guilty-until-proven, with
empirical repros. Motivation: the Opus verification wave confirmed 55/55 with
zero refutations, a pattern consistent with deference by a tier below the
finders. The panel changed 8 of 22 verdicts; per-finding reasoning, fix-sketch
concerns, and new evidence are in each finding's `fable_panel` field in
`verified-findings.json`.

Downgraded (Opus over-rated): F01, F06, F07, F13, F29 high→medium;
F30, F48 high→WEAK/medium. Upgraded (Opus wrongly weakened): F39
WEAK/low→CONFIRMED/medium. Held at high, independently matching the inline
rulings above: F12, F37. Net: high 20→13, medium 26→32, WEAK 2→3.

The 13 surviving highs — F05 F11 F12 F17 F18 F23 F35 F36 F37 F47 F53 F54 F55 —
are the load-bearing set; several carry fresh local repros (F53 rsync
interrupt, F35 rc-127 empty-queue, F18 AST floor, F36 upstream OpenPBS/TORQUE
source). The panel also surfaced fix-sketch soundness gaps the swarm must heed
(e.g. F07's errors-list conflates optional-sidecar misses with metric misses;
F17's `state='failed'` collides with requeue → F23 thrash; F35's stderr rule
must allowlist FATAL strings, not benign ones, to avoid re-breaking the 07-11
#5 fix; F37's `-M` value must resolve the SLURM cluster name, not the config
key; F47's reconstruct-and-dedup arm would strand unlaunched waves).
