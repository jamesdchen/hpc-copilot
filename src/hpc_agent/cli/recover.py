"""Recover verb-group argparse adapters (resubmit-failed).

The ``cmd_resubmit`` adapter is the Tier 2 escape hatch for the
registry-driven dispatcher: its body contains hand-written validation
logic (the canonical seven-category gate, per-element ``int()`` cast
with slot-indexed error messages, and an eleven-kwarg fan-out to
``resubmit_flow``) that doesn't fit the standard CliShape hooks. The
``CliShape`` declaration on the ``@primitive`` decorator therefore
points ``handler`` at this function via a lazy lookup; the dispatcher
delegates entirely once it sees ``handler is not None``.

Helpers come from :mod:`hpc_agent.cli._helpers` (the adapter SDK) so
the module has no dependency on ``agent_cli`` — that import direction
would re-introduce the cycle the migration is unwinding.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from hpc_agent import errors

# Canonical failure-category vocabulary. Must be the UNION of:
#   - the auto-classifier in hpc_agent.runner.cluster_failures_by_fingerprint
#     (gpu_oom, system_oom, walltime, node_failure, import_error,
#      file_not_found, permission_denied, disk_full, python_traceback)
#   - the human-supplied taxonomy here (segv, queue_stall, code_bug, unknown)
# A test in tests/test_resubmit_batching.py asserts the classifier never emits a
# category outside this set.
# B2: derived from the canonical FailureCategory StrEnum.
# Pre-B2 this was a literal frozenset that drifted from the classifier
# emissions in hpc_agent.runner; A4 landed the union as a literal,
# B2 makes the literal redundant by sourcing from the StrEnum so the
# drift class cannot recur. test_lifecycle.py asserts the cross-set
# invariants (classifier emissions ⊆ accepted ⊆ FailureCategory).
from hpc_agent._internal.lifecycle import FailureCategory as _FailureCategory
from hpc_agent.cli._helpers import EXIT_OK, _load_spec, _ok

_VALID_RESUBMIT_CATEGORIES = frozenset({fc.value for fc in _FailureCategory})


def cmd_resubmit(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec, schema_name="resubmit")
    failed = spec.get("failed_task_ids")
    category = spec.get("category")
    if not isinstance(failed, list) or not failed:
        raise errors.SpecInvalid("--spec.failed_task_ids must be a non-empty list")
    if not isinstance(category, str):
        raise errors.SpecInvalid("--spec.category must be a string")
    # Belt-and-braces: schema validation also enforces this enum, but
    # ``_validate_against_schema`` is a no-op when ``jsonschema`` is not
    # installed.  Keep the local check so the seven-category contract
    # holds either way.
    if category not in _VALID_RESUBMIT_CATEGORIES:
        raise errors.SpecInvalid(
            f"--spec.category must be one of {sorted(_VALID_RESUBMIT_CATEGORIES)}; got {category!r}"
        )

    from hpc_agent.ops.recover.flow import resubmit_flow

    # Validate per-element so a bad index surfaces with the slot
    # information rather than a bare ``ValueError: invalid literal for
    # int()``.
    parsed_failed: list[int] = []
    for i, t in enumerate(failed):
        try:
            parsed_failed.append(int(t))
        except (TypeError, ValueError) as exc:
            raise errors.SpecInvalid(
                f"--spec.failed_task_ids[{i}]={t!r} is not an integer"
            ) from exc

    result = resubmit_flow(
        Path(args.experiment_dir),
        args.run_id,
        failed_task_ids=parsed_failed,
        category=category,
        overrides=spec.get("overrides"),
        new_job_ids=spec.get("new_job_ids"),
        request_id=spec.get("request_id"),
        submit_to_cluster=bool(spec.get("submit_to_cluster", False)),
        script=spec.get("script"),
        backend=spec.get("backend"),
        job_name=spec.get("job_name"),
        job_env=spec.get("job_env"),
    )
    _ok(
        result.to_envelope_data(),
        # Honest now that resubmit_failed dedups on request_id: a replay
        # with the same spec is a no-op, just like submit.
        name="resubmit-failed",
    )
    return EXIT_OK
