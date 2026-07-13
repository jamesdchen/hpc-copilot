"""Direct-atom tests for the ``pack-record-receipt`` mutate primitive (T5).

Seeds a pack bind in the ``"pack"`` decision journal, calls the verb, and asserts
the receipt is journaled bound to the server-recomputed composite ``content_sha``
(cross-checked against :func:`state.pack_receipts.receipt_content_sha` and read
back CURRENT via ``slot_status``), that the recorded sha reflects DISK at record
time (the enforcement fire test — a file edited between caller-read and record is
recomputed, never caller-asserted), that a missing checked file and a no-current-
bind pack are both loud ``spec_invalid``, that a caller-authored slot records with
no membership check (the D7 reading for this mutate verb), and that ``evidence``
round-trips opaquely.

The ``"pack"`` journal scope + its path branch land in T8 (Wave C), so the
:func:`_pack_scope` fixture monkeypatches the scope kind + path into the ONE
decision-journal writer (the parallel bind_op posture) — the verb itself is
unchanged.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.pack_record_receipt import PackRecordReceiptSpec
from hpc_agent.ops.pack.record_receipt_op import pack_record_receipt
from hpc_agent.state import decision_journal as dj
from hpc_agent.state import pack_receipts as pr

if TYPE_CHECKING:
    import pytest as _pytest

_PACK = "toy-widgets"
_SLOT = "widget-audit"
_MANIFEST_SHA = hashlib.sha256(b"manifest-A").hexdigest()


@pytest.fixture
def _pack_scope(monkeypatch: _pytest.MonkeyPatch) -> None:
    """T8 seam: teach the ONE journal writer the ``"pack"`` scope + path branch."""
    monkeypatch.setattr(dj, "SCOPE_KINDS", dj.SCOPE_KINDS | {"pack"})
    orig = dj.decisions_path

    def _patched(experiment_dir: Path, scope_kind: str, scope_id: str) -> Path:
        if scope_kind == "pack":
            p = Path(experiment_dir) / ".hpc" / "packs" / f"{scope_id}.decisions.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        return orig(experiment_dir, scope_kind, scope_id)

    monkeypatch.setattr(dj, "decisions_path", _patched)


def _seed_bind(experiment_dir: Path, *, manifest_sha: str = _MANIFEST_SHA) -> None:
    """Append a ``pack-bind`` record so the pack has a current bind."""
    dj.append_decision(
        experiment_dir,
        scope_kind="pack",
        scope_id=_PACK,
        block=pr.PACK_BIND_BLOCK,
        response="bound",
        resolved={
            "pack": _PACK,
            "version": "1.2.0",
            "manifest_sha": manifest_sha,
            "files": [{"path": "vocab/readers.json", "sha256": manifest_sha}],
            "seams": ["reader_calls"],
        },
    )


def _write(experiment_dir: Path, rel: str, content: bytes) -> str:
    p = experiment_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _run(experiment_dir: Path, **spec: Any) -> Any:
    return pack_record_receipt(
        experiment_dir=experiment_dir,
        spec=PackRecordReceiptSpec.model_validate(spec),
    )


def _slot(experiment_dir: Path, slot: str = _SLOT) -> pr.SlotStatus:
    records = dj.read_decisions(experiment_dir, "pack", _PACK)
    return pr.slot_status(records, experiment_dir=experiment_dir, slot=slot)


# ── happy record ─────────────────────────────────────────────────────────────


def test_records_receipt_server_composite_reads_current_passed(
    tmp_path: Path, _pack_scope: None
) -> None:
    _seed_bind(tmp_path)
    sha = _write(tmp_path, "data/widget.csv", b"widget,rows\n1,2\n")

    result = _run(
        tmp_path,
        pack=_PACK,
        slot=_SLOT,
        checked=["data/widget.csv"],
        passed=True,
        evidence={"rows": 2},
    )

    # content_sha equals the ONE composite definition over the on-disk shas.
    expected = pr.receipt_content_sha(_MANIFEST_SHA, {"data/widget.csv": sha})
    assert result.content_sha == expected
    assert result.pack == _PACK
    assert result.version == "1.2.0"
    assert result.manifest_sha == _MANIFEST_SHA
    assert result.slot == _SLOT
    assert result.passed is True

    # The slot reduction reads it CURRENT+passed against the same disk.
    status = _slot(tmp_path)
    assert status.status == pr.CURRENT_PASSED
    assert status.passing is True
    assert status.passed is True


def test_passed_false_reads_current_failed(tmp_path: Path, _pack_scope: None) -> None:
    _seed_bind(tmp_path)
    _write(tmp_path, "data/widget.csv", b"bad\n")
    _run(tmp_path, pack=_PACK, slot=_SLOT, checked=["data/widget.csv"], passed=False)
    status = _slot(tmp_path)
    assert status.status == pr.CURRENT_FAILED
    assert status.passed is False
    assert status.passing is False


# ── the enforcement fire test: sha reflects DISK, never caller-asserted ───────


def test_recorded_sha_reflects_disk_at_record_time(tmp_path: Path, _pack_scope: None) -> None:
    _seed_bind(tmp_path)
    # The caller "read" the file at content A and would expect its composite…
    sha_a = _write(tmp_path, "data/widget.csv", b"AAAA\n")
    expected_a = pr.receipt_content_sha(_MANIFEST_SHA, {"data/widget.csv": sha_a})

    # …but the file changed on disk between caller-read and record.
    sha_b = _write(tmp_path, "data/widget.csv", b"BBBB\n")
    assert sha_b != sha_a

    result = _run(tmp_path, pack=_PACK, slot=_SLOT, checked=["data/widget.csv"], passed=True)

    # The verb recomputed at record time: the recorded sha is the DISK sha (B),
    # not the caller's stale expectation (A). No caller-suppliable sha is trusted.
    expected_b = pr.receipt_content_sha(_MANIFEST_SHA, {"data/widget.csv": sha_b})
    assert result.content_sha == expected_b
    assert result.content_sha != expected_a
    # And it reads CURRENT against the live (B) content.
    assert _slot(tmp_path).status == pr.CURRENT_PASSED


def test_receipt_reads_stale_after_checked_file_drifts(tmp_path: Path, _pack_scope: None) -> None:
    _seed_bind(tmp_path)
    _write(tmp_path, "data/widget.csv", b"AAAA\n")
    _run(tmp_path, pack=_PACK, slot=_SLOT, checked=["data/widget.csv"], passed=True)
    assert _slot(tmp_path).status == pr.CURRENT_PASSED

    # Edit the checked file AFTER recording — the journaled receipt now points at
    # the old composite sha → stale (drift = unsigned by construction).
    _write(tmp_path, "data/widget.csv", b"CCCC\n")
    assert _slot(tmp_path).status == pr.STALE


# ── loud legs ─────────────────────────────────────────────────────────────────


def test_missing_checked_file_is_spec_invalid(tmp_path: Path, _pack_scope: None) -> None:
    _seed_bind(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="not found or unreadable"):
        _run(tmp_path, pack=_PACK, slot=_SLOT, checked=["data/absent.csv"], passed=True)


def test_no_current_bind_is_spec_invalid(tmp_path: Path, _pack_scope: None) -> None:
    # No bind seeded → a dangling reference (the LOUD leg of D7 for a mutate verb).
    _write(tmp_path, "data/widget.csv", b"x\n")
    with pytest.raises(errors.SpecInvalid, match="no current bind"):
        _run(tmp_path, pack=_PACK, slot=_SLOT, checked=["data/widget.csv"], passed=True)


def test_rebind_at_new_sha_stales_the_old_receipt(tmp_path: Path, _pack_scope: None) -> None:
    _seed_bind(tmp_path)
    _write(tmp_path, "data/widget.csv", b"x\n")
    _run(tmp_path, pack=_PACK, slot=_SLOT, checked=["data/widget.csv"], passed=True)
    assert _slot(tmp_path).status == pr.CURRENT_PASSED

    # Re-bind at a NEW manifest sha → the old receipt's composite no longer matches
    # the current bind → stale, even though the checked file did not move.
    _seed_bind(tmp_path, manifest_sha=hashlib.sha256(b"manifest-B").hexdigest())
    assert _slot(tmp_path).status == pr.STALE


# ── the D7 reading: a caller-authored slot needs no membership check ──────────


def test_caller_authored_slot_records_without_membership_check(
    tmp_path: Path, _pack_scope: None
) -> None:
    _seed_bind(tmp_path)
    _write(tmp_path, "data/widget.csv", b"x\n")
    # A slot the manifest never listed in fills_slots still records — the
    # requirement originates with the caller (DP4); there is no skip/loud on slot.
    result = _run(tmp_path, pack=_PACK, slot="a-slot-core-never-heard-of", passed=True, checked=[])
    assert result.slot == "a-slot-core-never-heard-of"
    assert _slot(tmp_path, "a-slot-core-never-heard-of").status == pr.CURRENT_PASSED


# ── evidence round-trips opaquely ────────────────────────────────────────────


@pytest.mark.parametrize(
    "evidence",
    [
        {"nested": {"p_value": 0.03, "n": 100}},
        "free text evidence",
        None,
    ],
)
def test_evidence_round_trips_opaque(tmp_path: Path, _pack_scope: None, evidence: Any) -> None:
    _seed_bind(tmp_path)
    _run(tmp_path, pack=_PACK, slot=_SLOT, passed=True, checked=[], evidence=evidence)
    records = dj.read_decisions(tmp_path, "pack", _PACK)
    receipt = next(r for r in records if r["block"] == pr.PACK_RECEIPT_BLOCK)
    assert receipt["resolved"]["evidence"] == evidence
