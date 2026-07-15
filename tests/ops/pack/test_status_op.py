"""Tests for the ``pack-status`` read-only digest (domain-packs T6).

Covers: not-opted-in → empty + silent (zero journal probes); a bound pack with
current / failed / stale / missing slots; the advisory unfillable-requirement
report; a dangling manifest REPORTED (not raised — the query/mutate split); and a
multi-pack opt-in with the ``spec.pack`` filter.

The T8 ``"pack"`` journal scope kind is not landed (Wave C), so the op's one
journal read (``decision_journal.read_decisions(experiment_dir, "pack", name)``)
is MONKEYPATCHED to return crafted TOY records in the ``append_decision`` shape
(``{block, resolved, ts}``). Receipt shas are built with the reducers' OWN
one-definition ``receipt_content_sha`` so the fixtures compute the exact form the
record verb (T5) will and the read side rebuilds. Toy vocabulary only.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from hpc_agent._wire.actions.pack_status import PackStatusSpec
from hpc_agent.ops.pack import status_op
from hpc_agent.state.pack_receipts import (
    PACK_BIND_BLOCK,
    PACK_RECEIPT_BLOCK,
    PACK_SUBJECT_KIND,
    receipt_content_sha,
)

if TYPE_CHECKING:
    from pathlib import Path

_PACK = "toy-widgets"
_MANIFEST_REL = "packs/toy-widgets/manifest.json"
_VOCAB_REL = "packs/toy-widgets/vocab/readers.json"
_CHECKED_REL = "data/widgets.csv"


def _raw_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(experiment_dir: Path, rel: str, data: bytes) -> str:
    p = experiment_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return _raw_sha(data)


def _write_interview(experiment_dir: Path, packs: list[dict[str, Any]]) -> None:
    (experiment_dir / "interview.json").write_text(
        json.dumps({"executor_cmd": "run", "packs": packs}), encoding="utf-8"
    )


def _write_manifest(experiment_dir: Path, *, fills_slots: list[str], vocab_sha: str) -> None:
    manifest = {
        "name": _PACK,
        "version": "1.2.0",
        "files": [{"path": "vocab/readers.json", "sha256": vocab_sha}],
        "seams": {"reader_calls": "vocab/readers.json"},
        "fills_slots": fills_slots,
    }
    p = experiment_dir / _MANIFEST_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest), encoding="utf-8")


def _bind_record(
    manifest_sha: str, *, pack: str = _PACK, ts: str = "2026-07-08T00:00:00Z"
) -> dict[str, Any]:
    return {
        "block": PACK_BIND_BLOCK,
        "ts": ts,
        "resolved": {
            "pack": pack,
            "version": "1.2.0",
            "manifest_sha": manifest_sha,
            "files": [{"path": "vocab/readers.json", "sha256": manifest_sha}],
            "seams": ["reader_calls"],
        },
    }


def _receipt_record(
    *, slot: str, manifest_sha: str, checked: dict[str, str], passed: bool
) -> dict[str, Any]:
    content_sha = receipt_content_sha(manifest_sha, checked)
    return {
        "block": PACK_RECEIPT_BLOCK,
        "ts": "2026-07-08T00:01:00Z",
        "resolved": {
            "pack": _PACK,
            "slot": slot,
            "manifest_sha": manifest_sha,
            "checked": list(checked),
            "passed": passed,
            "content_sha": content_sha,
            "evidence": "opaque",
        },
    }


def _patch_journal(monkeypatch: Any, records_by_pack: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Monkeypatch the op's one journal read; return a call-log of pack names read."""
    calls: list[str] = []

    def _fake(experiment_dir: Any, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
        assert scope_kind == PACK_SUBJECT_KIND  # the T8 "pack" scope kind
        calls.append(scope_id)
        return records_by_pack.get(scope_id, [])

    monkeypatch.setattr(status_op.decision_journal, "read_decisions", _fake)
    return calls


# --- not opted in: empty + silent -------------------------------------------


def test_not_opted_in_is_empty_and_silent(tmp_path: Path, monkeypatch: Any) -> None:
    # No interview.json at all → empty result, and the journal is never probed.
    calls = _patch_journal(monkeypatch, {})
    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    assert result.packs == {}
    assert calls == []  # zero journal probes on the not-opted-in path


def test_interview_without_packs_block_is_empty(tmp_path: Path, monkeypatch: Any) -> None:
    (tmp_path / "interview.json").write_text(json.dumps({"executor_cmd": "run"}), encoding="utf-8")
    calls = _patch_journal(monkeypatch, {})
    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    assert result.packs == {}
    assert calls == []


# --- bound pack: the four slot statuses -------------------------------------


def test_bound_pack_slot_statuses(tmp_path: Path, monkeypatch: Any) -> None:
    vocab_sha = _write(tmp_path, _VOCAB_REL, b'["widgets.load_widget"]')
    checked_sha = _write(tmp_path, _CHECKED_REL, b"a,b\n1,2\n")
    _write_manifest(
        tmp_path,
        fills_slots=["slot-current", "slot-failed", "slot-stale", "slot-missing"],
        vocab_sha=vocab_sha,
    )
    slots = ["slot-current", "slot-failed", "slot-stale", "slot-missing"]
    _write_interview(
        tmp_path,
        [
            {
                "pack": _PACK,
                "manifest": _MANIFEST_REL,
                "receipt_bindings": [{"slot": s, "pack": _PACK} for s in slots],
            }
        ],
    )

    manifest_sha = "m" * 64
    checked = {_CHECKED_REL: checked_sha}
    records = [
        _bind_record(manifest_sha),
        _receipt_record(
            slot="slot-current", manifest_sha=manifest_sha, checked=checked, passed=True
        ),
        _receipt_record(
            slot="slot-failed", manifest_sha=manifest_sha, checked=checked, passed=False
        ),
        # stale: recorded against an OLDER manifest sha → currency recompute misses.
        _receipt_record(slot="slot-stale", manifest_sha="0" * 64, checked=checked, passed=True),
        # slot-missing: no receipt record at all.
    ]
    _patch_journal(monkeypatch, {_PACK: records})

    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    entry = result.packs[_PACK]
    assert entry.bind is not None
    assert entry.bind.pack == _PACK
    assert entry.bind.manifest_sha == manifest_sha
    assert entry.bind.bound_at == "2026-07-08T00:00:00Z"
    by_slot = {s.slot: s for s in entry.slots}
    assert by_slot["slot-current"].status == "current"
    assert by_slot["slot-current"].passed is True
    assert by_slot["slot-failed"].status == "failed"
    assert by_slot["slot-failed"].passed is False
    assert by_slot["slot-stale"].status == "stale"
    assert by_slot["slot-missing"].status == "missing"
    assert entry.unfillable == []  # every slot IS in fills_slots
    assert entry.dangling == []


# --- unfillable advisory -----------------------------------------------------


def test_unfillable_requirement_is_advisory(tmp_path: Path, monkeypatch: Any) -> None:
    vocab_sha = _write(tmp_path, _VOCAB_REL, b'["widgets.load_widget"]')
    # Manifest fills_slots is EMPTY → the bound slot is unfillable (advisory).
    _write_manifest(tmp_path, fills_slots=[], vocab_sha=vocab_sha)
    _write_interview(
        tmp_path,
        [
            {
                "pack": _PACK,
                "manifest": _MANIFEST_REL,
                "receipt_bindings": [{"slot": "widget-audit", "pack": _PACK}],
            }
        ],
    )
    manifest_sha = "m" * 64
    _patch_journal(monkeypatch, {_PACK: [_bind_record(manifest_sha)]})

    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    entry = result.packs[_PACK]
    assert len(entry.unfillable) == 1
    assert entry.unfillable[0].slot == "widget-audit"
    assert entry.unfillable[0].pack == _PACK
    # Advisory only — the slot is still reported (missing here), not gated away.
    assert {s.slot for s in entry.slots} == {"widget-audit"}


# --- dangling manifest: reported, NOT raised --------------------------------


def test_dangling_manifest_is_reported_not_raised(tmp_path: Path, monkeypatch: Any) -> None:
    # Opt in but never create the manifest file → a dangling reference.
    _write_interview(
        tmp_path,
        [{"pack": _PACK, "manifest": _MANIFEST_REL, "receipt_bindings": []}],
    )
    _patch_journal(monkeypatch, {_PACK: []})

    # A query REPORTS the dangling reference; it does not raise.
    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    entry = result.packs[_PACK]
    assert entry.bind is None
    assert len(entry.dangling) == 1
    assert entry.dangling[0].path == _MANIFEST_REL


def test_slot_bound_to_unbound_pack_is_dangling(tmp_path: Path, monkeypatch: Any) -> None:
    vocab_sha = _write(tmp_path, _VOCAB_REL, b'["widgets.load_widget"]')
    _write_manifest(tmp_path, fills_slots=["widget-audit"], vocab_sha=vocab_sha)
    _write_interview(
        tmp_path,
        [
            {
                "pack": _PACK,
                "manifest": _MANIFEST_REL,
                "receipt_bindings": [{"slot": "widget-audit", "pack": _PACK}],
            }
        ],
    )
    # No bind record → the slot is bound to a pack with no current bind.
    _patch_journal(monkeypatch, {_PACK: []})

    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    entry = result.packs[_PACK]
    assert entry.bind is None
    assert any(d.slot == "widget-audit" for d in entry.dangling)


# --- multi-pack + the spec.pack filter --------------------------------------


def test_multi_pack_filter(tmp_path: Path, monkeypatch: Any) -> None:
    # Two opted-in packs; the filter reports only the named one.
    for name in ("pack-a", "pack-b"):
        vsha = _write(tmp_path, f"packs/{name}/vocab.json", b'["widgets.load_widget"]')
        manifest = {
            "name": name,
            "version": "1.0.0",
            "files": [{"path": "vocab.json", "sha256": vsha}],
            "seams": {"reader_calls": "vocab.json"},
            "fills_slots": [],
        }
        mp = tmp_path / f"packs/{name}/manifest.json"
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(manifest), encoding="utf-8")
    _write_interview(
        tmp_path,
        [
            {"pack": "pack-a", "manifest": "packs/pack-a/manifest.json", "receipt_bindings": []},
            {"pack": "pack-b", "manifest": "packs/pack-b/manifest.json", "receipt_bindings": []},
        ],
    )
    calls = _patch_journal(
        monkeypatch,
        {
            "pack-a": [_bind_record("a" * 64, pack="pack-a")],
            "pack-b": [_bind_record("b" * 64, pack="pack-b")],
        },
    )

    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec(pack="pack-a"))
    assert set(result.packs) == {"pack-a"}
    assert result.packs["pack-a"].bind is not None
    assert result.packs["pack-a"].bind.pack == "pack-a"
    # Both packs' journals are read (the indices are precomputed), but only the
    # filtered pack is reported.
    assert set(calls) == {"pack-a", "pack-b"}


