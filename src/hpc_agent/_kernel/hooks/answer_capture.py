"""``PostToolUse`` hook — capture TYPED AskUserQuestion answers as utterances.

Sibling of :mod:`hpc_agent._kernel.hooks.utterance_capture`, closing proving
run #5's second capture gap: answers given through the harness's
``AskUserQuestion`` selector never pass ``UserPromptSubmit``, so a human who
TYPED the sweep into the question tool's free-text field ("Other") was
invisible to the authorship gate — the funnel's own interview channel
produced values the lock then refused. Claude Code runs this as a
``command`` hook wired into ``hooks.PostToolUse`` with ``matcher:
"AskUserQuestion"`` (see :func:`hpc_agent.agent_assets.install_agent_assets`);
the payload arrives as JSON on **stdin** and the hook prints **nothing**.

What counts as human-authored — the laundering line
---------------------------------------------------
Only answer text the human TYPED is captured: an answer that does not match
any option label the agent offered (the "Other" free-text path), plus any
free-text notes in ``annotations``. A CLICK on an agent-authored option label
is deliberately NOT captured: the agent wrote that text, and logging it as a
human utterance would reopen exactly the bare-``y``-laundering channel the
utterance lock exists to close (present a fabricated sweep as an option, have
the human click it, harvest the tokens). A multi-select answer composed
entirely of offered labels is likewise skipped; if any part was typed, the
whole answer string is captured verbatim.

Defensiveness
-------------
Mirrors the sibling hook: no scaffolding (:func:`~hpc_agent.state.utterances.
append_utterance` is non-creating of the journal namespace, so a question
answered in a non-hpc repo leaves zero footprint), size-capped storage,
silent stdout, and a clean exit-0 no-op on any malformed payload or write
error — a broken capture channel degrades to the pre-hook friction posture,
never wedges the tool call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

__all__ = ["capture", "main"]

_TOOL_NAME = "AskUserQuestion"


def _offered_labels(tool_input: Any) -> set[str]:
    """Every option label the agent offered, across all questions."""
    labels: set[str] = set()
    if not isinstance(tool_input, dict):
        return labels
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return labels
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = question.get("options")
        if not isinstance(options, list):
            continue
        for option in options:
            if isinstance(option, dict) and isinstance(option.get("label"), str):
                labels.add(option["label"].strip())
    return labels


def _is_clicked(answer: str, labels: set[str]) -> bool:
    """True when *answer* is composed entirely of offered option labels.

    A single-select click IS a label; a multi-select click is the selected
    labels joined by ``", "``. Recognising that join by naive comma-splitting
    breaks the moment a label itself contains a comma (finding #22): the split
    fragments no longer match any offered label, so a clicked, agent-authored
    option would be mis-read as human-typed and laundered into the authorship
    gate's trust anchor. Match structurally instead — the answer is a click iff
    it decomposes left-to-right into offered labels separated by the ``", "``
    join delimiter, trying the LONGEST offered label first so a label that
    itself contains ``", "`` is consumed before its fragments. Any residue that
    is not an offered label means the human typed something — the whole answer
    then counts as typed.
    """
    text = answer.strip()
    if text in labels:
        return True
    if not labels:
        return False
    ordered = sorted((lbl for lbl in labels if lbl), key=len, reverse=True)
    remaining = text
    matched_any = False
    while remaining:
        for label in ordered:
            if remaining == label:
                remaining = ""
                matched_any = True
                break
            if remaining.startswith(label + ", "):
                remaining = remaining[len(label) + 2 :]
                matched_any = True
                break
        else:  # no offered label consumes the head → typed residue
            return False
    return matched_any


def _typed_texts(payload: dict[str, Any]) -> list[str]:
    """The human-TYPED strings in an AskUserQuestion payload, in order."""
    tool_input = payload.get("tool_input")
    labels = _offered_labels(tool_input)

    texts: list[str] = []
    sources: list[Any] = []
    if isinstance(tool_input, dict):
        sources.append(tool_input.get("answers"))
        annotations = tool_input.get("annotations")
        if isinstance(annotations, dict):
            for annotation in annotations.values():
                if isinstance(annotation, dict) and isinstance(annotation.get("notes"), str):
                    texts.append(annotation["notes"])
    response = payload.get("tool_response")
    if isinstance(response, dict):
        sources.append(response.get("answers"))

    seen: set[str] = set(texts)
    for answers in sources:
        if not isinstance(answers, dict):
            continue
        for answer in answers.values():
            if not isinstance(answer, str) or not answer.strip():
                continue
            if _is_clicked(answer, labels) or answer in seen:
                continue
            seen.add(answer)
            texts.append(answer)
    return [t for t in texts if t.strip()]


def capture(payload: Any) -> list[dict[str, Any]]:
    """Pure core: append each typed answer to the cwd repo's utterance log.

    Returns the appended records (``[]`` when the payload is not an
    ``AskUserQuestion`` PostToolUse mapping, every answer was a click on an
    agent-authored option, or the cwd repo has no journal namespace — all
    clean no-ops).

    A valid ``HPC_ACTOR`` slug in the hook process env
    (:func:`hpc_agent.infra.env_flags.env_actor`, MH2/MH4) attributes each
    typed answer to the actor-suffixed locator; unset/invalid → the unsuffixed
    path, byte-identical to the single-actor world.
    """
    from hpc_agent.infra.env_flags import env_actor
    from hpc_agent.state.utterances import append_utterance

    if not isinstance(payload, dict) or payload.get("tool_name") != _TOOL_NAME:
        return []
    cwd = payload.get("cwd")
    cwd_dir = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())
    # Seam: MT1 adds ``actor=`` to ``append_utterance``. Pass it only when set
    # so this call works both before and after MT1 lands.
    actor = env_actor()
    kwargs = {"actor": actor} if actor else {}
    records = []
    for text in _typed_texts(payload):
        record = append_utterance(cwd_dir, text, **kwargs)
        if record is not None:
            records.append(record)
    return records


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, write log, never crash.

    Mirrors :func:`utterance_capture.main`: silent, exit 0 on every path.
    """
    del argv
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else None
    except (json.JSONDecodeError, ValueError):
        return 0

    try:
        capture(payload)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the harness
    raise SystemExit(main())
