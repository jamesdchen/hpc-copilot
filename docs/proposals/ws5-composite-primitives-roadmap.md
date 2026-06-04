# WS5 — Composite primitives roadmap

> **WS5 = collapse multi-step agent-driven sequences into composite primitives.**
> Every SKILL.md Step where the agent makes >1 tool call is a candidate. The
> agent's role per Step shrinks to one tool call; the deterministic
> sequencing lives in the CLI.

The WS5 audit (2026-06-04) inventoried 18 candidate Steps and ranked them
by leverage. This document tracks the implementation plan for every
candidate plus the design notes the audit produced.

## Status overview

| Rank | Candidate | LoC | Status | Commit |
|---|---|---|---|---|
| 1 | `submit-preflight` (install + load + cluster-ssh) | ~250 | **done** | `f5193068` |
| 2 | `skill-call` PostToolUse hook | ~60 | pending | — |
| 3 | `status-preflight` | ~250 | **done** | `cfbab103` |
| 3b | `aggregate-preflight` | ~250 | pending | — |
| 4 | `detect-entry-point` | ~80 | pending | — |
| 5 | `resolve-resources` | ~50 | pending | — |
| 6 | `classify-axis-preflight` | ~50 | pending | — |
| 7 | `inspect-parallel-axes` | ~40 | pending | — |
| 8 | `smoke-test-executor` | ~60 | pending | — |
| 9 | `check-task-generator-mismatch` | ~40 | pending | — |
| 10 | `detect-frozen-configs` | ~20 | **cut** | — (already a single shell call; not worth wrapping) |

## Recommended ordering (next 4 PRs)

The two top-of-each-skill `<skill>-preflight` family members are done; the
remaining family member (`aggregate-preflight`) is mechanical and worth
finishing first to lock in the pattern. After that, focus on the candidates
that close currently-open issues / lint violations.

### PR 1: `aggregate-preflight` (~250 LoC)

Mirror of `submit-preflight` but for `hpc-aggregate`:

- `install-commands` (always)
- `load-context` (always)
- `reconcile` when `data.load_context.envelope.data.next_step_hint == "monitor"` AND `--reconcile-scheduler` is supplied

