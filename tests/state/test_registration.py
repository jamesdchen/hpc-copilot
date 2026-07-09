"""Tests for the registration kernel substrate (T1).

Covers ``docs/design/registration-kernel.md`` R3/R5/R7: the CLOSED
``PREREQUISITE_KINDS`` equality pin, the STRUCTURE-only template loader (every
refusal fires), the full-address chain-entry model (unknown kind, non-empty
``requires`` on the generic ``attestation`` kind), and the append-only status
reduction (``current | stale | revoked | superseded | absent``) with its
``inspect.getsource`` route-through assertion (the enforcement-map "one kernel"
row). Toy-domain fixtures ONLY (the widget-batch lineage) — never quant
vocabulary.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent.state import attestation
from hpc_agent.state import registration as reg

if TYPE_CHECKING:
    from pathlib import Path

# --- toy-domain fixtures (widget-batch lineage; never quant vocabulary) ------

_TOY_FIELDS = ["widget-owner", "jam-threshold"]
_TOY_TEMPLATE: dict[str, Any] = {
    "fields": _TOY_FIELDS,
    "prerequisites": [
        {"slot": "widget-audit", "kind": "notebook-audit"},
        {
            "slot": "widget-repro",
            "kind": "reproduction",
            "requires": {"min_n": 2, "scales": ["main"]},
        },
        {"slot": "widget-sign", "kind": "attestation"},
    ],
}

_REG_ID = "widget-batch-42"
_DOSSIER_SHA = "dossier-sha-aaaa"


def _reg_record(
    *,
    dossier_sha: str = _DOSSIER_SHA,
    ts: str = "2026-07-08T00:00:00Z",
    registration_id: str = _REG_ID,
    view_sha: str = "view-sha-1",
) -> dict[str, Any]:
    """A crafted registration decision-journal record (append shape)."""
    return {
        "schema_version": 1,
        "ts": ts,
        "scope_kind": "registration",
        "scope_id": registration_id,
        "block": reg.REGISTRATION_BLOCK,
        "response": f"registering {registration_id}, dossier {dossier_sha[:8]}",
        "resolved": {
            "registration_id": registration_id,
            "run_id": "widget-run-1",
            "dossier_sha": dossier_sha,
            "template": "templates/widget.json",
            "template_sha": "tmpl-sha-1",
            "fields": {"widget-owner": "team-a", "jam-threshold": "7"},
            "prerequisites": [],
            "view_sha": view_sha,
        },
    }


def _revoke_record(
    *, ts: str = "2026-07-08T01:00:00Z", registration_id: str = _REG_ID
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ts": ts,
        "scope_kind": "registration",
        "scope_id": registration_id,
        "block": reg.REVOKE_BLOCK,
        "response": f"overturning {registration_id}: widget line discontinued",
        "resolved": {"registration_id": registration_id, "reason": "line discontinued"},
    }


# --- vocabulary pins (the DOSSIER_SOURCES equality-pin pattern) --------------


def test_prerequisite_kinds_is_the_closed_mechanism_noun_set() -> None:
    """``PREREQUISITE_KINDS`` equals the R3 closed set — exactly (equality pin).

    Every member is a core MECHANISM noun; adding one is a reviewed vocabulary
    change that must land here. Equality (not subset) so an ad-hoc kind cannot
    slip in.
    """
    expected = frozenset(
        {"notebook-audit", "reproduction", "scope-budget", "pack-receipt", "attestation"}
    )
    assert frozenset(reg.PREREQUISITE_KINDS) == expected


def test_status_vocabulary_is_the_closed_r7_set() -> None:
    expected = frozenset({"current", "stale", "revoked", "superseded", "absent"})
    assert frozenset(reg.STATUSES) == expected


def test_block_family_is_the_reviewed_set() -> None:
    """R6: the family is registration + revoke + review + conformance-verdict.

    ``registration-review`` (T6) and ``conformance-verdict`` (live-conformance T7)
    are the reviewed additions; this pin fails loudly if another block is added
    without review (equality, not subset). Membership + intent are exercised by
    ``test_family_set_admits_the_review_block``.
    """
    expected = frozenset(
        {"registration", "registration-revoke", "registration-review", "conformance-verdict"}
    )
    assert frozenset(reg.REGISTRATION_BLOCK_FAMILY) == expected
    assert reg.SUBJECT_KIND == "dossier"


# --- the template loader (R5): structure-only, every refusal fires ----------


def test_parse_template_accepts_a_toy_template() -> None:
    tmpl = reg.parse_template(_TOY_TEMPLATE, template_sha="sha-1")
    assert tmpl.fields == ("widget-owner", "jam-threshold")
    assert tmpl.template_sha == "sha-1"
    assert [p.slot for p in tmpl.prerequisites] == ["widget-audit", "widget-repro", "widget-sign"]
    repro = tmpl.prerequisites[1]
    assert repro.kind == "reproduction"
    # requires structure is preserved but its KEYS are not interpreted here (T4).
    assert repro.requires == {"min_n": 2, "scales": ["main"]}


def test_load_template_reads_raw_bytes_sha_and_parses(tmp_path: Path) -> None:
    path = tmp_path / "widget.json"
    raw = json.dumps(_TOY_TEMPLATE).encode("utf-8")
    path.write_bytes(raw)
    tmpl = reg.load_template(path)
    # raw-bytes sha, NOT normalize_source (a template is bind-as-data).
    assert tmpl.template_sha == hashlib.sha256(raw).hexdigest()
    assert tmpl.fields == ("widget-owner", "jam-threshold")


def test_load_template_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="no default template"):
        reg.load_template(tmp_path / "does-not-exist.json")


def test_load_template_refuses_non_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("this is not json", encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="not valid UTF-8 JSON"):
        reg.load_template(path)


def test_parse_template_refuses_empty_fields() -> None:
    with pytest.raises(errors.SpecInvalid, match="non-empty list of field slugs"):
        reg.parse_template({"fields": []}, template_sha="s")


def test_parse_template_refuses_empty_field_slug() -> None:
    with pytest.raises(errors.SpecInvalid, match="field slug"):
        reg.parse_template({"fields": [""]}, template_sha="s")


def test_parse_template_refuses_non_slug_field() -> None:
    with pytest.raises(errors.SpecInvalid, match="field slug"):
        reg.parse_template({"fields": ["has space"]}, template_sha="s")


def test_parse_template_refuses_duplicate_field_slug() -> None:
    with pytest.raises(errors.SpecInvalid, match="duplicate field slug"):
        reg.parse_template({"fields": ["widget-owner", "widget-owner"]}, template_sha="s")


def test_parse_template_refuses_unknown_prerequisite_kind() -> None:
    bad = {"fields": _TOY_FIELDS, "prerequisites": [{"slot": "x", "kind": "backtest"}]}
    with pytest.raises(errors.SpecInvalid, match="not one of the closed PREREQUISITE_KINDS"):
        reg.parse_template(bad, template_sha="s")


def test_parse_template_refuses_non_dict_requires() -> None:
    bad = {
        "fields": _TOY_FIELDS,
        "prerequisites": [{"slot": "x", "kind": "reproduction", "requires": ["not", "a", "dict"]}],
    }
    with pytest.raises(errors.SpecInvalid, match="'requires' must be a mapping"):
        reg.parse_template(bad, template_sha="s")


def test_parse_template_refuses_requires_on_attestation_kind() -> None:
    """R3: the generic ``attestation`` kind accepts NO ``requires``."""
    bad = {
        "fields": _TOY_FIELDS,
        "prerequisites": [{"slot": "x", "kind": "attestation", "requires": {"min_n": 1}}],
    }
    with pytest.raises(errors.SpecInvalid, match="accepts no 'requires'"):
        reg.parse_template(bad, template_sha="s")


def test_parse_template_refuses_duplicate_slot() -> None:
    bad = {
        "fields": _TOY_FIELDS,
        "prerequisites": [
            {"slot": "dup", "kind": "attestation"},
            {"slot": "dup", "kind": "notebook-audit"},
        ],
    }
    with pytest.raises(errors.SpecInvalid, match="duplicate slot"):
        reg.parse_template(bad, template_sha="s")


# --- the chain entry (R3): full address, refusals fire ----------------------


def test_parse_chain_entry_accepts_a_full_address() -> None:
    entry = reg.parse_chain_entry(
        {
            "slot": "widget-repro",
            "kind": "reproduction",
            "subject_id": "widget-repro-run-9",
            "content_sha": "repro-sha-1",
            "requires": {"min_n": 2},
        }
    )
    assert entry.slot == "widget-repro"
    assert entry.subject_id == "widget-repro-run-9"
    assert entry.content_sha == "repro-sha-1"
    assert entry.requires == {"min_n": 2}


def test_parse_chain_entry_refuses_unknown_kind() -> None:
    with pytest.raises(errors.SpecInvalid, match="not one of the closed PREREQUISITE_KINDS"):
        reg.parse_chain_entry(
            {"slot": "x", "kind": "risk-check", "subject_id": "s", "content_sha": "c"}
        )


def test_parse_chain_entry_refuses_bare_slug_without_address() -> None:
    """A bare slug (no subject_id/content_sha) cannot be checked for currency."""
    with pytest.raises(errors.SpecInvalid, match="'subject_id' must be a non-empty"):
        reg.parse_chain_entry({"slot": "x", "kind": "notebook-audit"})


def test_parse_chain_entry_refuses_empty_content_sha() -> None:
    with pytest.raises(errors.SpecInvalid, match="'content_sha' must be a non-empty"):
        reg.parse_chain_entry(
            {"slot": "x", "kind": "notebook-audit", "subject_id": "a", "content_sha": ""}
        )


def test_parse_chain_entry_refuses_requires_on_attestation_kind() -> None:
    with pytest.raises(errors.SpecInvalid, match="accepts no 'requires'"):
        reg.parse_chain_entry(
            {
                "slot": "x",
                "kind": "attestation",
                "subject_id": "a",
                "content_sha": "c",
                "requires": {"anything": 1},
            }
        )


# --- the reduction (R7): absent / current / stale / revoked / superseded -----


def test_reduce_absent_when_no_records() -> None:
    status = reg.reduce_registration([], registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA)
    assert status.status == reg.ABSENT
    assert status.winner is None
    assert status.registered_at is None
    assert status.superseded == ()


def test_reduce_current_when_live_sha_matches() -> None:
    records = [_reg_record()]
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA
    )
    assert status.status == reg.CURRENT
    assert status.registered_at == "2026-07-08T00:00:00Z"
    assert status.winner is not None
    assert status.winner["run_id"] == "widget-run-1"


def test_reduce_stale_when_dossier_drifted() -> None:
    """Failure class 4 closed for free: a moved dossier reads stale at read time."""
    records = [_reg_record(dossier_sha=_DOSSIER_SHA)]
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha="dossier-sha-MOVED"
    )
    assert status.status == reg.STALE


def test_reduce_stale_when_live_sha_unrecomputable() -> None:
    records = [_reg_record()]
    status = reg.reduce_registration(records, registration_id=_REG_ID, live_dossier_sha=None)
    assert status.status == reg.STALE


def test_reduce_revoked_when_newest_is_a_revoke() -> None:
    """R7: a newest overturn wins → revoked, even over a current dossier sha."""
    records = [
        _reg_record(ts="2026-07-08T00:00:00Z"),
        _revoke_record(ts="2026-07-08T01:00:00Z"),
    ]
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA
    )
    assert status.status == reg.REVOKED
    assert status.winner is not None
    assert status.winner["reason"] == "line discontinued"


def test_reduce_supersession_newer_registration_wins() -> None:
    """R7: re-registration under the same id makes the older record superseded."""
    old = _reg_record(dossier_sha="dossier-old", ts="2026-07-08T00:00:00Z", view_sha="v-old")
    new = _reg_record(dossier_sha="dossier-new", ts="2026-07-08T02:00:00Z", view_sha="v-new")
    status = reg.reduce_registration(
        [old, new], registration_id=_REG_ID, live_dossier_sha="dossier-new"
    )
    assert status.status == reg.CURRENT
    assert status.winner is not None
    assert status.winner["dossier_sha"] == "dossier-new"
    # the older registration is superseded, and surfaced for history views.
    assert len(status.superseded) == 1
    assert status.superseded[0]["dossier_sha"] == "dossier-old"


def test_reduce_ignores_other_registration_ids() -> None:
    """A mixed journal reduces per-id without a re-written filter loop."""
    mine = _reg_record(registration_id=_REG_ID)
    other = _reg_record(registration_id="widget-batch-OTHER", dossier_sha="other-sha")
    status = reg.reduce_registration(
        [other, mine], registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA
    )
    assert status.status == reg.CURRENT
    assert status.winner is not None
    assert status.winner["registration_id"] == _REG_ID


def test_reduce_skips_malformed_records() -> None:
    records: list[dict[str, Any]] = [
        {"block": "registration", "resolved": "not-a-dict"},
        {"garbage": True},
        _reg_record(),
    ]
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA
    )
    assert status.status == reg.CURRENT


# --- the "one kernel" route-through pin (enforcement-map row) ----------------


def test_reduction_routes_drift_through_the_kernel() -> None:
    """R7: the drift verdict is ``attestation.reduce``'s, never re-inlined here.

    Mirrors ``test_reduction_routes_drift_through_the_kernel`` in the notebook
    suite (the ``test_layers_share_one_drift_predicate`` precedent).
    """
    src = inspect.getsource(reg.reduce_registration)
    assert "attestation.reduce(" in src, (
        "reduce_registration must route the current/stale drift verdict through "
        "the attestation kernel, never re-inline a newest-first or sha-compare."
    )
    # And it must not re-inline the raw comparison the kernel owns.
    assert "content_sha ==" not in src and "dossier_sha ==" not in src


def test_reduction_verdict_matches_the_kernel_directly() -> None:
    """The reduction's current/stale answer is exactly the kernel's on the same input."""
    records = [_reg_record()]
    projected = [reg._project_registration(records[0], _REG_ID)]
    kernel_current = attestation.reduce(
        [p for p in projected if p], current_sha=_DOSSIER_SHA, subject_id=_REG_ID
    )
    assert kernel_current == attestation.CURRENT
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA
    )
    assert status.status == reg.CURRENT


# --- T6: the conformance declaration + the horizon consult -------------------
# (docs/design/live-conformance.md C-declare / C-horizon)

_NOW = "2026-07-10T00:00:00Z"


def _reg_record_with_horizon(
    horizon: str,
    *,
    ts: str = "2026-07-08T00:00:00Z",
    dossier_sha: str = _DOSSIER_SHA,
) -> dict[str, Any]:
    """A registration record carrying an opt-in ``conformance`` declaration."""
    rec = _reg_record(ts=ts, dossier_sha=dossier_sha)
    rec["resolved"]["conformance"] = {
        "baseline": {"path": "baseline.jsonl", "sha256": "b" * 64},
        "min_window_n": 20,
        "review_horizon": horizon,
    }
    return rec


def _review_record(
    horizon: str,
    *,
    ts: str,
    dossier_sha: str = _DOSSIER_SHA,
    registration_id: str = _REG_ID,
) -> dict[str, Any]:
    """A registration-review re-affirmation record (C-horizon shape)."""
    return {
        "schema_version": 1,
        "ts": ts,
        "scope_kind": "registration",
        "scope_id": registration_id,
        "block": reg.REGISTRATION_REVIEW_BLOCK,
        "response": f"re-affirming {registration_id}, dossier {dossier_sha[:8]}",
        "resolved": {
            "registration_id": registration_id,
            "dossier_sha": dossier_sha,
            "review_horizon": horizon,
        },
    }


def test_family_set_admits_the_review_block() -> None:
    """R6: the maintained family gained ``registration-review`` + ``conformance-verdict``."""
    assert reg.REGISTRATION_REVIEW_BLOCK == "registration-review"
    assert reg.CONFORMANCE_VERDICT_BLOCK == "conformance-verdict"
    assert set(reg.REGISTRATION_BLOCK_FAMILY) == {
        "registration",
        "registration-revoke",
        "registration-review",
        "conformance-verdict",
    }


def test_declaration_absent_is_none() -> None:
    """Opt-in (D7): no conformance block → None, no machinery, byte-identical."""
    assert reg.parse_conformance_declaration({}) is None
    assert reg.parse_conformance_declaration({"conformance": None}) is None


def test_declaration_roundtrips_through_the_one_validator() -> None:
    decl = reg.parse_conformance_declaration(
        {
            "conformance": {
                "baseline": {"path": "b.jsonl", "sha256": "s"},
                "keys": ["reading"],
                "min_window_n": 20,
                "review_horizon": "2026-12-01T00:00:00Z",
            }
        }
    )
    assert decl is not None
    assert decl.baseline.path == "b.jsonl"
    assert decl.keys == ("reading",)
    assert decl.min_window_n == 20
    assert decl.review_horizon == "2026-12-01T00:00:00Z"


def test_declaration_unknown_key_refused_loud() -> None:
    """R4: an opted-in requirement core cannot check must never silently pass."""
    with pytest.raises(errors.SpecInvalid, match="unknown key"):
        reg.parse_conformance_declaration(
            {
                "conformance": {
                    "baseline": {"path": "b", "sha256": "s"},
                    "min_window_n": 20,
                    "cadence": "weekly",
                }
            }
        )


def test_declaration_routes_through_conformance_validator() -> None:
    """Never a second validator — the routing calls the ONE declaration validator."""
    src = inspect.getsource(reg.parse_conformance_declaration)
    assert "conformance.validate_declaration(" in src


def test_horizon_lapsed_reduces_stale_with_cause() -> None:
    records = [_reg_record_with_horizon("2026-07-01T00:00:00Z")]  # before _NOW
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA, now=_NOW
    )
    assert status.status == reg.STALE
    assert status.stale_cause == reg.HORIZON_LAPSED


def test_horizon_not_yet_lapsed_stays_current() -> None:
    records = [_reg_record_with_horizon("2026-12-01T00:00:00Z")]  # after _NOW
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA, now=_NOW
    )
    assert status.status == reg.CURRENT
    assert status.stale_cause is None


def test_now_none_never_evaluates_horizon_byte_identical() -> None:
    # A lapsed horizon is INVISIBLE without now — the existing-caller path is exact.
    records = [_reg_record_with_horizon("2026-07-01T00:00:00Z")]
    without = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA
    )
    explicit_none = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA, now=None
    )
    assert without == explicit_none
    assert without.status == reg.CURRENT
    assert without.stale_cause is None


def test_review_record_extends_the_horizon() -> None:
    records = [
        _reg_record_with_horizon("2026-07-01T00:00:00Z", ts="2026-06-01T00:00:00Z"),
        _review_record("2026-12-01T00:00:00Z", ts="2026-06-15T00:00:00Z"),
    ]
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA, now=_NOW
    )
    assert status.status == reg.CURRENT
    assert status.stale_cause is None
    # a review is NEVER a winner nor a supersession — winner is still the registration.
    assert status.winner is not None
    assert status.winner["dossier_sha"] == _DOSSIER_SHA
    assert status.superseded == ()


def test_review_before_winning_registration_is_ignored() -> None:
    # Re-registration resets the clock; a review predating it referred to the OLD
    # registration and must not extend the new one's horizon.
    records = [
        _review_record("2026-12-01T00:00:00Z", ts="2026-05-01T00:00:00Z"),
        _reg_record_with_horizon("2026-07-01T00:00:00Z", ts="2026-06-01T00:00:00Z"),
    ]
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA, now=_NOW
    )
    assert status.status == reg.STALE
    assert status.stale_cause == reg.HORIZON_LAPSED


def test_drift_stale_takes_precedence_over_horizon() -> None:
    # A moved dossier is drift-stale (cause None); the horizon only lapses an
    # otherwise-CURRENT registration.
    records = [_reg_record_with_horizon("2026-07-01T00:00:00Z")]
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha="MOVED", now=_NOW
    )
    assert status.status == reg.STALE
    assert status.stale_cause is None


def test_revoke_ignores_horizon() -> None:
    records = [
        _reg_record_with_horizon("2026-07-01T00:00:00Z"),
        _revoke_record(ts="2026-07-08T05:00:00Z"),
    ]
    status = reg.reduce_registration(
        records, registration_id=_REG_ID, live_dossier_sha=_DOSSIER_SHA, now=_NOW
    )
    assert status.status == reg.REVOKED
    assert status.stale_cause is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
