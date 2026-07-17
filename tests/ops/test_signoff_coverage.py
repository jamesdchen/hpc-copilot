"""Behavior-pinning mutation battery for the notebook SIGN-OFF gate.

Target: ``src/hpc_agent/ops/decision/journal/signoff.py`` —
:func:`_assert_signoff_authorship` and its helpers. This is trust-core adjacent:
a silent flip here lets an UNaudited section be signed into the journal (a fake
attestation lands) or falsely blocks a real human sign-off.

The sibling ``tests/ops/notebook/test_notebook_gates_coverage.py`` already pins
the bare-ack floor, the slug-naming floor, the HUMAN_REQUIRED diff-token bar, the
trusted-display render lock (missing / stale), and the ``_tier`` threshold; and
``tests/ops/decision/test_multi_human_gate.py`` pins the MH6 reviewer≠author legs.
This file pins the seams those two do NOT cover, consequence-ranked:

* the block/scope CONVENTION (both directions) + the resolved-shape floor;
* Lock 2 — the un-fakeable ``section_sha`` recompute (a hash asserted into
  existence is refused);
* the one-shot unresolvable source+template refusal;
* the section-not-in-source refusal;
* the AUTO_CLEARED redundant-marking accept path (the raised bar WAIVED);
* the finding-10 TEMPORAL filter over the harness utterance log (a sign-off
  utterance older than the render the human saw is not attestation, and the
  agent-relayed ``response`` carries no authorship weight when a log is present).

Each assertion's docstring / comment names the mutant it kills. TOY vocabulary
only (widget lineage), never a real domain's words.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.ops.notebook.audit_view import AUTO_CLEARED, HUMAN_REQUIRED
from hpc_agent.ops.notebook.canonical import build_canonical_view, read_recorded_config
from hpc_agent.ops.notebook.render_store import render_path, write_render
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.ops.notebook.audit_view import SectionView

_AUDIT = "widget-audit"
_SECTION = "model-fit"
_MARKER = {"authorship_evidence": "missing"}

# Three fixed instants (seconds resolution) so the render-mtime temporal filter
# is exercised deterministically instead of racing a real ``now`` stamp.
_BEFORE = "2020-01-01T00:00:00+00:00"
_ANCHOR = "2020-06-01T00:00:00+00:00"
_AFTER = "2021-01-01T00:00:00+00:00"

# A one-section template; the source ADDS a `regularization` kwarg → a nonempty
# diff-from-template → HUMAN_REQUIRED, with `regularization` as the engageable
# diff identifier (the same shape the gates-coverage suite uses).
_TEMPLATE = """# %%
# hpc-audit-section: model-fit
model = fit(data)
"""


def _source(reg: str = "0.5") -> str:
    return f"""# %%
# hpc-audit-section: model-fit
model = fit(data, regularization={reg})
"""


# An INHERITED, assertion-free, flag-free section: source byte-identical to
# template → INHERITED ∧ 0 flags ∧ green (nothing to prove) → AUTO_CLEARED.
_CLEAR_MODULE = """# %%
# hpc-audit-section: prelude
x = 1
y = 2
"""
_CLEAR_SECTION = "prelude"


def _write_notebook(exp: Path, *, source_text: str, template_text: str = _TEMPLATE) -> None:
    (exp / "source.py").write_text(source_text, encoding="utf-8")
    (exp / "template.py").write_text(template_text, encoding="utf-8")
    (exp / "interview.json").write_text(
        json.dumps(
            {
                "goal": "fit the model",
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": _AUDIT,
                },
            }
        ),
        encoding="utf-8",
    )


def _canonical_section(exp: Path, section: str = _SECTION) -> SectionView:
    cfg = read_recorded_config(exp, _AUDIT)
    view = build_canonical_view(
        exp,
        audit_id=_AUDIT,
        source_relpath="source.py",
        template_relpath="template.py",
        cfg=cfg,
    )
    return next(v for v in view.sections if v.slug == section)


def _signoff(
    exp: Path,
    response: str,
    *,
    section: str = _SECTION,
    section_sha: str,
    view_sha: str,
    audit_id: str = _AUDIT,
    scope_kind: str = "notebook",
    block: str = "notebook-sign-off",
) -> Any:
    return append_decision(
        experiment_dir=exp,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": scope_kind,
                "scope_id": audit_id,
                "block": block,
                "response": response,
                "resolved": {
                    "audit_id": audit_id,
                    "section": section,
                    "section_sha": section_sha,
                    "view_sha": view_sha,
                },
            }
        ),
    )


def _marker_of(exc: BaseException) -> Any:
    return getattr(exc, "failure_features", None)


def _log_utterance_at(exp: Path, text: str, ts: str) -> None:
    """Seed one harness utterance with a CONTROLLED ts (the frozen 3-field shape)."""
    import hashlib

    from hpc_agent.state.run_record import journal_dir
    from hpc_agent.state.utterances import utterances_path

    journal_dir(exp)  # the namespace a real state write would have created
    path = utterances_path(exp)
    rec = {"ts": ts, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _set_render_mtime(exp: Path, sv: SectionView, ts: str) -> None:
    """Pin the render file's mtime — the finding-10 temporal anchor."""
    from hpc_agent.infra.time import parse_iso_utc

    path = render_path(exp, audit_id=_AUDIT, section=sv.slug, view_sha=sv.view_sha)
    epoch = parse_iso_utc(ts).timestamp()
    os.utime(path, (epoch, epoch))


