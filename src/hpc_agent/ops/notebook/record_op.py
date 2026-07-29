"""``notebook-record`` — the merged notebook-audit journaling verb.

One mutate verb, two kinds, dispatching on the spec's ``kind`` discriminator
to the two journaling seats that were previously standalone verbs:

* ``kind: "config"`` → :func:`hpc_agent.ops.notebook.record_config_op.notebook_record_config`
  — the STANDALONE audit's configuration seat (run #10, notebook-audit
  Amendment 2). Immutable-per-audit; refuses when the interview opt-in path
  already owns the config or when a config record already exists.
* ``kind: "receipt"`` → :func:`hpc_agent.ops.notebook.record_receipt_op.notebook_record_receipt`
  — the emitter's sha-bound render-receipt journaling (notebook-audit T10).
  Append-only; the parse IS the recompute, so a receipt binds only against
  the source as it currently sits on disk.

The implementations (and their design rationale) stay in their own modules;
this module owns only the registration and the dispatch. Both kinds append to
the same journal (``<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl``)
and share the same trust posture: the verb journals caller-side evidence, it
never actuates.

This file lives inside the ``notebook`` subject beside the two seats it
dispatches to, reaching only same-subject ``ops.notebook.*`` modules — the
subject-imports lint is satisfied by construction.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.notebook_record import NotebookRecordInput
from hpc_agent._wire.actions.notebook_record_config import (
    NotebookRecordConfigResult,
    NotebookRecordConfigSpec,
)
from hpc_agent._wire.actions.notebook_record_receipt import (
    NotebookRecordReceiptResult,
    NotebookRecordReceiptSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.record_config_op import notebook_record_config
from hpc_agent.ops.notebook.record_receipt_op import notebook_record_receipt

__all__ = ["notebook_record"]

_PRIMITIVE = "notebook-record"


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl"),
    ],
    error_codes=[errors.SpecInvalid],
    # Not idempotent under either kind: a config re-record is refused
    # (immutable-per-audit), a receipt re-record appends a fresh line
    # (append-only, newest-valid-wins on read) — honest, not retry-equivalent.
    idempotent=False,
    cli=CliShape(
        help=(
            "Journal one notebook-audit record; the spec's `kind` selects the "
            "seat. kind=config: record the audit configuration (input_roots / "
            "source_roots / attention_order / output_roots - opaque relpath "
            "lists) for a STANDALONE audit — without it the audit runs "
            "ROOTLESS-canonical (run #10). The canonical view reads the "
            "interview block FIRST when present (the opt-in path owns the "
            "config), else this journaled record; refuses when interview.json "
            "already carries audited_source for this audit_id or when a config "
            "record already exists (immutable-per-audit - supersede with a NEW "
            "audit_id); recording into an audit with existing journal entries "
            "succeeds with a LOUD warning (every view_sha moves, prior "
            "sign-offs read stale). kind=receipt: journal CODE render receipts "
            "for the sections of an audited source .py — parses the source ON "
            "DISK and binds each receipt to the freshly-parsed section sha (a "
            "receipt can only be recorded against current source and drifts "
            "stale when the section moves); unknown slugs are reported "
            "skipped; output_sha/error stay caller-attested (D9), weighed by "
            "the graduation consumers. Both kinds: pure local read + journal "
            "append, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookRecordInput,
        schema_ref=SchemaRef(input="notebook_record"),
    ),
    agent_facing=True,
)
def notebook_record(
    *, experiment_dir: Path, spec: NotebookRecordInput
) -> NotebookRecordConfigResult | NotebookRecordReceiptResult:
    """Dispatch on ``spec.kind`` to the config or receipt journaling seat.

    The discriminated entry is re-validated down to the seat's own spec type
    (dropping the wrapper's ``kind``) so each seat sees exactly the shape it
    was written against.
    """
    entry = spec.root
    payload = entry.model_dump(mode="json", exclude={"kind"})
    if entry.kind == "config":
        config_spec = NotebookRecordConfigSpec.model_validate(payload)
        return notebook_record_config(experiment_dir=experiment_dir, spec=config_spec)
    receipt_spec = NotebookRecordReceiptSpec.model_validate(payload)
    return notebook_record_receipt(experiment_dir=experiment_dir, spec=receipt_spec)
