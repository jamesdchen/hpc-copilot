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
  ``|``, ``&``, newlines), after skipping subshell parens, env-assignment
  prefixes, leading redirections (``>out qsub``, ``2>err qsub``), and benign
  wrappers (``time``, ``timeout``, ``nohup``, ``env``, ``nice``, ``stdbuf``,
  plus the transparently-exec'ing ``exec``/``command``) INCLUDING their
  flags/option values — the wrappers' canonical usage carries flags
  (``nice -n 10``, ``timeout -k 5 60``, ``stdbuf -oL``), and a flag must not
  hide the executing verb behind them;
* anywhere in the remote/argument command of an ``ssh``/``bash -c``/
  ``bash -lc`` — or an ``eval``/``xargs`` — segment: the transport/indirection
  nuance: ``ssh host qdel 1``, ``eval "qsub …"``, ``xargs qsub`` must be caught
  even though the fenced verb is not the local first token. Inner shell strings
  are recursed into; an unparseable inner string falls back to a word-boundary
  scan (fail-closed for the transport case);
* inside a command substitution or non-head subshell group — ``echo
  $(qsub …)`` executes qsub even though the head is ``echo`` — whose enclosed
  tokens are analysed as their own segment.

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

# Wrappers the fence sees through to the real command. Their flags and
# option/duration values (``nice -n 10``, ``timeout -k 5 60``, ``stdbuf -oL``)
# are skipped too — see :func:`_first_real_token`. ``exec`` and ``command``
# transparently exec the following verb (``exec qsub``, ``command sbatch``),
# so the real verb must become the head (finding #24).
_SKIP_WRAPPERS = frozenset({"time", "timeout", "nohup", "env", "nice", "stdbuf", "exec", "command"})

_SEGMENT_SPLIT = re.compile(r"(?:\|\||&&|[;|&\n])")
_WORD_FENCED = re.compile(r"(?<![\w./-])(" + "|".join(sorted(FENCED)) + r")(?![\w.-])")
# A wrapper's non-flag option value: a nice level, a signal number, or a
# timeout duration (``10``, ``5``, ``60s``) — never an executable name.
_WRAPPER_VALUE = re.compile(r"\d+(\.\d+)?[smhd]?", re.IGNORECASE)


def _is_redir_op(tok: str) -> bool:
    """A redirection operator token (``>``, ``>>``, ``<``, ``&>``, ``>&`` ...).

    The ``punctuation_chars`` lexer emits a run of ``<>&`` as one token; a bare
    ``&`` (background) has no ``<``/``>`` and is a segment operator, not a
    redirection, so it is excluded here.
    """
    return bool(tok) and all(c in "<>&" for c in tok) and ("<" in tok or ">" in tok)


def _first_real_token(tokens: list[str]) -> tuple[str | None, int]:
    """The executing token of a segment (skipping env prefixes/wrappers) + index."""
    i = 0
    in_wrapper = False
    while i < len(tokens):
        tok = tokens[i]
        # Redirection: ``>out``, ``>> log``, ``2> err`` — the shell performs the
        # redirection and executes the FOLLOWING command, so a leading redirect
        # must not become the head (finding #24). Skip the (optional fd) +
        # operator + target filename.
        if _is_redir_op(tok):
            i += 1
            if i < len(tokens) and not _is_redir_op(tokens[i]):
                i += 1  # the redirection target filename
            continue
        if tok.isdigit() and i + 1 < len(tokens) and _is_redir_op(tokens[i + 1]):
            i += 2  # a fd-prefixed redirection: ``2 > err``
            if i < len(tokens) and not _is_redir_op(tokens[i]):
                i += 1
            continue
        if tok and not tok.strip("("):  # subshell paren(s) — the command follows
            i += 1
            continue
        if "=" in tok and not tok.startswith(("=", "-")):  # VAR=value prefix
            i += 1
            continue
        base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if base in _SKIP_WRAPPERS:
            in_wrapper = True
            i += 1
            continue
        if in_wrapper and (tok.startswith("-") or _WRAPPER_VALUE.fullmatch(tok)):
            # The wrapper's flag or option/duration value, not the real
            # command (``nice -n 10 CMD``, ``timeout -k 5 60 CMD``).
            i += 1
            continue
        return base, i
    return None, len(tokens)


#: Shell operator tokens that end one command segment (as emitted by a
#: ``punctuation_chars`` lexer — quoted operators never appear as these).
_OPERATOR_TOKENS = frozenset({";", "|", "&", "&&", "||", ";;", "|&"})


def _analyze_tokens(tokens: list[str]) -> str | None:
    """The fenced verb one token-segment would execute, or None."""
    head, idx = _first_real_token(tokens)
    if head is not None:
        if head in FENCED:
            return head
        # Transport/indirection case: the remote/inner/argument command may
        # execute a fenced verb even though the local head is ssh/bash/eval/
        # xargs. Recurse into every subsequent token (ssh flags are fenced-
        # free; string args that ARE shell commands get re-analyzed; bare
        # fenced tokens block). ``eval "qsub …"`` and ``xargs qsub`` run the
        # following tokens as a command, so they join the transport branch
        # (finding #24).
        if head in ("ssh", "bash", "sh", "zsh", "eval", "xargs"):
            for tok in tokens[idx + 1 :]:
                base = tok.rsplit("/", 1)[-1].lower()
                if base in FENCED:
                    return base
                if _WORD_FENCED.search(tok):
                    inner = _fenced_in_command(tok)
                    if inner:
                        return inner
    # Command substitution / subshell group in NON-head position:
    # ``echo $(qsub …)`` executes qsub inside the group even though the head
    # is ``echo``. Analyse each group's enclosed tokens as their own segment
    # (finding #24). A group that IS the head (``(qdel 123)``) is already
    # caught by the paren-skip in _first_real_token.
    return _fenced_in_substitution(tokens)


def _fenced_in_substitution(tokens: list[str]) -> str | None:
    """The fenced verb a ``(...)`` group (command substitution or subshell)
    would execute, or None. Scans for the innermost enclosed run and analyses
    it as its own segment."""
    depth = 0
    group: list[str] = []
    for tok in tokens:
        if tok == "(":
            depth += 1
            if depth == 1:
                group = []
                continue
        if depth > 0:
            if tok == ")":
                depth -= 1
                if depth == 0:
                    verb = _analyze_tokens(group)
                    if verb:
                        return verb
                    continue
            group.append(tok)
    return None


def _fenced_in_command(command: str) -> str | None:
    """The fenced verb *command* would execute, or None when it is clean.

    QUOTE-AWARE FIRST (run-#10 false positive: a read-only ``grep`` whose
    quoted pattern contained a fenced verb and a ``|`` was regex-split MID-
    QUOTE, failed ``shlex``, and hit the fail-closed fallback). The primary
    path tokenizes each line with a ``punctuation_chars`` lexer — operators
    inside quotes stay inside their token, real operators come out as
    standalone boundary tokens — so ``grep "qsub|sbatch" log`` is one clean
    segment headed by ``grep``. The legacy regex-split path (with its
    fail-closed word-scan) remains ONLY for lines the lexer cannot parse.
    """
    for line in command.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            tokens = list(lex)
        except ValueError:
            verb = _fenced_in_line_legacy(line)
            if verb:
                return verb
            continue
        segment: list[str] = []
        for tok in [*tokens, ";"]:
            if tok in _OPERATOR_TOKENS:
                verb = _analyze_tokens(segment)
                if verb:
                    return verb
                segment = []
            else:
                segment.append(tok)
    return None


def _fenced_in_line_legacy(line: str) -> str | None:
    """The pre-quote-aware analysis, kept for unparseable lines only."""
    for segment in _SEGMENT_SPLIT.split(line):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            # Unbalanced quotes in an already-unparseable line. Fail CLOSED
            # on the transport case: any word-boundary fenced verb blocks.
            hit = _WORD_FENCED.search(segment)
            if hit:
                return hit.group(1)
            continue
        verb = _analyze_tokens(tokens)
        if verb:
            return verb
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
