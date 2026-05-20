# hpc-agent as a POSIX-native agent surface

Most agentic HPC tools fall into one of three buckets:

1. **LLM generates raw shell** — the agent emits `sbatch ...` directly. Fragile;
   the [fire-dynamics paper](https://arxiv.org/abs/2412.17146) found 8/9 failures
   on SLURM. The LLM has to memorise scheduler flags, partition names, environment
   modules, etc.; one stale piece of training data and the submission breaks
   silently.
2. **Python-only library** — the agent imports a package, e.g. `pysqa`, and
   calls into it. Forces the harness to be Python; loses subprocess isolation;
   no stable wire contract between agent and tool.
3. **Heavy middleware** — Parsl + Globus Compute + ProxyStore. Production-grade
   but a lot of moving parts; assumes the cluster has those services.

hpc-agent takes a fourth path: a **POSIX-native agent surface**.

## What that means

- **One binary**: `hpc-agent <subcommand>`. Standard Unix CLI.
- **One stdout shape**: a single-line JSON envelope.
  - Success: `{"ok": true, "idempotent": <bool>, "data": {...}}`
  - Failure: `{"ok": false, "error_code": "...", "category": "...", "retry_safe": <bool>, "remediation": "..."}`
  - See [`schemas/envelope.json`](../src/hpc_agent/schemas/envelope.json) and
    [`docs/reference/cli-spec.md`](cli-spec.md).
- **Stable exit codes**: 0 ok, 1 user error, 2 cluster/network, 3 internal.
  An agent harness can dispatch on the exit code BEFORE parsing JSON.
- **Versioned per-subcommand schemas**: `schemas/<name>.input.json`,
  `<name>.output.json`. Each carries `$id` + `$schema` so harnesses can validate.
- **JSON Schema 2020-12** for input/output validation. Standards-track; not a
  custom format.
- **Schemas are regenerated from Pydantic models** under
  `src/hpc_agent/_schema_models/`. External consumers still read
  the JSON files — that's the wire contract. The Python models are
  the framework's *authoring* surface; touching them and not
  regenerating is a CI failure
  (`scripts/build_schemas.py --check`). Same arrow direction as the
  `@primitive` registry → `docs/primitives/<name>.md` frontmatter.

## What this enables

- **Any harness**: bash, Python (`subprocess.run`), TypeScript (`Bun.spawn`),
  Rust (`std::process::Command`), Go, anything with a JSON parser.
  Harnesses don't need to import a Python package.
- **Honest error semantics**: `retry_safe: false` means "do not retry"; the
  category tells the harness whether to escalate to user, retry on network,
  or treat as bug. The agent doesn't have to interpret natural language to
  decide what to do next.
- **Schema evolution**: when fields are added, old harnesses ignore them; when
  fields are removed, the breaking change is visible in the schema diff. We
  don't break the wire contract silently.
- **Observability**: every primitive is also a documented operation in
  [`docs/primitives/`](primitives/). The catalog (`hpc-agent capabilities`)
  enumerates every tool the agent has, with idempotency, side-effects, and
  error codes inline.

## What this does NOT mean

- **Not an MCP server.** MCP is a different transport with its own conventions
  (stdio JSON-RPC framing, server-managed state, capability negotiation). The
  CLI is invoked-on-demand and stateless between calls. MCP wrappers can be
  written on top of the CLI if a downstream client wants them; we don't ship
  one.
- **Not Python-tied.** The agent harness can be in any language. The CLI
  binary is implemented in Python only because that's where the cluster-side
  primitives live.
- **Not a replacement for SLURM/SGE.** It's a thin layer that translates
  agent intent into well-shaped scheduler invocations and parses the results
  back into structured envelopes.

## Comparison

| | LLM-generates-shell | Python library | Heavy middleware | hpc-agent |
|---|---|---|---|---|
| Harness language | any | Python | Python | any |
| Schema-validated output | ❌ | partial | ❌ | ✅ |
| Stable exit-code contract | ❌ | n/a | ❌ | ✅ |
| Subprocess isolation | ✅ | ❌ | partial | ✅ |
| Failure semantics machine-readable | ❌ | partial | partial | ✅ |
| Onboarding cost | high (LLM has to know flags) | medium (lib API) | high (services) | low (one binary) |

If you want the design rationale in more depth, see
[`docs/reference/cli-spec.md`](cli-spec.md) for the contract and
[`docs/primitives/`](primitives/) for the per-operation reference.
