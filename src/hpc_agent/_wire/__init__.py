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
to date with the models). To add a new schema, define the model in the
package that owns it — a verb's ``*Input`` / ``*Result`` goes in the
matching ``actions/`` / ``queries/`` / ``validators/`` / ``workflows/``
module, whose name suffix ``build_schemas.py`` dispatches on
automatically; then run ``--write`` to emit the JSON. Discovery is by
``pkgutil.walk_packages`` over this package (see
``build_schemas.py::_build_schema_registry_for``), so there is **no
manual ``SCHEMA_REGISTRY`` list to append to** — the only edit for a
suffixed verb shape is defining the model.

Cross-cutting shapes that don't fit the suffix convention (the
envelope, escalation, failure-features, axes/manifest/stages shapes)
live in :mod:`hpc_agent._wire.fixtures` and are pinned by name in
``build_schemas.py::_NON_SUFFIX_MAPPING``; see that package's docstring
for the layout and the ``__module__`` constraint that makes moving one
expensive.
"""
