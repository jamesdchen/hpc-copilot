---
name: build-template
verb: scaffold
side_effects:
- writes-file: <repo_dir>/{.hpc/template.mk,.hpc/scaffold.py} (self-healing); <repo_dir>/{Makefile,.gitignore,pyproject.toml,.pre-commit-config.yaml,conftest.py,.github/workflows/ci.yml}
    (refuse-without-force at repo root)
idempotent: true
idempotency_key: repo_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent build-template [--repo-dir <repo_dir>] [--force]
  python: hpc_agent.incorporation.build.template.build_template
---
# build-template

Inject the experiment-template scaffold (Makefile, pre-commit config,
GitHub Actions workflow, `.hpc/` framework files, etc.) into a target
repo. Two tiers of files:

- **Framework-owned** (`.hpc/template.mk`, `.hpc/scaffold.py`) —
  re-injected verbatim on every run; self-healing.
- **Repo root** (`Makefile`, `pyproject.toml`, `.pre-commit-config.yaml`,
  `.github/workflows/ci.yml`, `conftest.py`) — refused without `--force`
  when they already exist, except for trivial non-destructive merges
  (an existing `Makefile` gains one `include` line; an existing
  `pyproject.toml` is left alone and the fragment is dropped under
  `.hpc/` for hand-merge).

## Inputs

- `repo_dir` (path): target repo to scaffold. Defaults to cwd.
- `force` (bool): allow overwrite of refuse-by-default repo-root files.

## Outputs

A dict describing the files written, skipped, and refused, with per-file
reason codes for the audit trail.
