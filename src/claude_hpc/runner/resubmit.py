"""Resubmit runner primitive."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal.primitive import SideEffect, primitive
from claude_hpc._schema_models.actions.resubmit import ResubmitSpec

if TYPE_CHECKING:
    from pathlib import Path

    from claude_hpc._internal.session import RunRecord


def derive_resubmit_request_id(
    *,
    failed_task_ids: list[int],
    category: str,
    overrides: dict[str, Any] | None,
) -> str:
    """Compute a deterministic dedupe key from the resubmit spec.

    Same input → same id, regardless of dict-key order in *overrides*.
    First 12 hex chars of sha256, prefixed with ``rs_`` for readability.
    """
    import hashlib

    payload = json.dumps(
        {
            "failed_task_ids": sorted(int(t) for t in failed_task_ids),
            "category": category,
            "overrides": overrides or {},
        },
        sort_keys=True,
    )
    return "rs_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@primitive(
    name="resubmit-failed",
    verb="mutate",
    side_effects=[
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (per-task retry counters)",
        ),
    ],
    error_codes=[errors.SpecInvalid, errors.JournalCorrupt],
    idempotent=True,
    idempotency_key="request_id",
    cli="hpc-agent resubmit --run-id <id> --spec spec.json [--experiment-dir <dir>]",
    agent_facing=True,
)
def resubmit_failed(
    experiment_dir: Path,
    run_id: str,
    *,
    spec: ResubmitSpec,
) -> tuple[RunRecord, bool, str]:
    """Record a resubmission attempt in the journal.

    The actual resubmit (writing a fresh sidecar + backend submission)
    is the caller's responsibility — this helper only updates per-task
    retry counters and (optionally) the active job_ids list. Pass
    ``new_job_ids`` after the backend reports them so the journal stays
    in sync for the next monitor session.

    Idempotent on ``request_id``. When the caller does not supply one,
    a deterministic id is derived from the spec via
    :func:`derive_resubmit_request_id`. A second call with the same
    ``request_id`` (whether explicit or derived) returns
    ``(record, deduped=True, request_id)`` without incrementing
    per-task retry counters.

    Returns ``(record, deduped, request_id)``.
    """
    failed_task_ids = list(spec.failed_task_ids)
    category = str(spec.category)
    overrides = dict(spec.overrides) if spec.overrides is not None else None
    new_job_ids = list(spec.new_job_ids) if spec.new_job_ids is not None else None
    request_id = spec.request_id

    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}")

    rid = request_id or derive_resubmit_request_id(
        failed_task_ids=failed_task_ids,
        category=category,
        overrides=overrides,
    )
    if record.last_resubmit_request_id and record.last_resubmit_request_id == rid:
        # Deduped: replay of the same resubmit. Don't increment counters.
        return record, True, rid

    retries = dict(record.retries)
    overrides = dict(overrides or {})
    for tid in failed_task_ids:
        key = str(tid)
        prior = retries.get(key, {})
        retries[key] = {
            "attempts": int(prior.get("attempts", 0)) + 1,
            "category": category,
            "overrides": overrides,
        }
    fields: dict[str, Any] = {
        "retries": retries,
        "last_resubmit_request_id": rid,
    }
    if new_job_ids is not None:
        fields["job_ids"] = list(new_job_ids)
    updated = session.update_run_status(experiment_dir, run_id, **fields)
    return updated, False, rid
