# Environment variables

Cross-cutting reference for every `HPC_*` env-var the framework reads.
Set on the local shell before invoking the CLI / slash command;
cluster-side scripts inherit a curated subset (see the per-template
preamble).

## Runtime / behaviour

| Variable | Default | Purpose |
|---|---|---|
| `HPC_CLUSTERS_CONFIG` | `<package>/config/clusters.yaml` | Path to a `clusters.yaml` override. Used by `hpc_agent.infra.clusters.load_clusters_config`. |
| `HPC_JOURNAL_DIR` | `~/.claude/hpc/` | Root of the per-experiment journal tree. External harnesses set this so their state lives outside the user's `~/.claude`. |
| `HPC_MAX_RUNS` | `500` | Max per-experiment sidecars retained before oldest-by-mtime eviction (`hpc_agent.state.runs`). |
| `HPC_CAMPAIGN_ID` | (unset) | Threaded through to every cluster job by the scheduler templates so `tasks.py` can read the prior iteration's history via `hpc_agent.execution.mapreduce.reduce.history.prior(...)`. |
| `HPC_TELEMETRY_SINK` | `none` | One of `none` / `stderr-jsonl` / `monitor-jsonl`. Routes `hpc_agent._kernel.extension.telemetry.record` events. |
| `HPC_AGENT_WORKER_JSON_SCHEMA` | (unset) | Set to `1`/`true` to spawn the **Claude** (`claude-cli`) worker with `--json-schema`, constraining the worker's final report so malformed JSON can't be emitted — the structural complement to `parse_worker_report`'s cross-field checks (`hpc_agent._kernel.lifecycle.invoke`). Binds the lenient `worker.output.json` (whether claude's mode requires a strict schema is the open #269 question). Off by default; when off, the plain transport + `parse_worker_report` floor carry the report. |
| `HPC_AGENT_CODEX_OUTPUT_SCHEMA` | (unset) | The **Codex** (`codex-cli`) counterpart: set to `1`/`true` to spawn the worker with `--output-schema`, binding the API-strict `worker.strict.output.json` (Codex's `--output-schema` requires `additionalProperties:false` + all-required). A **separate gate** from `HPC_AGENT_WORKER_JSON_SCHEMA` because turning the accelerator on is gated on a *per-harness* live-validation run (#269) — flipping one harness on must not silently flip an unvalidated other. Off by default. `gemini-cli` has no CLI decode schema (`responseSchema` is API/SDK-only), so it always leans on the `parse_worker_report` floor regardless of either flag. Making either the default once live-validated is tracked in [#269](https://github.com/jamesdchen/hpc-agent/issues/269). |

## Worker invoker (multi-harness)

The delegated headless worker can run under different agent harnesses
(`WorkerInvoker` drivers in `hpc_agent._kernel.lifecycle.invoke`). Each
driver normalizes four axes — headless transport, sandbox/network
posture off, the cluster-op tool-authorization fence, and
decode-schema-vs-`parse_worker_report` floor — plus the
`missing_credential_remediation()` pre-spawn auth guard and the
`cache_stats=None` fallback when the transport surfaces no billing usage.

| Variable | Default | Purpose |
|---|---|---|
| `HPC_AGENT_INVOKER` | (auto) | Explicit worker-invoker override, beating the ambient-credential auto-selection. Spawning transports: `claude-cli` (`claude -p --bare`, API key), `claude-cli-oauth` (Claude Code OAuth login), `codex-cli` (`codex exec`, `CODEX_API_KEY`), `gemini-cli` (`gemini -p`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`). The reserved non-spawning value `inline` runs the procedure in the caller's own context and is honored only by `hpc-agent run` (operator opt-in, #155). Auto-selection: Claude creds → `claude-cli`; else a Claude OAuth file → `claude-cli-oauth`; else `CODEX_API_KEY` → `codex-cli`; else Gemini creds → `gemini-cli`; else `claude-cli` (so its credential guard fires). |
| `CODEX_API_KEY` | (unset) | Auth for the `codex-cli` worker, scoped to the invocation. Preferred over ambient `OPENAI_API_KEY`, which a stored ChatGPT login in `~/.codex/auth.json` can shadow ([codex #3286](https://github.com/openai/codex/issues/3286)). |
| `GEMINI_API_KEY` | (unset) | Auth for the `gemini-cli` worker via the Gemini API. |
| `GOOGLE_API_KEY` | (unset) | Auth for the `gemini-cli` worker via Vertex AI (alternative to `GEMINI_API_KEY`). |
| `HPC_AGENT_CODEX_WORKER_MODEL` | `gpt-5.4-mini` | Overrides the `codex-cli` worker's pinned cheap model id (for when the default id is retired upstream before the constant is bumped). |
| `HPC_AGENT_GEMINI_WORKER_MODEL` | `gemini-2.5-flash` | Overrides the `gemini-cli` worker's pinned cheap model id. The default is a concrete id, not the `auto`/`flash`/`pro` aliases (which resolve to a preview generation). |

## Raw model-call adapter (`structured()`)

The raw model-call seam (`hpc_agent._kernel.lifecycle.structured.structured`)
resolves a `ChatModel` via `HPC_AGENT_MODEL`, exactly as `HPC_AGENT_INVOKER`
selects a spawned-worker transport. The one built-in adapter is
`openai-compat` — an OpenAI-compatible `/chat/completions` client that
targets DeepSeek-hosted, OpenAI, or a self-hosted vLLM by swapping these vars
(#304, Phase 2). **Default-off**: nothing is auto-selected; the seam is inert
until `HPC_AGENT_MODEL=openai-compat` (or an explicit `get_model("openai-compat")`).

| Variable | Default | Purpose |
|---|---|---|
| `HPC_AGENT_MODEL` | (unset) | Selects the `ChatModel` for `structured()` (`get_model`). Set to `openai-compat` to use the built-in OpenAI-compatible adapter. Unset → the seam raises `spec_invalid` (no model selected), the same shape as an unknown `HPC_AGENT_INVOKER`. |
| `HPC_AGENT_MODEL_BASE_URL` | (unset) | OpenAI-compatible API base URL, e.g. `https://api.deepseek.com/v1`, `https://api.openai.com/v1`, `http://localhost:8000/v1` (vLLM). Required for `openai-compat`; missing → `spec_invalid`. |
| `HPC_AGENT_MODEL_NAME` | (unset) | Model id to call, e.g. `deepseek-chat`, `gpt-4o`, the vLLM `--served-model-name`. Required for `openai-compat`; missing → `spec_invalid`. |
| `HPC_AGENT_MODEL_API_KEY` | (unset) | Bearer credential. Falls back to `OPENAI_API_KEY` then `DEEPSEEK_API_KEY` if unset. **Required for any non-loopback `base_url`**; a `localhost`/`127.0.0.1` base_url (a keyless vLLM) may omit it. Missing on a remote endpoint → `spec_invalid`. |
| `HPC_AGENT_MODEL_RESPONSE_FORMAT` | `json_schema` | Per-endpoint accelerator knob. `json_schema` (default) sends the target schema as a **strict decode constraint** (`response_format.type="json_schema"`, `strict:true`) so the server cannot emit non-conforming tokens — enforced by **OpenAI** and **self-hosted vLLM** (guided decoding). `json_object` requests JSON-valid output only and injects the schema as a prompt hint (the parse-validate-repair floor carries shape) — use this for **DeepSeek-hosted**, whose API historically supports only json_object. `none` sends no constraint and relies entirely on the floor. The floor (`structured()`) is the universal backstop in every mode. |

### Manual live-validation (the #269 discipline)

The `openai-compat` adapter ships **unvalidated against a live endpoint and
default-off**: the build sandbox has no provider credentials and blocks
outbound network, so — exactly as `HPC_AGENT_WORKER_JSON_SCHEMA` (#269) is
gated until a live `claude -p --json-schema` run is confirmed — a human must
run this smoke once against a real endpoint before relying on it:

1. Export the config for your endpoint. For **guaranteed strict decode** use
   OpenAI or a self-hosted vLLM:

   ```bash
   # OpenAI (strict json_schema honoured)
   export HPC_AGENT_MODEL=openai-compat
   export HPC_AGENT_MODEL_BASE_URL=https://api.openai.com/v1
   export HPC_AGENT_MODEL_NAME=gpt-4o
   export HPC_AGENT_MODEL_API_KEY=sk-...
   # (HPC_AGENT_MODEL_RESPONSE_FORMAT defaults to json_schema)

   # …or self-hosted vLLM (guided decoding; keyless localhost allowed)
   #   export HPC_AGENT_MODEL_BASE_URL=http://localhost:8000/v1
   #   export HPC_AGENT_MODEL_NAME=Qwen/Qwen2.5-7B-Instruct

   # …or DeepSeek-hosted — json_schema is NOT honoured there, so downgrade:
   #   export HPC_AGENT_MODEL_BASE_URL=https://api.deepseek.com/v1
   #   export HPC_AGENT_MODEL_NAME=deepseek-chat
   #   export HPC_AGENT_MODEL_API_KEY=sk-...
   #   export HPC_AGENT_MODEL_RESPONSE_FORMAT=json_object   # JSON-mode + floor
   ```

2. Run a minimal `structured()` smoke and confirm a validated instance:

   ```bash
   python - <<'PY'
   import pydantic
   from hpc_agent._kernel.lifecycle.structured import (
       ChatMessage, get_model, structured,
   )

   class Answer(pydantic.BaseModel):
       label: str
       count: int

   model = get_model()  # resolves openai-compat from HPC_AGENT_MODEL
   result = structured(
       model, Answer,
       [ChatMessage(role="user", content="Return label='ok' and count=3.")],
   )
   print("validated:", result)        # → Answer(label='ok', count=3)
   assert isinstance(result, Answer)
   PY
   ```

   A printed validated `Answer` confirms the round-trip: request built, the
   accelerator applied for the mode, the envelope parsed, and the floor
   validated. A `spec_invalid` means a missing/misnamed env var; an
   `ssh_unreachable` (the transport error class) means the endpoint was
   unreachable or returned a bad envelope/HTTP status — re-check base_url, key,
   model id, and `RESPONSE_FORMAT` against what the provider supports.

**Provider-support reality:** strict `json_schema` decode is enforced by
**OpenAI** and **self-hosted vLLM** (guided decoding); **DeepSeek's hosted
API** historically supports only `json_object`. For DeepSeek-hosted set
`HPC_AGENT_MODEL_RESPONSE_FORMAT=json_object` (JSON-mode + floor, best-effort
shape); for **guaranteed** strict decode use vLLM or OpenAI.

## SSH / rsync transport

| Variable | Default | Purpose |
|---|---|---|
| `HPC_SSH_TIMEOUT_SEC` | `60` | Per-call subprocess timeout for `ssh` / `scp` invocations from `hpc_agent.infra.remote`. Raise on slow login nodes; lowering risks false-positive timeouts. |
| `HPC_CLUSTER_SSH_TIMEOUT` | `15` | Per-probe timeout (seconds) for the `check-preflight --cluster` cluster ssh round-trips (the `cluster_ssh_echo` and merged echo+runtime-uv probes). The prior hardcoded 5s fired false `cluster_ssh_timeout` failures on healthy-but-loaded login nodes; 15s tolerates routine slowness. Pin tighter or looser as needed. A non-integer value falls back to the default. |
| `HPC_RSYNC_TIMEOUT_SEC` | `1800` | Per-call subprocess timeout for `rsync` push / pull. Raise when transferring large repos over slow links. |
| `HPC_NO_SSH_MULTIPLEX` | (unset) | Set to `1` to disable OpenSSH connection multiplexing. Some clusters disallow it (e.g. PAM session limits). Without multiplexing, every status poll pays a full SSH handshake. |
| `HPC_SSH_BINARY` | (auto) | Path to the `ssh` binary to invoke. On native Windows, when unset, hpc-agent prefers `C:\Windows\System32\OpenSSH\ssh.exe` over Git Bash's bundled `ssh` (Git's ssh can't reach the Windows OpenSSH named-pipe agent). Elsewhere it falls back to bare `ssh` on `PATH`. Set explicitly to pin a specific binary on any platform. |
| `HPC_SCP_BINARY` | (auto) | As `HPC_SSH_BINARY`, for `scp` (prefers `C:\Windows\System32\OpenSSH\scp.exe` on Windows when present). |
| `RSYNC_RSH` | (auto) | Standard rsync variable naming the remote shell. hpc-agent sets it to the resolved `HPC_SSH_BINARY` for rsync transfers when that isn't the bare `ssh` (e.g. native Windows OpenSSH), so rsync's ssh matches the rest of the transport. A value you set yourself is respected. |
| `HPC_SSH_NO_BACKOFF` | (unset) | Set to `1` to disable transient-failure exponential backoff. Used by the test suite when mocking subprocess; production callers should leave this alone. |
| `HPC_SUBMIT_NO_LOCK` | (unset) | Set to `1` to disable the per-repo submit-flow advisory flock. The lock serializes concurrent `submit-flow` / `submit-flow-batch` calls against the same experiment dir so two shells don't both fan out N qsubs at the cluster's sshd. Retained for two narrow callers: (a) the test suite, where `submit_flow` is exercised in parallel with mocked subprocess (no real qsub to race), and (b) operators who deliberately want concurrent submits (different specs, different shells) and have confirmed the cluster can absorb the burst. Disabling outside those two cases risks a scheduler-throttling stampede. |
| `HPC_AGENT_SKIP_PREFLIGHT` | (unset) | Set to `1` to skip `submit-flow`'s pre-flight probes (the ssh-reachability probe and the `command -v uv` runtime probe) — for an operator who just ran `check-preflight` and wants to save the duplicate round-trip. **Operator-only and deliberately not a spec field** (#275): an agent following the SKILL.md flow used to set a `skip_preflight: true` spec field, which silenced the uv runtime probe and launched arrays doomed by `HPC_RUNTIME=uv but 'uv' not on PATH`. Same operator-vs-agent boundary as `HPC_AGENT_INVOKER=inline` (#155); the two-phase canary gate's internal main-array launch skips the redundant probe through a Python-only kwarg, not this var. |

## Validation thresholds

There are no env-var knobs for validators; per-rule overrides live in
`.hpc/playbook.yaml` (version-controlled, per-project). See
[`config-precedence.md`](config-precedence.md).

## Discovery

Run `hpc-agent capabilities --full` to see the full operations
catalog plus all supported `clusters.yaml` keys (the latter come from
`hpc_agent.infra.clusters.CLUSTER_YAML_KEYS`). Env vars don't appear
there — this doc is the canonical list.
