"""``Stop`` hook — audit the final relay against the journal (conduct rule 10).

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.Stop`` array
(see :func:`hpc_agent.agent_assets.install_agent_assets`). It is invoked when
the agent is about to end its turn, receives the Stop payload as JSON on
**stdin**, and may emit ``{"decision": "block", "reason": ...}`` on **stdout**
to make the agent continue instead.

Why it exists
-------------
``verify-relay`` (:mod:`hpc_agent.ops.decision.verify_relay`) mechanized rule
10 — "never relay numbers/state that don't match the journal" — as a pure
audit verb, but nothing made a driving agent RUN it: the verb-only MVP was
explicitly staged, and an unaudited relay still reached the human (proving run
#3: "running" relayed while the journal said "failed"). ``Stop`` is the
cheapest sound seam: it fires exactly once, at the exact moment the outgoing
message is final, with the transcript on disk — so deterministic code can diff
the final text against the durable records before the human reads it.

Behaviour
---------
On a Stop event the hook:

1. resolves the cwd repo's journal namespace **without creating it** (the
   ``alert_count`` no-scaffold pattern) — no namespace → not an hpc repo →
   silent pass;
2. reads the session transcript (``transcript_path``) and extracts the final
   assistant message text (the trailing run of assistant entries);
3. finds which journaled run ids AND notebook audit ids the text actually
   mentions — number/state/status claims are only attributable to a run/audit
   the relay names, so a final message naming neither is a silent pass;
4. runs :func:`~hpc_agent.ops.decision.verify_relay.verify_relay` in-process
   for each mentioned run, and
   :func:`~hpc_agent.ops.decision.verify_relay.verify_notebook_relay` for each
   mentioned audit (the hook idiom: hook modules import the ops function
   directly — ``alert_count`` → ``notify``, the stop guards →
   ``skill_returns`` — rather than shelling out to a second subprocess). The
   notebook path does ZERO work — not even a journal read — when the final
   message names no audit;
5. on **contradiction** mismatches (``number`` / ``state`` / ``run_id``),
   blocks the stop once with the itemized mismatch summary as the reason, so
   the agent corrects the relay to match the journal. A notebook relay reuses
   these kinds (a wrong section status / module ``passed`` verdict → ``state``;
   a mismatched sha-hex → ``number``), so no new blocking kind is introduced.
   ``unverifiable`` claims are NOT surfaced here: a final message legitimately
   carries numbers the run's records never saw (test counts, line numbers), and
   a notebook claim whose ``.py`` source cannot resolve is likewise
   unverifiable, not a contradiction; the hook is a seatbelt against
   *contradicting* the durable record, and the useful-conservative unverifiable
   policy stays a verb-level concern.

Loop safety & defensiveness
---------------------------
* ``stop_hook_active`` → clean no-op: the hook blocks a given stop at most
  once, never loops, and never hard-blocks a session — after one forced
  continuation the corrected (or even uncorrected) relay goes through. This
  matches the sibling Stop guards exactly.
* Fail-open everywhere: no journal namespace, a missing/unreadable
  transcript, no run mentions, a per-run audit error, or any unexpected
  exception → silent pass, exit ``0``. A broken audit hook must degrade to
  the verb-only posture, never wedge the harness.

The relay-due DISCHARGE pass (the omission gate)
------------------------------------------------
Steps 3–5 audit what WAS said (distortion); nothing above enforces what MUST
be said (tonight's proving run: ``notebook-status`` computed ``passed`` and
the agent never relayed it). So the same stop also runs the discharge pass —
the omission-side complement:

* ``notebook-status`` journals a relay-due MARKER on a TERMINAL verdict, and
  ``notebook-audit-view`` journals a per-section MARKER (key token: the
  section's ``view_sha12``) when it builds the CANONICAL view of a
  human-required section (run-#11 item 3 — a render that reached the human as
  an unread file link is not a relay) (:mod:`hpc_agent.state.notebook_audit` —
  the deliberately narrow set: preview views and auto_cleared sections arm
  nothing);
* at stop, the hook loads the UNDISCHARGED markers of every audit journal in
  the SAME ``.hpc/notebooks`` dir the mention scan uses (the identical
  experiment-dir resolution — payload ``cwd`` → no-scaffold raw path). Unlike
  the mention-keyed passes above, this pass cannot be keyed on what the text
  names — an omission names nothing — so it scans the journals directly
  (capped);
* a marker whose key tokens (the state word / the module sha12) appear in the
  final text — plain substring, case-insensitive — is DISCHARGED (an appended
  record; the marker itself is never mutated). This runs even on a
  ``stop_hook_active`` forced continuation, so the corrected relay closes its
  own obligation at the very stop that carries it;
* a marker whose tokens are all absent blocks the stop ONCE — the same
  ``stop_hook_active`` block-once seam as the contradiction pass — with a
  verbatim-ready reason naming the undischarged verdict;
* the whole pass is fail-open: ANY exception in marker load/parse/check/
  discharge degrades to no-omission-findings (a hook that can wedge a session
  on one bad record is the failure class this posture exists to prevent).

Sign-off echo detection — laundered authorship (run-#11 queue item 2)
---------------------------------------------------------------------
The audit skill's "never compose the sign-off utterance" ban is conduct prose
with no code seat: a driving agent can DRAFT the very words the human then
pastes as their typed attestation, and the journaled ``notebook-sign-off``
record then reads as human-authored review it never was (the F-R number-word
class in reverse — F-R catches the model restating REJECTED content; this
catches the human restating MODEL-DRAFTED attestation). The same stop already
has both halves on disk, so :func:`_sign_off_echo_findings` flags a journaled
sign-off whose ``response`` echoes a *prior* assistant-authored line —
verbatim (whitespace-normalized substring) or near (conservative token
containment). Deliberate limits, all biased against a false block (a wrongly
laundered flag on an honest human is worse than a miss):

* only the LATEST sign-off per audit is checked (the freshest attestation is
  the one that could just have been laundered; this also bounds re-firing);
* the FINAL relay message is excluded from the corpus — a stop that legitimately
  QUOTES the response back while relaying it is not laundering (only a *prior*,
  pre-sign-off assistant line is);
* a minimum length (chars AND tokens) floors out short responses ("y", "ok",
  "looks good") that collide by chance; near-match needs high token containment.

Decision-state claims — an unjournaled decision EVENT (run-#11 queue item 5)
---------------------------------------------------------------------------
verify-relay audits the run's *numbers/status*; nothing audited a claim about a
DECISION EVENT — "revoked", "superseded", "greenlit", "journaled" (run #11: the
relay "your y is revoked and nothing has advanced" with ZERO journal record of
any revocation). :func:`_decision_state_findings` mirrors the rule-10 matching:
a decision-state verb is only attributable to a scope the relay NAMES, and must
be supported by that scope's decision journal — a positive verb needs a committed
greenlight, a revocation/supersession verb needs the journal to actually show the
greenlight no longer standing. An unsupported claim joins the rule-10 findings so
it carries the standard correct-the-relay remedy. Conservative by construction:
the verb and the scope id must share a LINE (so "the token was revoked" next to
an unrelated run id does not fire), and a scope-less claim is a deliberate miss.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

__all__ = [
    "build_hook_output",
    "final_assistant_text",
    "main",
    "mentioned_audit_ids",
    "mentioned_run_ids",
]

# Cap how many mentioned runs / audits one stop audits — the hook must stay cheap.
_MAX_RUNS_AUDITED = 5
_MAX_AUDITS_AUDITED = 5

# Caps for the relay-due discharge pass (which scans journals rather than
# keying on mentions — an omission names nothing): audits scanned per stop and
# undischarged markers surfaced per stop. Same stay-cheap posture as above.
_MAX_RELAY_DUE_AUDITS = 10
_MAX_RELAY_DUE_FINDINGS = 5

# Mismatch kinds that contradict the durable record (surfaced); the
# ``unverifiable`` kind is deliberately excluded (see module docstring). The
# notebook-audit relay (T11) deliberately REUSES these kinds — a wrong section
# status / module ``passed`` verdict is a ``state`` contradiction, a mismatched
# sha-hex a ``number`` one — so no new kind is added to the blocking set (and no
# wire-enum / schema change): the semantics stay coherent (a status IS a
# lifecycle-family claim; a sha is a value claim).
_CONTRADICTION_KINDS = frozenset({"number", "state", "run_id"})

# ─── the completer (rejector → completer; docs/design/stop-hook-completer.md) ──
#
# The completer path is CAPABILITY-GATED (D1): it is active only when the harness
# declares the ``stop-hook-append`` capability (a hook ``systemMessage`` it
# DISPLAYS). Absent/unknown — the default, since no harness declares it yet — the
# whole module degrades to today's REJECTOR EXACTLY (the block-once bounce). So
# every structure below is fully built but DARK until the capability reads true.

# Append caps (D4, the ``_MAX_*`` posture): a code-appended render is bounded so
# one pathological render cannot flood the turn; over-cap content degrades to the
# token-level floor plus a file reference.
_MAX_APPEND_ARTIFACT_BYTES = 8_000
_MAX_APPEND_RENDER_FILES_SCANNED = 40


class _AbsentMarker(NamedTuple):
    """An undischarged relay-due marker whose key tokens the relay never carried.

    Rejector: :attr:`omission_text` is the verbatim-ready block reason (today's
    string). Completer: :attr:`marker` is the resolved dict from which the owed
    artifact is composed (D4) and the completer-discharge is recorded (D3).
    """

    scope_kind: str
    scope_id: str
    marker: dict[str, Any]
    omission_text: str


class _Violation(NamedTuple):
    """A relayed claim that contradicts the durable record (violation class §2).

    Rejector: :attr:`text` is today's finding line. Completer: appended as a
    code-authored correction UNDER the claim, EXCEPT when the poisoned-decision
    test fires (a run/campaign scope with a still-pending brief whose content the
    claim tokens intersect), where it bounces instead. ``claim``/``journal_value``
    drive the correction and the poisoned intersection; an empty ``claim`` (a
    paraphrase / audit-scope finding) is append-only by construction.
    """

    scope_kind: str
    scope_id: str
    claim: str
    journal_value: str | None
    text: str


def _journal_runs_dir(experiment_dir: Path) -> Path:
    """``<journal home>/<repo_hash>/runs`` — WITHOUT creating (no-scaffold)."""
    from hpc_agent.state.run_record import _current_homedir, repo_hash

    return _current_homedir() / repo_hash(experiment_dir) / "runs"


def _notebook_audits_dir(experiment_dir: Path) -> Path:
    """``<experiment>/.hpc/notebooks`` — WITHOUT creating (no-scaffold).

    Constructed as a raw path (never ``RepoLayout(...).hpc``, which materializes
    the ``.hpc`` tree) so the discovery probe stays side-effect-free — a repo that
    has never run an audit is not scaffolded one by a Stop event.
    """
    return Path(experiment_dir).resolve() / ".hpc" / "notebooks"


def final_assistant_text(transcript_path: Path) -> str:
    """The final assistant message text from a session transcript, or ``""``.

    The transcript is JSONL, one message per line; the final relay is the
    trailing run of ``type == "assistant"`` entries (a single logical reply
    may span several assistant lines). Text blocks are joined in order.
    Tolerant: unreadable file or corrupt lines yield ``""`` / skip the line.
    """
    try:
        text = transcript_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return ""

    entries: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)

    trailing: list[dict[str, Any]] = []
    for entry in reversed(entries):
        if entry.get("type") == "assistant":
            trailing.append(entry)
        elif trailing:
            break
        elif entry.get("type") in ("user", "human", "system"):
            # A non-assistant message before any assistant tail → no final
            # assistant text (the turn ended without a reply?). Keep scanning
            # only while we have not started a tail.
            break
    trailing.reverse()

    parts: list[str] = []
    for entry in trailing:
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block_text = block.get("text")
                if isinstance(block_text, str) and block_text:
                    parts.append(block_text)
    return "\n".join(parts)


def mentioned_run_ids(relay_text: str, runs_dir: Path) -> list[str]:
    """Journaled run ids the relay text actually names, journal order.

    A claim is only attributable to a run the relay mentions, so the audit is
    keyed on substring presence of each ``<runs>/<run_id>.json`` stem in the
    final text. Filesystem errors yield an empty list (fail-open).
    """
    try:
        stems = sorted(p.stem for p in runs_dir.glob("*.json"))
    except OSError:
        return []
    return [rid for rid in stems if rid and rid in relay_text]


def mentioned_audit_ids(relay_text: str, notebooks_dir: Path) -> list[str]:
    """Notebook audit ids the relay text names, journal order.

    Mirrors :func:`mentioned_run_ids`: keyed on substring presence of each
    ``<notebooks>/<audit_id>.decisions.jsonl`` stem in the final text — a claim
    is only attributable to an audit the relay mentions. A glob-only probe (no
    journal is read) so a stop that names no audit does zero notebook work.
    Filesystem errors yield an empty list (fail-open).
    """
    try:
        ids = sorted(
            p.name[: -len(".decisions.jsonl")] for p in notebooks_dir.glob("*.decisions.jsonl")
        )
    except OSError:
        return []
    return [aid for aid in ids if aid and aid in relay_text]


def _relay_due_discharge_pass(
    experiment_dir: Path, notebooks_dir: Path, relay_text: str
) -> list[_AbsentMarker]:
    """Discharge relayed markers (as ``relay``); return the UNDISCHARGED ones.

    For every audit journal in *notebooks_dir* (the same no-scaffold dir the
    mention scan globs — capped at :data:`_MAX_RELAY_DUE_AUDITS`), load the
    UNDISCHARGED relay-due markers and check the final text for ANY of each
    marker's ``key_tokens`` (plain substring, case-insensitive):

    * found → append a discharge record with ``discharged_by="relay"`` (the model
      relayed the token — append-only; the marker is never mutated) and surface
      nothing;
    * absent → an :class:`_AbsentMarker`, carrying both the verbatim-ready
      rejector text AND the resolved marker (the completer sources its owed
      artifact + records the completer-discharge from it).

    Absent markers are NOT discharged here — the completer discharges them (D3,
    ``discharged_by="completer"``) only once it has actually appended the owed
    artifact, and the rejector never discharges an omission at all.

    Fail-open at every grain: a filesystem error, an unreadable journal, a
    malformed marker, or a failed discharge append is skipped, never raised —
    the callers additionally wrap the whole pass (Option-3 failure class: a
    hook that can wedge a session on one bad record).
    """
    try:
        audit_ids = sorted(
            p.name[: -len(".decisions.jsonl")] for p in notebooks_dir.glob("*.decisions.jsonl")
        )
    except OSError:
        return []

    from hpc_agent.state.notebook_audit import (
        DISCHARGED_BY_RELAY,
        read_undischarged_relay_markers,
        record_relay_discharge,
    )

    # Run-#10 #13: campaign scopes are the omission gate's SECOND source —
    # every terminal campaign_run outcome arms a marker on its campaign
    # journal (the run-#10 conduct strike: two exit-1 iterations read from a
    # background log and never surfaced). Same caps, same fail-open grain.
    scopes: list[tuple[str, str]] = [
        ("notebook", a) for a in audit_ids[:_MAX_RELAY_DUE_AUDITS] if a
    ]
    try:
        campaign_ids = sorted(
            p.parent.name
            for p in (Path(experiment_dir) / ".hpc" / "campaigns").glob("*/decisions.jsonl")
        )
        scopes += [("campaign", c) for c in campaign_ids[:_MAX_RELAY_DUE_AUDITS] if c]
    except OSError:
        pass

    lowered = relay_text.lower()
    absent: list[_AbsentMarker] = []
    for scope_kind, scope_id in scopes:
        try:
            markers = read_undischarged_relay_markers(
                experiment_dir, scope_id, scope_kind=scope_kind
            )
        except Exception:
            continue  # a journal we cannot read is a silent pass for that scope
        for marker in markers:
            try:
                tokens = [t for t in marker.get("key_tokens", []) if isinstance(t, str) and t]
                if not tokens:
                    continue  # malformed marker — never blocks, never raises
                if any(token.lower() in lowered for token in tokens):
                    record_relay_discharge(
                        experiment_dir,
                        audit_id=scope_id,
                        marker=marker,
                        scope_kind=scope_kind,
                        discharged_by=DISCHARGED_BY_RELAY,
                    )
                elif len(absent) < _MAX_RELAY_DUE_FINDINGS:
                    kind = str(marker.get("record_kind") or "notebook-status")
                    state = tokens[0]
                    # A two-token marker (notebook-status: state @ module sha12)
                    # names both; a one-token marker (notebook-audit-view: a
                    # section's view_sha12) names just the sha to relay. The
                    # record_kind already disambiguates, so the suffix is added
                    # only when a second token exists — no dangling "@ ?".
                    at = f" @ {tokens[1]}" if len(tokens) > 1 else ""
                    absent.append(
                        _AbsentMarker(
                            scope_kind=scope_kind,
                            scope_id=scope_id,
                            marker=marker,
                            omission_text=(
                                f"unrelayed terminal state: {kind} = {state}{at}"
                                " — relay it verbatim before closing."
                            ),
                        )
                    )
            except Exception:
                continue  # a marker we cannot check/discharge is a silent pass
    return absent


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


# ─── Sign-off echo detection (laundered authorship — queue item 2) ───────────
#
# Conservative thresholds, all biased against a false block. A response shorter
# than _MIN_ECHO_CHARS (or fewer than _MIN_ECHO_TOKENS words) is never matched —
# short attestations ("y", "ok", "looks good") collide by chance. A near-match
# needs the response's tokens to be almost wholly contained in an assistant line
# (_ECHO_TOKEN_OVERLAP), so a minor human edit still flags but two unrelated
# sentences sharing a few words do not.
_MAX_ECHO_AUDITS = 10
_MIN_ECHO_CHARS = 16
_MIN_ECHO_TOKENS = 3
_ECHO_TOKEN_OVERLAP = 0.9
_MAX_PRIOR_ASSISTANT_BYTES = 2_000_000
_MAX_ECHO_FINDINGS = 5

# ─── Decision-state claims (an unjournaled decision EVENT — queue item 5) ─────
#
# A small, conservative lexicon of PAST-TENSE assertions that a decision event
# happened. Word-boundary matched (so "unjournaled" does not read as a
# "journaled" claim). Positive verbs assert a decision was recorded/approved;
# the revocation verbs assert a prior decision no longer stands.
_DECISION_STATE_POSITIVE_RE = re.compile(r"\b(?:greenlit|greenlighted|journaled)\b", re.IGNORECASE)
_DECISION_STATE_NEGATIVE_RE = re.compile(r"\b(?:revoked|superseded)\b", re.IGNORECASE)
_MAX_STATE_CLAIM_FINDINGS = 5


def _norm(text: str) -> str:
    """Whitespace-normalized, lowercased — the echo comparison key."""
    return " ".join(text.split()).lower()


def _entry_text(entry: dict[str, Any]) -> str:
    """Join the text blocks of one transcript entry's message (str or list)."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            block_text = block.get("text")
            if isinstance(block_text, str) and block_text:
                parts.append(block_text)
    return "\n".join(parts)


