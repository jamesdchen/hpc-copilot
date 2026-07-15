"""Pydantic models for the ``program-init`` mutate verb (program-init P1a).

Wire surface over :mod:`hpc_agent.ops.pack.init_op` — the seat that MATERIALIZES
the PROGRAM layer of the three-tier pack architecture
(``docs/design/program-init.md``). At program creation, code CONSUMES a domain
pack's skeleton to generate a program template, stamps ``derived_from`` (the
lineage stamp) MECHANICALLY, seals the program manifest via the generic pack
re-seal, and binds the packs. One command = a working program layer.

Two modes:

* **create** — generate ``packs/<program>/`` fresh from a domain skeleton
  (verbatim copy + a code-authored provenance header), stamp lineage, seal, bind.
* **adopt** — migrate an EXISTING program pack (e.g. a lab's signed ``rv_audit.py``)
  onto its real lineage WITHOUT byte-changing any content file: stamp the recipe,
  reseal the manifest, rebind only the pack whose bytes moved.

Every field is mechanism identity — a program/pack slug, a manifest relpath, a
seam name, an opaque version echo, a 64-hex sha, an opaque caller-authored check
command core echoes and never interprets (DP2/DP4).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class ProgramInitSpec(BaseModel):
    """Inputs to ``program-init`` — generate or adopt a program pack layer."""

    model_config = ConfigDict(extra="forbid", title="program-init input spec")

    program: RunIdStrict = Field(
        description="The program pack slug to create/adopt (keys packs/<program>/)."
    )
    domain_manifest: str = Field(
        min_length=1,
        description=(
            "Experiment-dir-relative path to the DOMAIN pack manifest whose "
            "audit_template seam is consumed as the lineage source (e.g. "
            "packs/quant/manifest.json). Read on disk; the seam file's raw-bytes "
            "sha is recorded as derived_from.sha (never caller-suppliable)."
        ),
    )
    mode: Literal["create", "adopt"] = Field(
        default="create",
        description=(
            "'create' generates packs/<program>/ fresh from the domain skeleton; "
            "'adopt' migrates an existing program pack onto its lineage without "
            "byte-changing any content file (the signed-template migration path)."
        ),
    )
    template_relpath: str | None = Field(
        default=None,
        description=(
            "Experiment-dir-relative path for the program template file (create "
            "mode). Defaults to packs/<program>/templates/<program>_audit.py."
        ),
    )
    check: str | None = Field(
        default=None,
        description=(
            "Optional caller-authored check command core runs once after sealing "
            "(opaque — shlex-split, run shell=False in the experiment dir, never "
            "interpreted). Absent → the domain pack's receipt slots are reported "
            "as to-earn instead."
        ),
    )
    bind: bool = Field(
        default=True,
        description="Bind the packs after sealing (journaling the bind). Rarely disabled.",
    )


class DerivedFromEcho(BaseModel):
    """The stamped lineage — which domain seam the program was consumed from.

    Mechanism identity + freshness evidence only: ``pack``/``seam`` identify the
    derivation edge, ``version`` is an opaque echo, ``sha`` the raw-bytes sha of
    the consumed seam file (never compared for edge identity).
    """

    model_config = ConfigDict(extra="forbid", title="program-init derived-from echo")

    pack: str
    seam: str
    version: str
    sha: str


class ReceiptBindingEcho(BaseModel):
    """One receipt-binding suggestion for the interview to persist (slot + pack)."""

    model_config = ConfigDict(extra="forbid", title="program-init receipt binding echo")

    slot: str
    pack: str


class PackOptInEcho(BaseModel):
    """One opt-in entry the interview should persist under interview.json ``packs``.

    ``program-init`` does NOT write interview.json (the interview primitive is the
    one writer — DC9); it ECHOES the exact opt-in block so the on-ramp persists it.
    """

    model_config = ConfigDict(extra="forbid", title="program-init pack opt-in echo")

    pack: str
    manifest: str
    receipt_bindings: list[ReceiptBindingEcho] = Field(default_factory=list)


class ProgramBind(BaseModel):
    """One pack bound/rebound by ``program-init`` — its new manifest sha."""

    model_config = ConfigDict(extra="forbid", title="program-init bind")

    pack: str
    manifest_sha: str
    rebound: bool = Field(
        description="True when a prior bind existed and the manifest sha moved (re-bind = drift)."
    )


class ProgramSlotToEarn(BaseModel):
    """A domain receipt slot to earn after init, with its opaque caller check.

    ``check`` is the caller-authored command the driving skill runs to earn the
    receipt (from the interview receipt_bindings entry, when present); ``None``
    when the caller recorded none (the generic pack-record-receipt guidance
    applies). Core echoes it, never runs it here (DP2).
    """

    model_config = ConfigDict(extra="forbid", title="program-init slot to earn")

    slot: str
    pack: str
    check: str | None = None


class ProgramInitResult(BaseModel):
    """Echo of a ``program-init`` pass — what was written, stamped, and bound.

    ``derived_from`` is the mechanically-stamped lineage; ``created_files`` the
    relpaths written (empty for adopt's content files — adopt never byte-changes a
    template); ``binds`` the packs bound/rebound with their new manifest shas;
    ``check_ran``/``check_ok`` the caller-check outcome (a failing check is
    REPORTED, never raised); ``slots_to_earn`` the domain receipts still to earn
    when no check resolved; ``packs_optin`` the opt-in block for the interview to
    persist (init never writes interview.json — DC9).
    """

    model_config = ConfigDict(extra="forbid", title="program-init output data")

    program: str
    mode: Literal["create", "adopt"]
    derived_from: DerivedFromEcho
    created_files: list[str] = Field(default_factory=list)
    binds: list[ProgramBind] = Field(default_factory=list)
    check_ran: bool = False
    check_ok: bool | None = None
    slots_to_earn: list[ProgramSlotToEarn] = Field(default_factory=list)
    packs_optin: list[PackOptInEcho] = Field(default_factory=list)
    note: str | None = Field(
        default=None,
        description="Honest mechanical note (e.g. pack-status stays empty until the interview persists packs_optin).",
    )
