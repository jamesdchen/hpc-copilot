"""Evidence-memory boundary contracts (``docs/design/evidence-memory.md``).

T-NB — the NEVER-BLOCKING pin (the anti-stage-0-dedupe decision, load-bearing).
This module's ``test_surfacing_never_blocks`` is the single most important pin in
the plan: it mechanizes the kill-ledger decision against future contributors —
evidence CONTENT never gates, refuses, or reshapes a greenlight, and a collector
BUG can never become a submit error (the fail-open seat).

Three legs (E-embed + E3):

* (a) SOURCE SCAN — the embed seat (``ops/evidence_embed.build_evidence_embed``)
  contains no ``raise`` on any evidence-content branch, and the seat-level broad
  guard exists (the fail-open wrapper).
* (b) BEHAVIORAL byte-equality — ten negative-conclusion priors vs an empty
  namespace greenlight with a byte-identical decision surface (same
  ``needs_decision``, ``next_block``, ``stage_reached``).
* (c) FAULT INJECTION — ``collect_evidence`` monkeypatched to raise, AND a
  corrupted-journal fixture: the greenlight still completes, decision surface
  byte-identical, the embed disclosed as ``unavailable`` (or a tolerant digest).

TOY VOCABULARY ONLY: widget lineage, never a real domain's words.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from typing import TYPE_CHECKING, Any

from hpc_agent._wire.workflows.campaign_blocks import CampaignGreenlightSpec
from hpc_agent.meta.campaign.blocks import campaign_greenlight
from hpc_agent.meta.campaign.manifest import write_manifest
from hpc_agent.ops.evidence_embed import build_evidence_embed
from tests.contracts.never_blocking import assert_never_blocking

if TYPE_CHECKING:
    from pathlib import Path

_CID = "camp-widget-1"


def _seed_conclusions(experiment_dir: Path, n: int) -> None:
    """Write *n* NEGATIVE conclusion records straight to the journal (fixture).

    Written raw (bypassing the T8 gate, which would verify the fabricated citation
    shas against live stores) — the collector reads these journals directly, and
    at READ citations only DISCLOSE, never refuse.
    """
    conc_dir = experiment_dir / ".hpc" / "conclusions"
    conc_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        cid = f"widget-neg-{i:02d}"
        record = {
            "schema_version": 1,
            "ts": f"2025-11-{i + 1:02d}T00:00:00Z",
            "scope_kind": "conclusion",
            "scope_id": cid,
            "block": "conclusion",
            "response": f"conclude {cid} — deadbeef{i:02d}",
            "resolved": {
                "conclusion_id": cid,
                "tags": ["edge-x"],
                "citations": [
                    {"kind": "run", "ref": f"widget-run-{i}", "sha": f"deadbeef{i:02d}00"}
                ],
                "finding": "no alpha in this window",
            },
        }
        (conc_dir / f"{cid}.decisions.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )


def _greenlight_surface(experiment_dir: Path) -> tuple[Any, ...]:
    """Run campaign-greenlight (digest path) and return its DECISION SURFACE.

    The decision surface is everything a downstream driver branches on — NOT the
    brief (an additive advisory field). Byte-identical across evidence states is
    the never-blocking guarantee.
    """
    spec = CampaignGreenlightSpec.model_validate({"campaign_id": _CID})
    out = campaign_greenlight(experiment_dir, spec=spec)
    return (out.block, out.stage_reached, out.needs_decision, out.next_block, out.reason)


def _brief_evidence(experiment_dir: Path) -> dict[str, Any]:
    spec = CampaignGreenlightSpec.model_validate({"campaign_id": _CID})
    out = campaign_greenlight(experiment_dir, spec=spec)
    assert isinstance(out.brief, dict)
    ev = out.brief.get("evidence")
    assert isinstance(ev, dict)
    return ev


# ── (a) source scan ───────────────────────────────────────────────────────────


def test_embed_seat_never_raises() -> None:
    """The embed seat contains no ``raise`` — a disclosure path never gates."""
    assert_never_blocking(build_evidence_embed)


def test_embed_seat_has_broad_fail_open_guard() -> None:
    """The seat wraps the embed in a broad ``except Exception`` (fail-open E-embed).

    A bare ``except:`` or a narrow one would let a novel collector bug escape into
    the submit path — the never-blocking pin violated by accident. This asserts the
    guard catches ``Exception`` broadly.
    """
    source = textwrap.dedent(inspect.getsource(build_evidence_embed))
    tree = ast.parse(source)
    broad = [
        h
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for h in node.handlers
        if isinstance(h.type, ast.Name) and h.type.id == "Exception"
    ]
    assert broad, (
        "build_evidence_embed must wrap the embed in a broad `except Exception` (fail-open)"
    )


# ── (b) behavioral byte-equality ──────────────────────────────────────────────


def test_ten_negative_priors_greenlight_identically_to_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    priors = tmp_path / "priors"
    for base in (empty, priors):
        base.mkdir()
        write_manifest(base, campaign_id=_CID, goal="widget throughput")
    _seed_conclusions(priors, 10)

    # The DECISION SURFACE is byte-identical — the anti-stage-0-dedupe pin.
    assert _greenlight_surface(empty) == _greenlight_surface(priors)

    # ...yet the advisory evidence field DID surface the priors (proving the embed
    # ran and is not a no-op): the ten conclusions show up, empty shows none.
    ev_empty = _brief_evidence(empty)
    ev_priors = _brief_evidence(priors)
    assert ev_empty.get("conclusion_count") == 0
    assert ev_priors.get("conclusion_count", 0) >= 10


# ── (c) fault injection ───────────────────────────────────────────────────────


def test_collector_bug_degrades_to_unavailable_stub(tmp_path: Path, monkeypatch: Any) -> None:
    """A ``collect_evidence`` that RAISES never becomes a greenlight refusal."""
    base = tmp_path / "boom"
    base.mkdir()
    write_manifest(base, campaign_id=_CID, goal="widget throughput")
    baseline = _greenlight_surface(base)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("simulated collector bug")

    # build_evidence_embed imports collect_evidence at call time from this module,
    # so patching the attribute here reaches the seat's local import.
    monkeypatch.setattr("hpc_agent.state.evidence.collect_evidence", _boom)

    # The greenlight still completes, decision surface byte-identical...
    assert _greenlight_surface(base) == baseline
    # ...and the embed is disclosed as unavailable, never propagated.
    ev = _brief_evidence(base)
    assert ev.get("unavailable") is True
    assert "RuntimeError" in str(ev.get("reason", ""))


def test_corrupted_journal_still_greenlights(tmp_path: Path) -> None:
    """A corrupted conclusion journal never blocks the greenlight (tolerant read)."""
    empty = tmp_path / "clean"
    corrupt = tmp_path / "corrupt"
    for base in (empty, corrupt):
        base.mkdir()
        write_manifest(base, campaign_id=_CID, goal="widget throughput")
    conc_dir = corrupt / ".hpc" / "conclusions"
    conc_dir.mkdir(parents=True, exist_ok=True)
    (conc_dir / "broken.decisions.jsonl").write_text(
        "{not valid json at all\n\x00\x01garbage\n", encoding="utf-8"
    )

    # Greenlight completes with a byte-identical decision surface; the embed either
    # tolerates (a normal digest with skipped accounting) or discloses unavailable
    # — either way the greenlight is not blocked.
    assert _greenlight_surface(corrupt) == _greenlight_surface(empty)
    ev = _brief_evidence(corrupt)
    assert isinstance(ev, dict)  # completed, never raised