def _prior_assistant_texts(transcript_path: Path) -> list[str]:
    """Assistant texts BEFORE the final trailing assistant run, in order.

    The echo check compares a journaled sign-off against a *prior* assistant
    line — the drafting turn — so the final relay (which may legitimately QUOTE
    the response back while relaying it) is excluded. Capped and tolerant:
    unreadable file / corrupt lines yield ``[]`` / skip the line.
    """
    try:
        text = transcript_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    entries: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)

    # The trailing run of assistant entries is the final relay — exclude it.
    trailing_start = len(entries)
    for idx in range(len(entries) - 1, -1, -1):
        if entries[idx].get("type") == "assistant":
            trailing_start = idx
            continue
        break

    texts: list[str] = []
    total = 0
    for entry in entries[:trailing_start]:
        if entry.get("type") != "assistant":
            continue
        entry_text = _entry_text(entry)
        if not entry_text:
            continue
        texts.append(entry_text)
        total += len(entry_text)
        if total >= _MAX_PRIOR_ASSISTANT_BYTES:
            break
    return texts


def _sign_off_echo_findings(
    experiment_dir: Path, notebooks_dir: Path, prior_texts: list[str]
) -> list[str]:
    """Flag journaled sign-offs whose response echoes a prior assistant line.

    For each discoverable audit (capped), the LATEST ``notebook-sign-off``
    record's ``response`` is compared against the prior-assistant corpus:
    whitespace-normalized substring (the human pasted the model's sentence) or
    high token containment (a minor edit). Both gated by a minimum length so a
    short attestation never collides. Fail-open at every grain; capped findings.
    """
    if not prior_texts:
        return []
    try:
        audit_ids = sorted(
            p.name[: -len(".decisions.jsonl")] for p in notebooks_dir.glob("*.decisions.jsonl")
        )
    except OSError:
        return []

    blob_parts: list[str] = []
    lines: list[str] = []
    for raw in prior_texts:
        normalized = _norm(raw)
        if normalized:
            blob_parts.append(normalized)
        for segment in raw.splitlines():
            norm_line = _norm(segment)
            if len(norm_line) >= _MIN_ECHO_CHARS:
                lines.append(norm_line)
    blob = " \n ".join(blob_parts)
    if not blob:
        return []

    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.decision_journal import read_decisions

    findings: list[str] = []
    for audit_id in audit_ids[:_MAX_ECHO_AUDITS]:
        if len(findings) >= _MAX_ECHO_FINDINGS:
            break
        try:
            records = read_decisions(experiment_dir, "notebook", audit_id)
        except Exception:
            continue  # a journal we cannot read is a silent pass for that audit
        sign_offs = [r for r in records if r.get("block") == nb.SIGN_OFF_BLOCK]
        if not sign_offs:
            continue
        record = sign_offs[-1]  # only the latest attestation (conservative)
        response = record.get("response")
        if not isinstance(response, str):
            continue
        norm_resp = _norm(response)
        resp_tokens = norm_resp.split()
        if len(norm_resp) < _MIN_ECHO_CHARS or len(resp_tokens) < _MIN_ECHO_TOKENS:
            continue  # too short to attribute — never flag

        matched: str | None = None
        if norm_resp in blob:
            matched = norm_resp
        else:
            token_set = set(resp_tokens)
            for candidate in lines:
                cand_tokens = set(candidate.split())
                if not cand_tokens:
                    continue
                if len(token_set & cand_tokens) / len(token_set) >= _ECHO_TOKEN_OVERLAP:
                    matched = candidate
                    break
        if matched is None:
            continue

        resolved = record.get("resolved")
        section = resolved.get("section") if isinstance(resolved, dict) else None
        where = f" section {section}" if section else ""
        findings.append(
            f"[{audit_id}]{where}: the journaled sign-off response {response[:80]!r} "
            f"echoes a prior assistant-authored line ({matched[:80]!r}) — laundered "
            "authorship (the model drafted the words the human pasted as their "
            "attestation). Disclose that the sign-off wording was model-composed."
        )
    return findings


