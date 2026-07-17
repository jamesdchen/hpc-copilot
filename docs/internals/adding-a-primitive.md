# Adding a new primitive

Recipe for adding a wire-surface primitive (atom or workflow) to
hpc-agent. Mirrors the patterns the existing primitives follow
(`hpc-agent capabilities` is the live count); once you know the recipe
the per-primitive work is mechanical.

## Decide first

Two questions, both load-bearing:

1. **`agent_facing`?** True if an agent (LLM or external orchestrator)
   calls this primitive directly. False if it's a framework internal
   composed inside a workflow. The flag tiers `render_llms_full`
   output and routes the doc template you write below; the lint
   `lint_primitive_doc_templates.py` enforces the partition.
2. **`verb`?** One of `query`, `validate`, `mutate`, `submit`,
   `scaffold`, `workflow`. Three are agent-facing by construction
   (workflow / scaffold / validate — pinned by
   `tests/contracts/test_agent_facing_partition.py`); the others are mixed.

## A primitive owns its invariants

> **A primitive must not rely on a sibling procedure step for a
> correctness invariant.** If an invariant is required for the
> primitive's output to be valid — or for downstream / cluster execution
> to succeed — the primitive must establish or verify it itself, or fail
> loudly. It must never silently assume "the procedure already did it."

The decide/act boundary declares primitives first-class and directly
callable, so they must be **safe for any caller**: the faithful spawned
worker, an exploratory in-session agent that calls one primitive
directly, the headless campaign driver, or a raw `--spec` invocation.
The prose procedure (worker prompts, skill bodies) sequences primitives
for convenience — it is *not* a correctness dependency. The moment
execution leaves the procedure (the single-step-direct rule,
`HPC_AGENT_INVOKER=inline`, a direct CLI call), any invariant that lived
only in the prose silently evaporates.

Concretely, when you write a primitive:

- **Validate boundary inputs.** Don't trust that an upstream step
  normalized or range-checked them. (Wire models do the shape work;
  semantic checks are yours.)
- **Establish required artifacts, don't assume them.** If the primitive —
  or the cluster job it launches — needs a file/record to exist,
  create-or-verify it inside the primitive. Example: `submit-flow`
  *guarantees* the cluster-required per-run sidecar
  (`.hpc/runs/<run_id>.json`) exists before rsync — it synthesizes it
  from the spec when a prior step (Step 6d) did not, rather than shipping
  an empty `runs/` that dooms every cluster task
  (`ops/submit_flow.py::_ensure_run_sidecar`).
- **Carry resolved values through the contract, don't drop them.** If a
  value is resolved/validated upstream and the cluster needs it, the spec
  must carry it and the primitive must emit it. Example: scheduler
  `resources` (walltime/mem/cpus) are first-class on the submit spec and
  the backends emit the flags — they used to be resolved in skill prose
  and then silently dropped.
- **Fail loudly on a missing required artifact**, never silently no-op.
  A swallowed `FileNotFoundError` that the cluster will hard-fail on is a
  bug, not a tolerance (`runner.py` warns instead of swallowing).
- **Never destroy the thing you're operating on.** A prune/cleanup step
  must exclude the run it is currently submitting.

When you add or touch a primitive, add a **contract test** for any
load-bearing guarantee so it survives refactors without relying on
procedure fidelity — e.g. "after submit-flow the per-run sidecar for
`run_id` exists and is non-empty", "submit-flow never deletes the
`run_id` it is submitting". See `tests/ops/submit/test_flow.py`
(`TestSidecarGuarantee`).

## The recipe

### 1. Pydantic spec models

If the primitive takes a wire `--spec` payload, create
`src/hpc_agent/_wire/<name>.py`:

```python
from pydantic import BaseModel, ConfigDict, Field
from ._shared import RunIdStrict  # import shared types as needed

class <Name>Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", title="<name> input spec")

    run_id: RunIdStrict
    # ...
```

If the primitive returns a structured `data` block, define a
`<Name>Result` similarly. Reuse aliases from
`_wire/_shared.py` (`RunIdStrict`, `Scheduler`,
`LifecycleStateTerminal`, `ErrorCode`, etc.) — that's how the
inline-vs-cross-file-`$ref` decision stays automatic.

### 2. Register the model

Add `(<Name>Spec, "<name>.input.json")` and/or
`(<Name>Result, "<name>.output.json")` to `SCHEMA_REGISTRY` in
`scripts/build_schemas.py`. Run the one regen recipe (schemas are its
first step):

```bash
uv run python scripts/regen_all.py --write
```

The JSON file lands under `hpc_agent/schemas/`. You don't edit it
by hand again — the Pydantic model is the SoT, the JSON is
regenerated. (This is the same command you re-run in step 7 once the
atom and doc exist; it is idempotent.)

### 3. Decorate the atom

In the appropriate subject module under `ops/<subject>/`,
`meta/<subject>/`, or `incorporation/` (atoms, runner modules, and
flow composites all live together inside the subject — there is no
top-level `atoms/`, `runner/`, or `flows/` package any more):

