---
name: harness-capabilities
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent harness-capabilities [--spec <path>] [--experiment-dir <dir>]
  python: hpc_agent.ops.harness_capabilities.harness_capabilities
---
# harness-capabilities

Detect and report the **harness capability set as code can observe it** — LSP-style
capability negotiation for the harness contract
(`docs/internals/harness-contract.md`, "Capability negotiation"). The declaration
**is what code can verify**, never a self-asserted manifest: a capability the code
cannot observe reads `"unknown"`, not `true`.

A pure read: no SSH, no scheduler, no write, no state moved. Fail-open — an absent
or unreadable `settings.json` degrades to "no channels detected", never an error.

## Inputs

A `HarnessCapabilitiesSpec` (`hpc_agent._wire.queries.harness_capabilities`) — the
spec is **empty**: `{}` is the whole valid input. `extra="forbid"` still rejects a
bogus key, so the verb carries the same spec-invalid contract as every other
`--spec` primitive. `--spec` is optional; `hpc-agent harness-capabilities` runs on
its own.

The harness config it reads is `<CLAUDE_CONFIG_DIR or ~/.claude>/settings.json`
(honoring the documented `CLAUDE_CONFIG_DIR` relocation env var).

## Outputs

`data` is a `HarnessCapabilitiesResult`:

```
{
  "capabilities": {
    "<name>": {
      "present": true | false | "unknown",
      "channel": "<the seam the detection reads>",
      "evidence": { ... raw observations the verdict rolled up from ... }
    }
  },
  "tier_consequences": {
    "<name>": "<the named tier its absence degrades to>"
  }
}
```

The four capabilities, and the exact seam each `present` bit is detected from:

- **`utterance_log`** (capability 1) — `present` is whether the `UserPromptSubmit`
  utterance-capture hook is installed (the write channel that earns the
  full-strength authorship tier), matched by its module-path needle in
  `settings.json`. `evidence` also carries `answer_capture_hook` (the
  `AskUserQuestion` typed-answer channel), `elicitation_channel` (the MCP
  `ELICITATION_SUPPORTED` flag — see below), and `log_present_for_repo` (whether
  the utterance-log namespace already exists for this `--experiment-dir`,
  non-creating read via `state.utterances`).
- **`relay_enforcement`** (capability 2) — `present` is whether the relay-audit
  `Stop` hook is installed (its needle).
- **`backgrounding`** (capability 3) — always `true`: the detached-worker machinery
  is core-side. `evidence.watchdog_alert_hook` reports the alert-delivery
  `SessionStart` hook's presence honestly (detection without delivery is silence).
- **`trusted_display`** (capability 4) — `"unknown"`: the trusted-render capability
  has **no detection seam yet**. Reported unknown rather than asserted.

`tier_consequences` names, per capability, the exact degrade its absence implies —
quoted from the contract's friction-tier language (e.g. capability 1 absent falls
back to the journal-response friction tier at
`ops/decision/journal.py::_harness_human_texts` returning `None`).

### The elicitation channel

`evidence.elicitation_channel` reflects
`hpc_agent._kernel.extension.mcp_server.ELICITATION_SUPPORTED`, which is `False`:
the MCP server is a hand-rolled synchronous JSON-RPC loop with no server-initiated
request path, so the 2025-06-18 elicitation channel is **specified but not
implemented** (see the harness-contract doc). When absent it degrades to the hook
path (capability 1's `UserPromptSubmit` capture).

## Errors

- `spec_invalid` — a bogus/unexpected key in the (otherwise empty) spec. Not
  retry-safe; drop the key.

## Idempotency

A pure query, recomputed on every call from the config on disk + the repo's journal
namespace. No side effects, no identity key.

## Usage

```
hpc-agent harness-capabilities --experiment-dir .
```

This is **detection-as-negotiation**: the conformance kit (planned separately)
asserts `declared == detected == behaved` against exactly these seams.