def _decision_state_findings(
    experiment_dir: Path, relay_text: str, run_ids: list[str]
) -> list[_Violation]:
    """Flag decision-state claims no journal record supports (queue item 5).

    A decision-state verb is only attributable to a scope the relay NAMES (the
    rule-10 discipline): candidate scopes are the mentioned runs plus any
    mentioned campaign. The verb and the scope id must share a LINE, and the
    scope's decision journal must support the claim — a positive verb needs a
    committed ``y`` greenlight; a revocation/supersession verb needs the journal
    to show that greenlight no longer standing (or nothing to revoke at all).
    Fail-open per scope; capped; a scope-less claim is a deliberate miss.

    Returns :class:`_Violation`s: the ``claim`` carries the matched verb category
    (so the completer's poisoned-decision intersection can test it against a
    pending brief), ``text`` the verbatim rejector line.
    """
    has_pos = _DECISION_STATE_POSITIVE_RE.search(relay_text)
    has_neg = _DECISION_STATE_NEGATIVE_RE.search(relay_text)
    if not has_pos and not has_neg:
        return []  # fast path: no decision-state vocabulary anywhere

    scopes: list[tuple[str, str]] = [("run", rid) for rid in run_ids]
    try:
        campaign_ids = sorted(
            p.parent.name
            for p in (Path(experiment_dir) / ".hpc" / "campaigns").glob("*/decisions.jsonl")
        )
        scopes += [("campaign", c) for c in campaign_ids if c and c in relay_text]
    except OSError:
        pass
    if not scopes:
        return []  # attributable to no journaled scope — conservative miss

    from hpc_agent.state.decision_journal import is_latest_committed_greenlight, read_decisions

    relay_lines = relay_text.splitlines()
    findings: list[_Violation] = []
    for scope_kind, scope_id in scopes:
        if len(findings) >= _MAX_STATE_CLAIM_FINDINGS:
            break
        pos_here = False
        neg_here = False
        for line in relay_lines:
            if scope_id not in line:
                continue  # proximity: the verb must share the scope id's line
            if _DECISION_STATE_POSITIVE_RE.search(line):
                pos_here = True
            if _DECISION_STATE_NEGATIVE_RE.search(line):
                neg_here = True
        if not pos_here and not neg_here:
            continue
        try:
            records = read_decisions(experiment_dir, scope_kind, scope_id)
            standing = is_latest_committed_greenlight(experiment_dir, scope_kind, scope_id)
        except Exception:
            continue  # a scope we cannot read is a silent pass
        has_greenlight = any(r.get("response") == "y" for r in records)
        if pos_here and not has_greenlight and len(findings) < _MAX_STATE_CLAIM_FINDINGS:
            findings.append(
                _Violation(
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    claim="greenlit journaled",
                    journal_value=None,
                    text=(
                        f"[{scope_id}] decision-state claim (greenlit/journaled) has no "
                        "committed greenlight in the decision journal"
                    ),
                )
            )
        if neg_here and (not records or standing) and len(findings) < _MAX_STATE_CLAIM_FINDINGS:
            detail = (
                "the latest decision is a standing greenlight, not a revocation"
                if standing
                else "there is no decision record at all"
            )
            findings.append(
                _Violation(
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    claim="revoked superseded",
                    journal_value=None,
                    text=(
                        f"[{scope_id}] decision-state claim (revoked/superseded) has no "
                        f"supporting journal record — {detail}"
                    ),
                )
            )
    return findings


