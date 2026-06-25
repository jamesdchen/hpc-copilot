---
name: decorate-entry-point
verb: mutate
side_effects:
- filesystem: '<path> (in-place: import + @register_run)'
idempotent: true
idempotency_key: path
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent decorate-entry-point --path <path> --function-name <function_name>
  python: hpc_agent.incorporation.decorate_entry_point.decorate_entry_point
---
## Purpose

Add `@register_run` (and the `from hpc_agent import register_run` import, when absent) to a named module-level function via a structure-preserving AST line-splice. Returns `{path, function_name, decorated, already_decorated, import_added, lines_changed}`.

This is the deterministic replacement for the `Edit`-tool decoration the `hpc-wrap-entry-point` skill used to perform in Step 3a. The `@register_run` contract is exactly those two textual lines — every registration side effect happens at import time off the function's own signature — so a textual insert is sufficient and **the function body is left byte-identical**. Removing the free-form edit removes the affordance that once let a worker rewrite a scaffold's body into experiment logic instead of just decorating it (see `docs/internals/engineering-principles.md` — "The determinism boundary").

## Compose with

- Common predecessor: [detect-entry-point](detect-entry-point.md) (picks the file + function and classifies its CLI surface) — `hpc-wrap-entry-point` Step 2 routes a kwarg'd function here.
- Common successors: `classify-axis-auto` / `interview`. The decorated function's name becomes the `run_name`; `entry_point.kind = "register_run"`.

## Notes

- **Scope: an existing function whose parameters are already real kwargs.** The verb does NOT refactor. It refuses (`spec_invalid`) when the function is not a module-level `def`, the file does not parse, or the function carries a signature-rewriting decorator (`@hydra.main`, a consuming `@click.command` / `@app.command`) that `@register_run` cannot see through — route those to the wrapper fallback (3b) or the `python_module` path.
- **Idempotent** (key: `path`): re-running on an already-`@register_run`-decorated function is a no-op (`already_decorated: true`, no write).
- **Structure-preserving.** The import lands after any `from __future__` imports and the module docstring (so the file still parses); `@register_run` lands outermost over any existing non-consuming decorator. Original line endings (LF / CRLF) are preserved; every line except the inserted import + decorator is byte-identical.
- Pure local filesystem edit; no SSH, no cluster contact. Greenfield repos never reach it — `build-template` scaffolds an already-decorated stub; decoration is the mature-repo path.

