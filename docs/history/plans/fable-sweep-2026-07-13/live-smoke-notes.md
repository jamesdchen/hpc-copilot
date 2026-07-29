# Live smoke notes (in-session, 2026-07-13)

Read-only smoke of the actual `hpc-agent mcp-serve` surface (v0.11.0+dev.gfb8428c9)
run in the sweep session itself, after verification. Three observations that
bear on the findings:

1. **F46 confirmed empirically, not just structurally.** With `~/.claude/hpc`
   absent, two read-only MCP calls (`status-snapshot` with `mark_seen=false`,
   then `doctor`) scaffolded `~/.claude/hpc/a8de6a2c9cd4/` — repo.json,
   index.json, runs/ — with `first_seen` stamped at the exact status-snapshot
   call time. A pure read mints a journal namespace, as F46 states.

2. **F55/F56 severity input: the SSH engine is NOT opt-in on the MCP surface.**
   Both verbs returned `active_env_overrides: {"HPC_SSH_ENGINE": "asyncssh"}` —
   `mcp-serve` defaults the persistent engine ON (commit cf4651d). The skeptics
   rated F55 (post-dispatch EngineUnavailable → silent one-shot re-execution)
   and F56 (failure-path discard severs in-flight peers) as "opt-in gated";
   for MCP-driven usage they are DEFAULT-ON. The hardening swarm should treat
   their reachability accordingly.

3. **doctor's version_skew guard fires correctly** (flagged installed CLI
   fb8428c9 vs repo 76ef29cd after the appendix commit) — an example of a
   guard with a demonstrated live fire path.

Baseline for the fix swarm: full suite at 76ef29c (code-identical fb8428c) is
GREEN — 9455 passed, 25 skipped, 68 xfailed, 95s wall. Any red after a fix is
the fix's.