def _gather_violations(
    experiment_dir: Path, relay_text: str, run_ids: list[str], audit_ids: list[str]
) -> list[_Violation]:
    """Every violation-class finding (rule-10 + paraphrase + decision-state).

    The order is preserved from the pre-completer rejector: run rule-10, then
    notebook rule-10, then the paraphrase pass, then decision-state. Each helper
    is fail-open; the whole gather is additionally wrapped by the caller.
    """
    violations: list[_Violation] = []

    if run_ids:
        from hpc_agent._wire.queries.verify_relay import VerifyRelayInput
        from hpc_agent.ops.decision.verify_relay import verify_relay

        for run_id in run_ids[:_MAX_RUNS_AUDITED]:
            try:
                result = verify_relay(
                    experiment_dir=experiment_dir,
                    spec=VerifyRelayInput(run_id=run_id, relay_text=relay_text),
                )
            except Exception:
                continue  # a run we cannot audit is a silent pass for that run
            for m in result.mismatches:
                if m.kind not in _CONTRADICTION_KINDS:
                    continue
                nearest = f" (journal: {m.nearest_source_value})" if m.nearest_source_value else ""
                violations.append(
                    _Violation(
                        scope_kind="run",
                        scope_id=run_id,
                        claim=m.claim,
                        journal_value=m.nearest_source_value,
                        text=f"[{run_id}] {m.claim!r}: {m.detail}{nearest}",
                    )
                )

    if audit_ids:
        from hpc_agent.ops.decision.verify_relay import verify_notebook_relay

        for audit_id in audit_ids[:_MAX_AUDITS_AUDITED]:
            try:
                nb_result = verify_notebook_relay(experiment_dir, audit_id, relay_text)
            except Exception:
                continue  # an audit we cannot check is a silent pass for that audit
            for m in nb_result.mismatches:
                if m.kind not in _CONTRADICTION_KINDS:
                    continue
                nearest = f" (journal: {m.nearest_source_value})" if m.nearest_source_value else ""
                violations.append(
                    _Violation(
                        scope_kind="notebook",
                        scope_id=audit_id,
                        claim=m.claim,
                        journal_value=m.nearest_source_value,
                        text=f"[{audit_id}] {m.claim!r}: {m.detail}{nearest}",
                    )
                )

        # G1 — the paraphrase pass: relayed diff blocks in audit context must be
        # verbatim render content. Audit-scope with no per-claim value → an empty
        # ``claim`` (append-only by construction; never poisons a decision — the
        # sign-off boundary has its own gates).
        paraphrase = _paraphrase_findings(
            experiment_dir, relay_text, audit_ids[:_MAX_AUDITS_AUDITED]
        )
        for text in paraphrase:
            violations.append(
                _Violation(
                    scope_kind="notebook", scope_id="", claim="", journal_value=None, text=text
                )
            )

    # Decision-state claims — an unjournaled decision EVENT contradicts the record.
    with contextlib.suppress(Exception):
        violations.extend(_decision_state_findings(experiment_dir, relay_text, run_ids))
    return violations


