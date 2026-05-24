"""Cross-subject primitive bridge.

When code inside a subject (e.g. an atom in ``ops/recover/``) needs to
*call* a primitive that lives in a different subject, the subject-
imports lint correctly flags a direct
``from hpc_agent.ops.<other_subject>.<module> import …`` as a
cross-subject reach. Routing the call through this module is the
principled escape hatch: ``runner.py`` lives at the package root, so
the lint permits the import.

Post-P5a, workflows live at the ``ops/`` and ``meta/`` role roots as
sibling files (``ops/submit_flow.py``, ``meta/validate_campaign.py``,
…). Workflow-to-atom cross-subject calls happen via direct imports
from the workflow file — this bridge is no longer needed for them.
The bridge survives for the rarer atom-to-atom cross-subject case.

Conceptually this module mirrors what the registry does at the
metadata layer: ``composes=["primitive-name"]`` is the declarative
form; ``from hpc_agent.runner import primitive_name`` is the callable
form. ``scripts/lint_runner_shim.py`` enforces that every re-export
here is itself an ``@primitive``-decorated symbol — no helper /
constant / dataclass back-compat surface accretes. Helper-shaped
shared code belongs in ``infra/``.
"""

from __future__ import annotations

from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.ops.monitor.reconcile import mark_terminal, reconcile
from hpc_agent.ops.monitor.status import record_status
from hpc_agent.ops.recover.runner import resubmit_failed
from hpc_agent.ops.submit.runner import submit_and_record
from hpc_agent.ops.validate.executor_signatures import validate_executor_signatures
from hpc_agent.ops.validate.input_dataset import validate_input_dataset
from hpc_agent.ops.validate.stochastic_marker import validate_stochastic_marker
from hpc_agent.ops.validate.walltime_against_history import (
    validate_walltime_against_history,
)

__all__ = [
    "combine_wave",
    "mark_terminal",
    "reconcile",
    "record_status",
    "resubmit_failed",
    "submit_and_record",
    "validate_executor_signatures",
    "validate_input_dataset",
    "validate_stochastic_marker",
    "validate_walltime_against_history",
]
