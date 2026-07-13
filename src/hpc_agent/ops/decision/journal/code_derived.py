"""The code-derived-field gate (run #6 F1) — refuse a hand-committed derived field."""

from __future__ import annotations

from typing import Any

from hpc_agent import errors


def _assert_no_code_derived_fields(resolved: dict[str, Any] | None) -> None:
    """Refuse a ``resolved`` dict that hand-commits a CODE-DERIVED field.

    Run #6 finding F1: the driving agent hand-authored the sidecar's
    ``executor`` as the bare extension-less token ``monte_carlo_pi``; the
    dispatcher shelled it verbatim and exited 127 (canary_failed). The
    ``revise-resolved`` patch surface already refuses derived fields, but the
    journal's ``resolved`` was still an authorable side door — a greenlight
    committing ``executor``/``job_env``/… laundered a hand-authored derived
    value into the approved spec the driver then carries (§4 carry_fields).

    The refusal set is
    :data:`~hpc_agent.ops.submit.field_partition.JOURNAL_UNAUTHORABLE_FIELDS`
    (bound through the ``field_ownership`` facade, never copied) — the
    code-derived partition MINUS the three names a committed ``resolved``
    legitimately carries (``run_id``: a status/aggregate INPUT field;
    ``cmd_sha``: the §4 identity fast-path token ``block_drive`` reads;
    ``total_tasks``: count echoes are cross-checked against ``tasks.total()``
    downstream, finding 21). Scoping by audit keeps the guard fireable
    without breaking any green path (engineering-principles).

    Applies to EVERY append (any scope, any response): a derived value has no
    business in the journal regardless of how it got there. Raises
    :class:`errors.SpecInvalid` naming the field(s) and the sanctioned rail
    (``revise-resolved`` with the INPUT field to patch instead).
    """
    if not isinstance(resolved, dict) or not resolved:
        return
    # Bind (never copy) the partition through the top-level facade — the
    # direct ``hpc_agent.ops.submit.field_partition`` spelling trips the
    # subject-import lint from inside the ``decision`` subject.
    from hpc_agent.ops import field_ownership as _field_ownership

    offending = sorted(k for k in resolved if k in _field_ownership.JOURNAL_UNAUTHORABLE_FIELDS)
    if offending:
        raise errors.SpecInvalid(
            f"append-decision: resolved field(s) {offending} are CODE-DERIVED — "
            "the framework recomputes them from the input delta (executor from "
            "the interview's materialized entry, job_env/modules/conda_* from "
            "the cluster's clusters.yaml entry, ssh_target/backend/remote_path "
            "from the cluster). Hand-committing one is the run-#6 F1 bug (a "
            "hand-authored bare `executor` shelled verbatim → exit 127). Do not "
            "journal the derived value: name the INPUT field that should change "
            "via `hpc-agent revise-resolved` (e.g. to change the executor, "
            "patch `entry_point`; to change activation, patch `cluster`) and "
            "commit THAT delta instead."
        )
