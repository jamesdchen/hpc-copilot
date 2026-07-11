"""Contract: the wire ``FailureCategory`` Literal covers every classifier emission.

The wire alias :data:`hpc_agent._wire._shared.FailureCategory` types the
``error_class`` field of the ``failure_features`` evidence block
(:class:`hpc_agent._wire.fixtures.failure_features.FailureFeatures`). At the
monitor's terminal-FAILED resolve-and-recover tick,
``ops.recover.features_glue`` feeds the fingerprint classifier's raw
``error_class`` (from :data:`hpc_agent.infra.failure_signatures.CATALOG`)
straight into ``FailureFeatures.error_class`` via ``model_validate``.

Before bug-sweep 2026-07-11 #2 the Literal held only 8 coarse values while the
catalog emits 19 fine-grained classes, so an ordinary failure (``import_error``,
``python_traceback``, ``mpi_*`` ...) raised a pydantic ``ValidationError`` here
and killed the whole monitor tick. This test pins the invariant so the next
catalog row cannot silently re-introduce the crash:

1. Every category :data:`CLASSIFIER_CATEGORIES` emits is a member of
   ``get_args(FailureCategory)``.
2. ``get_args(FailureCategory)`` is itself a subset of the canonical StrEnum
   :class:`hpc_agent._kernel.contract.vocabulary.FailureCategory` — the wire
   Literal must not carry values the canonical home lacks.
"""

from __future__ import annotations

import typing

from hpc_agent._kernel.contract.vocabulary import FailureCategory as FailureCategoryEnum
from hpc_agent._wire._shared import FailureCategory
from hpc_agent.infra.failure_signatures import CLASSIFIER_CATEGORIES


def _wire_args() -> set[str]:
    return set(typing.get_args(FailureCategory))


def _enum_values() -> set[str]:
    return {member.value for member in FailureCategoryEnum}


def test_failure_category_covers_classifier_emissions() -> None:
    """Every category the fingerprint classifier can stamp must be a valid
    ``FailureFeatures.error_class`` value, or the resolve-and-recover tick
    crashes with a ``ValidationError``."""
    missing = set(CLASSIFIER_CATEGORIES) - _wire_args()
    assert not missing, (
        "The wire FailureCategory Literal must cover every category that "
        "hpc_agent.infra.failure_signatures.CATALOG emits — "
        "FailureFeatures.error_class validation crashes the monitor's "
        f"terminal-FAILED tick otherwise: {sorted(missing)}. Add the missing "
        "values to the Literal in src/hpc_agent/_wire/_shared.py."
    )


def test_failure_category_subset_of_kernel_enum() -> None:
    """The wire Literal must not carry values the canonical StrEnum lacks."""
    extras = _wire_args() - _enum_values()
    assert not extras, (
        "The wire FailureCategory Literal carries values the canonical "
        f"FailureCategory StrEnum lacks: {sorted(extras)}. Either add them to "
        "the StrEnum (SoT in src/hpc_agent/_kernel/contract/vocabulary.py) or "
        "remove them from the Literal in src/hpc_agent/_wire/_shared.py."
    )
