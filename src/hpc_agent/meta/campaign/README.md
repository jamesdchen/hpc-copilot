# meta/campaign

## What and why

`meta/campaign/` is the campaign lifecycle subject — a tagged,
closed-loop sequence of `ops/submit/` invocations sharing a
`campaign_id`. It covers the eight per-step primitives
(`campaign-init`, `-list`, `-status`, `-advance`, `-budget`,
`-converged`, `-health`, `-replay`) plus `load-context` (the
fresh-context bootstrap that reconstructs the active campaign from
on-disk state), the three human-touchpoint blocks in `blocks.py`
(`campaign-greenlight` / `campaign-watch` / `campaign-complete`), and
the cursor/dirs/manifest support modules that back them. Campaigns are
operations *about* operations, so this subject lives under `meta/`
rather than `ops/`.

The outer loop is **not** campaign-owned and is not in this package.
Blocks chain in code via the `block-drive` driver
(`_kernel/lifecycle/block_drive.py`, the `hpc-block-drive` console
script) over the neutral tick substrate `_kernel/lifecycle/drive.py`;
the dependency points `meta/campaign` → `_kernel/lifecycle`, never the
reverse. The autonomous refill actor the greenlit manifest authorizes
lives in `ops/campaign_refill.py` (`campaign-refill`, reached when
`campaign-watch` emits `watching_refill`; RFC #362,
`docs/design/campaign-async-refill.md`). (The former campaign-specific
`driver.py` shim + `hpc-campaign-driver` console script, which injected a
`StepTable` / `JudgementResolver` and spawned `claude -p` for judgment
steps, were **removed in the worker-removal wave** — see
`docs/internals/campaign-lifecycle.md`.)

## Invariant

`meta/campaign/` promises: on-disk state (run sidecars, journal,
cursors, manifest) is the *only* thing carried between steps. Every
primitive is pure read or scoped write under
`<experiment>/.hpc/campaigns/<id>/`; no primitive shells out to a
scheduler or spawns an LLM. The driver may spawn `claude -p`, but only
behind the explicit `--allow-agent-steps` opt-in — that's why the
driver is intentionally not a `@primitive`.

## Public vs internal

- **Public primitive modules** (auto-discovered by
  `_kernel/registry/primitive.py::register_primitives`):
  `atoms/advance.py`, `atoms/budget.py`, `atoms/converged.py`,
  `atoms/health.py`, `atoms/init.py`, `atoms/list_campaigns.py`
  (the name avoids shadowing the `list` builtin),
  `atoms/replay.py`, `atoms/status.py`, `atoms/load_context.py`.
- **Campaign blocks** (`blocks.py`): the three human-touchpoint blocks
  `campaign-greenlight` / `campaign-watch` / `campaign-complete`,
  chained by `block-drive` (`_kernel/lifecycle/block_drive.py`). There is
  no campaign-owned outer-loop entry point anymore — the former
  `driver.py:main` / `hpc-campaign-driver` console script was removed in
  the worker-removal wave.
- **Support modules** (internal to the subject, imported by the atoms
  above): `cursor.py`, `dirs.py`, `manifest.py`. External callers go
  through the primitive CLI, not these helpers.
- `__init__.py` is intentionally empty — there are no re-exports, by
  the post-reorg subject convention. Callers import the leaf module
  directly (e.g. `from hpc_agent.meta.campaign.dirs import campaign_dir`).
