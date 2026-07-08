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

* ``notebook-status`` journals a relay-due MARKER on a TERMINAL verdict
  (:mod:`hpc_agent.state.notebook_audit` — v1's deliberately narrow set);
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
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

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
) -> list[str]:
    """Discharge relayed markers; return the verbatim-ready omission findings.

    For every audit journal in *notebooks_dir* (the same no-scaffold dir the
    mention scan globs — capped at :data:`_MAX_RELAY_DUE_AUDITS`), load the
    UNDISCHARGED relay-due markers and check the final text for ANY of each
    marker's ``key_tokens`` (plain substring, case-insensitive):

    * found → append a discharge record (append-only; the marker is never
      mutated) and surface nothing;
    * absent → an omission finding, phrased verbatim-ready so the block reason
      hands the agent exactly the tokens whose relay will discharge it.

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
    omissions: list[str] = []
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
                    )
                elif len(omissions) < _MAX_RELAY_DUE_FINDINGS:
                    kind = str(marker.get("record_kind") or "notebook-status")
                    state = tokens[0]
                    sha12 = tokens[1] if len(tokens) > 1 else "?"
                    omissions.append(
                        f"unrelayed terminal state: {kind} = {state} @ "
                        f"{sha12} — relay it verbatim before closing."
                    )
            except Exception:
                continue  # a marker we cannot check/discharge is a silent pass
    return omissions


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


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a Stop *payload* to a block decision, or ``None``.

    Returns ``None`` (→ caller prints nothing, the stop proceeds) when:

    * *payload* is not a mapping, or ``stop_hook_active`` is truthy (this
      stop is already a hook-forced continuation; blocking again would loop —
      the relay-due discharge pass still records discharges first, so a
      corrected relay closes its marker, but a forced stop is NEVER blocked);
    * the cwd repo has no journal namespace (not an hpc repo — no-scaffold);
    * the transcript yields no final assistant text, or that text names no
      journaled run id AND no undischarged relay-due marker exists (nothing
      attributable to audit, nothing owed);
    * every audited claim is clean (or merely unverifiable) and every
      relay-due marker is discharged.

    Otherwise returns the Claude Code Stop hook-output shape with the
    itemized contradiction summary::

        {"decision": "block", "reason": "<mismatch summary + fix instruction>"}
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

    # The relay-due discharge pass (the omission gate) runs on EVERY stop with
    # final text — including a stop_hook_active forced continuation, so the
    # corrected relay discharges its own marker at the stop that carries it —
    # and is fail-open in full: any exception degrades to no omission findings.
    omissions: list[str] = []
    if notebooks_dir.is_dir():
        try:
            omissions = _relay_due_discharge_pass(cwd_dir, notebooks_dir, relay_text)
        except Exception:
            omissions = []

    if forced:
        # Block-once (the sibling Stop guards' seam, reused exactly): this stop
        # is already a hook-forced continuation — never block again, even when
        # a marker's tokens are still absent. Discharges above still landed.
        return None

    run_ids = mentioned_run_ids(relay_text, runs_dir) if runs_dir.is_dir() else []
    audit_ids = mentioned_audit_ids(relay_text, notebooks_dir) if notebooks_dir.is_dir() else []
    if not run_ids and not audit_ids and not omissions:
        return None  # nothing attributable to audit — the run path stays untouched

    findings: list[str] = []

    if run_ids:
        from hpc_agent._wire.queries.verify_relay import VerifyRelayInput
        from hpc_agent.ops.decision.verify_relay import verify_relay

        for run_id in run_ids[:_MAX_RUNS_AUDITED]:
            try:
                result = verify_relay(
                    experiment_dir=cwd_dir,
                    spec=VerifyRelayInput(run_id=run_id, relay_text=relay_text),
                )
            except Exception:
                continue  # a run we cannot audit is a silent pass for that run
            for m in result.mismatches:
                if m.kind not in _CONTRADICTION_KINDS:
                    continue
                nearest = f" (journal: {m.nearest_source_value})" if m.nearest_source_value else ""
                findings.append(f"[{run_id}] {m.claim!r}: {m.detail}{nearest}")

    if audit_ids:
        from hpc_agent.ops.decision.verify_relay import verify_notebook_relay

        for audit_id in audit_ids[:_MAX_AUDITS_AUDITED]:
            try:
                nb_result = verify_notebook_relay(cwd_dir, audit_id, relay_text)
            except Exception:
                continue  # an audit we cannot check is a silent pass for that audit
            for m in nb_result.mismatches:
                if m.kind not in _CONTRADICTION_KINDS:
                    continue
                nearest = f" (journal: {m.nearest_source_value})" if m.nearest_source_value else ""
                findings.append(f"[{audit_id}] {m.claim!r}: {m.detail}{nearest}")

        # G1 — the paraphrase pass: relayed diff blocks in audit context must
        # be verbatim render content (verify-relay = wrong tokens; relay-due =
        # silence; this = re-typed content). Helper is fail-open and capped.
        findings.extend(_paraphrase_findings(cwd_dir, relay_text, audit_ids[:_MAX_AUDITS_AUDITED]))

    if not findings and not omissions:
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
    if omissions:
        # The omission side of the same boundary: a computed terminal verdict
        # the human never saw. Each finding is verbatim-ready — relaying the
        # named state/sha12 tokens is exactly what discharges the marker.
        segments.append("hpc-agent relay-due discharge (the omission gate): " + " ".join(omissions))
    return {"decision": "block", "reason": " ".join(segments)}


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
