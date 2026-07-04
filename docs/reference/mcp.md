# MCP server (`hpc-agent mcp-serve`)

hpc-agent ships an optional **Model Context Protocol** server: a fourth agent
surface (alongside slash commands, workflow skills, and worker prompts) that
exposes the primitive registry to any MCP-speaking client (Claude, Codex,
Gemini, or your own harness) as native tools, resources, and prompts.

It is an **additive projection of the CLI, not a rewrite.** Discovery is read
straight from the `@primitive` registry — the same registry `cli/parser.py`
walks to build argparse — so the MCP tool list can never drift from the CLI.
Invocation drives the same `cli.dispatch.main` code path the `hpc-agent`
binary runs — in-process by default (warm registry, ~40 ms/call vs ~1.2 s
for the injectable subprocess fallback; measured 2026-07-04) — so every
contract the CLI already carries is inherited verbatim: the `{ok, error_code, category,
retry_safe, remediation}` envelope, the 0/1/2/3 exit codes, JSON-Schema spec
validation, and the journal/idempotency guarantees. The CLI stays the single
source of truth.

```
            ┌─────────────────────────────────────────────┐
 MCP client │  initialize / tools/list / tools/call / ...  │
 (Claude,   └───────────────────────┬─────────────────────┘
  Codex,                            │ JSON-RPC 2.0 (stdio)
  …)                                ▼
                         hpc-agent mcp-serve
                  ┌──────────────────┴──────────────────┐
   projection ◄── │ @primitive registry  →  tools/...    │
                  │ tools/call  →  cli.dispatch.main(…)  │ ──► CLI envelope
                  │   (in-process; subprocess fallback)  │
                  └──────────────────────────────────────┘     (verbatim)
```

## Start it

```bash
hpc-agent mcp-serve                      # read-only, full catalog (default)
hpc-agent mcp-serve --allow-mutations    # also expose submit/aggregate/scaffold
hpc-agent mcp-serve --catalog tiered     # find/describe/run-primitive only
```

It speaks newline-delimited JSON-RPC 2.0 on **stdout**; diagnostics go to
**stderr**. It is a long-lived process — unlike every other verb it does *not*
emit the one-shot JSON envelope.

### Wiring into a client

Most clients take a stdio server command. For example, in a Claude Code
`.mcp.json` (or any client's MCP config):

```json
{
  "mcpServers": {
    "hpc-agent": { "type": "stdio", "command": "hpc-agent", "args": ["mcp-serve"] }
  }
}
```

Use `["mcp-serve", "--catalog", "tiered"]` for large catalogs, and add
`"--allow-mutations"` only when the client is trusted to submit jobs.

The equivalent imperative form in Claude Code (writes the block above when run
with `--scope project`; default scope is `local`):

```bash
claude mcp add --scope project hpc-agent -- hpc-agent mcp-serve
```

`--` is required — it separates Claude Code's own flags from the server command,
so server flags go after it (`-- hpc-agent mcp-serve --catalog tiered`). To pass
the SSH agent socket through to read-only cluster-query tools, add it via
`--env` (and keep the server name off the slot right after `--env`):
`claude mcp add --env SSH_AUTH_SOCK=$SSH_AUTH_SOCK --scope project hpc-agent -- hpc-agent mcp-serve`.

## Safety model

This is the reason the server is worth shipping. A CLI worker has a shell, so
the headless-worker fence has to re-deny `scancel` / `qdel` / `ssh` / `curl` on
three separate config surfaces (see
[`_kernel/lifecycle/invoke.py`](../../src/hpc_agent/_kernel/lifecycle/invoke.py)).
An MCP client has **no shell** — it can only call the verbs the server exposes.
So the deny boundary collapses to "which verbs are registered as tools":

- **Read-only by default.** Only `query` / `validate` primitives are exposed.
  Mutating verbs (`mutate` / `submit` / `scaffold` / `workflow`) require
  `--allow-mutations`.
- **No cancel / raw-submit verb exists in the registry at all.** `scancel`,
  `qdel`, `sbatch`, `qsub` are never hpc-agent primitives, so they are
  structurally unreachable through this surface regardless of the flag. The
  invariant is pinned by `tests/test_mcp_server.py`.
- A `tools/call` for a gated verb returns a JSON-RPC `-32602` error (a contract
  violation), not a silent success — including via the tiered-mode
  `run-primitive` indirection, which routes through the same gate.

> Read-only does not mean local-only: `query` primitives such as `status` or
> `inspect-cluster` perform **read-only** SSH (`qstat` / `scontrol`). That is the
> point of a monitoring tool and carries no destructive scheduler capability.

## Tools

In the default `--catalog full` mode, every exposed primitive becomes one typed
tool:

- **name** — the primitive's wire name (e.g. `summarize-submit-plan`).
- **inputSchema** — built from the `CliShape`: `--spec` primitives embed the
  packaged `schemas/<name>.input.json` under a `spec` object property; each
  `CliArg` becomes a property; `experiment_dir` is optional.
- **annotations** — `readOnlyHint` (true for query/validate), `destructiveHint`,
  `idempotentHint`, projected from the registry metadata.

### `--catalog tiered`

Advertises only `find`, `describe`, and a generic `run-primitive` tool, keeping
the per-tool schemas of all ~60 read-only primitives out of the model's context
until pulled on demand. This mirrors the CLI's `find` → `describe` → invoke
discovery and is the recommended mode for context-sensitive / long-running
loops. `run-primitive` takes `{ "name": "<primitive>", "arguments": {...} }` and
is subject to the same safety gate.

### The failure contract is preserved

MCP collapses results into `{ content, isError }`, which would lose the CLI's
machine-readable failure semantics. The server keeps them: the full envelope
**plus the process `exit_code`** rides in `structuredContent`, and `isError` is
set from `ok` / exit code. A client that reads `structuredContent` recovers the
exact `error_code` / `category` / `retry_safe` / `remediation` it had over the
CLI.

```jsonc
// tools/call result for a failed status check
{
  "isError": true,
  "content": [{ "type": "text", "text": "{...}" }],
  "structuredContent": {
    "ok": false, "error_code": "ssh_unreachable", "category": "network",
    "retry_safe": true, "remediation": "…", "exit_code": 2
  }
}
```

## Resources

Read-only context, each backed by a CLI verb:

| URI | Backed by |
|---|---|
| `hpc-agent://capabilities` | `hpc-agent capabilities` (operations catalog + env metadata) |
| `hpc-agent://clusters` | `hpc-agent clusters list` |

## Prompts

The four user-facing workflow slash commands (`submit-hpc`, `monitor-hpc`,
`aggregate-hpc`, `campaign-hpc`) are surfaced as MCP prompts; `prompts/get`
returns the command's markdown body as a user message.

## Version skew

`serverInfo.version` and the `initialize` `instructions` both carry the
hpc-agent package version, so a client can detect a daemon/package mismatch.
Because the default runner dispatches in-process (and the subprocess fallback
invokes `sys.executable -m hpc_agent`), the server and the code it drives are
always the same install — there is no version drift between them.

## What it is not

- **Not a replacement for the CLI or the documented POSIX contract.** It is one
  more surface over the same core; the CLI remains the source of truth and the
  primary integration path (see [`agent-surface.md`](agent-surface.md)).
- **Not a way to reach destructive scheduler operations.** Those are not
  primitives; no flag exposes them.
