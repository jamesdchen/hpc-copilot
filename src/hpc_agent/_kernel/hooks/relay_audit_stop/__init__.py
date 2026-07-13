"""``Stop`` hook — audit the final relay against the journal (conduct rule 10).

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.Stop`` array
(see :func:`hpc_agent.agent_assets.install_agent_assets`). It is invoked when
the agent is about to end its turn, receives the Stop payload as JSON on
**stdin**, and may emit ``{"decision": "block", "reason": ...}`` on **stdout**
to make the agent continue instead.

Package layout
--------------
This is a subpackage, but the HOOK-ENTRY MODULE PATH is load-bearing and
UNCHANGED: ``agent_assets`` writes the needle
``hpc_agent._kernel.hooks.relay_audit_stop`` into users' ``settings.json`` and
invokes it as ``python -m hpc_agent._kernel.hooks.relay_audit_stop`` (dispatched
by :mod:`.__main__`). So this ``__init__`` stays the entry: it owns
:func:`build_hook_output` and :func:`main`, and re-exports the five audit passes
and the shared public helpers. The five audits live one-per-module:

* audit 1 — the rule-10 contradiction pass (:mod:`._contradiction`,
  ``_gather_violations`` → ``verify_relay`` / ``verify_notebook_relay``);
* audit 2 — the relay-due DISCHARGE pass, the omission gate (:mod:`._relay_due`,
  ``_relay_due_discharge_pass``);
* audit 3 — the paraphrase pass (:mod:`._paraphrase`, ``_paraphrase_findings``);
* audit 4 — sign-off echo detection (:mod:`._echo`, ``_sign_off_echo_findings``);
* audit 5 — decision-state claims (:mod:`._decision_state`,
  ``_decision_state_findings``).

The output composers (rejector + completer) live in :mod:`._output`; the shared
substrate (transcript parsing, mention scans, the two NamedTuples) in
:mod:`._shared`.

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
import sys
from pathlib import Path
from typing import Any

# The five audit passes (one per module) plus the shared substrate are imported
# here so the subpackage split is transparent to any importer of the historical
# entry path ``hpc_agent._kernel.hooks.relay_audit_stop``: they are re-exported
# module attributes even though (being private) they are not in ``__all__``.
from ._contradiction import (  # audit 1 (rule-10 contradiction)
    _CONTRADICTION_KINDS,  # noqa: F401  (re-exported; conformance keys its blocking set on it)
    _gather_violations,
)
from ._decision_state import _decision_state_findings  # audit 5 (decision-state)  # noqa: F401
from ._echo import (  # audit 4 (sign-off echo)
    _journal_echo_provenance,
    _prior_assistant_texts,
    _sign_off_echo_findings,
)
from ._output import _completer_output, _rejector_output
from ._paraphrase import _paraphrase_findings  # audit 3 (paraphrase)  # noqa: F401
from ._relay_due import _relay_due_discharge_pass  # audit 2 (omission gate)
from ._shared import (
    _AbsentMarker,
    _journal_runs_dir,
    _notebook_audits_dir,
    _Violation,  # noqa: F401  (re-exported from the historical entry path)
    final_assistant_text,
    mentioned_audit_ids,
    mentioned_run_ids,
)

__all__ = [
    "build_hook_output",
    "final_assistant_text",
    "main",
    "mentioned_audit_ids",
    "mentioned_run_ids",
]


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

    # Sign-off echo detection (queue item 2, RE-RULED 2026-07-10): JOURNAL-ONLY
    # provenance — never surfaced, never blocks (drafting help is sanctioned
    # amplification; the y-ack-ease hazard lives at the sign-off gates). The
    # detection scans the audit journals directly and each finding becomes one
    # deduped notebook-echo-provenance record. Fail-open in full.
    if notebooks_dir.is_dir():
        with contextlib.suppress(Exception):
            _journal_echo_provenance(
                cwd_dir,
                _sign_off_echo_findings(
                    cwd_dir, notebooks_dir, _prior_assistant_texts(Path(transcript))
                ),
            )

    # Violation-class findings (rule-10 + paraphrase + decision-state). Fail-open.
    try:
        violations = _gather_violations(cwd_dir, relay_text, run_ids, audit_ids)
    except Exception:
        violations = []

    if not run_ids and not audit_ids and not absent_markers and not violations:
        return None  # nothing attributable to audit — the run path stays untouched

    if not completer_active:
        return _rejector_output(violations, absent_markers)
    return _completer_output(cwd_dir, forced, append_on_block_ok, violations, absent_markers)


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
