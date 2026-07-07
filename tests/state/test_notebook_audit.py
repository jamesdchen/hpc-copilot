"""Tests for the per-section audit-state reduction (``state/notebook_audit.py``, T6).

Journal fixtures are written with the REAL writer (``append_decision`` for human
sign-offs; ``record_auto_clear`` for the code class) — never hand-forged JSONL.
Covers the full T6 vocabulary, supersession both directions, the stale-auto-clear
fall-through, the rollup verdict, the kernel route-through (inspect.getsource),
and malformed-record tolerance.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import Section, parse_percent_source
from hpc_agent.state.decision_journal import append_decision, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT = "demo-audit"

_SOURCE = """\
# %%
# hpc-audit-section: load-data
import pandas as pd
df = pd.read_csv("in.csv")

# %%
# hpc-audit-section: fit-model
model = fit(df)
"""

# Same module with the fit-model section edited (its sha moves; load-data's does not).
_SOURCE_EDITED = """\
# %%
# hpc-audit-section: load-data
import pandas as pd
df = pd.read_csv("in.csv")

# %%
# hpc-audit-section: fit-model
model = fit(df, regularize=True)
"""


def _section(source: str, slug: str) -> Section:
    parsed = parse_percent_source(source)
    return next(s for s in parsed.sections if s.slug == slug)


def _records(tmp_path: Path):
    return read_decisions(tmp_path, "notebook", _AUDIT)


def _sign_off(tmp_path: Path, slug: str, section_sha: str, view_sha: str = "view-1") -> None:
    """A human sign-off — the real append-decision record (block=notebook-sign-off)."""
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response="y",
        resolved={
            "audit_id": _AUDIT,
            "section": slug,
            "section_sha": section_sha,
            "view_sha": view_sha,
        },
    )


# ── unsigned ────────────────────────────────────────────────────────────────


def test_unsigned_on_empty_journal(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.UNSIGNED
    assert audit.attestor is None
    assert audit.signed_section_sha is None
    assert audit.current_section_sha == sec.section_sha


# ── signed_current ──────────────────────────────────────────────────────────


def test_signed_current(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    _sign_off(tmp_path, "load-data", sec.section_sha)
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.SIGNED_CURRENT
    assert audit.attestor == "human"
    assert audit.signed_section_sha == sec.section_sha
    assert audit.view_sha == "view-1"


# ── signed_stale (sign then edit → stale by recompute) ──────────────────────


def test_signed_stale_after_edit(tmp_path: Path) -> None:
    old = _section(_SOURCE, "fit-model")
    _sign_off(tmp_path, "fit-model", old.section_sha)
    new = _section(_SOURCE_EDITED, "fit-model")
    assert new.section_sha != old.section_sha  # the edit really moved the hash
    audit = nb.audit_section(_records(tmp_path), "fit-model", new.section_sha)
    assert audit.status == nb.SIGNED_STALE
    assert audit.attestor == "human"
    assert audit.signed_section_sha == old.section_sha  # attested the OLD sha
    assert audit.current_section_sha == new.section_sha


# ── auto_cleared (code record at current sha) ───────────────────────────────


def test_auto_cleared(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    rec = nb.record_auto_clear(
        tmp_path,
        audit_id=_AUDIT,
        section="load-data",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
        view_sha="view-x",
    )
    # A code record NEVER reads as a human ack.
    assert rec["response"] == nb.AUTO_CLEAR_RESPONSE == "auto_cleared"
    assert rec["response"] != "y"
    assert rec["resolved"]["attestor"] == "code"
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.AUTO_CLEARED
    assert audit.attestor == "code"
    assert audit.view_sha == "view-x"


def test_auto_clear_bind_refuses_mismatched_sha(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    with pytest.raises(errors.SpecInvalid):
        nb.record_auto_clear(
            tmp_path,
            audit_id=_AUDIT,
            section="load-data",
            section_sha=sec.section_sha,
            recompute="deadbeef" * 8,  # a different sha — bind must refuse
        )
    # Nothing was journaled (the lock fired before the append).
    assert _records(tmp_path) == []


# ── supersession, both directions (newest wins regardless of class) ─────────


def test_auto_clear_then_human_sign_supersedes_to_signed_current(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    nb.record_auto_clear(
        tmp_path,
        audit_id=_AUDIT,
        section="load-data",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
    )
    _sign_off(tmp_path, "load-data", sec.section_sha)  # newer human record wins
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.SIGNED_CURRENT
    assert audit.attestor == "human"


def test_human_sign_then_auto_clear_supersedes_to_auto_cleared(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    _sign_off(tmp_path, "load-data", sec.section_sha)
    nb.record_auto_clear(
        tmp_path,
        audit_id=_AUDIT,
        section="load-data",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
    )  # newer code record wins
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.AUTO_CLEARED
    assert audit.attestor == "code"


# ── stale auto-clear → NOT auto_cleared → unsigned (drift = unsigned) ────────


def test_stale_auto_clear_falls_through_to_unsigned(tmp_path: Path) -> None:
    old = _section(_SOURCE, "fit-model")
    nb.record_auto_clear(
        tmp_path,
        audit_id=_AUDIT,
        section="fit-model",
        section_sha=old.section_sha,
        recompute=old.section_sha,
    )
    new = _section(_SOURCE_EDITED, "fit-model")
    audit = nb.audit_section(_records(tmp_path), "fit-model", new.section_sha)
    # A stale CODE clearance is NOT signed_stale and NOT auto_cleared — unsigned.
    assert audit.status == nb.UNSIGNED
    assert audit.attestor == "code"  # identity still surfaced


# ── rollup pass / fail ──────────────────────────────────────────────────────


def test_rollup_passes_when_every_required_section_current(tmp_path: Path) -> None:
    source = parse_percent_source(_SOURCE)
    for sec in source.sections:
        _sign_off(tmp_path, sec.slug, sec.section_sha)
    rollup = nb.audit_module(
        tmp_path, _AUDIT, source=source, required_slugs=["load-data", "fit-model"]
    )
    assert rollup.passed is True
    assert {s.slug for s in rollup.sections} == {"load-data", "fit-model"}
    assert all(s.status == nb.SIGNED_CURRENT for s in rollup.sections)


def test_rollup_fails_when_one_section_unsigned(tmp_path: Path) -> None:
    source = parse_percent_source(_SOURCE)
    load = _section(_SOURCE, "load-data")
    _sign_off(tmp_path, "load-data", load.section_sha)  # only one of two
    rollup = nb.audit_module(
        tmp_path, _AUDIT, source=source, required_slugs=["load-data", "fit-model"]
    )
    assert rollup.passed is False
    by_slug = {s.slug: s for s in rollup.sections}
    assert by_slug["load-data"].status == nb.SIGNED_CURRENT
    assert by_slug["fit-model"].status == nb.UNSIGNED


def test_rollup_required_section_absent_from_source_is_unsigned(tmp_path: Path) -> None:
    source = parse_percent_source(_SOURCE)  # has load-data, fit-model
    rollup = nb.audit_module(
        tmp_path, _AUDIT, source=source, required_slugs=["load-data", "not-in-source"]
    )
    assert rollup.passed is False
    by_slug = {s.slug: s for s in rollup.sections}
    assert by_slug["not-in-source"].status == nb.UNSIGNED
    assert by_slug["not-in-source"].current_section_sha is None


def test_rollup_empty_required_passes_vacuously(tmp_path: Path) -> None:
    source = parse_percent_source(_SOURCE)
    rollup = nb.audit_module(tmp_path, _AUDIT, source=source, required_slugs=[])
    assert rollup.passed is True
    assert rollup.sections == ()


# ── malformed record skipped, not fatal ─────────────────────────────────────


def test_malformed_record_skipped_not_fatal(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    # A sign-off record missing section_sha → projects to a content_sha=None
    # attestation the kernel refuses; it must be skipped, not raise.
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response="y",
        resolved={"audit_id": _AUDIT, "section": "load-data"},  # no section_sha
    )
    _sign_off(tmp_path, "load-data", sec.section_sha)  # a valid record after it
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.SIGNED_CURRENT  # the malformed one did not strand it


def test_lone_malformed_record_reads_unsigned(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response="y",
        resolved={"audit_id": _AUDIT, "section": "load-data"},  # malformed
    )
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.UNSIGNED


def test_non_notebook_block_ignored(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    # A scope-lock-style record in a different-shaped journal must never be read
    # as a section attestation (different block).
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block="some-other-block",
        response="y",
        resolved={"section": "load-data", "section_sha": sec.section_sha},
    )
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.UNSIGNED


# ── kernel route-through (the enforcement-map "one kernel" row) ──────────────


def test_reduction_routes_drift_through_the_kernel() -> None:
    src = inspect.getsource(nb.audit_section)
    assert "attestation.reduce(" in src, (
        "audit_section must route the current/stale/absent drift verdict through "
        "the attestation kernel, never re-inline newest-first drift."
    )


def test_auto_clear_writer_routes_through_the_kernel_bind() -> None:
    src = inspect.getsource(nb.record_auto_clear)
    assert "attestation.bind(" in src, (
        "record_auto_clear must route the recompute-and-compare lock through "
        "attestation.bind, never re-inline it."
    )


def test_render_receipt_writer_routes_through_the_kernel_bind() -> None:
    src = inspect.getsource(nb.record_render_receipt)
    assert "attestation.bind(" in src, (
        "record_render_receipt must route the recompute-and-compare lock through "
        "attestation.bind, never re-inline it."
    )


def test_render_receipt_reader_routes_drift_through_the_kernel() -> None:
    src = inspect.getsource(nb.read_render_receipts)
    assert "attestation.reduce(" in src, (
        "read_render_receipts must route the current/stale freshness verdict "
        "through the attestation kernel, never re-inline the sha compare."
    )


# ── render receipts (T10): bind + journal + newest-valid + freshness ─────────


def test_render_receipt_binds_and_journals(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "fit-model")
    rec = nb.record_render_receipt(
        tmp_path,
        audit_id=_AUDIT,
        section="fit-model",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
        output_sha="out-1",
        error=False,
    )
    # A receipt is honest, mechanical evidence — never a human ack, never a clearance.
    assert rec["block"] == nb.RENDER_RECEIPT_BLOCK == "notebook-render-receipt"
    assert rec["response"] == nb.RENDER_RECEIPT_RESPONSE == "rendered"
    assert rec["response"] not in ("y", "auto_cleared")
    assert rec["resolved"]["attestor"] == "code"
    assert rec["resolved"]["output_sha"] == "out-1"
    assert rec["resolved"]["error"] is False


def test_render_receipt_bind_refuses_mismatched_sha(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "fit-model")
    with pytest.raises(errors.SpecInvalid):
        nb.record_render_receipt(
            tmp_path,
            audit_id=_AUDIT,
            section="fit-model",
            section_sha=sec.section_sha,
            recompute="deadbeef" * 8,  # a different sha — bind must refuse
            output_sha="out-1",
            error=False,
        )
    assert _records(tmp_path) == []  # the lock fired before the append


def test_read_render_receipts_fresh_when_sha_matches(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "fit-model")
    nb.record_render_receipt(
        tmp_path,
        audit_id=_AUDIT,
        section="fit-model",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
        output_sha="out-1",
        error=False,
    )
    got = nb.read_render_receipts(tmp_path, _AUDIT, current_shas={"fit-model": sec.section_sha})
    assert got["fit-model"]["fresh"] is True
    assert got["fit-model"]["error"] is False
    assert got["fit-model"]["output_sha"] == "out-1"
    assert got["fit-model"]["section_sha"] == sec.section_sha


def test_read_render_receipts_stale_after_edit(tmp_path: Path) -> None:
    old = _section(_SOURCE, "fit-model")
    nb.record_render_receipt(
        tmp_path,
        audit_id=_AUDIT,
        section="fit-model",
        section_sha=old.section_sha,
        recompute=old.section_sha,
        output_sha="out-1",
        error=False,
    )
    new = _section(_SOURCE_EDITED, "fit-model")
    assert new.section_sha != old.section_sha
    got = nb.read_render_receipts(tmp_path, _AUDIT, current_shas={"fit-model": new.section_sha})
    # The receipt was bound at the OLD sha → stale (greens nothing downstream).
    assert got["fit-model"]["fresh"] is False
    assert got["fit-model"]["section_sha"] == old.section_sha


def test_read_render_receipts_newest_valid_wins(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "fit-model")
    for out in ("out-1", "out-2"):
        nb.record_render_receipt(
            tmp_path,
            audit_id=_AUDIT,
            section="fit-model",
            section_sha=sec.section_sha,
            recompute=sec.section_sha,
            output_sha=out,
            error=False,
        )
    got = nb.read_render_receipts(tmp_path, _AUDIT, current_shas={"fit-model": sec.section_sha})
    assert got["fit-model"]["output_sha"] == "out-2"  # newest wins


def test_receipt_does_not_change_audit_section_status(tmp_path: Path) -> None:
    # A render receipt rides the SAME journal but must never be read as a section
    # attestation — it is not a sign-off or an auto-clear, so audit_section stays
    # UNSIGNED for a section that only has a receipt.
    sec = _section(_SOURCE, "fit-model")
    nb.record_render_receipt(
        tmp_path,
        audit_id=_AUDIT,
        section="fit-model",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
        output_sha="out-1",
        error=False,
    )
    audit = nb.audit_section(_records(tmp_path), "fit-model", sec.section_sha)
    assert audit.status == nb.UNSIGNED


def test_read_render_receipts_absent_section_is_not_fresh(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "fit-model")
    nb.record_render_receipt(
        tmp_path,
        audit_id=_AUDIT,
        section="fit-model",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
        output_sha="out-1",
        error=False,
    )
    # A section absent from current_shas cannot be fresh (nothing to compare).
    got = nb.read_render_receipts(tmp_path, _AUDIT, current_shas={})
    assert got["fit-model"]["fresh"] is False