# --- derived_from lineage echo (P1a, DC10) ----------------------------------


def _bind_record_named(name: str, manifest_sha: str) -> dict[str, Any]:
    return {
        "block": PACK_BIND_BLOCK,
        "ts": "2026-07-15T00:00:00Z",
        "resolved": {"pack": name, "version": "0.2.0", "manifest_sha": manifest_sha, "seams": []},
    }


def _setup_lineage(tmp_path: Path, *, source_seam_bytes: bytes, stamp_sha: str) -> None:
    """A source (domain) pack + a program pack whose manifest stamps derived_from."""
    # Source pack: an audit_template seam file on disk.
    seam_sha = _write(tmp_path, "packs/dom/templates/skel.py", source_seam_bytes)
    src_manifest = {
        "name": "dom",
        "version": "0.2.0",
        "files": [{"path": "templates/skel.py", "sha256": seam_sha}],
        "seams": {"audit_template": "templates/skel.py"},
        "fills_slots": [],
    }
    p = tmp_path / "packs/dom/manifest.json"
    p.write_text(json.dumps(src_manifest), encoding="utf-8")
    # Program pack: manifest carries derived_from pointing at dom's audit_template.
    prog_tmpl_sha = _write(tmp_path, "packs/prog/templates/prog_audit.py", b"# prog\n")
    prog_manifest = {
        "name": "prog",
        "version": "0.2.0",
        "files": [{"path": "templates/prog_audit.py", "sha256": prog_tmpl_sha}],
        "seams": {"audit_template": "templates/prog_audit.py"},
        "fills_slots": [],
        "derived_from": {
            "pack": "dom",
            "seam": "audit_template",
            "version": "0.2.0",
            "sha": stamp_sha,
        },
    }
    (tmp_path / "packs/prog/manifest.json").write_text(json.dumps(prog_manifest), encoding="utf-8")
    _write_interview(
        tmp_path,
        [
            {"pack": "dom", "manifest": "packs/dom/manifest.json", "receipt_bindings": []},
            {"pack": "prog", "manifest": "packs/prog/manifest.json", "receipt_bindings": []},
        ],
    )