def _rejector_output(
    violations: list[_Violation], echoes: list[str], absent_markers: list[_AbsentMarker]
) -> dict[str, Any] | None:
    """Today's REJECTOR shape — the capability-absent (dark) default (D1).

    Byte-for-byte the pre-completer behavior: violation-class findings + echoes +
    omission findings are itemized into ONE block reason, or ``None`` when there
    is nothing to say. This is what the completer degrades to wherever the
    ``stop-hook-append`` capability is absent/unknown.
    """
    findings = [v.text for v in violations]
    omissions = [am.omission_text for am in absent_markers]
    if not findings and not omissions and not echoes:
        return None

    segments: list[str] = []
    if findings:
        segments.append(
            "hpc-agent relay audit (conduct rule 10): the final message contradicts "
            f"the durable records — {len(findings)} mismatch(es): "
            + "; ".join(findings)
            + ". Correct the relay to match the journal (verify with "
            "`hpc-agent verify-relay`) before ending the turn — never relay "
            "numbers or state the journal does not support."
        )
    if echoes:
        segments.append(
            "hpc-agent sign-off echo (laundered authorship): "
            + "; ".join(echoes)
            + " Never compose the human's sign-off utterance."
        )
    if omissions:
        segments.append("hpc-agent relay-due discharge (the omission gate): " + " ".join(omissions))
    return {"decision": "block", "reason": " ".join(segments)}


