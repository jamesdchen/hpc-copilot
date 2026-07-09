"""Tests for the domain-pack bind currency + slot receipt reduction (T2).

Covers: current-bind selection (newest wins; a re-bind revokes the old by
construction), the slot vocabulary (current+passed / current+failed / stale /
missing), drift both ways (a checked file edited on disk, and the bind re-bound),
receipt supersession, the kernel route-through (``inspect.getsource``), and the
canonical-JSON record-form vs read-form byte agreement.

The T5 record verb (``pack-record-receipt``) and the T8 ``"pack"`` journal scope
do not exist yet (parallel/later waves), so records are crafted as TOY dicts in
the append_decision shape (``{block, resolved}``) — the reducers take a record
list, never read a journal themselves. Receipt shas are built with the module's
OWN one-definition :func:`receipt_content_sha`, so the fixtures compute the exact
form T5 will and the read side rebuilds.
"""

from __future__ import annotations

import hashlib
import inspect
from typing import TYPE_CHECKING, Any

from hpc_agent.state import pack_receipts as pr

if TYPE_CHECKING:
    from pathlib import Path

_PACK = "toy-widgets"
_SLOT = "widget-audit"

_SHA_A = hashlib.sha256(b"manifest-A").hexdigest()
_SHA_B = hashlib.sha256(b"manifest-B").hexdigest()


def _raw_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(tmp_path: Path, rel: str, content: bytes) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return _raw_sha(p)


def _bind_record(manifest_sha: str, *, version: str = "1.2.0") -> dict[str, Any]:
    return {
        "block": pr.PACK_BIND_BLOCK,
        "resolved": {
            "pack": _PACK,
            "version": version,
            "manifest_sha": manifest_sha,
            "files": [{"path": "vocab/readers.json", "sha256": _SHA_A}],
            "seams": ["reader_calls"],
        },
    }


def _receipt_record(
    manifest_sha: str,
    checked: dict[str, str],
    *,
    slot: str = _SLOT,
    passed: bool = True,
) -> dict[str, Any]:
    """A ``pack-receipt`` record whose composite content_sha is the record-form sha."""
    content_sha = pr.receipt_content_sha(manifest_sha, checked)
    return {
        "block": pr.PACK_RECEIPT_BLOCK,
        "resolved": {
            "pack": _PACK,
            "version": "1.2.0",
            "manifest_sha": manifest_sha,
            "slot": slot,
            "checked": list(checked),
            "passed": passed,
            "evidence": "opaque-blob",
            "content_sha": content_sha,
            "attestor": "code",
        },
    }


# ── current_bind ─────────────────────────────────────────────────────────────


def test_current_bind_none_when_unbound() -> None:
    assert pr.current_bind([]) is None
    assert pr.current_bind([{"block": "unrelated", "resolved": {}}]) is None


def test_current_bind_newest_wins_and_rebind_revokes_old() -> None:
    records = [_bind_record(_SHA_A), _bind_record(_SHA_B, version="2.0.0")]
    bind = pr.current_bind(records)
    assert bind is not None
    assert bind.manifest_sha == _SHA_B  # the newer bind is current
    assert bind.version == "2.0.0"
    assert bind.pack == _PACK
    assert bind.seams == ("reader_calls",)


def test_current_bind_skips_malformed() -> None:
    # A bind with no manifest_sha is refused by the kernel validate → skipped.
    records = [_bind_record(_SHA_A), {"block": pr.PACK_BIND_BLOCK, "resolved": {"pack": _PACK}}]
    bind = pr.current_bind(records)
    assert bind is not None and bind.manifest_sha == _SHA_A


# ── slot reduction: current ──────────────────────────────────────────────────


def test_slot_current_passed(tmp_path: Path) -> None:
    sha = _write(tmp_path, "data/widget.csv", b"widget,rows\n1,2\n")
    checked = {"data/widget.csv": sha}
    records = [_bind_record(_SHA_A), _receipt_record(_SHA_A, checked, passed=True)]
    status = pr.slot_status(records, experiment_dir=tmp_path, slot=_SLOT)
    assert status.status == pr.CURRENT_PASSED
    assert status.passing is True
    assert status.passed is True
    assert status.reason is None
    assert status.checked == ("data/widget.csv",)


def test_slot_current_failed(tmp_path: Path) -> None:
    sha = _write(tmp_path, "data/widget.csv", b"widget,rows\n1,2\n")
    checked = {"data/widget.csv": sha}
    records = [_bind_record(_SHA_A), _receipt_record(_SHA_A, checked, passed=False)]
    status = pr.slot_status(records, experiment_dir=tmp_path, slot=_SLOT)
    assert status.status == pr.CURRENT_FAILED
    assert status.passing is False
    assert status.passed is False
    assert status.reason == "failed"


# ── slot reduction: drift both ways ──────────────────────────────────────────


