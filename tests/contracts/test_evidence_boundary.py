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


# ── T11: the remaining enforcement rows ───────────────────────────────────────


def test_citation_kinds_closed_and_equals_wire_literal() -> None:
    """``CITATION_KINDS`` is a CLOSED, mechanism-only set == the wire ``CitationKind``.

    A domain word (a metric, a strategy) becoming a kind is index poisoning — those
    ride ``ref`` as opaque identity, never a new kind. The state frozenset and the
    wire literal are two spellings of ONE closed vocabulary.
    """
    from typing import get_args

    from hpc_agent._wire.queries.evidence import CitationKind
    from hpc_agent.state.evidence import CITATION_KINDS

    assert frozenset({"dossier", "run", "fingerprint", "attestation"}) == CITATION_KINDS
    assert set(get_args(CitationKind)) == set(CITATION_KINDS)


def test_one_collector_route_through_every_surface() -> None:
    """Both verbs, the embed seat, and the queue collector route through the ONE
    ``collect_evidence`` — no surface re-walks or re-reduces (the one-collector row).
    """
    from hpc_agent.ops import attention_queue as aq
    from hpc_agent.ops.evidence_brief_op import evidence_brief
    from hpc_agent.ops.evidence_embed import build_evidence_embed
    from hpc_agent.ops.evidence_period_op import evidence_period

    surfaces = (
        evidence_brief,
        evidence_period,
        build_evidence_embed,
        aq.collect_campaign_unconcluded,
    )
    for func in surfaces:
        src = inspect.getsource(func)
        assert "collect_evidence(" in src, (
            f"{func.__qualname__} must route through collect_evidence"
        )


def test_kernel_and_citation_route_through() -> None:
    """Conclusion attestations route through the ONE kernel (bind/reduce) and every
    citation verifies at APPEND via ``resolve_citation`` — never a re-inlined compare.
    """
    from hpc_agent.ops.decision.journal import _assert_conclusion_full
    from hpc_agent.state.evidence import reduce_conclusion

    reduce_src = inspect.getsource(reduce_conclusion)
    assert "attestation.reduce(" in reduce_src  # winner-selection via the ONE kernel

    gate_src = inspect.getsource(_assert_conclusion_full)
    assert "attestation.bind(" in gate_src  # content_sha hash-locked via the kernel
    assert "resolve_citation(" in gate_src  # every citation verified server-side at append


def test_no_conclusion_affordance_in_registry() -> None:
    """No mutate/workflow verb is named conclude/conclusion — append-decision under
    the gated block is the ONLY write path (Lock 1, the no-unlock-verb doctrine).
    """
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    for name in get_registry():
        low = name.lower()
        assert "conclude" not in low and "conclusion" not in low, (
            f"a verb-shaped conclusion affordance appeared in the registry: {name!r}"
        )


def test_no_code_path_mechanically_writes_the_conclusion_block() -> None:
    """No core writer hand-commits ``block=\"conclusion\"`` — the block only rides a
    human ``append-decision`` through the gate (no agent-authored conclusions).
    """
    import pathlib

    src_root = pathlib.Path(inspect.getfile(build_evidence_embed)).parents[1]  # hpc_agent/
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if 'block="conclusion"' in text or "block='conclusion'" in text:
            offenders.append(str(py))
    assert not offenders, f"a code path mechanically writes block=conclusion: {offenders}"


def test_render_has_no_interpretation_vocabulary() -> None:
    """The digest render composes counts/dates/shas only — no urgency / recommendation
    vocabulary in its source literals (the D6 no-fabricated-urgency rule).
    """
    from hpc_agent.ops import evidence_render

    banned = ("urgent", "recommend", "should", "promising", "stale", "must ", "don't")
    tree = ast.parse(inspect.getsource(evidence_render))
    # Exclude docstrings / bare-expression strings (prose ABOUT the rule, not render
    # output) — only string constants that actually flow into the digest count.
    docstrings = {
        stmt.value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for stmt in node.body
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
    }
    literals = [
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings
    ]
    for text in literals:
        for word in banned:
            assert word not in text, (
                f"interpretation vocabulary {word!r} in a render literal: {text!r}"
            )


def test_index_is_disposable_render_and_embed_write_nothing(tmp_path: Path) -> None:
    """The projection is DISPOSABLE: rendering / embedding persists no file under the
    namespace — no digest file, no watermark (a persisted projection would be a
    second source of truth).
    """
    from hpc_agent.ops.evidence_embed import build_evidence_embed as _embed

    def _snapshot(root: Path) -> set[str]:
        return {str(p.relative_to(root)) for p in root.rglob("*")}

    write_manifest(tmp_path, campaign_id=_CID, goal="widget throughput")
    _seed_conclusions(tmp_path, 3)
    before = _snapshot(tmp_path)
    _embed(tmp_path, tags=["edge-x"])
    _embed(tmp_path, tags=[])
    assert _snapshot(tmp_path) == before  # the embed created nothing


def test_conclusions_required_nowhere_zero_is_a_normal_advisory(tmp_path: Path) -> None:
    """Conclusions are required NOWHERE at creation: an empty namespace yields a
    NORMAL advisory embed (counts of zero), never a refusal / unavailable / gate.
    """
    from hpc_agent.ops.evidence_embed import build_evidence_embed as _embed

    ev = _embed(tmp_path, tags=["edge-x"])
    assert ev.get("unavailable") is not True
    assert ev.get("conclusion_count") == 0
    assert ev.get("unconcluded_count") == 0