def _render_by_view_sha(experiment_dir: Path, audit_id: str, view_sha12: str) -> str | None:
    """The trusted render file selected BY *view_sha12* in its filename (D4).

    ``.hpc/renders/<audit_id>/*.md`` — the ONE file whose name carries the sha
    (never a glob-all — the sha embedded in the filename IS the addressing). A
    filesystem error / no match / unreadable file yields ``None`` (the completer
    degrades to the token-level floor). Capped scan.
    """
    rdir = Path(experiment_dir) / ".hpc" / "renders" / audit_id
    try:
        if not rdir.is_dir():
            return None
        for scanned, f in enumerate(sorted(rdir.glob("*.md"))):
            if scanned >= _MAX_APPEND_RENDER_FILES_SCANNED:
                break
            if view_sha12 in f.name:
                try:
                    return f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
    except OSError:
        return None
    return None


def _compose_owed_artifact(experiment_dir: Path, am: _AbsentMarker) -> str:
    """The owed artifact for an omission, sourced from FILES only (D4).

    A render view-marker → the trusted render's own content, selected by
    ``view_sha12`` (the ONE composer, verbatim-by-construction — the G1
    paraphrase class cannot exist for appended content). Over the append cap →
    the token floor plus a file reference. Every other marker → the token floor:
    the journal record's ``record_kind`` + ``key_tokens`` verbatim (a
    ``notebook-status`` terminal has no render file). NEVER quotes model text.
    """
    from hpc_agent.state.notebook_audit import RENDER_RELAY_DUE_RECORD_KIND

    marker = am.marker
    tokens = [t for t in marker.get("key_tokens", []) if isinstance(t, str) and t]
    kind = str(marker.get("record_kind") or "notebook-status")

    if kind == RENDER_RELAY_DUE_RECORD_KIND and tokens:
        view_sha12 = tokens[0]
        body: str | None = None
        try:
            body = _render_by_view_sha(experiment_dir, am.scope_id, view_sha12)
        except Exception:
            body = None
        if body is not None:
            if len(body.encode("utf-8", errors="ignore")) <= _MAX_APPEND_ARTIFACT_BYTES:
                return (
                    f"hpc-agent relay-due — code-appended render (audit {am.scope_id}, "
                    f"view_sha {view_sha12}; model-untouched):\n{body}"
                )
            # Over-cap → token floor + a reference to the render file (D4).
            return (
                f"hpc-agent relay-due — the render for view_sha {view_sha12} exceeds the "
                f"append cap; see .hpc/renders/{am.scope_id}/*{view_sha12}*.md. "
                f"notebook-audit-view = {view_sha12}."
            )

    state = tokens[0] if tokens else "?"
    at = f" @ {tokens[1]}" if len(tokens) > 1 else ""
    return (
        "hpc-agent relay-due — code-appended terminal verdict (model-untouched): "
        f"{kind} = {state}{at}."
    )


