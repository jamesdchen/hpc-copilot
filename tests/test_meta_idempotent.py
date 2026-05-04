"""Cross-validation: agent_cli ``_meta_idempotent`` vs the catalog.

B4 rewire guard. Asserts:

1. ``_meta_idempotent("<name>")`` returns the same value the catalog
   declares for that primitive (no silent drift between the in-process
   helper and the on-disk @primitive decorations).
2. The default-True fallback is still True for unknown names — kept so
   the migration can land incrementally without a hard breakage on any
   un-migrated callsite.
3. The cache (functools.cache) does not leak state between primitives
   with different idempotency.
"""

from __future__ import annotations

import pytest

from hpc_mapreduce.agent_cli import _meta_idempotent
from hpc_mapreduce.operations import operations_catalog


def test_meta_idempotent_matches_catalog() -> None:
    """For every primitive in the catalog, the helper must agree on the flag."""
    catalog = operations_catalog()
    assert catalog, "operations_catalog() unexpectedly empty — migration check is no-op"
    mismatches: list[str] = []
    for entry in catalog:
        name = entry.get("name")
        if not name:
            continue
        catalog_flag = bool(entry.get("idempotent", True))
        helper_flag = _meta_idempotent(name)
        if helper_flag != catalog_flag:
            mismatches.append(
                f"{name}: catalog={catalog_flag} helper={helper_flag}"
            )
    assert not mismatches, "drift between catalog and _meta_idempotent: " + "; ".join(
        mismatches
    )


def test_meta_idempotent_unknown_name_defaults_true() -> None:
    """Unknown / unmigrated callsites get the pre-B4 default."""
    # use a name that is highly unlikely to ever exist
    assert _meta_idempotent("___definitely_not_a_real_primitive___") is True


def test_meta_idempotent_distinguishes_idempotent_and_not() -> None:
    """The cache must not collapse different primitives to the same answer.

    ``build-executor`` is the canonical non-idempotent primitive
    (verb=scaffold, refuses to overwrite without --force). Pair it with
    a known idempotent one and assert the helper returns the right
    answer for each — guarding against a typo'd ``functools.cache``
    that ignored its argument.
    """
    catalog = {e["name"]: bool(e.get("idempotent", True)) for e in operations_catalog()}
    if "build-executor" not in catalog:
        pytest.skip("build-executor primitive not registered in this checkout")
    # Known non-idempotent
    assert _meta_idempotent("build-executor") is catalog["build-executor"]
    # Known idempotent — pick the first idempotent entry deterministically
    idem_name = next(
        (n for n, flag in sorted(catalog.items()) if flag is True),
        None,
    )
    assert idem_name is not None
    assert _meta_idempotent(idem_name) is True
