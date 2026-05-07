---
name: axes-init
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/.hpc/axes.yaml
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-mapreduce axes-init
  python: claude_hpc.atoms.axes_init.axes_init
---
# axes-init

Scaffold `<experiment>/.hpc/axes.yaml` with the deployer's hints
about which axes the cold-start picker should treat as homogeneous.
Once runtime priors exist, the warm-path picker uses observed CV
instead and the file becomes a fallback.

## Inputs

- `experiment_dir` (path) — repo root the file will land under.
- `axes` (list of `{name, size}` dicts, optional) — every parallel
  axis in the experiment. Order is significant: it defines the
  cartesian-product convention by which `task_id` maps to axis
  values (last axis varies fastest, numpy/row-major). Required for
  `submit-flow`'s `wave_map` building. The full input schema lives
  at `claude_hpc/schemas/axes.json` (Pydantic-emitted from
  `_schema_models/axes.py:AxesConfig`).
- `homogeneous_axes` (list of str, optional) — names the deployer
  believes have low runtime-cost variance. The cold-start picker
  promotes the first one onto the task array. When `axes` is also
  supplied, every name here must appear in `axes`.
- `force` (bool, default false) — overwrite an existing
  `axes.yaml`. Default behavior is **refuse**: a hand-edited file
  may carry intentional overrides the framework would otherwise
  clobber.

## Outputs

`{axes_path, axes, homogeneous_axes, wrote, reason}`. `wrote=False`
when the file already existed and `force=False`; `reason` carries
the human-facing explanation in that case.

## Errors

- `spec_invalid` — a name in `homogeneous_axes` is not a member of
  `axes`, or a duplicate appears in `homogeneous_axes`, or
  `axes_schema_version` mismatches the framework's expected value.

## Notes

The framework only stores fields it can independently act on.
Experiment-specific reasoning about *why* an axis is homogeneous
("we ran a 4-replicate canary on the seed axis and saw <2% CV")
lives in the agent's chat context, not here. Two-line shape only;
prose explanations belong in the campaign's `interview.json` notes
field instead.
