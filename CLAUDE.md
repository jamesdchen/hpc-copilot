# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repo. Loaded
automatically at the start of a session.

## Engineering principles

### Verify a guard can actually fire before classifying it as "intentional"

When you hit a constraint, a defensive default, an apparent duplication, or
anything that *looks* deliberate, do not default to "leave it, it's by
design." Establish **which** it is: check whether the protection can actually
fire, and whether changing it alters behavior a real path or a test would
notice. A guard that can never fire is inertia, not design — and a comment
asserting a reason ("so legacy X validates", "cluster-side baseline") is a
claim to verify, not evidence.

This cuts both ways — apply it before you *preserve* something **and** before
you *remove* it. Real examples from this codebase:

- **Looked intentional, was inert.** Output schemas typed `run_id` as a loose
  `str` "so legacy sidecars validate." But `run_sidecar_path` already
  validates every run_id against the strict `^[A-Za-z0-9._\-]+$` pattern at
  the filesystem layer, so the loose-output guard could never accept anything
  the strict one wouldn't — and the one case it *could* fire (the framework
  emitting a malformed id) is a bug it would hide rather than catch. Tightened
  to `RunIdStrict` on output.
- **Looked intentional, was misattributed.** `infra/parsing.py` was assumed to
  be a "cluster-side baseline" that couldn't import the package. It is not
  deployed to the cluster and the dispatcher never classifies — it only
  captures stderr. The framing was simply wrong.
- **Looked like dead duplication, was load-bearing.**
  `runner_failures._FAILURE_CATEGORY_PATTERNS` looked like a removable
  duplicate of `failure_signatures.CATALOG`, but three tests iterate it as the
  canonical set of "categories the classifier can emit" to assert
  `FailureCategoryResubmittable` covers them. Removing it re-points a contract;
  it is not free.

The cheap, repeatable check: *can this protection actually fire, and does
changing it alter behavior a test or a real code path would notice?* Answer
that before classifying — for both keep and remove decisions.
