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
"""

from __future__ import annotations

import json
import os
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


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a Stop *payload* to a block decision, or ``None``.

    Returns ``None`` (→ caller prints nothing, the stop proceeds) when:

    * *payload* is not a mapping, or ``stop_hook_active`` is truthy (this
      stop is already a hook-forced continuation; blocking again would loop);
    * the cwd repo has no journal namespace (not an hpc repo — no-scaffold);
    * the transcript yields no final assistant text, or that text names no
      journaled run id (nothing attributable to audit);
    * every audited claim is clean (or merely unverifiable).

    Otherwise returns the Claude Code Stop hook-output shape with the
    itemized contradiction summary::

        {"decision": "block", "reason": "<mismatch summary + fix instruction>"}
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("stop_hook_active"):
        return None

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

    run_ids = mentioned_run_ids(relay_text, runs_dir) if runs_dir.is_dir() else []
    audit_ids = mentioned_audit_ids(relay_text, notebooks_dir) if notebooks_dir.is_dir() else []
    if not run_ids and not audit_ids:
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

    if not findings:
        return None

    reason = (
        "hpc-agent relay audit (conduct rule 10): the final message contradicts "
        f"the durable records — {len(findings)} mismatch(es): "
        + "; ".join(findings)
        + ". Correct the relay to match the journal (verify with "
        "`hpc-agent verify-relay`) before ending the turn — never relay "
        "numbers or state the journal does not support."
    )
    return {"decision": "block", "reason": reason}


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
