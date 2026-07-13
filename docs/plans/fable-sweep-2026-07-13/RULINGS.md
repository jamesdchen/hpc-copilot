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
