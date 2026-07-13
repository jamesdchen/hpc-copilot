# Engineering principles — narrative & drift-log history

Companion to [`engineering-principles.md`](engineering-principles.md). That page
is the **normative** reference: the two irreducible judgment rules ("verify a
guard can actually fire" and the four-question library-knowledge boundary test)
plus the enforcement maps naming the lint/test that holds each mechanized line.
CLAUDE.md points there, and it is the entry point maintainers read.

This page holds the **per-incident narrative and drift-log history** that
explains *why* the enforcement looks the way it does — the record kept so the
next "let's just document it" proposal has the base rate, without bloating the
normative page. Nothing here is load-bearing for CI; it is context. When an
enforcement-map row cites an incident, the terse justification stays on the row;
the longer story lives here or in the design docket the row names.

## Drift log (why prose alone failed)

Recorded so the next "let's just document it" proposal has the base rate:
the `CLAUDE.md` predecessor of the engineering-principles page asserted three
present-tense facts. By 2026-06, `_FAILURE_CATEGORY_PATTERNS` no longer existed
(collapsed into `CLASSIFIER_CATEGORIES`; the prose still said "three tests
iterate it") and the deploy-ship list it cited omitted `executor_cli.py`. The
lints and tests from the same era all still held. Facts belong where they are
checked; the normative page cites sources of truth
(`transport._build_deploy_items`, the lint's `KNOWLEDGE_PACKAGES`) instead of
restating their contents.