def _compose_correction(v: _Violation) -> str:
    """A code-authored correction UNDER a contradicted claim (§2 violation class).

    Quotes the claim (the model's error, visible but neutralized) and the
    journal's actual value — the same ``nearest_source_value`` the rejector
    reason carries — so the human reads the correct value in the same turn.
    """
    return (
        "hpc-agent relay correction — code-appended, model-untouched (conduct rule 10; "
        "the model's claim is quoted, the journal value is authoritative):\n  " + v.text
    )


def _compose_echo_disclosure(echo: str) -> str:
    """A code-appended sign-off-echo disclosure (§2 echo class, RULED append-only).

    The completer NEVER bounces for an echo — the model cannot repair authorship,
    so a forced turn produces nothing the disclosure does not. The disclosure
    carries the re-attestation request for a load-bearing sign-off.
    """
    return (
        "hpc-agent sign-off echo disclosure — code-appended (laundered authorship): "
        + echo
        + " The attestation stands only if the human re-affirms in their own words."
    )


def _is_poisoned_decision(experiment_dir: Path, v: _Violation) -> bool:
    """The poisoned-decision test (§2): does *v* contradict a PENDING proposal?

    Keyed on the brief store (``state/decision_briefs.py::read_briefs`` — persists
    in BOTH driving modes), NEVER on the block-drive-only ``pending_decision``
    marker and NEVER on ``is_latest_committed_greenlight``. Poisoned iff: the
    scope's LATEST persisted brief has NO subsequent committed ``y`` in the
    decision journal (still pending), AND the claim's tokens intersect that
    brief's content. Fail-open (any error → not poisoned — bias to the append
    path, since an append is always safe and a bounce is the cost this design
    kills). A campaign scope has no run-brief store, so it never poisons here.
    """
    if not v.claim:
        return False  # no value token to intersect (paraphrase / audit-scope)
    try:
        from hpc_agent.state.decision_briefs import read_briefs

        briefs = read_briefs(experiment_dir, v.scope_id)
    except Exception:
        return False
    if not briefs:
        return False
    latest = briefs[-1]
    latest_ts = str(latest.get("ts") or "")
    try:
        from hpc_agent.state.decision_journal import read_decisions

        decisions = read_decisions(experiment_dir, v.scope_kind, v.scope_id)
    except Exception:
        decisions = []
    for d in decisions:
        if d.get("response") == "y" and str(d.get("ts") or "") >= latest_ts:
            return False  # the pending brief was greenlit → not poisoned
    try:
        brief_blob = json.dumps(latest.get("brief") or {}, sort_keys=True, default=str).lower()
    except (TypeError, ValueError):
        return False
    claim_tokens = [t for t in re.split(r"\W+", v.claim.lower()) if len(t) >= 2]
    return any(t in brief_blob for t in claim_tokens)


def _poison_reason(poisoned: list[_Violation]) -> str:
    """The bounce reason for poisoned-decision violations (the surviving bounce)."""
    return (
        "hpc-agent relay audit (poisoned decision — conduct rule 10): the final message "
        f"contradicts the durable records AND feeds a PENDING decision — {len(poisoned)} "
        "finding(s): " + "; ".join(v.text for v in poisoned) + ". A code-appended footnote "
        "is not enough under a pending proposal — re-relay the corrected proposal (verify "
        "with `hpc-agent verify-relay`) before ending the turn."
    )


