"""Conformance kit K7 — the STOCK in-toto / DSSE round-trip (``docs/design/
conformance-kit.md`` D-K4, "The kit assertion").

The PORTABILITY proof for K3's ``export-attestations`` projector: a THIRD
PARTY's tooling reads our attestations. K3 (``ops/export_attestations.py``) emits
one unsigned DSSE envelope per sealed dossier entry, each wrapping an in-toto
Statement v1. K7 exports a real bundle over a seeded toy run and then, with the
STOCK ``securesystemslib`` (DSSE envelope model) and ``in_toto_attestation`` (the
in-toto Statement metadata model) libraries, proves:

* **(a)** each DSSE envelope parses under the stock envelope model
  (``securesystemslib.dsse.Envelope.from_dict``) — ``payloadType`` / ``payload``
  / ``signatures`` fields, and the base64 ``payload`` decodes byte-exact;
* **(b)** each decoded payload validates as an in-toto **Statement v1** under the
  stock metadata model (``in_toto_attestation.v1.statement.Statement.validate``,
  via the generated protobuf ``json_format.ParseDict``);
* **(c)** subject digests round-trip BYTE-EXACT against the dossier manifest
  entries (the digests K3 copied verbatim from the seal survive the stock
  parse);
* **(d)** the unsigned-signatures posture is EXPLICIT — our envelope's
  ``signatures == []`` (the signing lane is reserved, ``docs/design/
  conformance-kit.md`` D-K4 "Unsigned v1, DSSE-ready").

**Scope of the claim, as the doc pins it (pre-implementation review
2026-07-07):** "verify" here means PARSE + subject-digest comparison, NOT DSSE
signature verification. The implementation-time CHECK the doc reserved — "if
stock tooling rejects empty-signature envelopes, narrow the claim" — RESOLVED in
our favour: ``securesystemslib.dsse.Envelope.from_dict`` ACCEPTS an empty
``signatures`` list (it round-trips to an empty signature map), so the full
parse/validate/digest round-trip holds. Leg (a) asserts that acceptance
explicitly, so a future stock-lib version that starts rejecting empty signatures
turns the assertion red rather than silently weakening the proof.

**The optional-dep guard.** ``in_toto_attestation`` + ``securesystemslib`` are
dev-deps of the kit's CI lane ONLY (installed in the conformance CI job the way
the plugins job installs jupytext); they NEVER enter core dependencies. Every
leg is ``pytest.importorskip``-guarded (at CALL time, so the module always
imports from the wheel), and the kit stays green without the optional deps. The
guard-can-fire discipline for that skip is proven by the always-runs leg in the
mirror unit test (``tests/conformance_kit/test_attestation_export_unit.py``).
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

if TYPE_CHECKING:
    from pathlib import Path


class _ToyBundle(NamedTuple):
    """The exported attestations bundle for a seeded toy run, plus its dossier tie-back."""

    lines: list[str]
    entry_by_path: dict[str, dict[str, Any]]
    bundle_sha256: str


def _seed_and_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ToyBundle:
    """Seed a toy run through the REAL writers and export its attestations.

    Mirrors K3's boundary-test seeding (``tests/contracts/
    test_attestation_export_boundary.py``, leg (d)): a run with a sidecar + a
    journal record + a decision-journal entry, seeded through the real writers,
    then exported with the real ``export-attestations`` verb. Returns the JSONL
    lines and the dossier signature's entries the projection tied back to, so the
    stock-library legs can check the digests round-trip.
    """
    from hpc_agent._wire.actions.export_attestations import ExportAttestationsSpec
    from hpc_agent.ops.export_attestations import export_attestations
    from hpc_agent.ops.export_dossier import compute_dossier_signature
    from hpc_agent.state import run_record
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    # Redirect the per-user journal home into the test's tmp dir.
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    experiment = tmp_path / "exp"
    experiment.mkdir()
    run_id = "20260101-000001-aaaaaaa"

    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
    )
    upsert_run(
        experiment,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/remote",
            job_name="p",
            job_ids=["9001"],
            total_tasks=2,
            submitted_at="2026-01-01T00:00:00Z",
            experiment_dir=str(run_id),
        ),
    )
    append_decision(experiment, scope_kind="run", scope_id=run_id, block="s1", response="y")

    result = export_attestations(
        experiment_dir=experiment, spec=ExportAttestationsSpec(run_id=run_id)
    )

    from pathlib import Path as _Path

    lines = [ln for ln in _Path(result.output_path).read_text(encoding="utf-8").splitlines() if ln]
    assert lines, "export produced no Statements for a seeded run"
    assert len(lines) == result.statement_count

    sig = compute_dossier_signature(experiment, run_id)
    assert result.bundle_sha256 == sig.bundle_sha256
    entry_by_path = {e["path"]: e for e in sig.entries}
    assert len(lines) == len(sig.entries)
    return _ToyBundle(lines=lines, entry_by_path=entry_by_path, bundle_sha256=sig.bundle_sha256)


def _stock_envelope_model() -> Any:
    """The stock DSSE envelope model, or SKIP — the optional-dep guard (call-time)."""
    dsse = pytest.importorskip("securesystemslib.dsse")
    return dsse.Envelope


def _stock_statement_model() -> tuple[Any, Any, Any]:
    """The stock in-toto Statement model + protobuf machinery, or SKIP.

    Returns ``(Statement, StatementPb, json_format)`` — the wrapper whose
    ``.validate()`` enforces the in-toto Statement-v1 rules, the generated
    protobuf message we parse our JSON payload into, and protobuf's
    ``json_format`` for that parse.
    """
    statement_mod = pytest.importorskip("in_toto_attestation.v1.statement")
    statement_pb2 = pytest.importorskip("in_toto_attestation.v1.statement_pb2")
    json_format = pytest.importorskip("google.protobuf.json_format")
    return statement_mod.Statement, statement_pb2.Statement, json_format


# --- (a) DSSE envelope parses under the stock envelope model -----------------


def test_each_envelope_parses_under_stock_dsse_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every exported line parses as a DSSE envelope under ``securesystemslib``.

    ``Envelope.from_dict`` reads the ``payloadType`` / ``payload`` / ``signatures``
    fields and base64-decodes the payload; we assert the decoded bytes match a
    byte-exact re-decode of the raw field and that the empty ``signatures`` list
    is ACCEPTED (round-tripping to an empty signature map) — the doc's reserved
    "does stock tooling reject empty signatures?" check, pinned as resolved.
    """
    from hpc_agent.ops.export_attestations import DSSE_PAYLOAD_TYPE

    Envelope = _stock_envelope_model()
    bundle = _seed_and_export(tmp_path, monkeypatch)

    for line in bundle.lines:
        raw = json.loads(line)
        envelope = Envelope.from_dict(raw)
        assert envelope.payload_type == DSSE_PAYLOAD_TYPE
        # base64 payload decodes byte-exact (the stock model does the decode).
        assert envelope.payload == base64.b64decode(raw["payload"])
        # empty signatures accepted — round-trips to an empty signature map.
        assert not envelope.signatures


