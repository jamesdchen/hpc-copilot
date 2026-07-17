"""Tests for the #222 per-campaign provenance manifest.

The manifest pairs each run_id / trial_token of a campaign with its full
{code sha, data sha, env hash, params, cluster} fingerprint in one diffable,
signable artifact, derived purely from the sidecars the submit path already
writes. Properties pinned:

* It groups runs by ``campaign_id`` and projects only the provenance
  allowlist (no leaking of arbitrary sidecar fields).
* v1-shaped sidecars (no ``data_sha``/``env_hash``) project those fields as
  ``null`` rather than dropping the run.
* The signature is deterministic over content and changes when any captured
  provenance value changes.
* The written file is self-attesting: stripping ``signature`` and re-hashing
  reproduces it.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.ops.provenance_manifest import (
    KNOWN_PROVENANCE_MANIFEST_SCHEMA_VERSIONS,
    PROVENANCE_MANIFEST_SCHEMA_VERSION,
    build_provenance_manifest,
    manifest_signature,
    verify_provenance_manifest,
    write_provenance_manifest,
)
from hpc_agent.state.runs import write_run_sidecar


def _write(experiment_dir: Path, run_id: str, **overrides: object) -> None:
    kwargs: dict = dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
    )
    kwargs.update(overrides)
    write_run_sidecar(experiment_dir, **kwargs)


def test_manifest_groups_runs_by_campaign(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "20260101-000001-aaaaaaa",
        campaign_id="camp-A",
        data_sha="a" * 64,
        env_hash="b" * 64,
        cluster="hoffman2",
    )
    _write(tmp_path, "20260101-000002-bbbbbbb", campaign_id="camp-B")
    _write(
        tmp_path,
        "20260101-000003-ccccccc",
        campaign_id="camp-A",
        data_sha="c" * 64,
        env_hash="d" * 64,
    )

    manifest = build_provenance_manifest(tmp_path, "camp-A")
    assert manifest["manifest_schema_version"] == PROVENANCE_MANIFEST_SCHEMA_VERSION
    assert manifest["campaign_id"] == "camp-A"
    assert manifest["run_count"] == 2
    run_ids = {r["run_id"] for r in manifest["runs"]}
    assert run_ids == {"20260101-000001-aaaaaaa", "20260101-000003-ccccccc"}

    first = next(r for r in manifest["runs"] if r["run_id"].endswith("aaaaaaa"))
    assert first["cmd_sha"] == "0" * 64
    assert first["tasks_py_sha"] == "1" * 64
    assert first["data_sha"] == "a" * 64
    assert first["env_hash"] == "b" * 64
    assert first["cluster"] == "hoffman2"


def test_manifest_projects_only_allowlist(tmp_path: Path) -> None:
    # ``executor`` / ``result_dir_template`` are on the sidecar but NOT in the
    # provenance projection — they must not leak into the signable artifact.
    _write(tmp_path, "20260101-000001-aaaaaaa", campaign_id="camp")
    run = build_provenance_manifest(tmp_path, "camp")["runs"][0]
    assert "executor" not in run
    assert "result_dir_template" not in run
    assert set(run) == {
        "run_id",
        "cmd_sha",
        "tasks_py_sha",
        "data_sha",
        "env_hash",
        "hpc_agent_version",
        "cluster",
        "profile",
        "submitted_at",
        "trial_tokens",
    }


def test_manifest_v1_shaped_sidecar_nulls_new_fields(tmp_path: Path) -> None:
    # A run with no data_sha/env_hash (older submit path) still appears; the
    # missing provenance is recorded as null, not dropped.
    _write(tmp_path, "20260101-000001-aaaaaaa", campaign_id="camp")
    run = build_provenance_manifest(tmp_path, "camp")["runs"][0]
    assert run["data_sha"] is None
    assert run["env_hash"] is None


def test_manifest_pairs_trial_tokens(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "20260101-000001-aaaaaaa",
        campaign_id="camp",
        trial_tokens=[10, 11],
    )
    run = build_provenance_manifest(tmp_path, "camp")["runs"][0]
    assert run["trial_tokens"] == [10, 11]


def test_manifest_empty_campaign_is_well_formed(tmp_path: Path) -> None:
    manifest = build_provenance_manifest(tmp_path, "nonexistent")
    assert manifest["run_count"] == 0
    assert manifest["runs"] == []
    assert manifest["campaign_id"] == "nonexistent"


def test_signature_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    _write(tmp_path, "20260101-000001-aaaaaaa", campaign_id="camp", data_sha="a" * 64)
    m1 = build_provenance_manifest(tmp_path, "camp")
    assert manifest_signature(m1) == manifest_signature(m1)

    # A changed captured provenance value changes the signature.
    write_run_sidecar(
        tmp_path,
        run_id="20260101-000001-aaaaaaa",
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py --seed $SEED",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=2,
        tasks_py_sha="1" * 64,
        campaign_id="camp",
        data_sha="f" * 64,  # changed
    )
    m2 = build_provenance_manifest(tmp_path, "camp")
    assert manifest_signature(m2) != manifest_signature(m1)


def test_write_manifest_is_self_attesting(tmp_path: Path) -> None:
    _write(tmp_path, "20260101-000001-aaaaaaa", campaign_id="camp", env_hash="e" * 64)
    target, written_obj = write_provenance_manifest(tmp_path, "camp")
    assert target == tmp_path / ".hpc" / "provenance" / "camp.json"
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == written_obj  # the returned object IS what was written

    sig = written.pop("signature")
    # Re-deriving the signature over the body (sans signature) reproduces it.
    assert manifest_signature(written) == sig


# --- R3 (v2): the wheel sha is a SIGNED manifest field ------------------------


def test_schema_version_is_v2() -> None:
    # The R3 bump: the signable manifest now covers the code VERSION.
    assert PROVENANCE_MANIFEST_SCHEMA_VERSION == 2
    assert PROVENANCE_MANIFEST_SCHEMA_VERSION in KNOWN_PROVENANCE_MANIFEST_SCHEMA_VERSIONS


def test_wheel_version_is_carried_and_signed(tmp_path: Path) -> None:
    # The wheel sha rides every run entry AND is covered by the signature —
    # tampering with it must break verification (THE point of the unit).
    _write(tmp_path, "20260101-000001-aaaaaaa", campaign_id="camp", hpc_agent_version="1.2.3")
    _, written = write_provenance_manifest(tmp_path, "camp")
    assert written["manifest_schema_version"] == 2
    assert written["runs"][0]["hpc_agent_version"] == "1.2.3"
    assert verify_provenance_manifest(written) is True

    tampered = json.loads(json.dumps(written))
    tampered["runs"][0]["hpc_agent_version"] = "9.9.9-evil"
    assert verify_provenance_manifest(tampered) is False, (
        "a flipped wheel sha must break the signature — the field is signed"
    )


def test_absent_wheel_projects_and_signs_an_explicit_null_marker() -> None:
    # A sidecar with no recorded version → an explicit ``null`` marker in the
    # projection (never a silent omission), and that null is part of the signed
    # body: turning it into a value breaks the signature.
    from hpc_agent.ops.provenance_manifest import project_run_provenance

    projected = project_run_provenance({"cmd_sha": "0" * 64})  # no hpc_agent_version key
    assert "hpc_agent_version" in projected
    assert projected["hpc_agent_version"] is None

    body = {
        "manifest_schema_version": 2,
        "campaign_id": "camp",
        "run_count": 1,
        "runs": [{"run_id": "r", **projected}],
    }
    signed = dict(body)
    signed["signature"] = manifest_signature(body)
    assert verify_provenance_manifest(signed) is True

    tampered = json.loads(json.dumps(signed))
    tampered["runs"][0]["hpc_agent_version"] = "smuggled"
    assert verify_provenance_manifest(tampered) is False, (
        "the signed null marker cannot be turned into a value without breaking the sig"
    )


def test_verify_accepts_a_v1_manifest_and_refuses_an_unknown_version() -> None:
    # Read-compat: a v1 manifest (no wheel field, signature over the v1 body)
    # STILL verifies against this v2 build — the signed manifest_schema_version
    # tells the verifier which field-set was hashed.
    v1_body = {
        "manifest_schema_version": 1,
        "campaign_id": "legacy",
        "run_count": 1,
        "runs": [
            {
                "run_id": "20250101-000001-legacy0",
                "cmd_sha": "0" * 64,
                "tasks_py_sha": "1" * 64,
                "data_sha": None,
                "env_hash": None,
                "cluster": "hoffman2",
                "profile": "p",
                "submitted_at": "2025-01-01T00:00:00Z",
                "trial_tokens": [],
            }
        ],
    }
    v1 = dict(v1_body)
    v1["signature"] = manifest_signature(v1_body)
    assert verify_provenance_manifest(v1) is True

    # A future/unknown bump is REFUSED, not silently trusted.
    future_body = dict(v1_body, manifest_schema_version=999)
    future = dict(future_body)
    future["signature"] = manifest_signature(future_body)
    assert verify_provenance_manifest(future) is False

    # Missing / empty signature → not verifiable.
    assert verify_provenance_manifest({"manifest_schema_version": 2}) is False
    assert verify_provenance_manifest({"manifest_schema_version": 2, "signature": ""}) is False


def test_write_manifest_sanitizes_campaign_in_filename(tmp_path: Path) -> None:
    _write(tmp_path, "20260101-000001-aaaaaaa", campaign_id="team/exp")
    target, _ = write_provenance_manifest(tmp_path, "team/exp")
    assert target.name == "team_exp.json"
    assert target.parent == tmp_path / ".hpc" / "provenance"


def test_sidecar_roundtrips_data_and_env(tmp_path: Path) -> None:
    # The sidecar write/read path carries the two new fields.
    from hpc_agent.state.runs import read_run_sidecar

    _write(tmp_path, "20260101-000001-aaaaaaa", data_sha="a" * 64, env_hash="b" * 64)
    data = read_run_sidecar(tmp_path, "20260101-000001-aaaaaaa")
    assert data["data_sha"] == "a" * 64
    assert data["env_hash"] == "b" * 64


def test_sidecar_backfills_data_and_env_to_none(tmp_path: Path) -> None:
    from hpc_agent.state.runs import read_run_sidecar

    _write(tmp_path, "20260101-000001-aaaaaaa")
    data = read_run_sidecar(tmp_path, "20260101-000001-aaaaaaa")
    assert data["data_sha"] is None
    assert data["env_hash"] is None


# --- provenance-manifest primitive (#312 Gap 2) -------------------------------


def test_primitive_writes_manifest_and_returns_summary(tmp_path: Path) -> None:
    from hpc_agent._wire.actions.provenance_manifest import ProvenanceManifestInput
    from hpc_agent.ops.provenance_manifest import provenance_manifest

    _write(tmp_path, "20260101-000001-aaaaaaa", campaign_id="camp", data_sha="a" * 64)
    out = provenance_manifest(
        experiment_dir=tmp_path, spec=ProvenanceManifestInput(campaign_id="camp")
    )
    target = Path(out["path"])
    assert target == tmp_path / ".hpc" / "provenance" / "camp.json"
    assert out["campaign_id"] == "camp"
    assert out["run_count"] == 1
    written = json.loads(target.read_text(encoding="utf-8"))
    # The returned signature matches the self-attesting file's.
    assert out["signature"] == written.pop("signature")
    assert manifest_signature(written) == out["signature"]
    assert written["runs"][0]["data_sha"] == "a" * 64


def test_primitive_unknown_campaign_yields_empty_manifest(tmp_path: Path) -> None:
    from hpc_agent._wire.actions.provenance_manifest import ProvenanceManifestInput
    from hpc_agent.ops.provenance_manifest import provenance_manifest

    out = provenance_manifest(
        experiment_dir=tmp_path, spec=ProvenanceManifestInput(campaign_id="ghost")
    )
    assert out["run_count"] == 0
    assert Path(out["path"]).is_file()


def test_primitive_is_registered_and_agent_facing() -> None:
    from hpc_agent._kernel.registry.primitive import get_meta, register_primitives

    register_primitives()
    meta = get_meta("provenance-manifest")
    assert meta.verb == "mutate"
    assert meta.agent_facing is True
    assert meta.idempotent is True