```python
@primitive(
    name="<name>",
    verb="query",  # or workflow/scaffold/validate/mutate/submit
    side_effects=[],  # SideEffect("ssh", "<cluster>") etc.
    error_codes=[errors.SpecInvalid],  # HpcError subclasses
    idempotent=True,
    idempotency_key="run_id",  # or None for non-stateful
    cli="hpc-agent <name> --spec <path>",  # the shell invocation
    agent_facing=True,
)
def <name>(experiment_dir: Path, *, spec: <Name>Spec) -> <Name>Result:
    # Destructure into typed locals at the top:
    run_id = spec.run_id
    # ...
```

The `cli=` and `agent_facing=` fields are read by
`_kernel/registry/operations.py::operations_catalog()` and projected
into the `capabilities` envelope plus `docs/generated/operations.md`.

### 4. Discovery is automatic

You don't need to touch any module list. `register_primitives()` walks
every public module under `_PRIMITIVE_PACKAGES` (in
`src/hpc_agent/_kernel/registry/primitive.py`) — `hpc_agent.ops`,
`hpc_agent.meta`, `hpc_agent.incorporation`, `hpc_agent.state`,
`hpc_agent.cli`, `hpc_agent.recovery`, `hpc_agent._kernel.extension` — and your
`@primitive(...)` decorator registers itself on import. Adding a
primitive in a brand-new top-level package is the only case that
requires a one-line change to `_PRIMITIVE_PACKAGES`.

`composes=["primitive-name"]` is order-agnostic — atoms and their
composers can be discovered in any order; the registry is finalized
in a single pass once every module has been imported.

### 5. CLI handler (only if exposing a new subcommand)

In the appropriate `src/hpc_agent/cli/<module>.py` (e.g. `cli/submit.py`,
`cli/lifecycle.py`):

```python
def cmd_<name>(args: argparse.Namespace) -> int:
    raw = _load_spec(args.spec, schema_name="<name>")
    spec = <Name>Spec.model_validate(raw)
    result = <name>(args.experiment_dir, spec=spec)
    _ok(result.model_dump(mode="json"), name="<name>")
    return EXIT_OK
```

Wire into `build_parser()` in `cli/parser.py` alongside the other
subcommands. The existing `_validate_against_schema` call (in
`cli/_helpers.py`) provides diagnostic-quality error messages on top of
Pydantic's own validation (kept for the better error path).

### 6. Write the doc

Two templates, picked by `agent_facing`:

#### Agent-facing (`agent_facing=True`)

```markdown
# <name>

<one-paragraph what+why>

## Inputs

- field (type) — description.

## Outputs

`{...}` shape.

## Errors

- error_code — when fired.

## Idempotency

Description of replay semantics.

## Notes

Anything else.
```

#### Internal (`agent_facing=False`)

```markdown
# <name>

> **Internal primitive.** [where it's composed from]

<one-paragraph what+why>

## Composers

- who calls this (workflows, slash commands, ad-hoc tooling).

## Invariants

- pure read / write boundaries
- ordering / precedence rules

## Coupling

What changes alongside this if you edit it.

## Failure modes

Known sharp edges.
```

The frontmatter (name / verb / side_effects / etc.) is
auto-generated. Only write the body.

### 7. Run all the regen + lint gates

```bash
uv run python scripts/regen_all.py --write
uv run python scripts/lint_primitive_doc_templates.py
```

`regen_all.py` runs all six generators in dependency order (schemas →
baked `operations.json` → primitive frontmatter → primitive index →
operations index → verb-module map) plus the
`check_no_pending_primitive_docs` gate — one recipe instead of the
divergent per-doc enumerations that used to drift. A new *ungrouped*
verb also regenerates `cli/_verb_module_map.py` (the CLI single-verb
fast-path map): an earlier recipe omitted that step, so a session
following it shipped a stale map.

Pre-commit runs regen-all and the doc-template lint. CI's `--check` mode
(`python scripts/regen_all.py --check`) trips on any drift.

### 8. Tests

Minimum:

- A unit test exercising the atom directly (construct the spec,
  call, assert on result).
- The existing `test_primitive_registry.py` and
  `test_primitive_frontmatter.py` parametrize over the registry, so
  the auto-discovery walk picks up your primitive and you get
  registry-level coverage for free.
- The `test_schema_models_roundtrip.py` round-trip test
  parametrizes over `SCHEMA_REGISTRY` — your new schema gets
  emit-byte-equal coverage automatically.

If the new primitive is a workflow, the existing
`tests/contracts/test_agent_facing_partition.py::test_workflows_are_agent_facing`
will fail unless you set `agent_facing=True`.

## What you don't have to do

- Edit any JSON file by hand.
- Edit `docs/generated/operations.md`, `docs/primitives/README.md`,
  or any per-primitive frontmatter — the regen scripts own those.
- Mirror your enum values in multiple places —
  `_wire/_shared.py` is the SoT for shared constraints
  (run_id, scheduler, lifecycle, error codes); import the alias.
