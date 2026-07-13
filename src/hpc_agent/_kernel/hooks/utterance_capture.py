"""``UserPromptSubmit`` hook — capture each human prompt into the utterance log.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s
``hooks.UserPromptSubmit`` array (see
:func:`hpc_agent.agent_assets.install_agent_assets`). It fires the moment the
human submits a prompt — before the model sees it — receives the payload as
JSON on **stdin**, and appends the prompt to the per-repo utterance log
(:mod:`hpc_agent.state.utterances`). It prints **nothing**: UserPromptSubmit
stdout is injected into the model's context, and this hook's whole point is to
write a record the model never mediates.

Why it exists
-------------
Proving run #4: the human-authorship gate
(:func:`hpc_agent.ops.decision.journal._assert_human_authorship`) verified
REQUIRED_CALLER value tokens against decision-journal ``response`` fields —
which the driving agent itself writes. Friction, not a lock: the same model
that fabricates a value can fabricate the quote. This hook is the out-of-band
capture that upgrades the gate: the HARNESS writes each human utterance
(ts + sha256 + raw text) to ``<journal home>/<repo_hash>/utterances.jsonl``,
and the gate then requires value tokens to derive from that log — text a
human verifiably typed — falling back to the journal-response friction
posture only where no log exists.

Defensiveness
-------------
* **No scaffolding** (the ``alert_count`` pattern): the hook is installed
  user-globally and fires in every repo; the append is a silent no-op unless
  the journal namespace for the payload's ``cwd`` already exists
  (:func:`hpc_agent.state.utterances.append_utterance` is non-creating of the
  namespace). A prompt typed in a non-hpc project leaves zero footprint.
* **Human-typed only** (proving run #5): harness-injected user turns —
  ``<task-notification>`` blocks, system reminders, local-command echoes —
  also fire ``UserPromptSubmit`` and were logged as "human" text. A prompt
  opening with a harness tag is dropped: notification text is
  agent-influenced, so admitting it would hand the authorship gate's trust
  anchor back to the model.
* **Size-capped**: stored text is capped (~4KB/entry); the sha256 always
  covers the full raw prompt.
* For a malformed payload, an empty prompt, or any write error it is a clean
  no-op — prints nothing, exits ``0``. It never raises and never exits
  non-zero: a broken capture hook must degrade to the pre-hook friction
  posture, never wedge prompt submission.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

__all__ = ["capture", "main"]

# Harness-injected user turns (background-task notifications, system
# reminders, local-command echoes) fire ``UserPromptSubmit`` too — proving
# run #5 logged a ``<task-notification>`` block as a "human utterance". That
# text is NOT human-typed, and pieces of it are agent-influenced (a
# background command's description/summary), so letting it into the log is a
# laundering channel into the authorship gate's trust anchor. A payload whose
# prompt OPENS with one of these tags is dropped; a human prompt merely
# quoting a tag mid-text still lands. The filter is the write-API's PUBLIC
# reference symbol (``state.utterances.is_harness_injected`` — one
# definition every conforming writer shares, per
# ``docs/internals/harness-contract.md``); this hook is its Claude Code
# UserPromptSubmit binding.


def capture(payload: Any) -> dict[str, Any] | None:
    """Pure core: append the payload's prompt to the cwd repo's utterance log.

    Returns the appended record, or ``None`` when the payload is not a
    mapping, carries no non-empty string ``prompt``, opens with a
    harness-injection tag (:func:`hpc_agent.state.utterances.is_harness_injected`
    — not human-typed), or the cwd repo has no journal namespace
    (no-scaffold rule) — all clean no-ops.

    When the hook process env sets a valid ``HPC_ACTOR`` slug
    (:func:`hpc_agent.infra.env_flags.env_actor`, MH2/MH4), the prompt is
    attributed — appended to the actor-suffixed locator. Unset or an invalid
    slug → the unsuffixed path, byte-identical to the single-actor world.
    """
    from hpc_agent.infra.env_flags import env_actor
    from hpc_agent.state.utterances import append_utterance, is_harness_injected

    if not isinstance(payload, dict):
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if is_harness_injected(prompt):
        return None
    cwd = payload.get("cwd")
    cwd_dir = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())
    # The actor kwarg is passed only when a declared actor resolves (the tests
    # pin the omitted-when-unset call shape). Annotated dict[str, Any]: a bare
    # dict[str, str] splat maps onto the keyword-only ``bound`` param under
    # mypy's invariance and broke CI's whole-tree check.
    actor = env_actor()
    kwargs: dict[str, Any] = {"actor": actor} if actor else {}
    return append_utterance(cwd_dir, prompt, **kwargs)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, write log, never crash.

    Mirrors the sibling hook entrypoints: any unexpected error is swallowed
    and reported as a clean no-op (exit ``0``), and nothing is ever printed
    (UserPromptSubmit stdout would be injected into the model's context —
    this hook's record must stay out-of-band). ``argv`` is accepted for
    symmetry and unused.
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
