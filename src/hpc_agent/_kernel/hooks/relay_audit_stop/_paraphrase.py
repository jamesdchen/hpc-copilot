"""Audit 3 — the paraphrase pass (the verbatim-block check, G1).

verify-relay catches wrong TOKENS; the relay-due gate catches OMISSION; this
pass catches PARAPHRASE — a relayed ```diff block whose content lines are not
verbatim in any of the mentioned audits' current trusted renders.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Paraphrase-pass bounds (G1): cap the render corpus and the checked lines so
#: the Stop hook stays cheap on pathological inputs.
_MAX_RENDER_CORPUS_BYTES = 2_000_000
_MAX_RENDER_FILES = 40
_MAX_DIFF_LINES_CHECKED = 200

_DIFF_FENCE_RE = re.compile(r"```diff\n(.*?)```", re.DOTALL)


def _paraphrase_findings(experiment_dir: Path, relay_text: str, audit_ids: list[str]) -> list[str]:
    """G1 — the verbatim-block check: relayed diff content must BE render content.

    verify-relay catches wrong TOKENS; the relay-due gate catches OMISSION;
    this pass catches PARAPHRASE — a relayed ```diff block whose content lines
    do not exist in any of the mentioned audits' current trusted renders
    (.hpc/renders/<audit_id>/*.md). Scoped tightly against false positives:
    only fenced ``diff`` blocks whose own text or 3 preceding lines carry
    audit vocabulary ("section") are checked — a relayed *git* diff never
    qualifies. Content lines are the +/- lines (never the +++/--- headers).
    Fail-open at every grain and capped; worst case is one block-once nudge.
    """
    findings: list[str] = []
    try:
        corpus_parts: list[str] = []
        total = 0
        nfiles = 0
        for audit_id in audit_ids:
            rdir = Path(experiment_dir) / ".hpc" / "renders" / audit_id
            if not rdir.is_dir():
                continue
            for f in sorted(rdir.glob("*.md")):
                if nfiles >= _MAX_RENDER_FILES or total >= _MAX_RENDER_CORPUS_BYTES:
                    break
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                corpus_parts.append(text)
                total += len(text)
                nfiles += 1
        if not corpus_parts:
            return []
        corpus = "\n".join(corpus_parts)

        checked = 0
        lines_before: list[str] = relay_text.splitlines()
        for m in _DIFF_FENCE_RE.finditer(relay_text):
            block = m.group(1)
            # Audit-context scoping: the block itself or its 3 preceding
            # lines must mention "section" (the render vocabulary).
            start_line = relay_text[: m.start()].count("\n")
            context = "\n".join(lines_before[max(0, start_line - 3) : start_line + 1])
            if "section" not in block.lower() and "section" not in context.lower():
                continue
            for line in block.splitlines():
                if checked >= _MAX_DIFF_LINES_CHECKED:
                    return findings
                if not line or line[0] not in "+-" or line.startswith(("+++", "---")):
                    continue
                stripped = line.strip()
                if len(stripped) < 4:
                    continue  # bare +/- markers carry no content to verify
                checked += 1
                if line not in corpus and stripped not in corpus:
                    findings.append(
                        "relayed diff content not found in any current render "
                        f"for the mentioned audits: {stripped[:80]!r} — relay "
                        "diffs verbatim from the render files, never re-typed."
                    )
                    break  # one finding per block is enough to force the re-relay
    except Exception:
        return []  # the paraphrase pass is never load-bearing (Option-3 class)
    return findings
