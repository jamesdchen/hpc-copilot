"""Render relay-due markers — the omission gate's SECOND producer (run-#11 item 3).

``notebook-audit-view`` arms a per-section relay-due MARKER when it builds the
CANONICAL view of a HUMAN-REQUIRED section, keyed on that section's
``view_sha12`` (the hash the trusted render filename is addressed by). The
relay-audit Stop hook discharges it only when that sha12 reaches the human — a
render that arrived as an unread file link (run #11's live failure) is not a
relay. Mirrors ``tests/ops/test_notebook_status.py``'s relay-due block: the
producer side is pinned here (marker journaled / deduplicated / narrow set);
the block-and-discharge side is pinned in
``tests/_kernel/hooks/test_relay_audit_stop.py``.

The seam under test is :func:`hpc_agent.ops.notebook.view_op._to_result`, which
owns the marker write given a built view + the ``canonical`` flag — exercised
here with a REAL :func:`~hpc_agent.ops.notebook.audit_view.build_audit_view`
output so the tier classification (auto_cleared vs human_required) is genuine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.ops.notebook.audit_view import (
    AUTO_CLEARED,
    HUMAN_REQUIRED,
    build_audit_view,
)
from hpc_agent.ops.notebook.view_op import _to_result
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.ops.notebook.audit_view import AuditView

_AUDIT = "demo-audit"

# Two sections. load-data is byte-identical to the template (inherited, no
# assertions, no flags → AUTO_CLEARED); fit-model is edited (modified →
# HUMAN_REQUIRED). So exactly one section is human-required.
_TEMPLATE = """\
# %%
# hpc-audit-section: load-data
import pandas as pd
df = pd.read_csv("in.csv")

# %%
# hpc-audit-section: fit-model
model = fit(df)
"""
_SOURCE = _TEMPLATE.replace("model = fit(df)", "model = fit(df, reg=True)")


def _view() -> AuditView:
    return build_audit_view(
        parse_percent_source(_SOURCE),
        parse_percent_source(_TEMPLATE),
        [],
    )


def _tier(view: AuditView, slug: str) -> str:
    return next(sv.tier for sv in view.sections if sv.slug == slug)


def _view_sha12(view: AuditView, slug: str) -> str:
    return next(sv.view_sha for sv in view.sections if sv.slug == slug)[:12]


def _markers(tmp_path: Path) -> list[dict]:
    return [
        r
        for r in read_decisions(tmp_path, "notebook", _AUDIT)
        if r.get("block") == nb.RELAY_DUE_BLOCK
    ]


def test_canonical_human_required_section_journals_one_marker(tmp_path: Path) -> None:
    """The CANONICAL view of a human-required section arms a marker whose single
    key token is that section's view_sha12; the auto_cleared section arms none."""
    view = _view()
    assert _tier(view, "fit-model") == HUMAN_REQUIRED
    assert _tier(view, "load-data") == AUTO_CLEARED

    _to_result(tmp_path, _AUDIT, view, canonical=True)

    markers = _markers(tmp_path)
    assert len(markers) == 1
    resolved = markers[0]["resolved"]
    assert markers[0]["response"] == nb.RELAY_DUE_RESPONSE
    assert resolved["record_kind"] == nb.RENDER_RELAY_DUE_RECORD_KIND == "notebook-audit-view"
    assert resolved["audit_id"] == _AUDIT
    # The one token is fit-model's view_sha12 — the render-file address — and NOT
    # load-data's (an auto_cleared section is never relay-due).
    assert resolved["key_tokens"] == [_view_sha12(view, "fit-model")]
    assert _view_sha12(view, "load-data") not in resolved["key_tokens"]
    assert resolved["created_at"]


def test_reviewing_the_same_section_does_not_rearm(tmp_path: Path) -> None:
    """Deduplicated on (record_kind, key_tokens): re-viewing the same section at
    the same content arms nothing new — what keeps the verb idempotent."""
    view = _view()
    _to_result(tmp_path, _AUDIT, view, canonical=True)
    _to_result(tmp_path, _AUDIT, view, canonical=True)
    assert len(_markers(tmp_path)) == 1


def test_preview_view_journals_nothing(tmp_path: Path) -> None:
    """A PREVIEW (canonical=false) view journals NO marker, even for a
    human-required section — its view_shas are not gate-acceptable, so the
    omission obligation would be un-dischargeable-by-a-canonical-relay noise."""
    view = _view()
    assert _tier(view, "fit-model") == HUMAN_REQUIRED
    _to_result(tmp_path, _AUDIT, view, canonical=False)
    assert _markers(tmp_path) == []
    # A preview never even scaffolds the notebook journal via the marker path.
    assert not (tmp_path / ".hpc" / "notebooks").exists()
