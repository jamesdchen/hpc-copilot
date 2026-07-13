"""Hook-output composers — the REJECTOR (dark default) and the COMPLETER.

The completer path is CAPABILITY-GATED (D1): active only when the harness
declares the ``stop-hook-append`` capability. Absent/unknown — the default,
since no harness declares it yet — everything here degrades to the REJECTOR
EXACTLY (the block-once bounce). See ``docs/design/stop-hook-completer.md``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ._shared import _AbsentMarker, _Violation

# Append caps (D4, the ``_MAX_*`` posture): a code-appended render is bounded so
# one pathological render cannot flood the turn; over-cap content degrades to the
# token-level floor plus a file reference.
_MAX_APPEND_ARTIFACT_BYTES = 8_000
_MAX_APPEND_RENDER_FILES_SCANNED = 40


def _rejector_output(
    violations: list[_Violation], absent_markers: list[_AbsentMarker]
) -> dict[str, Any] | None:
    """Today's REJECTOR shape — the capability-absent (dark) default (D1).

    The pre-completer behavior minus the echo segment (RE-RULED 2026-07-10:
    echo is journal-only provenance, never surfaced in EITHER mode):
    violation-class findings + omission findings are itemized into ONE block
    reason, or ``None`` when there is nothing to say. This is what the
    completer degrades to wherever the ``stop-hook-append`` capability is
    absent/unknown.
    """
    findings = [v.text for v in violations]
    omissions = [am.omission_text for am in absent_markers]
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
    absent_markers: list[_AbsentMarker],
) -> dict[str, Any] | None:
    """The COMPLETER shape (D1–D4): APPEND what code holds, bounce only on poison.

    * Omissions → append the owed artifact (D4) and record a completer-discharge
      (D3, ``discharged_by="completer"``); no bounce.
    * Violations → append a code-authored correction UNDER the claim, EXCEPT a
      poisoned-decision violation, which BOUNCES (the surviving block).
    * Echoes are NOT handled here (RE-RULED 2026-07-10): journal-only
      provenance, recorded upstream in ``build_hook_output`` in both modes.

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

    out: dict[str, Any] = {}
    if append_parts:
        out["systemMessage"] = "\n\n".join(append_parts)
    if poisoned:
        out["decision"] = "block"
        out["reason"] = _poison_reason(poisoned)
    return out or None
