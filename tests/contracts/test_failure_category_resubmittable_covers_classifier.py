"""Contract: ``FailureCategoryResubmittable`` covers every classifier emission.

The wire alias
:data:`hpc_agent._schema_models._shared.FailureCategoryResubmittable`
governs the values accepted by ``resubmit --spec.category``. Two
classifiers can emit a category that the resubmit path must then
accept:

* :data:`hpc_agent.ops.recover.failure_signatures.CATALOG` — the
  high-priority pattern catalog (``classify()``).
* :data:`hpc_agent.ops.recover.runner_failures._FAILURE_CATEGORY_PATTERNS` — the
  sibling fingerprint classifier used by
  :func:`cluster_failures_by_fingerprint`.

When ``FailureCategoryResubmittable`` is a strict subset of either
emitter, a real failure mode classifies cleanly cluster-side but the
resubmit path silently rejects the resulting category with a 400. The
audit found exactly this: the Literal excluded ``import_error``,
``file_not_found``, ``permission_denied``, ``disk_full``, and
``python_traceback`` — every classifier emitted them; resubmit rejected
them. PR A widens the Literal; this test pins the invariant so the next
catalog row can't silently break resubmit.

The test asserts two properties:

1. The union of categories emitted by both classifiers is a subset of
   ``FailureCategoryResubmittable`` (the wire Literal must accept
   everything either classifier emits).
2. ``FailureCategoryResubmittable`` is itself a subset of the StrEnum
   :class:`hpc_agent._internal.lifecycle.FailureCategory` — the
   StrEnum is the canonical Python home (per its docstring); the wire
   Literal must not carry values the StrEnum lacks.
"""

from __future__ import annotations

import typing

from hpc_agent._internal.lifecycle import FailureCategory as FailureCategoryEnum
from hpc_agent._schema_models._shared import (
    FailureCategoryResubmittable,
)
from hpc_agent.ops.recover.failure_signatures import CATALOG
from hpc_agent.ops.recover.runner_failures import _FAILURE_CATEGORY_PATTERNS


def _resubmittable_args() -> set[str]:
    return set(typing.get_args(FailureCategoryResubmittable))


def _catalog_categories() -> set[str]:
    """Every ``error_class`` value :data:`CATALOG` carries."""
    return {sig.error_class for sig in CATALOG}


def _fingerprint_categories() -> set[str]:
    """Every category :data:`_FAILURE_CATEGORY_PATTERNS` can emit."""
    # Each entry is ``(category, regex)``. Add the two sentinel
    # categories emitted by ``cluster_failures_by_fingerprint`` for
    # missing logs and SSH-unreachable transports — those are
    # category strings the caller may see, even though they don't
    # live in the pattern table.
    base = {cat for cat, _pat in _FAILURE_CATEGORY_PATTERNS}
    base |= {"ssh_unreachable", "log_missing"}
    return base


def _enum_values() -> set[str]:
    return {member.value for member in FailureCategoryEnum}


def test_resubmittable_covers_catalog_emissions() -> None:
    """Every category :data:`CATALOG` emits must be resubmittable."""
    catalog = _catalog_categories()
    resubmittable = _resubmittable_args()
    missing = catalog - resubmittable
    assert not missing, (
        "FailureCategoryResubmittable must be a superset of every "
        "category that hpc_agent.ops.recover.failure_signatures.CATALOG emits "
        f"— resubmit would 400 these otherwise: {sorted(missing)}. "
        "Add the missing values to the Literal in "
        "src/hpc_agent/_schema_models/_shared.py."
    )


def test_resubmittable_covers_fingerprint_emissions() -> None:
    """Every category the fingerprint classifier emits must be resubmittable."""
    fp = _fingerprint_categories()
    resubmittable = _resubmittable_args()
    missing = fp - resubmittable
    # ``ssh_unreachable`` and ``log_missing`` are observation
    # categories carried on the failure rollup, not resubmit inputs —
    # they can be in fp but not in resubmittable. Exempt them.
    missing -= {"ssh_unreachable", "log_missing"}
    assert not missing, (
        "FailureCategoryResubmittable must be a superset of every "
        "category that hpc_agent.ops.recover.runner_failures._FAILURE_CATEGORY_PATTERNS "
        f"emits — resubmit would 400 these otherwise: {sorted(missing)}."
    )


def test_resubmittable_subset_of_lifecycle_enum() -> None:
    """The wire Literal must not carry values the canonical StrEnum lacks.

    :class:`hpc_agent._internal.lifecycle.FailureCategory` is the
    canonical Python home for this vocabulary (per its docstring).
    The wire Literal exists for schema generation only and must mirror
    the enum.
    """
    resubmittable = _resubmittable_args()
    enum = _enum_values()
    extras = resubmittable - enum
    assert not extras, (
        "FailureCategoryResubmittable carries values the canonical "
        f"FailureCategory StrEnum lacks: {sorted(extras)}. Either add "
        "the values to the StrEnum (canonical SoT in "
        "src/hpc_agent/_internal/lifecycle.py) or remove them from the "
        "Literal."
    )
