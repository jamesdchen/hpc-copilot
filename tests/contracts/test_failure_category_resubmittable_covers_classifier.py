"""Contract: ``FailureCategoryResubmittable`` covers every classifier emission.

The wire alias
:data:`hpc_agent._wire._shared.FailureCategoryResubmittable`
governs the values accepted by ``resubmit --spec.category``. Two
classifiers can emit a category that the resubmit path must then
accept:

* :data:`hpc_agent.infra.failure_signatures.CATALOG` — the single
  pattern catalog (``classify()``); ``cluster_failures_by_fingerprint``
  delegates to it (so ``CLASSIFIER_CATEGORIES`` is the emitted-category set).

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
   :class:`hpc_agent._kernel.contract.vocabulary.FailureCategory` — the
   StrEnum is the canonical Python home (per its docstring); the wire
   Literal must not carry values the StrEnum lacks.
"""

from __future__ import annotations

import typing

from hpc_agent._kernel.contract.vocabulary import FailureCategory as FailureCategoryEnum
from hpc_agent._wire._shared import (
    FailureCategoryResubmittable,
)
from hpc_agent.infra.failure_signatures import CATALOG, CLASSIFIER_CATEGORIES


def _resubmittable_args() -> set[str]:
    return set(typing.get_args(FailureCategoryResubmittable))


def _catalog_categories() -> set[str]:
    """Every ``error_class`` value :data:`CATALOG` carries."""
    return {sig.error_class for sig in CATALOG}


def _fingerprint_categories() -> set[str]:
    """Every category ``cluster_failures_by_fingerprint`` can emit."""
    # The runner now delegates to ``classify()``, so its emitted classes are
    # the catalog's (CLASSIFIER_CATEGORIES). Add the two sentinel categories
    # ``cluster_failures_by_fingerprint`` emits for missing logs and
    # SSH-unreachable transports — category strings the caller may see even
    # though they aren't catalog rows.
    base = set(CLASSIFIER_CATEGORIES)
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
        "category that hpc_agent.infra.failure_signatures.CATALOG emits "
        f"— resubmit would 400 these otherwise: {sorted(missing)}. "
        "Add the missing values to the Literal in "
        "src/hpc_agent/_wire/_shared.py."
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
        "category cluster_failures_by_fingerprint (via failure_signatures.CLASSIFIER_CATEGORIES) "
        f"emits — resubmit would 400 these otherwise: {sorted(missing)}."
    )


def test_status_classifier_maps_every_catalog_class() -> None:
    """Every catalog ``error_class`` must be handled by the ``/status``
    classifier's ``_SIGNATURE_TO_CATEGORY`` map (or be one of the locally
    handled / sentinel classes), so a new catalog row cannot silently fall
    through to ``"unknown"`` and degrade the /status action table.
    """
    from hpc_agent.execution.mapreduce.reduce.classify import _SIGNATURE_TO_CATEGORY

    catalog = _catalog_categories()
    # ``unknown`` is the catalog's own catch-all; classify_failure returns it
    # by design (no remap needed).
    unmapped = catalog - set(_SIGNATURE_TO_CATEGORY) - {"unknown"}
    assert not unmapped, (
        "execution/mapreduce/reduce/classify.py::_SIGNATURE_TO_CATEGORY is "
        "missing catalog error_class values, so /status would classify these "
        f"real failures as 'unknown': {sorted(unmapped)}. Add them to the map."
    )


def test_resubmittable_subset_of_lifecycle_enum() -> None:
    """The wire Literal must not carry values the canonical StrEnum lacks.

    :class:`hpc_agent._kernel.contract.vocabulary.FailureCategory` is the
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
        "src/hpc_agent/_kernel/contract/vocabulary.py) or remove them "
        "from the Literal."
    )