def test_lineage_echo_current_when_source_seam_matches(tmp_path: Path, monkeypatch: Any) -> None:
    seam_bytes = b"# domain skeleton\n"
    stamp_sha = _raw_sha(seam_bytes)  # stamp matches the on-disk source seam
    _setup_lineage(tmp_path, source_seam_bytes=seam_bytes, stamp_sha=stamp_sha)
    _patch_journal(
        monkeypatch,
        {
            "dom": [_bind_record_named("dom", "d" * 64)],
            "prog": [_bind_record_named("prog", "p" * 64)],
        },
    )
    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    lineage = result.packs["prog"].derived_from
    assert lineage is not None
    assert (lineage.pack, lineage.seam, lineage.freshness) == ("dom", "audit_template", "current")
    # The lineage-ROOT (dom) has no stamp → None.
    assert result.packs["dom"].derived_from is None


def test_lineage_echo_behind_when_source_seam_drifted(tmp_path: Path, monkeypatch: Any) -> None:
    seam_bytes = b"# domain skeleton v2\n"
    _setup_lineage(tmp_path, source_seam_bytes=seam_bytes, stamp_sha="a" * 64)  # stamp != on-disk
    _patch_journal(
        monkeypatch,
        {
            "dom": [_bind_record_named("dom", "d" * 64)],
            "prog": [_bind_record_named("prog", "p" * 64)],
        },
    )
    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    assert result.packs["prog"].derived_from is not None
    assert result.packs["prog"].derived_from.freshness == "behind"  # edge NOT severed


