"""Pydantic models that author the wire schemas under ``hpc_agent/schemas/``.

The hand-authored JSON files are the *wire SoT* — that's what
external agents (integrator harnesses, the LLM reading
``capabilities --full``, the in-process ``validate_output()`` boundary
check) actually read. The
models in this package are the *authoring SoT* — the human edits
Python with mypy/IDE support, and ``scripts/build_schemas.py`` emits
the JSON via ``model_json_schema()``. Same arrow direction as the
``@primitive`` decorator → ``docs/primitives/<name>.md`` frontmatter:
the registry is what you author, the markdown is the artifact, but
the markdown is what gets read on disk.

Pre-commit + CI run ``build_schemas.py --check`` so an edit to a
Pydantic model without regenerating the JSON is a CI failure.
``--write`` regenerates.

Every wire schema authors through Pydantic (run
``python scripts/build_schemas.py --check`` to verify the JSON is up
to date with the models). To add a new schema, define the model
here and append a ``(model, json_path)`` entry to
``SCHEMA_REGISTRY`` in ``scripts/build_schemas.py``; then run
``--write`` to emit the JSON.
"""