# ── block / scope convention (both directions) ────────────────────────────────


def test_signoff_block_refused_for_non_notebook_scope(tmp_path: Path) -> None:
    """The ``notebook-sign-off`` block is notebook-scope-ONLY. A non-notebook
    scope_kind under this block is refused BEFORE any recompute.

    kills: dropping the ``is_signoff_block and scope_kind != 'notebook'`` guard
    (letting a sign-off be laundered under a run/scope journal)."""
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "scope",
            "scope_id": "calib",
            "block": "notebook-sign-off",
            "response": "y",
            "resolved": {"audit_id": _AUDIT, "section": _SECTION},
        }
    )
    with pytest.raises(errors.SpecInvalid, match="scope_kind='notebook'"):
        append_decision(experiment_dir=tmp_path, spec=spec)


def test_non_signoff_record_not_gated_by_signoff(tmp_path: Path) -> None:
    """A record that is NOT a notebook sign-off passes the sign-off gate untouched
    (the gate returns early). A campaign greenlight commits with no sign-off
    machinery firing.

    kills: a mutation that makes the sign-off gate fire for every record (e.g.
    dropping the early ``return`` for non-sign-off records)."""
    out = append_decision(
        experiment_dir=tmp_path,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": "campaign",
                "scope_id": "widget-camp",
                "block": "campaign-greenlight",
                "response": "y",
                "resolved": {},
            }
        ),
    )
    assert out.count == 1


# ── resolved-shape floor (each field required; view_sha specifically) ─────────


@pytest.mark.parametrize("drop", ["audit_id", "section", "section_sha", "view_sha"])
def test_signoff_missing_required_field_refused(tmp_path: Path, drop: str) -> None:
    """EACH of {audit_id, section, section_sha, view_sha} is required; dropping any
    one refuses and NAMES the missing field. view_sha binds what-the-human-saw and
    is explicitly in the required set.

    kills: removing any name from the required tuple (in particular removing
    ``view_sha`` — a mutant that would let a sign-off omit what the human saw)."""
    resolved: dict[str, Any] = {
        "audit_id": _AUDIT,
        "section": _SECTION,
        "section_sha": "a" * 64,
        "view_sha": "b" * 64,
    }
    del resolved[drop]
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "notebook",
            "scope_id": _AUDIT,
            "block": "notebook-sign-off",
            "response": "model-fit reviewed",
            "resolved": resolved,
        }
    )
    with pytest.raises(errors.SpecInvalid) as ei:
        append_decision(experiment_dir=tmp_path, spec=spec)
    assert drop in str(ei.value)


def test_signoff_empty_field_is_treated_as_missing(tmp_path: Path) -> None:
    """A present-but-EMPTY required field is missing (the ``not value`` leg), not a
    valid value.

    kills: weakening ``not isinstance(value, str) or not value`` to a presence-only
    ``key in resolved`` check."""
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "notebook",
            "scope_id": _AUDIT,
            "block": "notebook-sign-off",
            "response": "model-fit reviewed",
            "resolved": {
                "audit_id": _AUDIT,
                "section": _SECTION,
                "section_sha": "a" * 64,
                "view_sha": "",  # present but empty
            },
        }
    )
    with pytest.raises(errors.SpecInvalid, match="view_sha"):
        append_decision(experiment_dir=tmp_path, spec=spec)