def test_lineage_echo_source_not_bound(tmp_path: Path, monkeypatch: Any) -> None:
    seam_bytes = b"# domain skeleton\n"
    _setup_lineage(tmp_path, source_seam_bytes=seam_bytes, stamp_sha=_raw_sha(seam_bytes))
    # dom has NO bind record → source-not-bound.
    _patch_journal(monkeypatch, {"dom": [], "prog": [_bind_record_named("prog", "p" * 64)]})
    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    assert result.packs["prog"].derived_from is not None
    assert result.packs["prog"].derived_from.freshness == "source-not-bound"


def test_legacy_pack_derived_from_serializes_as_null(tmp_path: Path, monkeypatch: Any) -> None:
    """Memo hazard 4 pin: a legacy pack's entry emits ``"derived_from": null``.

    ``cli/_dispatch.py`` serializes results via ``model_dump(mode='json')`` with NO
    ``exclude_none``, so the additive optional field appears as ``null`` for every
    legacy pack. This is ACCEPTED (do NOT touch the global serializer); pinned here.
    """
    vocab_sha = _write(tmp_path, _VOCAB_REL, b'["widgets.load_widget"]')
    _write_manifest(tmp_path, fills_slots=[], vocab_sha=vocab_sha)
    _write_interview(tmp_path, [{"pack": _PACK, "manifest": _MANIFEST_REL, "receipt_bindings": []}])
    _patch_journal(monkeypatch, {_PACK: [_bind_record("m" * 64)]})
    result = status_op.pack_status(experiment_dir=tmp_path, spec=PackStatusSpec())
    entry = result.packs[_PACK]
    assert entry.derived_from is None
    # The dispatch serialization form (model_dump json, no exclude_none) shows null.
    dumped = entry.model_dump(mode="json")
    assert "derived_from" in dumped and dumped["derived_from"] is None
