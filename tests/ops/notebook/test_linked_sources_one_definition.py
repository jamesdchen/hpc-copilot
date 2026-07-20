"""Route-through pins: import→source resolution has ONE home (B7 one-definition).

``ops/notebook/linked_sources.py`` is the ONE definition of "which file does
this import resolve to under a caller source_root" — extracted 2026-07-07 so
``notebook-lint`` (rule 3) and ``notebook-draft-context`` resolve identically,
and consumed by the graduation gate via the recorded ``linked_sources``
(``{module, file, module_sha}``) it produces. These pins are the
``test_layers_share_one_drift_predicate`` pattern applied here: each consumer
must ROUTE THROUGH the shared symbols, and neither may re-inline the
``<module>/__init__.py`` filesystem probe the one definition owns — a re-forked
copy would silently diverge (a lint that links a file the draft-context does
not, or vice versa; the 2026-07 philosophy-audit B7 conversion).
"""

from __future__ import annotations

import inspect


def test_lint_rule3_routes_through_the_one_resolution() -> None:
    from hpc_agent.ops.notebook import lint

    src = inspect.getsource(lint.notebook_lint)
    assert "resolve_linked_sources" in src, "rule 3 must route through the shared resolution"


def test_draft_context_routes_through_the_one_resolution() -> None:
    from hpc_agent.ops.notebook import draft_context_op

    for fn in (draft_context_op._resolve_declared_engine, draft_context_op._resolve_engine):
        assert "resolve_module_file" in inspect.getsource(fn), (
            "draft-context engine resolution must route through the shared definition"
        )


def test_no_consumer_reinlines_the_init_py_probe() -> None:
    """The ``<pkg>/__init__.py`` candidate probe belongs to the ONE definition
    (``linked_sources.resolve_module_file``) — a consumer module growing its own
    copy is the fork this pin exists to catch."""
    from hpc_agent.ops import notebook_gate
    from hpc_agent.ops.notebook import draft_context_op, lint

    for module in (lint, draft_context_op, notebook_gate):
        assert '__init__.py"' not in inspect.getsource(module), (
            f"{module.__name__} re-inlines the module-file resolution probe"
        )


def test_gate_routes_dotted_find_spec_through_the_one_exec_free_walk() -> None:
    """The gate's dotted-name origin lookup routes through the ONE exec-free
    definition (``linked_sources.find_spec_origin_exec_free``) — a dotted
    ``find_spec`` execs the parent's ``__init__.py``, so the never-exec walk of
    ``submodule_search_locations`` has ONE home, never a re-inlined copy in the
    gate."""
    from hpc_agent.ops import notebook_gate

    assert "find_spec_origin_exec_free" in inspect.getsource(notebook_gate._find_spec_origin)
