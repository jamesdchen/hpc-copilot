"""Audit 4 — sign-off echo detection (laundered authorship, queue item 2).

Flags a journaled ``notebook-sign-off`` whose ``response`` echoes a prior
assistant-authored line — model-composed wording pasted as the human's typed
attestation. RE-RULED 2026-07-10: JOURNAL-ONLY provenance — never surfaced,
never blocks (drafting help is sanctioned amplification). See the package
docstring's "Sign-off echo detection" section for the conservative thresholds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
) -> list[tuple[str, str, str]]:
    """Detect sign-offs whose response echoes a prior assistant line.

    Returns ``(audit_id, response_sha12, detail_text)`` triples — JOURNAL-ONLY
    provenance input (2026-07-10 user ruling: the surfaced nag is REMOVED; echo
    detection never blocks and never appends — see
    ``state/notebook_audit.py::record_echo_provenance``).

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

    findings: list[tuple[str, str, str]] = []
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
        import hashlib

        response_sha12 = hashlib.sha256(norm_resp.encode("utf-8")).hexdigest()[:12]
        findings.append(
            (
                audit_id,
                response_sha12,
                f"[{audit_id}]{where}: the journaled sign-off response {response[:80]!r} "
                f"matches a prior assistant-authored line ({matched[:80]!r}) — "
                "model-composed wording (provenance record; drafting help is "
                "sanctioned, this is the archive's honesty about authorship).",
            )
        )
    return findings


def _journal_echo_provenance(experiment_dir: Path, echoes: list[tuple[str, str, str]]) -> None:
    """Journal echo provenance (JOURNAL-ONLY — never surfaced, never blocks).

    The 2026-07-10 user ruling: LLM drafting help is desired human
    amplification; the y-ack-ease hazard is guarded by the digest-read /
    tiered sign-off gates, not by wording originality. Each detection becomes
    one deduped ``notebook-echo-provenance`` record
    (:func:`hpc_agent.state.notebook_audit.record_echo_provenance`). Fail-open
    per record — provenance must never wedge a stop.
    """
    from hpc_agent.state.notebook_audit import record_echo_provenance

    for audit_id, response_sha12, detail in echoes:
        try:
            record_echo_provenance(
                experiment_dir,
                audit_id=audit_id,
                response_sha12=response_sha12,
                detail=detail,
            )
        except Exception:
            continue
