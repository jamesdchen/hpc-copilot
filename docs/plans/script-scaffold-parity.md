# Script-scaffold parity follow-up plan

## Handoff context

Current state: PR #123 on branch `claude/interview-entry-point` landed two things:

1. The wrapper-as-fallback reframe — `@register_run` direct decoration is the canonical Python entry-point path; `shell_command` wrapper materialization is the fallback for non-Python or decorator-conflicting entry points.
2. A `/wrap-entry-point-hpc` slash command + `hpc-wrap-entry-point` skill that onboards an *existing* entry point in a repo.

This work is the follow-up that:

- Adds `.py`-script as a peer scaffold to notebooks.
- Extends `/wrap-entry-point-hpc` to also handle greenfield repos (no entry point yet).
- Pins the canonical contract in one doc page.

**`/setup-hpc` (user-level install + cluster preflight) is explicitly out of scope.** This is per-repo work — a user with N experiment repos shouldn't redo user-level setup N times.

## What we're trying to do

Make hpc-agent's contract legible as **"give me a `@register_run`-decorated Python function"** — where the function can live in a notebook, a `.py` script, or a package module — rather than as "author a notebook." The machinery is already file-agnostic (`discover_runs` AST-walks both `.py` and `.ipynb`). The lock-in is in the on-ramp and the docs.

The fix: make the **per-repo on-ramp** bilingual. Notebook stays. Script becomes a peer.

## Anchor reads (do these in parallel before editing)

The agent should read these files first to anchor before any writes:

- `src/hpc_agent/incorporation/build/template.py` — the `build-template` primitive (where the `--shape` flag lands).
- `src/hpc_agent/incorporation/build/scaffolds/experiment.ipynb.tmpl` — the existing notebook scaffold (mirror its shape for the `.py` version).
- `src/slash_commands/skills/hpc-wrap-entry-point/SKILL.md` — the per-repo skill being extended.
- `src/hpc_agent/_kernel/extension/worker_prompts/submit.md` — Step 1 has the "notebook-first" language to fix.
- `README.md` — check the elevator pitch's framing.
- `src/hpc_agent/experiment_kit/discover.py` — confirm `discover_runs` is genuinely file-agnostic; needed for the contract page.

## Deliverables

Five concrete deliverables. Each scoped tightly.

### 1. Add a `.py`-script scaffold next to the notebook scaffold

- **New file**: `src/hpc_agent/incorporation/build/scaffolds/train.py.tmpl` — stdlib-only `train.py` with:
  - `from hpc_agent import register_run`
  - `@register_run def run(<typed-kwargs>) -> None: ...` — the entry point.
  - `if __name__ == "__main__":` block with an argparse parser calling `run(**vars(args))`.
  - One-line docstring referencing the canonical contract page from deliverable (3).
- **Update `src/hpc_agent/incorporation/build/template.py`** (the `build-template` primitive) to accept `--shape {notebook,script}`. **Default to `script`** — the mature-repo audience is the dominant case at scale-up time.
- **Tests**: parametrize existing `build-template` tests over both shapes; assert both produce discoverable `@register_run` functions (use `discover_runs` against the scaffolded repo as the assertion).

### 2. Extend `/wrap-entry-point-hpc` to handle the no-entry-point case

The skill currently detects an existing entry point and walks the user through `@register_run` decoration. Add a Step 0 that runs first.

- **Modify `src/slash_commands/skills/hpc-wrap-entry-point/SKILL.md`**:
  - Add **Step 0: Detect or scaffold an entry point** before the existing Step 1.
  - If no entry-point file is found in the repo (no `main.py`, no `train.py`, no `__main__.py`, no `console_scripts`, no `*.ipynb` with `@register_run`), offer the user a choice: scaffold a notebook (literate, iteration-phase) or a `.py` script (already-finalized executor). Default to script.
  - Call out to `build-template --shape {chosen}` to do the scaffolding so there's one source of truth.
  - Then proceed through the existing Step 1 onwards against the freshly-scaffolded file.
