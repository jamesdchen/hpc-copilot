"""Behaviour-pinning mutation coverage for :mod:`hpc_agent.state.notebook_audit`.

Companion to ``tests/state/test_notebook_audit.py`` (which covers the T6
vocabulary end-to-end) and mirroring the landed ``test_journal_coverage.py``
style: each test below adds an assertion that KILLS a specific surviving mutant
in the notebook-audit TRUST reduction — the machinery that decides whether a
section is cleared for graduation. A silent bug here lets unaudited source
through the gate (or falsely blocks audited source), so the reduction's exact
membership sets, its block→attestor map, and its recompute-drift branches are
pinned here rather than left covered-but-UNASSERTED.

Journal fixtures use the REAL writers (``append_decision`` for human sign-offs,
``record_auto_clear`` for the code class) — never hand-forged JSONL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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


# ── the graduation-pass membership sets (the trust threshold) ─────────────────
# The gate passes a section iff its status is in PASSING_STATUSES. A mutant that
# WIDENS this set (adds SIGNED_STALE or UNSIGNED) would let a drifted/unsigned
# section graduate — the exact silent-bug class the task names. Pinned as an
# explicit membership assertion (the test_journal_coverage _RESUBMITTABLE_* idiom).


def test_passing_statuses_is_exactly_the_two_current_states() -> None:
    """PASSING = {signed_current, auto_cleared} — the two 'current at this hash'
    states. SIGNED_STALE and UNSIGNED must NOT be members: a stale sign-off or an
    unsigned section may never pass graduation. Kills a widened-set mutant."""
    expected = {nb.SIGNED_CURRENT, nb.AUTO_CLEARED}
    assert expected == set(nb.PASSING_STATUSES)
    assert nb.SIGNED_STALE not in nb.PASSING_STATUSES
    assert nb.UNSIGNED not in nb.PASSING_STATUSES


def test_section_statuses_is_the_full_vocabulary_superset_of_passing() -> None:
    """The four-status vocabulary is complete, and PASSING is a strict subset —
    exactly two of the four statuses clear the gate."""
    expected = {nb.SIGNED_CURRENT, nb.AUTO_CLEARED, nb.SIGNED_STALE, nb.UNSIGNED}
    assert expected == set(nb.SECTION_STATUSES)
    assert nb.PASSING_STATUSES < nb.SECTION_STATUSES
    assert len(nb.PASSING_STATUSES) == 2


# ── the block→attestor map (only sign-off / auto-clear green a section) ───────
# _BLOCK_ATTESTOR is the ONE gate that decides which journal records enter the
# section reduction at all. A receipt / draft / config block must NEVER be an
# attestor block — else a code render-receipt (evidence, not a clearance) or a
# draft (authorship, not a review) could green a section. Pinned as a membership
# invariant so a mutant that registers one of those blocks is killed.


def test_block_attestor_maps_only_signoff_and_autoclear() -> None:
    assert nb._BLOCK_ATTESTOR == {nb.SIGN_OFF_BLOCK: "human", nb.AUTO_CLEAR_BLOCK: "code"}
    # The non-attestation blocks that ride the SAME journal must stay OUT — none
    # may enter the sign-off reduction and green a section.
    for foreign in (
        nb.RENDER_RECEIPT_BLOCK,
        nb.DRAFT_BLOCK,
        nb.AUDIT_CONFIG_BLOCK,
        nb.RELAY_DUE_BLOCK,
        nb.ECHO_PROVENANCE_BLOCK,
    ):
        assert foreign not in nb._BLOCK_ATTESTOR


# ── audit_section: a signed section DROPPED from the current source ───────────
# The `current_section_sha is None or newest is None` early-return guard: when a
# section was signed but is now ABSENT from the source (current_sha=None) it must
# read UNSIGNED — but still SURFACE the newest record's identity (the docstring's
# "a signed section missing from the current source" case). Kills an `or`→`and`
# mutation of the guard (which would route a None current-sha into the kernel
# reduce) and the identity-surfacing conditionals in the early-return branch.


def test_signed_section_absent_from_source_is_unsigned_but_surfaces_identity(
    tmp_path: Path,
) -> None:
    sec = _section(_SOURCE, "fit-model")
    _sign_off(tmp_path, "fit-model", sec.section_sha, view_sha="view-fit")
    # The section is now gone from the source → current_section_sha is None.
    audit = nb.audit_section(_records(tmp_path), "fit-model", None)
    assert audit.status == nb.UNSIGNED
    assert audit.current_section_sha is None
    # The prior sign-off's identity is still surfaced (not None-erased).
    assert audit.signed_section_sha == sec.section_sha
    assert audit.view_sha == "view-fit"
    assert audit.attestor == "human"


# ── recompute-drift consumer: a stale sign-off does NOT pass the module gate ──
# The recompute-bind lock at the notebook-audit consumer: audit_module routes the
# per-section drift verdict through the kernel, and a section signed-then-edited
# reads SIGNED_STALE (not passing). Ties the drift verdict to ModuleAudit.passed —
# a mutant that maps STALE-human to a passing status, or that computes `passed`
# with `any` instead of `all`, is killed.


def test_stale_human_signoff_fails_the_module_rollup(tmp_path: Path) -> None:
    source_old = parse_percent_source(_SOURCE)
    for sec in source_old.sections:
        _sign_off(tmp_path, sec.slug, sec.section_sha)

    # Edit ONLY fit-model — its hash moves; load-data stays current.
    source_new = parse_percent_source(_SOURCE_EDITED)
    rollup = nb.audit_module(
        tmp_path, _AUDIT, source=source_new, required_slugs=["load-data", "fit-model"]
    )
    by_slug = {s.slug: s for s in rollup.sections}
    assert by_slug["load-data"].status == nb.SIGNED_CURRENT
    assert by_slug["fit-model"].status == nb.SIGNED_STALE  # drift = stale, not current
    assert rollup.passed is False  # one stale section sinks the whole rollup


# ── record_auto_clear: view_sha threaded only when supplied ───────────────────
# The `if view_sha:` guard in the writer: an auto-clear with no view_sha must
# journal NO view_sha key (and read back view_sha=None), byte-compatible with a
# pre-view record. Kills a mutant that always stamps the key.


def test_auto_clear_omits_view_sha_when_not_supplied(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    rec = nb.record_auto_clear(
        tmp_path,
        audit_id=_AUDIT,
        section="load-data",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
        # no view_sha
    )
    assert "view_sha" not in rec["resolved"]
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.status == nb.AUTO_CLEARED
    assert audit.view_sha is None


def test_auto_clear_threads_view_sha_when_supplied(tmp_path: Path) -> None:
    sec = _section(_SOURCE, "load-data")
    rec = nb.record_auto_clear(
        tmp_path,
        audit_id=_AUDIT,
        section="load-data",
        section_sha=sec.section_sha,
        recompute=sec.section_sha,
        view_sha="view-ac",
    )
    assert rec["resolved"]["view_sha"] == "view-ac"
    audit = nb.audit_section(_records(tmp_path), "load-data", sec.section_sha)
    assert audit.view_sha == "view-ac"
