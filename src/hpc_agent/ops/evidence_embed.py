"""The greenlight / S1 evidence embed — the ONE fail-open seat (E-embed).

Design: ``docs/design/evidence-memory.md`` E-embed (Wave C, T9). The
campaign-greenlight brief and the submit-S1 resolved surface each gain an
ADDITIVE ``evidence`` field = the point-query digest for the new work's declared
scope tags + its lineage key. Both seats call THIS one helper (never a private
re-collection — the one-collector enforcement row): it runs
:func:`state.evidence.collect_evidence` + :func:`ops.evidence_render.render_brief`
and returns the additive dict.

**FAIL-OPEN — the load-bearing rule (E-embed).** The embed must never mint a new
failure mode in the submit/greenlight path. The collector's tolerant reads cover
expected I/O noise, but a BUG anywhere in ``collect_evidence`` / the render raised
mid-greenlight would turn an advisory digest into a submit refusal — the
never-blocking pin violated by accident. So this helper wraps the ENTIRE embed in
a broad guard: ANY exception degrades to ``{"unavailable": True, "reason": "<class:
msg>"}`` — disclosed in the brief, logged, NEVER propagated. The greenlight / S1
decision surface (gates, ``needs_decision``, ``next_block``) is byte-identical
whether evidence collected, collected empty, or failed. Collector failure is never
a submit error; the remedy is running ``evidence-brief`` directly, where the same
failure IS loud (only the embedded advisory seats fail open — the APPEND gate,
where citation verification is load-bearing, refuses loudly and never runs here).

No dossier resolver is injected here: a ``dossier`` citation with no resolver
DISCLOSES at read (:func:`state.evidence.collect_evidence`), it never raises — the
read-side posture (evidence legitimately moves after a conclusion is recorded).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["build_evidence_embed"]

_log = logging.getLogger(__name__)


def build_evidence_embed(
    experiment_dir: Path,
    *,
    tags: Sequence[str] | None = None,
    lineage: str | None = None,
) -> dict[str, Any]:
    """Point-query digest for the new work's *tags* + *lineage* — fail-open.

    Returns the additive ``evidence`` field for a greenlight / S1 brief. On
    success: ``{computed_at, tags, lineage, conclusion_count, unconcluded_count,
    render}`` — ``render`` is the code-composed markdown digest for verbatim relay.
    On ANY exception (a collector bug, a render bug, a corrupted store the tolerant
    reads did not catch): the disclosed stub ``{"unavailable": True, "reason":
    "<ExcClass: msg>"}``. NEVER raises.
    """
    try:
        from hpc_agent.infra.time import utcnow_iso
        from hpc_agent.ops.evidence_render import render_brief
        from hpc_agent.state.evidence import collect_evidence

        collection = collect_evidence(experiment_dir, tags=tags, lineage=lineage)
        computed_at = utcnow_iso()
        render = render_brief(collection, computed_at=computed_at)
        return {
            "computed_at": computed_at,
            "tags": list(collection.tags),
            "lineage": collection.lineage,
            "conclusion_count": len(collection.conclusions),
            "unconcluded_count": len(collection.unconcluded),
            "render": render,
        }
    except Exception as exc:  # noqa: BLE001 — fail-open at the advisory seat (E-embed)
        _log.warning("evidence embed unavailable (degraded to disclosed stub): %r", exc)
        return {"unavailable": True, "reason": f"{type(exc).__name__}: {exc}"[:200]}
