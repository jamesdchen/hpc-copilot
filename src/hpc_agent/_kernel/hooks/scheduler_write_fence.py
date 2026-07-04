"""``PreToolUse`` hook — block mutating scheduler commands from the agent.

Conduct rule 7 mechanized (proving-run-3 finding (d), policy decided by James
2026-07-04): **consequences are gated, curiosity isn't.** The driving agent may
gather information freely — ``ssh``, ``qstat``/``squeue``/``qacct``, any
read-only probe — but the consequence-bearing scheduler verbs (``qsub``,
``sbatch``, ``qdel``, ``scancel``, ``qmod``, ``qalter``) belong to code (the
blocks) exclusively. Before this hook, prose was the only guard, and prose
drifts with every model/harness update.

Harness-mediated (a ``command`` hook in ``hooks.PreToolUse``, matcher
``Bash``), not a ``@primitive``. Receives the PreToolUse payload as JSON on
stdin; **exit 2 blocks the tool call** and stderr is surfaced to the agent as
the reason. A bash-level ``case`` pre-filter in the registered command keeps
the common path at builtin cost — only payloads mentioning a fenced verb reach
Python at all.

Why command-position analysis (not a bare substring match): the pre-filter's
substring hit may be an innocent argument — ``grep qsub log``, ``hpc-agent
describe submit-flow`` mentioning ``qsub`` in a help string, ``echo qdel``.
The fence blocks only when a fenced verb can actually EXECUTE:

* first token of any shell segment (segments split on ``;``, ``&&``, ``||``,
  ``|``, ``&``, newlines), after skipping env-assignment prefixes and benign
  wrappers (``time``, ``timeout <n>``, ``nohup``, ``env``, ``nice``);
* anywhere in the remote command of an ``ssh``/``bash -c``/``bash -lc``
  segment — the transport nuance: ``ssh host qdel 1`` must be caught even
  though ``qdel`` is not the local first token. Inner shell strings are
  recursed into; an unparseable inner string falls back to a word-boundary
  scan (fail-closed for the transport case).

The hpc-agent CLI itself is never fenced: the blocks run scheduler commands
REMOTELY through ``ssh_run`` inside Python — their Bash command line is
``hpc-agent <verb> ...``, which carries no fenced token.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

FENCED = frozenset({"qsub", "sbatch", "qdel", "scancel", "qmod", "qalter"})

# Wrappers whose next token is the real command.
_SKIP_WRAPPERS = frozenset({"time", "nohup", "env", "nice", "stdbuf"})
# Wrappers that consume ONE argument before the real command.
_SKIP_WITH_ARG = frozenset({"timeout"})

_SEGMENT_SPLIT = re.compile(r"(?:\|\||&&|[;|&\n])")
_WORD_FENCED = re.compile(r"(?<![\w./-])(" + "|".join(sorted(FENCED)) + r")(?![\w.-])")


def _first_real_token(tokens: list[str]) -> tuple[str | None, int]:
    """The executing token of a segment (skipping env prefixes/wrappers) + index."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "=" in tok and not tok.startswith(("=", "-")):  # VAR=value prefix
            i += 1
            continue
        base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if base in _SKIP_WRAPPERS:
            i += 1
            continue
        if base in _SKIP_WITH_ARG:
            i += 2
            continue
        return base, i
    return None, len(tokens)


def _fenced_in_command(command: str) -> str | None:
    """The fenced verb *command* would execute, or None when it is clean."""
    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            # Unbalanced quotes (often a segment-split artifact of a larger
            # quoted string). Fail CLOSED on the transport case: any
            # word-boundary fenced verb in the raw text blocks.
            hit = _WORD_FENCED.search(segment)
            if hit:
                return hit.group(1)
            continue
        head, idx = _first_real_token(tokens)
        if head is None:
            continue
        if head in FENCED:
            return head
        # Transport/nested-shell case: the remote/inner command may execute a
        # fenced verb even though the local head is ssh/bash. Recurse into
        # every subsequent token (ssh flags are fenced-free; string args that
        # ARE shell commands get re-analyzed; bare fenced tokens block).
        if head in ("ssh", "bash", "sh", "zsh"):
            for tok in tokens[idx + 1 :]:
                base = tok.rsplit("/", 1)[-1].lower()
                if base in FENCED:
                    return base
                if _WORD_FENCED.search(tok):
                    inner = _fenced_in_command(tok)
                    if inner:
                        return inner
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0  # malformed payload: never wedge the harness on the fence
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not command:
        return 0
    verb = _fenced_in_command(command)
    if verb is None:
        return 0
    sys.stderr.write(
        f"scheduler-write-fence: `{verb}` is a mutating scheduler command — "
        "the agent never runs these (code owns cluster actions; design "
        "human-amplification-blocks §1, conduct rule 7). Submit/cancel through "
        "the block verbs (`submit-s2`/`submit-s3`, `hpc-agent kill`), which "
        "gate on a journaled human greenlight. Read-only probes (qstat/"
        "squeue/qacct, plain ssh) are allowed — re-run without the mutating "
        "verb if you only meant to look."
    )
    return 2  # PreToolUse contract: exit 2 blocks the tool call


if __name__ == "__main__":
    raise SystemExit(main())