# ── Lock 2: the un-fakeable section_sha recompute ─────────────────────────────


def test_signoff_fabricated_section_sha_refused(tmp_path: Path) -> None:
    """A sign-off asserting a ``section_sha`` the source does NOT hash to is refused
    at the attestation bind — a hash cannot be asserted into existence (D5 lock 2).
    The fabricated section body is a real section (so the not-found leg is not what
    fires) but the asserted sha is wrong.

    kills: dropping the ``recompute=sect.section_sha`` bind, or flipping the bind's
    equality — THE central un-fakeability lock of the whole gate."""
    _write_notebook(tmp_path, source_text=_source())
    sv = _canonical_section(tmp_path)
    write_render(tmp_path, audit_id=_AUDIT, view=sv)  # render present → not the render leg
    with pytest.raises(errors.SpecInvalid, match="does not match the recomputed"):
        _signoff(
            tmp_path,
            "model-fit reviewed — the regularization term is sound",
            section_sha="deadbeef" * 8,  # not the real recomputed sha
            view_sha=sv.view_sha,
        )


def test_signoff_correct_section_sha_binds(tmp_path: Path) -> None:
    """The companion: the freshly-recomputed section_sha binds and the sign-off
    lands. Proves the bind is a real gate that PASSES on the true sha, not an
    always-refuse."""
    _write_notebook(tmp_path, source_text=_source())
    sv = _canonical_section(tmp_path)
    write_render(tmp_path, audit_id=_AUDIT, view=sv)
    _signoff(
        tmp_path,
        "model-fit reviewed — the regularization term is sound",
        section_sha=sv.section_sha,
        view_sha=sv.view_sha,
    )
    recs = read_decisions(tmp_path, "notebook", _AUDIT)
    assert any(r.get("block") == "notebook-sign-off" for r in recs)


# ── section-not-in-source + unresolvable source/template ──────────────────────


def test_signoff_section_not_in_source_refused(tmp_path: Path) -> None:
    """Signing a section slug absent from the audited source is refused — a sign-off
    must name a CURRENT section. Fires AFTER the authorship floor (the response
    names the ghost slug) and BEFORE the bind.

    kills: dropping the ``sect is None`` refusal (which would let a sign-off name a
    section that no longer exists)."""
    _write_notebook(tmp_path, source_text=_source())
    with pytest.raises(errors.SpecInvalid, match="not found in the audited source"):
        _signoff(
            tmp_path,
            "ghost-section reviewed and it looks correct",
            section="ghost-section",
            section_sha="a" * 64,
            view_sha="b" * 64,
        )


def test_signoff_unresolvable_source_and_template_named_together(tmp_path: Path) -> None:
    """With NO audited_source block and no source/template in resolved, BOTH
    unresolved ingredients are named in a SINGLE refusal (the one-shot refusal that
    replaced the three-bounce discovery).

    kills: a mutation that names only the first unresolved ingredient (dropping the
    ``' + '.join(unresolved)`` accumulation)."""
    # interview.json WITHOUT an audited_source block → nothing supplies source/template.
    (tmp_path / "interview.json").write_text(json.dumps({"goal": "g"}), encoding="utf-8")
    with pytest.raises(errors.SpecInvalid) as ei:
        _signoff(
            tmp_path,
            "model-fit reviewed thoroughly",
            section_sha="a" * 64,
            view_sha="b" * 64,
        )
    msg = str(ei.value)
    assert "source" in msg
    assert "template" in msg


# ── AUTO_CLEARED: voluntary human sign-off is ACCEPTED and marked redundant ────


def test_signoff_auto_cleared_marked_redundant_and_bar_waived(tmp_path: Path) -> None:
    """A voluntary human sign-off of an AUTO_CLEARED section is ACCEPTED, marked
    ``resolved['redundant'] = True``, and the raised diff-token bar is WAIVED (the
    response names the slug but engages no diff identifier — there is no change to
    engage).

    kills: (a) flipping the AUTO_CLEARED branch to refuse; (b) dropping the
    ``resolved['redundant'] = True`` marking; (c) removing the early ``return`` so
    the HUMAN_REQUIRED diff bar fires on a changeless section."""
    _write_notebook(tmp_path, source_text=_CLEAR_MODULE, template_text=_CLEAR_MODULE)
    sv = _canonical_section(tmp_path, section=_CLEAR_SECTION)
    assert sv.tier == AUTO_CLEARED  # guard-can-fire: the branch under test is reachable
    write_render(tmp_path, audit_id=_AUDIT, view=sv)
    out = _signoff(
        tmp_path,
        "prelude reviewed, looks fine to me",  # names slug, engages NO diff token
        section=_CLEAR_SECTION,
        section_sha=sv.section_sha,
        view_sha=sv.view_sha,
    )
    assert out.record.resolved["redundant"] is True


