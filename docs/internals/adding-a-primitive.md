# Adding a new primitive

Recipe for adding a wire-surface primitive (atom or workflow) to
claude-hpc. Mirrors the patterns the existing 50 primitives follow;
once you know the recipe the per-primitive work is mechanical.

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
   `tests/test_agent_facing_partition.py`); the others are mixed.

## The recipe

### 1. Pydantic spec models

If the primitive takes a wire `--spec` payload, create
`src/claude_hpc/_schema_models/<name>.py`:

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
`_schema_models/_shared.py` (`RunIdStrict`, `Scheduler`,
`LifecycleStateTerminal`, `ErrorCode`, etc.) — that's how the
inline-vs-cross-file-`$ref` decision stays automatic.

### 2. Register the model

Add `(<Name>Spec, "<name>.input.json")` and/or
`(<Name>Result, "<name>.output.json")` to `SCHEMA_REGISTRY` in
`scripts/build_schemas.py`. Run:

```bash
uv run python scripts/build_schemas.py --write
```

The JSON file lands under `claude_hpc/schemas/`. You don't edit it
by hand again — the Pydantic model is the SoT, the JSON is
regenerated.

### 3. Decorate the atom

In the appropriate module (`atoms/`, `runner/`, `flows/`, etc.):

```python
@primitive(
    name="<name>",
    verb="query",  # or workflow/scaffold/validate/mutate/submit
    side_effects=[],  # SideEffect("ssh", "<cluster>") etc.
    error_codes=[errors.SpecInvalid],  # HpcError subclasses
    idempotent=True,
    idempotency_key="run_id",  # or None for non-stateful
    cli="hpc-mapreduce <name> --spec <path>",  # the shell invocation
    agent_facing=True,
)
def <name>(experiment_dir: Path, *, spec: <Name>Spec) -> <Name>Result:
    # Destructure into typed locals at the top:
    run_id = spec.run_id
    # ...
```

The `cli=` and `agent_facing=` fields are read by
`_internal/operations.py::operations_catalog()` and projected into
the `capabilities` envelope plus `docs/generated/operations.md`.

### 4. Add to `_PRIMITIVE_MODULES`

If your atom's module is new, add it to `_PRIMITIVE_MODULES` in
`src/claude_hpc/_internal/_primitive.py` (the explicit registration
ordering). `lint_primitive_modules.py` greps `@primitive(` and
catches missing entries.

### 5. CLI handler (only if exposing a new subcommand)

In `src/claude_hpc/agent_cli.py`:

```python
def cmd_<name>(args: argparse.Namespace) -> int:
    raw = _load_spec(args.spec, schema_name="<name>")
    spec = <Name>Spec.model_validate(raw)
    result = <name>(args.experiment_dir, spec=spec)
    _ok(result.model_dump(mode="json"), name="<name>")
    return EXIT_OK
```

Wire into `_main_parser()` alongside the other subcommands. The
existing `_validate_against_schema` call provides
diagnostic-quality error messages on top of Pydantic's own
validation (kept for the better error path).

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
uv run python scripts/build_primitive_frontmatter.py --write
uv run python scripts/build_primitive_index.py
uv run python scripts/build_operations_index.py
uv run python scripts/build_schemas.py --write
uv run python scripts/lint_primitive_modules.py
uv run python scripts/lint_primitive_doc_templates.py
```

Pre-commit runs all six. CI's `--check` mode trips on any drift.

### 8. Tests

Minimum:

- A unit test exercising the atom directly (construct the spec,
  call, assert on result).
- The existing `test_primitive_registry.py` and
  `test_primitive_frontmatter.py` parametrize over the registry, so
  adding to `_PRIMITIVE_MODULES` automatically gets you registry-level
  coverage.
- The `test_schema_models_roundtrip.py` round-trip test
  parametrizes over `SCHEMA_REGISTRY` — your new schema gets
  emit-byte-equal coverage automatically.

If the new primitive is a workflow, the existing
`test_agent_facing_partition.py::test_workflows_are_agent_facing`
will fail unless you set `agent_facing=True`.

## What you don't have to do

- Edit any JSON file by hand.
- Edit `docs/generated/operations.md`, `docs/primitives/README.md`,
  or any per-primitive frontmatter — the regen scripts own those.
- Mirror your enum values in multiple places —
  `_schema_models/_shared.py` is the SoT for shared constraints
  (run_id, scheduler, lifecycle, error codes); import the alias.
