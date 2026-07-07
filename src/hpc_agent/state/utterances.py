"""Utterance log — harness-captured human prompts, per journal namespace.

The trust anchor for the human-authorship gate
(:func:`hpc_agent.ops.decision.journal._assert_human_authorship`). The gate's
v1 verified REQUIRED_CALLER value tokens against decision-journal ``response``
fields — text the driving agent itself writes, so "a guard the LLM itself
satisfies is not a guard" (engineering-principles) applied in full. This store
closes that loop: a ``UserPromptSubmit`` hook
(:mod:`hpc_agent._kernel.hooks.utterance_capture`) appends each human prompt
here **out-of-band** — the harness, not the model, is the writer — so the gate
can require value tokens to derive from text a human verifiably typed.

Storage::

    <journal home>/<repo_hash>/utterances.jsonl     # append-only

One JSON object per line: ``{"ts": <iso>, "sha256": <full-text digest>,
"text": <raw prompt, size-capped>}``. ``sha256`` is always over the FULL raw
prompt, so a capped entry still carries a verifiable fingerprint of what the
human sent.

No-scaffold discipline (the ``notify._alerts_paths`` pattern): the capture
hook is installed user-globally and fires in ANY repo the user works in, so
the writer must never create a ``<repo_hash>/`` namespace for an arbitrary
cwd (proving-run #3 finding g — leaked namespace dirs). An append lands only
when the namespace directory already exists — i.e. the cwd is a repo some
hpc-agent state write already claimed. Reads are equally non-creating.

Fail-open everywhere: a missing namespace, an unwritable log, or a corrupt
line degrades to "no utterances", never an exception — a broken capture
channel must degrade to the v1 friction posture, not wedge the harness.

HARNESS WRITE API
-----------------
This module is the reference implementation of the utterance-log write API
normatively specified in ``docs/internals/harness-contract.md`` (capability 1
+ §2). :func:`append_utterance` is the SOLE writer; a SECOND conforming harness
(the scheduled v1.5 jupytext render — a notebook sign-off cell IS out-of-band
from the LLM) writes records the reader here accepts. The API is importable by
HARNESS-SIDE code only.

The obligations a conforming writer honors, all embodied below — change them
here and the contract doc + a second harness drift:

* the storage locator (:func:`utterances_path`) reusing
  ``state.run_record._current_homedir`` + ``repo_hash`` (never a re-implemented
  hash);
* the FROZEN record schema — ``{ts, sha256, text}``, sorted-keys JSON, one per
  line, append-only, oldest-first; ``sha256`` over the FULL raw text, ``text``
  capped at :data:`MAX_UTTERANCE_BYTES` on a CODEPOINT boundary;
* the NO-SCAFFOLD precondition (write only into an existing namespace dir);
* the PROVENANCE contract (only human-TYPED text; the writer runs out-of-band
  from the LLM's tool surface and filters harness-injected / agent-authored
  text — the ``_HARNESS_INJECTION_RE`` / ``_is_clicked`` reference filters in
  ``_kernel.hooks.utterance_capture`` / ``.answer_capture``);
* FAIL-OPEN (any error → clean no-op, degrading to the friction tier).

**The LLM must never gain a sanctioned write call.** There is deliberately NO
CLI verb, MCP tool, or primitive that writes an utterance — appending one is the
harness's exclusive, out-of-band act. A write verb would let the model author
its own authorship evidence, exactly the laundering channel the authorship gate
exists to close (a guard the LLM itself satisfies is not a guard). The absence
of any utterance-writing verb is pinned by the contract test in
``tests/contracts/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "MAX_UTTERANCE_BYTES",
    "append_utterance",
    "read_utterances",
    "utterances_path",
]

# Per-entry cap on the stored prompt text (UTF-8 bytes). A pasted log dump or
# giant prompt is truncated for storage; the sha256 still covers the full raw
# text so the entry remains a verifiable fingerprint of the whole utterance.
MAX_UTTERANCE_BYTES = 4096

_UTTERANCES_NAME = "utterances.jsonl"

_log = logging.getLogger(__name__)


def utterances_path(experiment_dir: Path) -> Path:
    """``<journal home>/<repo_hash>/utterances.jsonl`` — WITHOUT creating.

    Deliberately not :func:`~hpc_agent.state.run_record.journal_dir` (which
    mkdirs the namespace + writes ``repo.json``): both the capture hook and
    the authorship-gate reader run in arbitrary cwds and must never scaffold
    a journal namespace there (the ``notify._alerts_paths`` precedent).
    """
    from hpc_agent.state.run_record import _current_homedir, repo_hash

    return _current_homedir() / repo_hash(experiment_dir) / _UTTERANCES_NAME


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes, never mid-codepoint."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def append_utterance(experiment_dir: Path, text: str) -> dict[str, Any] | None:
    """Append one human prompt to the repo's utterance log; return the record.

    Returns ``None`` (a clean no-op) when:

    * *text* is empty — nothing to attest;
    * the journal namespace for *experiment_dir* does not exist yet — the
      no-scaffold rule: a user-global hook firing in a non-hpc repo must not
      leak ``<repo_hash>/`` directories (see the module docstring);
    * any filesystem error occurs — fail-open, the capture channel degrades
      rather than breaking the harness.

    The stored ``text`` is capped at :data:`MAX_UTTERANCE_BYTES`; ``sha256``
    always digests the FULL raw text.
    """
    if not text:
        return None
    try:
        path = utterances_path(experiment_dir)
        if not path.parent.is_dir():
            return None
        from hpc_agent.infra.time import utcnow_iso

        record: dict[str, Any] = {
            "ts": utcnow_iso(),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text": _truncate_utf8(text, MAX_UTTERANCE_BYTES),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return record
    except (OSError, ValueError, UnicodeError):
        return None


def read_utterances(experiment_dir: Path) -> list[dict[str, Any]]:
    """Every logged utterance for *experiment_dir*, oldest first — non-creating.

    Returns ``[]`` when the log (or the namespace) does not exist. Blank and
    individually-corrupt lines are skipped — one bad line never strands the
    rest of the trail (the decision-journal read discipline).
    """
    try:
        path = utterances_path(experiment_dir)
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _log.warning("utterances: skipping unreadable log (%s)", exc)
        return []
    records: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records