def _completer_output(
    experiment_dir: Path,
    forced: bool,
    append_on_block_ok: bool,
    violations: list[_Violation],
    echoes: list[str],
    absent_markers: list[_AbsentMarker],
) -> dict[str, Any] | None:
    """The COMPLETER shape (D1–D4): APPEND what code holds, bounce only on poison.

    * Omissions → append the owed artifact (D4) and record a completer-discharge
      (D3, ``discharged_by="completer"``); no bounce.
    * Violations → append a code-authored correction UNDER the claim, EXCEPT a
      poisoned-decision violation, which BOUNCES (the surviving block).
    * Echoes → append the disclosure; NEVER bounce.

    Composition (D2): completions/corrections ride ONE ``systemMessage``; a
    poisoned bounce ALSO carries ``{"decision":"block","reason":...}`` for those
    findings ONLY (the appended findings are NOT re-stated). On a
    ``stop_hook_active`` forced continuation, completions still run and NOTHING
    bounces (loop-safe by construction). Discharge is gated on confirmed display
    (D2): where a bounce exists and the harness has NOT confirmed it displays a
    ``systemMessage`` on a BLOCKED stop, completions DEFER to the (never-blocked)
    post-continuation stop rather than riding a possibly-swallowed message.
    """
    from hpc_agent.state.notebook_audit import DISCHARGED_BY_COMPLETER, record_relay_discharge

    corrections: list[_Violation] = []
    poisoned: list[_Violation] = []
    for v in violations:
        # The poisoned bounce is itself block-once: on a forced continuation it
        # never fires (a swallowed correction still beats a re-bounce loop).
        if (
            (not forced)
            and v.scope_kind in ("run", "campaign")
            and _is_poisoned_decision(experiment_dir, v)
        ):
            poisoned.append(v)
        else:
            corrections.append(v)

    # The judgment class (unanswered question / abandoned continuation) has NO
    # members in THIS hook — the sibling Stop guards own those bounces — so the
    # only surviving bounce here is the poisoned-decision one.
    will_block = bool(poisoned)
    defer = will_block and not append_on_block_ok

    append_parts: list[str] = []
    if not defer:
        for am in absent_markers:
            artifact = _compose_owed_artifact(experiment_dir, am)
            try:
                record_relay_discharge(
                    experiment_dir,
                    audit_id=am.scope_id,
                    marker=am.marker,
                    scope_kind=am.scope_kind,
                    discharged_by=DISCHARGED_BY_COMPLETER,
                )
            except Exception:
                continue  # cannot record the discharge → do not claim it; leave owed
            append_parts.append(artifact)
        append_parts.extend(_compose_correction(v) for v in corrections)
        append_parts.extend(_compose_echo_disclosure(e) for e in echoes)

    out: dict[str, Any] = {}
    if append_parts:
        out["systemMessage"] = "\n\n".join(append_parts)
    if poisoned:
        out["decision"] = "block"
        out["reason"] = _poison_reason(poisoned)
    return out or None


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Map a Stop *payload* to the hook-output shape (rejector OR completer), or ``None``.

    Capability-gated (D1): when the harness declares the ``stop-hook-append``
    capability (``ops/harness_capabilities.py::detect_stop_hook_append`` is
    ``True``) the COMPLETER runs — code appends owed artifacts / corrections via
    ``systemMessage`` and the stop PROCEEDS, bouncing only on a poisoned decision.
    Absent/unknown (the default, since no harness declares it yet) it degrades to
    the REJECTOR EXACTLY (today's block-once). See
    ``docs/design/stop-hook-completer.md``.

    Returns ``None`` (the stop proceeds, nothing printed) when the payload is not
    a mapping, the cwd repo has no journal namespace (no-scaffold), the transcript
    yields no final assistant text, or there is nothing owed / contradicted.
    """
    if not isinstance(payload, dict):
        return None
    forced = bool(payload.get("stop_hook_active"))

    cwd = payload.get("cwd")
    cwd_dir = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())

    runs_dir = _journal_runs_dir(cwd_dir)
    notebooks_dir = _notebook_audits_dir(cwd_dir)
    # An hpc repo has a run journal OR a notebook-audit journal (a pre-submit
    # prelude repo audits source before any run exists). Neither → not an hpc
    # repo — silent pass, no-scaffold.
    if not runs_dir.is_dir() and not notebooks_dir.is_dir():
        return None

    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    relay_text = final_assistant_text(Path(transcript))
    if not relay_text:
        return None

    # D1 capability gate — read via the ONE detection home. Fail-open: any error
    # reading the capability degrades to the rejector (never the completer).
    try:
        from hpc_agent.ops.harness_capabilities import (
            detect_stop_hook_append,
            detect_stop_hook_append_on_block,
        )

        completer_active = detect_stop_hook_append() is True
        append_on_block_ok = detect_stop_hook_append_on_block() is True
    except Exception:
        completer_active = False
        append_on_block_ok = False

    # The relay-due discharge pass (the omission gate) runs on EVERY stop with
    # final text — including a forced continuation — discharging FOUND tokens as
    # ``relay`` and returning the still-undischarged (absent) markers. Fail-open
    # in full: any exception degrades to no omission findings.
    absent_markers: list[_AbsentMarker] = []
    if notebooks_dir.is_dir():
        try:
            absent_markers = _relay_due_discharge_pass(cwd_dir, notebooks_dir, relay_text)
        except Exception:
            absent_markers = []

    if not completer_active and forced:
        # REJECTOR block-once (verbatim today): a hook-forced continuation is
        # never re-blocked; the FOUND-token discharges above still landed.
        return None

    run_ids = mentioned_run_ids(relay_text, runs_dir) if runs_dir.is_dir() else []
    audit_ids = mentioned_audit_ids(relay_text, notebooks_dir) if notebooks_dir.is_dir() else []

    # Sign-off echo detection (queue item 2): a laundered attestation names
    # nothing in the final text, so this scans the audit journals directly.
    # Fail-open in full.
    echoes: list[str] = []
    if notebooks_dir.is_dir():
        try:
            echoes = _sign_off_echo_findings(
                cwd_dir, notebooks_dir, _prior_assistant_texts(Path(transcript))
            )
        except Exception:
            echoes = []

    # Violation-class findings (rule-10 + paraphrase + decision-state). Fail-open.
    try:
        violations = _gather_violations(cwd_dir, relay_text, run_ids, audit_ids)
    except Exception:
        violations = []

    if not run_ids and not audit_ids and not absent_markers and not echoes and not violations:
        return None  # nothing attributable to audit — the run path stays untouched

    if not completer_active:
        return _rejector_output(violations, echoes, absent_markers)
    return _completer_output(
        cwd_dir, forced, append_on_block_ok, violations, echoes, absent_markers
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, maybe print, never crash.

    Reads the Stop payload from stdin, runs :func:`build_hook_output`, and
    prints the resulting JSON to stdout when non-``None``. Any unexpected
    error is swallowed and reported as a clean no-op (exit ``0``): a broken
    audit must degrade to the verb-only posture (the stop proceeds), never
    wedge the harness. ``argv`` is accepted for symmetry and unused.
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
        output = build_hook_output(payload)
    except Exception:
        return 0

    if output is not None:
        print(json.dumps(output), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the harness
    raise SystemExit(main())
