"""The ONE tolerant interview.json locate+load skeleton (subject-neutral).

Many gates probe ``interview.json`` for their own block — ``audited_source``,
``packs``, ``actors``, ``audited_source.input_roots`` — and every one of them
opens with the identical *locate + tolerant load* preamble: try the canonical
campaign-dir root first, accept ``.hpc/interview.json`` defensively (the
``detect_entry_point`` convention), and read each candidate SILENTLY — a
missing file, an unreadable/corrupt file, or a non-object top level is skipped,
never raised (the D7 not-opted-in fail-safe).

This module extracts ONLY that skeleton. It lives in ``state/`` on purpose:
``state.*`` is the shared substrate any ``ops``/``meta`` subject may import,
whereas an ``ops``-side home would be a *subject* and the subject-import lint
(``scripts/lint_subject_imports.py``) forbids one subject reaching into
another's internals. Each caller keeps its OWN block extraction — the part that
knows which key it wants and whether a present-but-malformed block is a silent
default or a loud :class:`errors.SpecInvalid`. This function knows nothing about
any block; it only yields the parseable candidate objects.

Yielding (not returning the first) preserves both attested postures byte for
byte: a caller that COMMITS to the first parseable document ``return``\\s inside
the first iteration; a caller that FALLS THROUGH to the next candidate when its
block is absent simply keeps looping. Documents are yielded in canonical order
(root before ``.hpc/``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

__all__ = ["INTERVIEW_JSON_RELPATHS", "iter_interview_docs"]

#: The interview.json locations probed, in precedence order: the canonical
#: campaign-dir root first, ``.hpc/interview.json`` accepted defensively.
INTERVIEW_JSON_RELPATHS: tuple[str, ...] = ("interview.json", ".hpc/interview.json")


def iter_interview_docs(experiment_dir: Path | str) -> Iterator[dict[str, Any]]:
    """Yield each parseable interview.json object under *experiment_dir*, in order.

    Walks :data:`INTERVIEW_JSON_RELPATHS`; for each candidate that is a readable
    file whose contents parse to a JSON object, yields that ``dict``. A missing
    file, an unreadable/corrupt file (``OSError``/``ValueError`` — the latter
    covers ``json.JSONDecodeError``), or a non-object top level is skipped
    silently. The caller does its own block extraction on each yielded document.
    """
    base = Path(experiment_dir)
    for rel in INTERVIEW_JSON_RELPATHS:
        path = base / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            yield doc
