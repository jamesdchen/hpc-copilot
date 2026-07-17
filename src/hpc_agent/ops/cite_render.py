"""Deterministic markdown render for ``cite-check`` (the ``relay_render.py`` /
``recipe_render.py`` posture — the number → paper transcription audit).

Pure string formatting over the audit's own structured fields — the two-bucket
disclosure (``matched`` / ``uncitable``), each finding's surface claim, its
one-line detail, and the ``nearest_chain_value`` CONTEXT on an uncitable finding.
It imports nothing LLM-adjacent and nothing from ``_wire`` (the ``ops`` op owns
the Pydantic boundary), takes no free-prose parameter, and never NAMES a metric:
a finding renders as a claim string + a bucket word + a disclosed detail. The
boundary test (``tests/contracts/test_cite_check_boundary.py``) pins this posture.
"""

from __future__ import annotations

from typing import Any

__all__ = ["render_cite_check"]


def render_cite_check(result: dict[str, Any]) -> str:
    """Render the cite-check result dict as one deterministic markdown document.

    *result* is the ``CiteCheckResult`` as a plain dict (the op dumps the model to
    JSON mode before calling here, so the render path stays wire-free). The output
    is stable for a given result so a reviewer can diff two renders.
    """
    seed_kind = str(result.get("seed_kind", ""))
    seed_ref = str(result.get("seed_ref", ""))
    clean = bool(result.get("clean"))
    claims_checked = int(result.get("claims_checked", 0))
    findings = list(result.get("findings") or [])
    sources = list(result.get("sources_consulted") or [])

    matched = [f for f in findings if f.get("kind") == "matched"]
    uncitable = [f for f in findings if f.get("kind") == "uncitable"]

    lines: list[str] = []
    lines.append(f"# cite-check — {seed_kind} `{seed_ref}`")
    lines.append("")
    verdict = "CLEAN" if clean else "UNCITABLE NUMBERS FOUND"
    lines.append(
        f"verdict: **{verdict}** — {claims_checked} claim(s) checked, "
        f"{len(matched)} matched, {len(uncitable)} uncitable."
    )
    lines.append("")
    lines.append(
        "> DISCLOSES, never gates: an uncitable number is surfaced for a human to "
        "resolve, never refused. `nearest_chain_value` is CONTEXT, not an alignment."
    )
    lines.append("")

    # 1. Uncitable — the numbers no sealed value backs (the disclosure).
    lines.append(f"## Uncitable ({len(uncitable)})")
    lines.append("")
    if uncitable:
        for f in uncitable:
            nearest = f.get("nearest_chain_value")
            nearest_str = f" — nearest sealed value: `{nearest}`" if nearest else ""
            lines.append(f"- `{f.get('claim', '')}` — {f.get('detail', '')}{nearest_str}")
    else:
        lines.append("_(every checked number is backed by a sealed value)_")
    lines.append("")

    # 2. Matched — reported for auditability; clean ignores these.
    lines.append(f"## Matched ({len(matched)})")
    lines.append("")
    if matched:
        for f in matched:
            lines.append(f"- `{f.get('claim', '')}` — {f.get('detail', '')}")
    else:
        lines.append("_(no matched numbers)_")
    lines.append("")

    # 3. Sealed artifacts consulted.
    lines.append(f"## Sealed artifacts consulted ({len(sources)})")
    lines.append("")
    if sources:
        for s in sources:
            lines.append(f"- `{s}`")
    else:
        lines.append(
            "_(no sealed metrics_aggregate.json resolved — every number is uncitable-against-it)_"
        )
    lines.append("")

    return "\n".join(lines)