def test_slot_stale_when_checked_file_edited(tmp_path: Path) -> None:
    sha = _write(tmp_path, "data/widget.csv", b"widget,rows\n1,2\n")
    checked = {"data/widget.csv": sha}
    records = [_bind_record(_SHA_A), _receipt_record(_SHA_A, checked, passed=True)]
    # Edit the file on disk after the receipt was recorded → its sha moves.
    _write(tmp_path, "data/widget.csv", b"widget,rows\n1,2\n3,4\n")
    status = pr.slot_status(records, experiment_dir=tmp_path, slot=_SLOT)
    assert status.status == pr.STALE
    assert status.passing is False
    assert status.reason == "stale"


def test_slot_stale_when_manifest_rebound(tmp_path: Path) -> None:
    sha = _write(tmp_path, "data/widget.csv", b"widget,rows\n1,2\n")
    checked = {"data/widget.csv": sha}
    # Receipt recorded under bind A; a later re-bind moves the manifest sha to B.
    records = [
        _bind_record(_SHA_A),
        _receipt_record(_SHA_A, checked, passed=True),
        _bind_record(_SHA_B),
    ]
    status = pr.slot_status(records, experiment_dir=tmp_path, slot=_SLOT)
    assert status.status == pr.STALE
    assert status.manifest_sha == _SHA_B  # reduced against the CURRENT bind


def test_slot_stale_when_checked_file_missing(tmp_path: Path) -> None:
    checked = {"data/gone.csv": hashlib.sha256(b"once").hexdigest()}
    records = [_bind_record(_SHA_A), _receipt_record(_SHA_A, checked, passed=True)]
    status = pr.slot_status(records, experiment_dir=tmp_path, slot=_SLOT)
    assert status.status == pr.STALE  # a deleted checked file is drift, not a pass


def test_slot_stale_when_no_current_bind(tmp_path: Path) -> None:
    sha = _write(tmp_path, "data/widget.csv", b"x")
    checked = {"data/widget.csv": sha}
    records = [_receipt_record(_SHA_A, checked, passed=True)]  # no bind at all
    status = pr.slot_status(records, experiment_dir=tmp_path, slot=_SLOT)
    assert status.status == pr.STALE
    assert status.manifest_sha is None


# ── slot reduction: missing + supersession ───────────────────────────────────


def test_slot_missing_when_no_receipt(tmp_path: Path) -> None:
    records = [_bind_record(_SHA_A)]
    status = pr.slot_status(records, experiment_dir=tmp_path, slot=_SLOT)
    assert status.status == pr.MISSING
    assert status.passed is None
    assert status.reason == "missing"
    assert status.checked == ()


def test_newest_receipt_supersedes(tmp_path: Path) -> None:
    sha = _write(tmp_path, "data/widget.csv", b"x")
    checked = {"data/widget.csv": sha}
    # An older passing receipt is superseded by a newer failing one (same slot).
    records = [
        _bind_record(_SHA_A),
        _receipt_record(_SHA_A, checked, passed=True),
        _receipt_record(_SHA_A, checked, passed=False),
    ]
    status = pr.slot_status(records, experiment_dir=tmp_path, slot=_SLOT)
    assert status.status == pr.CURRENT_FAILED


def test_slot_statuses_batch(tmp_path: Path) -> None:
    sha = _write(tmp_path, "data/widget.csv", b"x")
    checked = {"data/widget.csv": sha}
    records = [
        _bind_record(_SHA_A),
        _receipt_record(_SHA_A, checked, slot="widget-audit", passed=True),
        _receipt_record(_SHA_A, checked, slot="stats-check", passed=False),
    ]
    out = pr.slot_statuses(
        records, experiment_dir=tmp_path, slots=["widget-audit", "stats-check", "unfilled"]
    )
    assert out["widget-audit"].status == pr.CURRENT_PASSED
    assert out["stats-check"].status == pr.CURRENT_FAILED
    assert out["unfilled"].status == pr.MISSING


# ── kernel route-through (the enforcement-map "one kernel" row) ───────────────


def test_current_bind_routes_through_the_kernel() -> None:
    src = inspect.getsource(pr.current_bind)
    assert "attestation.reduce(" in src, (
        "current_bind must route the currency verdict through the attestation "
        "kernel, never re-inline newest-first drift."
    )


def test_slot_status_routes_drift_through_the_kernel() -> None:
    src = inspect.getsource(pr.slot_status)
    assert "attestation.reduce(" in src, (
        "slot_status must route the current/stale drift verdict through the "
        "attestation kernel, never re-inline the sha compare."
    )


# ── canonical-JSON record-form vs read-form byte agreement ───────────────────


def test_content_sha_record_form_equals_read_form() -> None:
    checked = {"b/two.txt": "sha2", "a/one.txt": "sha1"}
    # Key order must not matter (sort_keys) and the two call sites must byte-agree.
    record_form = pr.receipt_content_sha(_SHA_A, checked)
    read_form = pr.receipt_content_sha(_SHA_A, dict(reversed(list(checked.items()))))
    assert record_form == read_form
    # A moved manifest sha or a moved file sha changes the composite.
    assert pr.receipt_content_sha(_SHA_B, checked) != record_form
    assert pr.receipt_content_sha(_SHA_A, {**checked, "a/one.txt": "sha1x"}) != record_form