# --- (b) decoded payload validates as an in-toto Statement v1 ----------------


def test_each_payload_validates_as_in_toto_statement_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every decoded payload validates as an in-toto Statement v1 (stock model).

    We parse the JSON payload into the generated ``Statement`` protobuf via
    ``json_format.ParseDict`` (proving the ``_type`` / ``subject`` / ``predicateType``
    / ``predicate`` shape maps onto the spec's protobuf) and call the stock
    wrapper's ``.validate()`` — the library's own Statement-v1 rule check
    (correct ``_type``, ≥1 subject, each subject digest set, predicateType + a
    predicate object present).
    """
    from hpc_agent.ops.export_attestations import IN_TOTO_STATEMENT_TYPE

    Envelope = _stock_envelope_model()
    Statement, StatementPb, json_format = _stock_statement_model()
    bundle = _seed_and_export(tmp_path, monkeypatch)

    for line in bundle.lines:
        envelope = Envelope.from_dict(json.loads(line))
        payload = json.loads(envelope.payload)
        pb = json_format.ParseDict(payload, StatementPb())
        statement = Statement.copy_from_pb(pb)
        statement.validate()  # raises on any Statement-v1 violation
        assert pb.type == IN_TOTO_STATEMENT_TYPE


# --- (c) subject digests round-trip byte-exact against the dossier manifest --


def test_subject_digests_round_trip_against_the_dossier_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stock-parsed subject digests equal the dossier manifest entries, byte-exact.

    The portability payoff: the ``sha256`` K3 copied VERBATIM from the seal
    survives the stock library's parse. For each envelope we read the subject
    ``name`` + digest OUT OF the generated protobuf (not our JSON) and compare to
    the dossier signature's entry the projection came from.
    """
    Envelope = _stock_envelope_model()
    Statement, StatementPb, json_format = _stock_statement_model()
    bundle = _seed_and_export(tmp_path, monkeypatch)

    seen: set[str] = set()
    for line in bundle.lines:
        envelope = Envelope.from_dict(json.loads(line))
        pb = json_format.ParseDict(json.loads(envelope.payload), StatementPb())
        assert len(pb.subject) == 1
        name = pb.subject[0].name
        assert name in bundle.entry_by_path, f"stock-parsed subject {name!r} is not a dossier entry"
        assert pb.subject[0].digest["sha256"] == bundle.entry_by_path[name]["sha256"]
        seen.add(name)

    # every dossier entry produced exactly one Statement (no drop, no dup).
    assert seen == set(bundle.entry_by_path)


# --- (d) the unsigned-signatures posture is explicit -------------------------


def test_unsigned_signatures_posture_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our envelope's ``signatures`` is EXACTLY ``[]`` — the reserved signing lane.

    Asserted against OUR emitted JSON (not the stock model, which normalizes to a
    signature map): v1 is unsigned by design, and adding a signature later
    changes nothing upstream (``docs/design/conformance-kit.md`` D-K4). This leg
    still ``importorskip``s the stock lib so it shares the guard tier — the kit is
    a single optional-dep lane.
    """
    _stock_envelope_model()  # share the optional-dep guard tier
    bundle = _seed_and_export(tmp_path, monkeypatch)

    for line in bundle.lines:
        envelope = json.loads(line)
        assert envelope["signatures"] == [], "v1 is UNSIGNED — signatures must be exactly []"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
