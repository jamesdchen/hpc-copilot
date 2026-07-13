# Fable sweep 2026-07-13 — machine-readable appendix

Raw artifacts of the experiment-runtime breakdown sweep run at HEAD `fb8428c`
(code-identical to `d731b12` except uv.lock). The narrative plan doc that ranks
these into work packages lands as a sibling file; these JSONs are the source of
truth the hardening swarm should consume programmatically.

- `verified-findings.json` — 57 canonical findings (55 CONFIRMED / 2 WEAK,
  18 high / 28 medium / 9 low post-verification). Each record carries: the
  finder's failure scenario + fix sketch + evidence, the triage spot-check
  rationale, cross-lens merge/related annotations (compounding chains), and
  every adversarial skeptic vote with reasoning (2 independent votes per
  original-high, 1 per medium/low; all Opus, refute-by-default).
  `severity_disputed` marks the two findings (F12, F37) where the two skeptics
  split on severity — conservative rating kept in `final_severity`.
- `critic-gaps.json` — the completeness critic's 8 unswept areas (each with a
  concrete probe for a follow-up sweep) and 8 live-cluster checks for
  scheduler-domain assertions that could not be tested in-sandbox.

Method: 10 lens finders (submit/resume, transport-ssh, scheduler backends,
cluster runtime, monitor/recovery, aggregation/results, state/journal/
concurrency, campaign/overnight, agent surface, operator-error/preflight),
mandatorily deduped against docs/internals/bug-sweep-2026-07-11.md (fixed +
refuted) and docs/plans/architecture-review-2026-07-13.md (banked); 4-agent
triage (3 shards + cross-lens dedup); 14-agent adversarial verification;
1 completeness critic. Line numbers are as of `fb8428c` — verify live before
acting, per the repo's own rule.
