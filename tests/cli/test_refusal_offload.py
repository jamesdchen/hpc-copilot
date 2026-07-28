"""F2 (context-footprint plan): oversized structural-refusal offload.

The branchpoint rule applied to the error path: the action line stays inline,
the forensic detail is the branch — offloaded to a content-addressed
``.hpc/briefs/refusal-<sha12>.txt`` the reader opens only when debugging.
Pinned here: the offload fires only over budget, only for STRUCTURAL refusals
(never the authorship-marked read-and-sign ones), only into an EXISTING
``.hpc`` (the no-scaffold rule), always with a disclosed pointer, and
fail-open on any write error — an under-budget refusal is byte-identical.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.cli._helpers import (
    _REFUSAL_INLINE_MAX_BYTES,
    _offload_oversized_refusal,
)


def _payload(message: str, *, remediation: str | None = None, features: dict | None = None):
    payload = {
        "ok": False,
        "error_code": "spec_invalid",
        "message": message,
        "category": "user",
        "retry_safe": False,
    }
    if remediation is not None:
        payload["remediation"] = remediation
    if features is not None:
        payload["failure_features"] = features
    return payload


_LONG = "\n".join(f"line {i}: " + "x" * 80 for i in range(60))  # ~5 KB


def test_under_budget_refusal_is_byte_identical(tmp_path: Path) -> None:
    (tmp_path / ".hpc").mkdir()
    payload = _payload("short message", remediation="short remedy")
    out = _offload_oversized_refusal(payload, tmp_path)
    assert out is payload  # the SAME object — no copy, no mutation
    assert not (tmp_path / ".hpc" / "briefs").exists()


def test_oversized_structural_refusal_offloads_with_pointer(tmp_path: Path) -> None:
    (tmp_path / ".hpc").mkdir()
    payload = _payload(_LONG, remediation="do the thing")
    out = _offload_oversized_refusal(payload, tmp_path)
    assert out is not payload
    assert len(out["message"].encode("utf-8")) < len(_LONG.encode("utf-8"))
    assert "full detail: .hpc/briefs/refusal-" in out["message"]
    # The brief carries the FULL text (message + remediation).
    briefs = list((tmp_path / ".hpc" / "briefs").iterdir())
    assert len(briefs) == 1
    full = briefs[0].read_text(encoding="utf-8")
    assert _LONG in full
    assert "do the thing" in full
    # Short remediation stays inline untouched.
    assert out["remediation"] == "do the thing"


def test_offload_is_content_addressed_idempotent(tmp_path: Path) -> None:
    (tmp_path / ".hpc").mkdir()
    _offload_oversized_refusal(_payload(_LONG), tmp_path)
    _offload_oversized_refusal(_payload(_LONG), tmp_path)
    assert len(list((tmp_path / ".hpc" / "briefs").iterdir())) == 1


def test_authorship_marked_refusal_never_offloads(tmp_path: Path) -> None:
    # The read-and-sign refusals are the human's inline surface (elicitation
    # retirement) — however long, they stay whole.
    (tmp_path / ".hpc").mkdir()
    payload = _payload(_LONG, features={"authorship_evidence": "missing"})
    out = _offload_oversized_refusal(payload, tmp_path)
    assert out is payload
    assert not (tmp_path / ".hpc" / "briefs").exists()


def test_no_hpc_dir_means_no_offload_and_no_scaffold(tmp_path: Path) -> None:
    payload = _payload(_LONG)
    out = _offload_oversized_refusal(payload, tmp_path)
    assert out is payload
    assert not (tmp_path / ".hpc").exists()


def test_write_failure_fails_open_to_inline(tmp_path: Path) -> None:
    hpc = tmp_path / ".hpc"
    hpc.mkdir()
    # A FILE squatting the briefs path makes mkdir raise (works under any
    # uid, unlike permission bits, which root ignores).
    (hpc / "briefs").write_text("not a directory", encoding="utf-8")
    payload = _payload(_LONG)
    out = _offload_oversized_refusal(payload, tmp_path)
    assert out is payload  # byte-identical envelope, no crash
    assert (hpc / "briefs").read_text(encoding="utf-8") == "not a directory"


def test_budget_constant_is_generous() -> None:
    # A sanity pin so a future edit cannot silently make the offload the
    # common path: typical refusals must stay far under the budget.
    assert _REFUSAL_INLINE_MAX_BYTES >= 1500
