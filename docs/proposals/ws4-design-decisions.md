# WS4 lint + contract — design decisions

The WS4 implementation (`scripts/lint_skills.py` + `tests/contract/*`)
landed with 5 open design questions in its report. Verdicts + the
landed implementation below.

## 1. Lint severity ladder

> Should `prose-decide` and `trailing-narration-example` (both at zero
> today) flip to hard fail right now?

**Yes.** Both rules fire deterministically against synthetic input (the
WS4 "verify-can-fire" gate confirmed it), the per-skill count is zero
across every skill, and any new violation is a real regression of the
0.10.0→0.10.6 prose-fix sequence. Keeping them at `warn` only invites
re-introduction.

**Landed**: severity for both rules promoted from `"warn"` to `"error"`
in `scripts/lint_skills.py`. The rationale lives in each rule's
`description` field for future audit.

## 2. Slash-command wrappers

> The `/submit-hpc`-style files under `src/slash_commands/commands/` are
> human-dialog wrappers and legitimately need free-form prose. Currently
> out of scope — should the linter have a separate looser mode for them,
> or just stay silent?

**Skip them entirely.** The slash-command wrappers exist precisely to
elicit caller intent in free-form prose ("what's your goal?", "which
cluster?") — applying the agent-skill lint rules to them would force
either many false positives or a confusing severity ladder.

**Landed**: `SKILLS_DIR` in `lint_skills.py` is already
`src/slash_commands/skills/` and never walks into `commands/`. No code
change required — the silent skip is the design.

## 3. `step-without-action-ending` over-fires

> Refine the rule (e.g. require an explicit "auto-resolve" or
> "ambiguities" keyword nearby) or accept the noise?

**Refine.** The 29 false positives were almost all bookkeeping headings
where the action is implied by branch bullets enumerating a finite
outcome set (`terminal` / `abandoned` / `in-flight`, etc.). The rule
should treat those as action-endings.

**Landed**: the bookkeeping regex in `check_step_ends_in_action`
expanded to cover `Resolve <field>`, `Cache check`, `Pre-fill from
memory`, `Detect or scaffold`, `Identify the run`, `Cover non-axis`,
`Skip if caller supplied`, `Try the cheap match`, `Branch on`. Tracked
inline with a 2026-06-04 audit-rationale comment.

## 4. WS3 xfail rendezvous

> When WS3 lands `failure_features` for a given verb, the xpass surfaces
> — but does maintainer drop it from `XFAIL_NO_FAILURE_FEATURES`
> manually, or should the test promote a passing-xfail to a hard
> assertion automatically (`strict_xfail`)?

**Yes — strict_xfail in principle.** Auto-promoting xpass → fail
prevents the punch list from quietly draining without anyone updating
the catalog. Implementation deferred: the current contract tests use
dynamic `pytest.xfail()` calls inside test bodies (not
`@pytest.mark.xfail(strict=True)` markers), so flipping the strict bit
requires refactoring to parametrize-level markers first. Open follow-up:
convert `XFAIL_NO_FAILURE_FEATURES` + the dynamic `pytest.xfail` calls
into `pytest.mark.xfail(strict=True, reason=...)` parametrize entries.
Sized ~80 LoC across `test_primitive_remediation.py` + `test_schema_roundtrip.py`.

## 5. Fixture seam ownership

> The schema-roundtrip "known-good fixture" punch list is 28 entries
> deep. Is that one big WS5 fixture sweep, or do we crawl it as part of
> each downstream feature touch?

**Crawl per feature.** A 28-entry fixture sweep is too big to land
cleanly in one PR (review will get tangled with the verb behaviour
changes); crawling per feature couples the fixture to the change that
needs it, which is also the change that exercises it. The xfail entries
naturally drain over time without an explicit sweep PR.

**Landed**: documented as policy; no code change. The xfail catalogue
in `test_schema_roundtrip.py` is the punch list.

## Summary

| # | Question | Verdict | Code change |
|---|---|---|---|
| 1 | Severity flip? | Yes | `scripts/lint_skills.py` — 2 rules promoted |
| 2 | Looser mode for slash wrappers? | No, skip entirely | None — `SKILLS_DIR` already excludes |
| 3 | Refine `step-without-action-ending`? | Yes, expand bookkeeping regex | `scripts/lint_skills.py` — regex expanded |
| 4 | strict_xfail for WS3 rendezvous? | Yes in principle, refactor needed | Deferred — open follow-up |
| 5 | Fixture seam: sweep vs. crawl? | Crawl per feature | None — policy doc only |
