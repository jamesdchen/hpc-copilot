"""Pydantic models that author the wire schemas under ``claude_hpc/schemas/``.

The hand-authored JSON files are the *wire SoT* — that's what
external agents (MARs, the LLM reading ``capabilities --full``, the
in-process ``validate_output()`` boundary check) actually read. The
models in this package are the *authoring SoT* — the human edits
Python with mypy/IDE support, and ``scripts/build_schemas.py`` emits
the JSON via ``model_json_schema()``. Same arrow direction as the
``@primitive`` decorator → ``docs/primitives/<name>.md`` frontmatter:
the registry is what you author, the markdown is the artifact, but
the markdown is what gets read on disk.

Pre-commit + CI run ``build_schemas.py --check`` so an edit to a
Pydantic model without regenerating the JSON is a CI failure.
``--write`` regenerates.

Spike scope: only ``submit-flow`` (input + output) is migrated;
the other 51 JSON files are still hand-authored. Roll out atom by
atom by adding (model, json_path) entries to ``SCHEMA_REGISTRY``
in ``scripts/build_schemas.py``.
"""
