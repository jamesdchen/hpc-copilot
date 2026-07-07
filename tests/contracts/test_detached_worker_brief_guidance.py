"""Contract: the surfaces that await a DETACHED worker pin the brief-source rule.

A detached submit block (``submit-s2``/``-s3``/``-s4``) hands the agent a
``wait-detached`` lease and, on ``worker_exited``, the brief must come from ONE
``block-drive`` tick that replays the finished block's recorded terminal — the
code-digested ``brief`` plus the code-rendered ``relay`` line surfaced VERBATIM.
Composing the brief from the worker's LOG / tail (job numbers, node names, wall
times, read-time timestamps) is exactly the rule-10 relay-audit violation
(proving run #9: two strikes, two correction rounds, both from log-scraped
numbers).

That rule lived only as load-bearing prose with no contract pinning it. This
binds it to every surface that instructs awaiting a detached worker (enumerated
by scanning ``src/slash_commands`` for ``wait-detached``), so dropping any of the
three halves — the ``worker_exited``→``block-drive``-tick source, the
relay-VERBATIM binding, or the never-compose-from-the-worker-log prohibition —
fails CI.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SLASH_ROOT = _REPO_ROOT / "src/slash_commands"

# The affordance that marks a surface as instructing awaiting a DETACHED worker.
# The INVOCATION form (``hpc-agent wait-detached``), not a bare mention. Since
# 2026-07-07 (connection-broker.md) ``status-watch`` is ALSO detach-by-contract,
# so hpc-status now carries the invocation form affirmatively (it no longer
# denies wait-detached applies) and is enumerated + bound by the rule below,
# exactly like hpc-submit's S2/S3/S4 blocks.
_DETACH_WAIT_MARKER = "hpc-agent wait-detached"

# The anchor surface — the submit SKILL is where detached S2/S3/S4 workers live;
# if the enumeration ever stops finding it, the scan (not the rule) has broken.
_ANCHOR = _SLASH_ROOT / "skills/hpc-submit/SKILL.md"


def _surfaces_awaiting_a_detached_worker() -> list[Path]:
    """Every SKILL / command markdown under ``src/slash_commands`` that mentions
    ``wait-detached`` — i.e. instructs the agent to await a detached worker."""
    candidates = sorted(_SLASH_ROOT.glob("skills/*/SKILL.md")) + sorted(
        _SLASH_ROOT.glob("commands/*.md")
    )
    return [p for p in candidates if _DETACH_WAIT_MARKER in p.read_text(encoding="utf-8")]


def _paragraph_with(text: str, needle: str) -> str:
    """Return the whole markdown paragraph containing *needle* — from the
    preceding blank line to the next blank line (the ``worker_exited`` rule is a
    single bolded paragraph in the submit SKILL)."""
    low = text.lower()
    i = low.find(needle.lower())
    if i < 0:
        return ""
    start = text.rfind("\n\n", 0, i)
    start = 0 if start < 0 else start + 2
    end = text.find("\n\n", i)
    return text[start:] if end < 0 else text[start:end]


def test_the_submit_skill_is_still_a_detached_await_surface() -> None:
    """The enumeration is only meaningful if it finds the anchor — a scan that
    silently matches nothing would make every assertion below vacuously pass."""
    surfaces = _surfaces_awaiting_a_detached_worker()
    assert surfaces, (
        f"no surface under {_SLASH_ROOT} mentions {_DETACH_WAIT_MARKER!r} — the "
        f"scan broke; the worker_exited brief-source rule can't be pinned to "
        f"nothing"
    )
    assert _ANCHOR in surfaces, (
        f"{_ANCHOR.name} (hpc-submit) must instruct awaiting a detached worker "
        f"(mention {_DETACH_WAIT_MARKER!r}) — it owns the detached S2/S3/S4 blocks"
    )


def test_detached_await_surfaces_pin_the_worker_exited_brief_source() -> None:
    for surface in _surfaces_awaiting_a_detached_worker():
        text = surface.read_text(encoding="utf-8")
        block = _paragraph_with(text, "worker_exited")
        name = f"{surface.parent.name}/{surface.name}"
        assert block, (
            f"{name} awaits a detached worker but carries no `worker_exited` "
            f"brief-source rule — on worker exit the brief comes from ONE "
            f"block-drive tick, never the worker's log"
        )

        # (a) the source: worker_exited → ONE block-drive tick (replay), never the log.
        assert "block-drive" in block and re.search(r"\btick\b", block), (
            f"{name}: the worker_exited rule must source the brief from ONE "
            f"`block-drive` tick (the recorded-terminal replay), not a re-run "
            f"or a hand-read of the log"
        )

        # (b) the binding: the code-rendered relay line is surfaced VERBATIM.
        assert "relay" in block and re.search(r"VERBATIM", block), (
            f"{name}: the rule must bind the code-rendered `relay` line to being "
            f"surfaced VERBATIM (code drafts the human-facing line, not the LLM)"
        )

        # (c) the prohibition: never compose the brief from the worker's log/tail.
        assert re.search(r"never from the worker's log", block) and re.search(
            r"[Cc]omposing the brief yourself from the worker log", block
        ), (
            f"{name}: the rule must PROHIBIT composing the brief from the worker "
            f"log / tail (job numbers, node names, wall times, read-time "
            f"timestamps) — the rule-10 relay-audit strike class"
        )