Same SubResult / overall semantics as the other two. Closes the
`<skill>-preflight` family completely (#3 of the audit's Top-3).

Schemas + ops module + tests + docs + SKILL.md wire-up.

### PR 2: `smoke-test-executor` (~60 LoC)

Wraps `hpc-build-executor` Step 6's banned-by-its-own-lint `python -c`
invocation:

```bash
hpc-agent smoke-test-executor --module-path <path> [--output-file <path>]
```

Internally runs:

```python
import argparse, importlib.util, sys
spec = importlib.util.spec_from_file_location("m", "<path>")
m = importlib.util.module_from_spec(spec)
sys.modules["m"] = m
spec.loader.exec_module(m)
m.compute(argparse.Namespace(output_file="<path-or-/tmp/smoke.csv>"))
```

Returns the standard SuccessEnvelope with `{exit_code, stdout_tail,
stderr_tail}` so the build-executor skill can branch deterministically.
Closes a lint violation already on disk (WS4's `prose-decide` /
`step-without-action-ending` won't see it once the `python -c` is gone).

### PR 3: `detect-entry-point` (~80 LoC)

Collapses the 6-shell-probe block that's literally duplicated in
`hpc-wrap-entry-point` Steps 0 (greenfield branch) and Step 1
(mature-repo branch):

```bash
hpc-agent detect-entry-point --experiment-dir <path>
```

Returns:

```json
{
  "kind": "greenfield" | "detected",
  "candidates": [
    {"path": "train.py", "argv_kind": "argparse" | "click" | "typer" | "hydra" | "fire" | "__main__"},
    ...
  ],
  "decoration_found": [...]  // @register_run files
}
```

Removes 6 raw-shell permission prompts per onboard. Eliminates the
`find` / `grep` / `head` ban-surface area that SKILL.md prose currently
has to dodge.

### PR 4: `skill-call` PostToolUse hook (~60 LoC, harness-level)

Auto-fetch the sub-skill return envelope after every `Skill(<sub>)`
returns, so the agent never has to remember to chain `fetch-skill-return`
manually. Implementation: a Python helper at
`hpc_agent/_kernel/hooks/skill_return_autofetch.py` + a settings.json
fragment that `install-commands` injects into `~/.claude/settings.json`'s
`hooks.PostToolUse` array.

Hook payload reads the just-completed Skill tool's output, checks if the
skill is in `_KNOWN_SKILLS`, and if so reads
`<experiment_dir>/.hpc/_returns/<skill>.json` + injects it into the
agent's next observation. Removes one of two seams where the parent
skill's prose-discipline still matters.

**Caveat from the audit**: this verb is harness-mediated, not pure CLI.
The `Skill(<sub>)` tool call still has to come from the agent (the
harness owns the verb), but the *mandatory* follow-up `fetch-skill-return`
can be auto-injected. Lower LoC but higher coordination cost — it
crosses the package / harness-config boundary.

## Second-tier candidates (PR 5-8)

| # | Candidate | LoC | Why later |
|---|---|---|---|
| 5 | `resolve-resources` (priors + cluster default + partition) | ~50 | Modest leverage; auto-resolves silently today so the agent rarely narrates it. Clean asyncio target. |
| 6 | `classify-axis-preflight` (discover + cache-check + recall) | ~50 | Fires only when classify is invoked (cache-hit-skip in submit's Step 4). Collapse prose-discipline, not parallelism. |
| 7 | `inspect-parallel-axes` (multi-Read) | ~40 | Fires once per executor build. Bounded scope. |
| 9 | `check-task-generator-mismatch` (canonical-JSON compare) | ~40 | Most paths short-circuit early. Worth doing for the boilerplate but not urgent. |

## Cut

- **#10 `detect-frozen-configs`**: already a single shell call
  (`ls configs/*.yaml configs/*.yml conf/*.yaml`). Wrapping it as a CLI
  verb adds maintenance for ~zero leverage. Drop.

## Surprises from the audit (action items)

These aren't composite primitives per se but were surfaced by the audit
and deserve their own follow-ups:

1. **`hpc-build-executor` Step 6 violates its own skill rule** —
   issues a `python -c` smoke test even though the execution-style
   header bans it. WS4's `prose-decide` (or a new rule) should catch
   this once the lint severity flips to hard fail. The fix is PR 2 above
   — the verb makes the violation moot.

2. **`hpc-wrap-entry-point` Steps 0 and 1 are near-duplicates** — the
   5-shell-probe block is literally repeated. PR 3 dedupes both.

3. **The file-primitive emit/fetch architecture (WS2) has already done
   half of WS5's job.** PR 4 finishes the other half via the
   PostToolUse hook.

## Pattern: `<skill>-preflight` family

The submit / status / aggregate / campaign skills all start with the same
top-of-skill boilerplate (install-commands + load-context + optional
reconcile + optional cluster_ssh_echo). Once `aggregate-preflight`
lands, the pattern is canonical and any new workflow skill should add
its own `<skill>-preflight` companion verb on day one.

Future generalization: a single `workflow-preflight --skill <name>` verb
that internally branches per skill, instead of N near-duplicate verbs.
Defer until a 4th workflow skill needs it (premature abstraction
otherwise).

## Why this matters

Per the determinism migration's framing: the agent is good at exactly two
things — mapping user intent → spec and picking from finite enumerated
recovery menus. It's bad at long deterministic sequences and resisting
helpfulness instincts. Every WS5 candidate above is one specific case
where today's prose-discipline contract is being replaced by a CLI
state machine the agent can't skip, reorder, or narrate around.
