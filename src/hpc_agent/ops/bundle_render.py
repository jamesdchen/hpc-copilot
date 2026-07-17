"""Deterministic markdown render for ``export-bundle``'s ``VERIFY`` manifest (the
``recipe_render.py`` / ``cite_render.py`` posture — publication bundle).

Pure string formatting over the bundle's own structured fields — IDENTITY (the
seed + primary run), the per-link MECHANICAL/DISCLOSED/ABSENT classification, the
CODE-emitted verdict, the union-of-disclosures ledger, and the member pointers.
It imports nothing LLM-adjacent and nothing from ``_wire`` (the ``ops`` op owns
the Pydantic boundary), takes no free-prose parameter, and never NAMES a metric:
a link renders as a fixed name + a status word + a disclosed detail, a disclosure
as an origin + a detail. The boundary test
(``tests/contracts/test_publication_bundle_boundary.py``) pins this posture.

The render is a SEALED bundle member (typed ``verify``), so it is deliberately
computed from the PRE-SEAL data only — the per-link classification, verdict, and
member list — and NEVER references ``bundle_sha256`` (which is the hash OVER the
sealed members, this render among them, and so cannot appear inside a member
without a self-reference). The ``bundle_sha256`` lives in the top-level
``VERIFY.json`` seal, which the render points a reviewer at.
"""

from __future__ import annotations

from typing import Any

__all__ = ["render_verify"]


def render_verify(verify: dict[str, Any]) -> str:
    """Render the pre-seal VERIFY view as one deterministic markdown document.

    *verify* is the pre-seal classification view (a plain dict the op builds
    before sealing): ``seed`` / ``primary_run_id`` / ``links`` / ``verdict`` /
    ``disclosures`` / ``members``. The output is stable for a given view so a
    reviewer can diff two renders. Deliberately carries NO ``bundle_sha256`` (see
    the module docstring).
    """
    seed = verify.get("seed") or {}
    seed_kind = str(seed.get("kind", ""))
    seed_ref = str(seed.get("ref", ""))
    primary = verify.get("primary_run_id")
    links = list(verify.get("links") or [])
    verdict = str(verify.get("verdict", ""))
    disclosures = list(verify.get("disclosures") or [])
    members = verify.get("members") or {}

    lines: list[str] = []
    lines.append(f"# Publication bundle VERIFY — {seed_kind} `{seed_ref}`")
    lines.append("")
    lines.append(f"primary run: `{primary if primary else '-'}`")
    lines.append("")
    lines.append(
        "> The integrity seal (`bundle_sha256`) + the full member entry list live "
        "in `VERIFY.json`, the top-level self-attesting manifest. Recompute it "
        "offline: sha256 each member, then the canonical digest over the "
        "path-sorted entries (see `offline_verify` in `VERIFY.json`)."
    )
    lines.append("")

    # 1. The honest verdict — CODE-emitted, relayed verbatim.
    lines.append("## Verdict")
    lines.append("")
    lines.append(verdict if verdict else "_(no verdict)_")
    lines.append("")

    # 2. Per-link reproducibility classification.
    lines.append(f"## Reproducibility links ({len(links)})")
    lines.append("")
    if links:
        lines.append("| link | status | detail |")
        lines.append("|---|---|---|")
        for link in links:
            name = str(link.get("link", ""))
            status = str(link.get("status", ""))
            detail = str(link.get("detail", ""))
            lines.append(f"| {name} | {status} | {detail} |")
    else:
        lines.append("_(no links classified)_")
    lines.append("")

    # 3. The union-of-disclosures ledger — disclosed, never a failure.
    lines.append(f"## Disclosures ({len(disclosures)})")
    lines.append("")
    if disclosures:
        for d in disclosures:
            origin = str(d.get("origin", ""))
            code = d.get("code")
            detail = str(d.get("detail", d.get("note", "")))
            code_str = f"**{code}** — " if code else ""
            lines.append(f"- [{origin}] {code_str}{detail}")
    else:
        lines.append("_(nothing disclosed — every link is mechanical or absent-by-choice)_")
    lines.append("")

    # 4. Member pointers — where each part of the bundle lives.
    lines.append("## Members")
    lines.append("")
    if isinstance(members, dict) and members:
        for key in sorted(members):
            value = members.get(key)
            lines.append(f"- `{key}`: `{value if value else '-'}`")
    else:
        lines.append("_(no member pointers)_")
    lines.append("")

    return "\n".join(lines)
