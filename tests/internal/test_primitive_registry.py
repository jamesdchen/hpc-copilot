"""Tests for the @primitive decorator and runtime registry.

The registry is the SoT for primitive metadata that other layers
(``operations.py``, ``docs/primitives/*.md`` frontmatter,
``scripts/build_*_index.py``) read instead of duplicating by hand.
These tests pin the decorator's contract; cross-validation against
schemas and frontmatter lives in ``test_primitive_spine.py`` once
decoration is complete.
"""

from __future__ import annotations

import dataclasses

import pytest

from claude_hpc import (
    PrimitiveMeta,
    SideEffect,
    get_meta,
    get_registry,
    primitive,
)
from claude_hpc._internal.primitive import _REGISTRY


def test_decorator_registers_under_given_name() -> None:
    """@primitive(name=...) puts the function in the registry under that name."""
    fname = "test-decorator-registers"

    @primitive(name=fname, verb="query", description="t")
    def my_op() -> int:
        return 7

    try:
        meta = get_meta(fname)
        assert meta.name == fname
        assert meta.verb == "query"
        assert meta.func is my_op
        assert my_op() == 7
    finally:
        _REGISTRY.pop(fname, None)


def test_decorator_attaches_meta_attribute() -> None:
    """The decorated function gets a ``_primitive_meta`` attr pointing at the meta."""
    fname = "test-attaches-meta"

    @primitive(name=fname, verb="mutate")
    def my_op() -> None:
        """First-line docstring."""

    try:
        assert hasattr(my_op, "_primitive_meta")
        assert my_op._primitive_meta is get_meta(fname)
        assert my_op._primitive_meta.description == "First-line docstring."
    finally:
        _REGISTRY.pop(fname, None)


def test_decorator_rejects_duplicate_name() -> None:
    """Registering two distinct functions under the same name is an error."""
    fname = "test-duplicate-name"

    @primitive(name=fname, verb="query")
    def first() -> None:
        pass

    try:
        with pytest.raises(ValueError, match="already registered"):

            @primitive(name=fname, verb="query")
            def second() -> None:
                pass
    finally:
        _REGISTRY.pop(fname, None)


def test_decorator_idempotent_for_same_function() -> None:
    """Re-decorating the same function (e.g. test reload) is a no-op."""
    fname = "test-idempotent"

    def my_op() -> None:
        pass

    try:
        decorated = primitive(name=fname, verb="query")(my_op)
        again = primitive(name=fname, verb="query")(decorated)
        assert again is decorated
        assert get_meta(fname).func is my_op
    finally:
        _REGISTRY.pop(fname, None)


def test_get_registry_snapshot_independent_of_mutation() -> None:
    """Mutating the returned dict must not leak back into the registry."""
    fname = "test-snapshot-independence"

    @primitive(name=fname, verb="query")
    def my_op() -> None:
        pass

    try:
        snap = get_registry()
        assert fname in snap
        snap.pop(fname)
        assert fname in get_registry()
    finally:
        _REGISTRY.pop(fname, None)


def test_side_effect_dataclass_immutable() -> None:
    """SideEffect is frozen so meta can be safely shared across threads."""
    se = SideEffect("rsync", "host:/path")
    with pytest.raises(dataclasses.FrozenInstanceError):
        se.kind = "ssh"  # type: ignore[misc]


def test_primitive_meta_carries_all_fields() -> None:
    """All decorator kwargs round-trip onto the PrimitiveMeta.

    ``composes`` now holds ``PrimitiveMeta`` refs (resolved from
    function refs at decoration time). Register a stub atom so the
    composes lookup succeeds.
    """
    atom_name = "test-carries-fields-atom"
    fname = "test-carries-fields"

    @primitive(name=atom_name, verb="query")
    def atom_op() -> None:
        pass

    @primitive(
        name=fname,
        verb="workflow",
        composes=[atom_op],
        side_effects=[SideEffect("rsync", "x"), SideEffect("ssh", "y")],
        idempotent=False,
        idempotency_key="run_id",
        exit_codes=[(0, "ok"), (1, "user-error")],
        description="explicit description",
        cli="hpc-agent test-carries-fields --spec <path>",
        agent_facing=True,
    )
    def my_op() -> None:
        pass

    try:
        meta = get_meta(fname)
        assert isinstance(meta, PrimitiveMeta)
        assert len(meta.composes) == 1
        assert meta.composes[0].name == atom_name
        assert meta.composes[0].func is atom_op
        assert meta.side_effects == (
            SideEffect("rsync", "x"),
            SideEffect("ssh", "y"),
        )
        assert meta.idempotent is False
        assert meta.idempotency_key == "run_id"
        assert meta.exit_codes == ((0, "ok"), (1, "user-error"))
        assert meta.description == "explicit description"
        assert meta.cli == "hpc-agent test-carries-fields --spec <path>"
        assert meta.agent_facing is True
        # And atom_op defaults to agent_facing=False
        assert get_meta(atom_name).agent_facing is False
    finally:
        _REGISTRY.pop(fname, None)
        # atom_op is also registered; pop it too so it doesn't leak
        # into other tests that walk the full registry (e.g.
        # test_every_registered_primitive_has_a_doc).
        _REGISTRY.pop(atom_name, None)
