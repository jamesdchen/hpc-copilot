---
name: build-template
verb: scaffold
inputs:
- name: repo_dir
  type: path
  description: Target repository root. Defaults to cwd. Must already exist.
- name: force
  type: bool
  description: Overwrite repo-root files that already exist. The framework-owned .hpc/
    assets are re-injected regardless.
  default: false
side_effects:
- writes-file: <repo>/.hpc/{template.mk,scaffold.py} (re-injected every run)
- writes-file: <repo>/{Makefile,.pre-commit-config.yaml,.github/workflows/ci.yml,conftest.py,pyproject.toml}
    (refuses to overwrite without --force)
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent build-template [--repo-dir <dir>] [--force]
  python: hpc_agent.atoms.build_template.build_template
exit_codes:
- 0: ok
- 1: spec_invalid
---

## Purpose

Inject the experiment-template scaffold into a target repo. The scaffold lives *inside* hpc-agent (`hpc_agent/template/scaffold/`) — there is no separate template repo to clone. `build-template` extends what the framework already does with single files (`build-executor`, `build-tasks-py`, the `.hpc/cli.py` copy) to a whole-project scaffold: a `template.mk` make-fragment, the `scaffold.py` notebook helper, a CI workflow, a pre-commit re-export gate, a `conftest.py`, and a `pyproject.toml`.

Injection beats a separate template repo: the scaffold version-locks to the installed hpc-agent (no drift), it works on an existing repo rather than only a fresh clone, and re-running upgrades the framework-owned assets the way `.hpc/cli.py` already self-heals.

## Compose with

- Common predecessors: none — this is the first call when setting up a new experiment repo.
- Common successors: `make new-experiment` (scaffolds an experiment notebook via the injected `.hpc/scaffold.py`), then `discover-runs` / the standard submit pipeline.

## Notes

- **Two tiers, two overwrite policies.** The framework-owned `.hpc/` assets (`.hpc/template.mk`, `.hpc/scaffold.py`) are re-injected verbatim on every run — self-healing, never refused. Repo-root files (`Makefile`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `conftest.py`, `pyproject.toml`) have paths fixed by make / pip / pre-commit / GitHub Actions and so cannot live under `.hpc/`; they are refused without `--force` when they already exist.
- **Non-destructive merges.** An existing `Makefile` is never clobbered — `build-template` only appends the one `include .hpc/template.mk` line if it is missing. An existing `pyproject.toml` is left untouched entirely; the fragment is written to `.hpc/pyproject-fragment.toml` and reported under `needs_manual_merge` for a hand-merge.
- **The `hpc_agent.template` library is not injected.** It is a `pip install hpc-agent` dependency — imported, never vendored into the target repo.
- **Result shape.** `{repo_dir, framework_files, written, skipped, merged, needs_manual_merge}` — `framework_files` are the self-healing `.hpc/` assets, `written` are root files newly created (or overwritten under `--force`), `skipped` already existed and were refused, `merged` were updated non-destructively.
