---
name: campaign-init
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/.hpc/campaigns/<id>/manifest.json
idempotent: true
idempotency_key: campaign_id
error_codes: []
backed_by:
  cli: hpc-agent campaign-init
  python: claude_hpc.atoms.campaign_init.campaign_init
---
# campaign-init

Scaffold `<experiment>/.hpc/campaigns/<campaign_id>/manifest.json`
from CLI args. The manifest is an audit record — the framework
reads back budget + stop_criteria via `campaign-budget` and
`campaign-converged`; everything else (goal, strategy.params) is
opaque round-tripped context.

## Inputs

- `experiment_dir` (path) — repo root.
- `campaign_id` (str, required) — slug identifier; must match
  `[A-Za-z0-9._-]+` so the on-disk path is filesystem-safe.
- `goal` (str, default `""`) — free-form prose; framework treats
  as opaque text.
- Stop criteria (all optional): `max_iters`, `metric`, `target`,
  `direction` (`minimize`/`maximize`), `plateau_window`,
  `plateau_tolerance`. Combine these to build a `stop_criteria`
  block; consumed by `campaign-converged`.
- Budget (all optional): `max_jobs`, `max_tasks`,
  `max_walltime_sec`. Combine these to build a `budget` block;
  consumed by `campaign-budget`.
- `strategy_name` (str, optional) and `strategy_params_json` (JSON
  string, optional) — opaque to the framework, displayed for
  humans/agents only. Validates as JSON if supplied.

The full schema is at `claude_hpc/schemas/campaign_manifest.json`
(Pydantic-emitted from `_schema_models/campaign_manifest.py:CampaignManifest`).

## Outputs

`{manifest_path, campaign_dir}`. Atomic write — partial writes are
not observable.

## Idempotency

Re-running with the same args produces the same file byte-for-byte.
Re-running with different args **overwrites**. The agent should
treat `campaign-init` as a one-shot at campaign creation, not as
an in-flight mutator: edits to a live campaign's manifest are out
of scope (use `campaign-replay` to fork a new campaign instead).

## Notes

`campaign-init` does not submit any jobs. Pair with `submit-flow
--spec ... --campaign-id <id>` to launch iteration 0; `monitor-flow`
+ `aggregate-flow` carry the run through to terminal; the
agent/orchestrator decides when to call `submit-flow` again with
the same `campaign_id` to advance iterations.
