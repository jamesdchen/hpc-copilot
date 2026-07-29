"""Wire models for the merged ``notebook-record`` mutate verb.

One verb, two kinds — the two notebook-audit journaling seats that were
previously the standalone verbs ``notebook-record-config`` and
``notebook-record-receipt``:

* ``kind: "config"`` — the standalone audit's configuration seat (run #10,
  ``docs/design/notebook-audit.md`` Amendment 2), immutable-per-audit;
* ``kind: "receipt"`` — the emitter's sha-bound render-receipt journaling
  (notebook-audit T10), append-only.

The per-kind spec + result shapes stay authored in their own wire modules
(:mod:`hpc_agent._wire.actions.notebook_record_config` /
:mod:`hpc_agent._wire.actions.notebook_record_receipt` — their docstrings
carry the design rationale); this module only wraps them into the merged
verb's ``notebook_record.input.json`` / ``notebook_record.output.json``
union. The wrapped classes are listed in ``scripts/build_schemas.py``'s
embedded-sub-model skip set so they no longer emit standalone per-verb
schema files.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, RootModel

from hpc_agent._wire.actions.notebook_record_config import (
    NotebookRecordConfigResult,
    NotebookRecordConfigSpec,
)
from hpc_agent._wire.actions.notebook_record_receipt import (
    NotebookRecordReceiptResult,
    NotebookRecordReceiptSpec,
)


class NotebookRecordConfigEntry(NotebookRecordConfigSpec):
    """The config kind's spec: :class:`NotebookRecordConfigSpec` plus the discriminator."""

    kind: Literal["config"]


class NotebookRecordReceiptEntry(NotebookRecordReceiptSpec):
    """The receipt kind's spec: :class:`NotebookRecordReceiptSpec` plus the discriminator."""

    kind: Literal["receipt"]


class NotebookRecordInput(
    RootModel[
        Annotated[
            NotebookRecordConfigEntry | NotebookRecordReceiptEntry,
            Field(discriminator="kind"),
        ]
    ]
):
    """The merged verb's ``--spec``: a kind-discriminated journaling request.

    Emitted as ``notebook_record.input.json``.
    """


class NotebookRecordResult(RootModel[NotebookRecordConfigResult | NotebookRecordReceiptResult]):
    """The merged verb's output union — one shape per kind.

    Emitted as ``notebook_record.output.json``.
    """
