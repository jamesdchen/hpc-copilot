"""Audit 2 — the relay-due DISCHARGE pass (the omission gate).

The omission-side complement of the contradiction passes: it enforces what MUST
be said. ``notebook-status``/``notebook-audit-view``/``campaign_run`` arm
relay-due markers on terminal outcomes; this pass discharges the ones the final
text carried and surfaces the ones it did not. See the package docstring's
"relay-due DISCHARGE pass" section for the full design.
"""

from __future__ import annotations

from pathlib import Path

from ._shared import _AbsentMarker

# Caps for the relay-due discharge pass (which scans journals rather than
# keying on mentions — an omission names nothing): audits scanned per stop and
# undischarged markers surfaced per stop. Same stay-cheap posture as the
# mention-keyed passes.
_MAX_RELAY_DUE_AUDITS = 10
_MAX_RELAY_DUE_FINDINGS = 5


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
