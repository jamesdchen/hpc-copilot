---
name: export-package
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/src/*.py
- writes-sidecar: <experiment>/.hpc/.build-cache.json
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent export-package
  python: hpc_agent.atoms.export_package.export_package
---
# export-package

Build the experiment's `src/` package from its notebooks. The experiment
repo commits **nothing generated**: the notebook → `.py` export is a
submit-time (and CI / local-repro / serial-elision-gate) step, not a
committed artifact. `export-package` globs the notebooks, derives each
output path by convention, auto-picks the exporter, content-hash-caches
unchanged notebooks, and writes the whole `src/` package.

## Inputs

- `experiment_dir` (path) — repo root; output lands at
  `<experiment_dir>/src/`.
- `force` (bool, default false) — ignore the content-hash build cache
  (`.hpc/.build-cache.json`) and re-export every notebook.
- `notebooks_dir` (str, default `notebooks`) — directory holding the
  `{pipeline,executors,scripts}/` notebook subtrees.

## Outputs

`{src_dir, built, cache_hits, n_notebooks, cache_path}`. `built` lists
the `src/` modules (re)exported this call; `cache_hits` the unchanged
ones skipped. A second call with no notebook edits is therefore all
`cache_hits` and byte-stable.

## Errors

- `spec_invalid` — two notebooks map to the same `src/` module name
  after the ordering-prefix strip, or a notebook stem is not a valid
  Python module name.

## Idempotency

Keyed on `experiment_dir`. Each notebook's concatenated code-cell
sources are hashed against `.hpc/.build-cache.json`; an unchanged
notebook whose output still exists is skipped. Export is pure AST
extraction plus a `ruff` post-pass — no notebook execution.

## Notes

- **Convention, not a manifest.** Notebooks under `notebooks/pipeline/`,
  `notebooks/executors/`, and `notebooks/scripts/` export; nothing else
  does. The output module name is the notebook stem with a leading
  `\d+[a-z]?_` ordering prefix stripped — `01_loading.ipynb` →
  `src/loading.py`.
- **Exporter auto-picked by content.** A notebook that imports
  `hpc_agent.template` is a `@register_run` executor → strict-AST
  `export_notebook` (the runtime is inlined, so the cluster node stays
  stdlib-only). Otherwise it is a pipeline-library notebook → the
  `# export`-marker `export_notebook_markers`.
- **The node never builds.** Submit builds locally — where `hpc_agent`
  is installed — and ships finished `.py`. `src/` is `.gitignore`d in
  the experiment repo; output stays at the repo-root `src/` so
  `src.<module>` imports and `PYTHONPATH` are unchanged.
