"""Pydantic models for the ``pack-refresh`` mutate verb (domain-packs auto-remedy).

Wire surface over :mod:`hpc_agent.ops.pack.refresh_op` — the mechanical remedy the
2026-07-10 ruling authorises ("the pack gate MAY auto-remedy; latency is to be
OBLITERATED", ``docs/design/domain-packs.md`` drift log). Given an experiment dir,
``pack-refresh`` detects which BOUND packs' manifests are STALE against on-disk
bytes (the MINIMAL set — editing one pack's content never forces another's
rebuild), re-seals each stale manifest GENERICALLY from its declarative
``sweep.json`` recipe (pure hashing; DP2 holds — no pack code runs), re-binds it
through the existing ``pack-bind`` path (journaling old→new shas — the drift event
IS the archive record), and REPORTS which caller-authored receipt slots must be
re-earned plus each one's caller-side check command (core never runs the check).

Every field is mechanism identity — a pack slug, a sha, a relpath, an opaque
caller-authored command string core echoes and never interprets.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class PackRefreshSpec(BaseModel):
    """Inputs to ``pack-refresh`` — one named pack, or every opted-in pack."""

    model_config = ConfigDict(extra="forbid", title="pack-refresh input spec")

    pack: RunIdStrict | None = Field(
        default=None,
        description="Pack slug to refresh; omit to refresh every opted-in pack.",
    )


class PackSlotToReearn(BaseModel):
    """One receipt slot that must be re-earned after a refresh, with its remedy.

    ``status`` is the post-refresh slot reduction (``stale`` / ``missing`` /
    ``failed`` — a re-bind moves the manifest sha so a covered receipt reads
    ``stale`` by construction). ``check`` is the caller-authored, opaque command
    the driving skill runs to re-emit the receipt (from the interview
    ``receipt_bindings`` entry); ``None`` when the caller recorded no check command
    (the generic ``pack-record-receipt`` guidance applies).
    """

    model_config = ConfigDict(extra="forbid", title="pack slot to re-earn")

    slot: str
    pack: str
    status: str
    check: str | None = None


class PackRefreshEntry(BaseModel):
    """The refresh outcome for one pack — what moved and what must be re-earned.

    ``recipe_found`` is False when no ``sweep.json`` sits beside the manifest (core
    cannot generically re-seal it; the manifest is left untouched). ``stale`` /
    ``rebound`` record whether the manifest was semantically stale and re-bound.
    The file deltas name exactly which sealed files moved (the drift the archive
    records); ``slots_to_reearn`` lists every caller-authored obligation now
    un-cleared plus its check command.
    """

    model_config = ConfigDict(extra="forbid", title="pack refresh entry")

    pack: str
    recipe_found: bool
    stale: bool
    rebound: bool
    old_manifest_sha: str | None = None
    new_manifest_sha: str | None = None
    added_files: list[str] = Field(default_factory=list)
    removed_files: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    slots_to_reearn: list[PackSlotToReearn] = Field(default_factory=list)
    note: str | None = Field(
        default=None,
        description="Honest mechanical note (e.g. no recipe found, dangling reference).",
    )


class PackRefreshResult(BaseModel):
    """Echo of a ``pack-refresh`` pass — one entry per reported pack.

    ``any_rebound`` is True when at least one manifest was re-sealed + re-bound
    (the journal now carries the old→new drift event). ``refreshed`` is keyed by
    pack slug (the ``pack-status`` precedent)."""

    model_config = ConfigDict(extra="forbid", title="pack-refresh output data")

    any_rebound: bool = False
    refreshed: dict[str, PackRefreshEntry] = Field(default_factory=dict)