- The slash command body (`src/slash_commands/commands/wrap-entry-point-hpc.md`) currently frames the skill as "mature-repo onboarding" — narrow. Broaden the framing to cover both states (greenfield scaffolding + existing-entry-point onboarding) in one short paragraph. Don't rename the file; just update the prose.

### 3. Write one canonical contract page

- **New file**: `docs/internals/experiment-contract.md` (~80 lines). Sections:
  - **What is an experiment?** A `@register_run`-decorated Python function with typed kwargs. The framework's contract is that function — nothing else.
  - **Where can the function live?** Three peer locations: a notebook (`.ipynb`), a script (`.py`), or a package module (`mypkg/runner.py`). The framework treats them identically — `discover_runs` AST-walks all three.
  - **Notebook-specific step**: `export-package` converts `.ipynb` to `src/<name>.py` for the cluster (which runs stdlib-only `.py`). For repos whose entry point is already `.py`, this step is a no-op.
  - **The `tasks.py` contract is unchanged** regardless of where the function lives.
  - **Fallback for non-Python entry points**: when the entry point is a compiled binary, a shell script, or has a decorator conflict (e.g. `@hydra.main`), `/wrap-entry-point-hpc` materializes a `@register_run` wrapper as the rescue boat. Document this as the documented fallback, not the default.
- Cross-link from `README.md` and from the slash command body in `src/slash_commands/commands/wrap-entry-point-hpc.md`.

### 4. Targeted prose update in two places

- **`src/hpc_agent/_kernel/extension/worker_prompts/submit.md` Step 1** — currently:
  > "Notebook-first. The researcher authors only a notebook carrying a `@register_run def run(...)` — no axis declaration, no `tasks.py`, no CLI glue. Discovery is `discover_runs` over `notebooks/`, which AST-walks `.py` and `.ipynb` files (skipping `.hpc/`)..."

  Rewrite to lead with **function-first**, not notebook-first:
  > "Discover the user's `@register_run` function. The function may live in a notebook (`.ipynb`), a script (`.py`), or a package module — `discover_runs` AST-walks all three. The example invocation below scans `notebooks/`; for scripts at the repo root or under `src/`, the discovery walks those too."

  Update the bash example if needed. Then regenerate the prefix snapshot fixture:
  ```bash
  WORKER_PROMPT_SNAPSHOT_UPDATE=1 python -m pytest tests/worker_prompts/test_prefix_snapshot.py -q
  ```

- **`README.md`** — check the elevator pitch. If it leads with "notebook," fix it. Otherwise add a one-line note pointing at `docs/internals/experiment-contract.md`.

**Don't sweep further.** Most other "notebook" mentions in `docs/` are accurate or contextual; the new contract page is the new SoT, and old pages can age into linking to it.

### 5. One reference example in fixtures

- **New directory**: `tests/fixtures/sample_experiments/script/` with a complete minimal `.py`-shape experiment:
  - `train.py` with `@register_run def run(seed: int, lr: float) -> None: ...`
  - `.hpc/tasks.py` doing a tiny `enumerated` sweep over a few (seed, lr) combos.
  - `pyproject.toml` declaring the package (minimal — name, version, `hpc-agent` dep).
- **New end-to-end test** that proves discovery + dispatch work on the `.py`-only path:
  - Walk this fixture with `discover_runs`; assert the `@register_run` function is found.
  - Load `tasks.py` via `load_tasks_module`; assert `total()` matches the fixture's expected count, `resolve(i)` returns the expected dict shape.
  - (Optional) Drive `compute(args)` through `experiment_kit._runtime` to confirm the function actually runs.

## Explicit non-goals

