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

_Documentation pending._