# ── the finding-10 TEMPORAL filter over the harness utterance log ─────────────
# With an utterance log present, the tier runs over LOGGED HUMAN UTTERANCES and
# the agent-relayed ``response`` carries no authorship weight. A candidate must be
# logged AT OR AFTER the render the human saw (its mtime) — a prior prompt that
# named the slug is NOT attestation (the standing-sign-off class).


def test_signoff_harness_log_fresh_utterance_accepted(tmp_path: Path) -> None:
    """A logged human utterance typed AFTER the render, naming the slug and engaging
    the ``regularization`` diff identifier, satisfies the tier — the sign-off lands.

    kills: a mutation that inverts the ``when >= floor`` freshness comparison (which
    would drop a genuinely-fresh utterance)."""
    _write_notebook(tmp_path, source_text=_source())
    sv = _canonical_section(tmp_path)
    assert sv.tier == HUMAN_REQUIRED
    write_render(tmp_path, audit_id=_AUDIT, view=sv)
    _set_render_mtime(tmp_path, sv, _ANCHOR)
    _log_utterance_at(
        tmp_path, "model-fit reviewed — the regularization term is sound", _AFTER
    )
    # The agent-relayed response is deliberately WORTHLESS (a bare ack): only the
    # fresh logged utterance can carry the sign-off.
    _signoff(tmp_path, "y", section_sha=sv.section_sha, view_sha=sv.view_sha)
    recs = read_decisions(tmp_path, "notebook", _AUDIT)
    assert any(r.get("block") == "notebook-sign-off" for r in recs)


def test_signoff_harness_log_stale_utterance_refused(tmp_path: Path) -> None:
    """The ONLY slug-naming utterance was logged BEFORE the render existed (the
    standing-sign-off class). It is filtered out; the candidate pool is empty and
    the sign-off is refused with the E2 authorship marker — even though the
    agent-relayed ``response`` is a perfect engaging sign-off (it carries no weight
    once a log is present).

    kills: dropping the render-mtime anchor / the temporal filter entirely (which
    would let a pre-render prompt stand in as attestation)."""
    _write_notebook(tmp_path, source_text=_source())
    sv = _canonical_section(tmp_path)
    write_render(tmp_path, audit_id=_AUDIT, view=sv)
    _set_render_mtime(tmp_path, sv, _ANCHOR)
    _log_utterance_at(
        tmp_path, "model-fit reviewed — the regularization term is sound", _BEFORE
    )
    with pytest.raises(errors.SpecInvalid) as ei:
        _signoff(
            tmp_path,
            "model-fit reviewed — the regularization term is sound",  # perfect, but AGENT text
            section_sha=sv.section_sha,
            view_sha=sv.view_sha,
        )
    assert "logged human utterance" in str(ei.value)
    assert _marker_of(ei.value) == _MARKER


def test_signoff_harness_log_fresh_but_unengaged_refused(tmp_path: Path) -> None:
    """A FRESH logged utterance that names the slug but engages NO diff identifier is
    refused at the HUMAN_REQUIRED bar (the bar is enforced over the log tier, not
    only the friction tier).

    kills: a mutation that skips the diff-token bar whenever the log tier supplied
    the candidate."""
    _write_notebook(tmp_path, source_text=_source())
    sv = _canonical_section(tmp_path)
    write_render(tmp_path, audit_id=_AUDIT, view=sv)
    _set_render_mtime(tmp_path, sv, _ANCHOR)
    _log_utterance_at(tmp_path, "model-fit reviewed and it looks correct to me", _AFTER)
    with pytest.raises(errors.SpecInvalid) as ei:
        _signoff(tmp_path, "y", section_sha=sv.section_sha, view_sha=sv.view_sha)
    assert "HUMAN-REQUIRED" in str(ei.value)
    assert _marker_of(ei.value) == _MARKER


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