- **Don't touch `/setup-hpc`.** It's user-level (install, cluster auth, preflight cache). This work is per-repo. A user with multiple experiment repos shouldn't redo user-level setup multiple times.
- **Don't rename the `notebooks/` directory.** Search root, not a constraint. Renaming would be the most disruptive change here and the directory name doesn't materially mislead.
- **Don't sweep every "notebook" mention in `docs/`.** Most are accurate or contextual. The new contract page from deliverable (3) is the new SoT.
- **Don't rewrite `export-package`.** It's a notebook-conversion primitive; it should stay notebook-shaped. The contract page just notes it's a no-op for `.py`-shape repos.
- **Don't touch the `shell_command` wrapper machinery in `src/hpc_agent/incorporation/wrap_entry_point.py`.** The just-landed demote already positions it correctly.
- **Don't rename `/wrap-entry-point-hpc`.** Slight name awkwardness for the greenfield case is acceptable; renaming churns surfaces that just got renamed once already.
- **Don't add a migration guide.** Nothing breaks for existing users.

## Acceptance criteria

When this lands:

- `hpc-agent build-template --shape script` writes a working `train.py` (with `@register_run` + argparse).
- A user running `/wrap-entry-point-hpc` on an empty repo gets offered the choice "notebook or script?" and ends up with a working starter + `@register_run` + `tasks.py`.
- A user running `/wrap-entry-point-hpc` on a repo that already has `train.py` gets the existing decoration flow unchanged.
- `/setup-hpc` is unmodified.
- `docs/internals/experiment-contract.md` exists and is the canonical "what is an experiment" page.
- `src/hpc_agent/_kernel/extension/worker_prompts/submit.md` Step 1 no longer leads with "notebook-first."
- A `.py`-shape sample experiment in `tests/fixtures/sample_experiments/script/` passes end-to-end discovery + dispatch.
- All 212 existing tests still pass:
  ```bash
  python -m pytest tests/ops/memory/ tests/incorporation/ tests/worker_prompts/ tests/contracts/ --ignore=tests/_wire/test_schema_models_roundtrip.py -q
  ```

## Sequencing

Mostly parallel; one sequential dependency.

- **Group A (parallel)**:
  - Deliverable (1) — `.py` scaffold + `build-template` update.
  - Deliverable (5) — sample experiment fixture + e2e test.
  - Deliverable (3) — canonical contract page.
- **Group B (after A)**:
  - Deliverable (2) — `/wrap-entry-point-hpc` skill extension. Needs (1)'s scaffold to call into via `build-template`.
- **Group C (last)**:
  - Deliverable (4) — targeted prose update + snapshot regen.

Use parallel tool calls aggressively within each group. Independent reads can be batched. Independent edits to different files can be batched.

## Verify

Before committing:

```bash
# Regenerate the prefix snapshot if submit.md changed
WORKER_PROMPT_SNAPSHOT_UPDATE=1 python -m pytest tests/worker_prompts/test_prefix_snapshot.py -q

# Full nearby test suite
python -m pytest tests/ops/memory/ tests/incorporation/ tests/worker_prompts/ tests/contracts/ --ignore=tests/_wire/test_schema_models_roundtrip.py -q
```

Expected: 212+ passing (the new e2e fixture test adds at least one).

## Branch + PR

Open a **new branch** off `main`: `claude/script-scaffold-parity` (or similar). Different scope from PR #123 — deserves its own review.

Open a fresh PR; don't fold into PR #123.

Commit message footer: `https://claude.ai/code/session_01UY6w7JKZGBm71kxtyk48vs`

## Decision points to flag back to the user

If unclear during execution, flag these (don't guess):

1. **Default scaffold shape**: plan says `script`. If `notebook` feels more natural for the existing user base, surface the trade-off and let the user decide.
2. **`/wrap-entry-point-hpc` naming**: now that it handles both greenfield and existing-entry-point cases, the name is slightly off. The plan says **don't rename**. If during execution you find the name's actively misleading, surface the concern.
3. **README scope**: if the README has a notebook-heavy elevator pitch that wants a bigger rewrite than "one-line note pointing at the contract page," surface the scope question — the plan caps at minimal touches.

## Estimated scope

6–10 files. Each small. ~half a day at a normal pace; less in parallel.
