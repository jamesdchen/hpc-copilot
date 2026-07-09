"""Unit + seat tests for the domain-pack receipt gate (domain-packs T9).

:func:`hpc_agent.ops.pack_gate.assert_pack_receipts_current` is the ONE
definition wired at TWO synchronous seats (``ops/resolve_submit_inputs``
pre-sidecar, ``ops/submit_flow`` pre-staging). It is opt-in + fail-safe (the
``ops/notebook_gate`` posture): with no ``packs`` block it is a byte-identical
no-op; opted in, every ``receipt_bindings`` slot must reduce to CURRENT + passed
or the submit is refused (:class:`errors.PackReceiptsMissing`), and a broken
setup (dangling manifest / unbound pack) is a loud :class:`errors.SpecInvalid`.
The seat tests pin that both seats route through the gate and fire BEFORE any
sidecar-write / staging-SSH work. Toy-domain vocabulary only.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.ops.pack_gate import assert_pack_receipts_current
from hpc_agent.state.pack_receipts import (
    PACK_BIND_BLOCK,
    PACK_RECEIPT_BLOCK,
    receipt_content_sha,
)

if TYPE_CHECKING:
    from pathlib import Path

_PACK = "toy-widgets"
_SLOT = "widget-audit"
_MANIFEST_REL = "packs/toy/manifest.json"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _raw_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_pack(experiment: Path) -> tuple[str, dict[str, Any]]:
    """Write a toy pack (manifest + one seam file). Returns (manifest_sha, manifest)."""
    pack_root = experiment / "packs" / "toy"
    pack_root.mkdir(parents=True, exist_ok=True)
    reader_blob = json.dumps(["widgets.load_widget"]).encode("utf-8")
    (pack_root / "readers.json").write_bytes(reader_blob)
    manifest = {
        "name": _PACK,
        "version": "1.0.0",
        "files": [{"path": "readers.json", "sha256": _raw_sha(reader_blob)}],
        "seams": {"reader_calls": "readers.json"},
        "fills_slots": [_SLOT],
    }
    manifest_blob = json.dumps(manifest).encode("utf-8")
    (pack_root / "manifest.json").write_bytes(manifest_blob)
    return _raw_sha(manifest_blob), manifest


def _journal_path(experiment: Path, pack: str = _PACK) -> Path:
    path = RepoLayout(experiment).hpc / "packs" / f"{pack}.decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_record(experiment: Path, record: dict[str, Any], *, pack: str = _PACK) -> None:
    path = _journal_path(experiment, pack)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _write_bind(experiment: Path, manifest: dict[str, Any], manifest_sha: str) -> None:
    _append_record(
        experiment,
        {
            "block": PACK_BIND_BLOCK,
            "resolved": {
                "pack": manifest["name"],
                "version": manifest["version"],
                "manifest_sha": manifest_sha,
                "files": manifest["files"],
                "seams": list(manifest["seams"]),
            },
        },
        pack=manifest["name"],
    )


def _write_receipt(
    experiment: Path,
    manifest: dict[str, Any],
    manifest_sha: str,
    *,
    slot: str = _SLOT,
    checked: dict[str, bytes] | None = None,
    passed: bool = True,
) -> None:
    """Journal a pack-receipt for *slot* at the CURRENT composite sha.

    *checked* maps experiment-relative paths → bytes; each file is written and the
    composite ``content_sha`` is computed server-style so the receipt reads CURRENT
    until something moves.
    """
    checked = {"data/widgets.csv": b"a,b\n1,2\n"} if checked is None else checked
    on_disk: dict[str, str] = {}
    for rel, data in checked.items():
        p = experiment / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        on_disk[rel] = _raw_sha(data)
    content_sha = receipt_content_sha(manifest_sha, on_disk)
    _append_record(
        experiment,
        {
            "block": PACK_RECEIPT_BLOCK,
            "resolved": {
                "pack": manifest["name"],
                "version": manifest["version"],
                "manifest_sha": manifest_sha,
                "slot": slot,
                "checked": list(checked),
                "passed": passed,
                "content_sha": content_sha,
                "attestor": "code",
            },
        },
        pack=manifest["name"],
    )


def _write_interview(
    experiment: Path,
    *,
    opted_in: bool = True,
    bindings: list[dict[str, str]] | None = None,
    manifest_rel: str = _MANIFEST_REL,
    pack: str = _PACK,
) -> None:
    doc: dict[str, Any] = {"goal": "toy"}
    if opted_in:
        doc["packs"] = [
            {
                "pack": pack,
                "manifest": manifest_rel,
                "receipt_bindings": bindings
                if bindings is not None
                else [{"slot": _SLOT, "pack": pack}],
            }
        ]
    (experiment / "interview.json").write_text(json.dumps(doc), encoding="utf-8")


# ── D7 fail-safe silence ─────────────────────────────────────────────────────


def test_no_interview_json_is_silent_noop(experiment: Path) -> None:
    assert_pack_receipts_current(experiment)  # no raise, no pack on disk needed


def test_interview_without_packs_is_silent_noop(experiment: Path) -> None:
    _write_interview(experiment, opted_in=False)
    assert_pack_receipts_current(experiment)  # no raise


def test_empty_receipt_bindings_pass_when_pack_current(experiment: Path) -> None:
    """A pack opted in for SEAM data (no receipt_bindings) gates on no receipt."""
    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)
    _write_interview(experiment, bindings=[])
    assert_pack_receipts_current(experiment)  # no raise


# ── opted-in PASS ────────────────────────────────────────────────────────────


def test_current_passed_receipt_passes(experiment: Path) -> None:
    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)
    _write_receipt(experiment, manifest, manifest_sha, passed=True)
    _write_interview(experiment)
    assert_pack_receipts_current(experiment)  # no raise


# ── opted-in REFUSAL: PackReceiptsMissing (precondition_failed) ───────────────


def test_missing_receipt_fires_naming_slot(experiment: Path) -> None:
    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)  # bound, but NO receipt
    _write_interview(experiment)

    with pytest.raises(errors.PackReceiptsMissing) as ei:
        assert_pack_receipts_current(experiment)
    msg = str(ei.value)
    assert _SLOT in msg
    assert "missing" in msg
    assert ei.value.error_code == "precondition_failed"
    assert ei.value.retry_safe is False
    assert "pack-record-receipt" in (ei.value.remediation or "")


def test_failed_receipt_fires(experiment: Path) -> None:
    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)
    _write_receipt(experiment, manifest, manifest_sha, passed=False)
    _write_interview(experiment)

    with pytest.raises(errors.PackReceiptsMissing, match="failed"):
        assert_pack_receipts_current(experiment)


def test_stale_receipt_fires_when_checked_file_drifts(experiment: Path) -> None:
    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)
    _write_receipt(experiment, manifest, manifest_sha, checked={"data/widgets.csv": b"a,b\n1,2\n"})
    _write_interview(experiment)
    # The checked file drifts AFTER the receipt → composite sha moves → STALE.
    (experiment / "data" / "widgets.csv").write_bytes(b"a,b\n9,9\n")

    with pytest.raises(errors.PackReceiptsMissing, match="stale"):
        assert_pack_receipts_current(experiment)


# ── opted-in BROKEN-setup: SpecInvalid (the T9 refusal split) ─────────────────


def test_missing_manifest_is_loud(experiment: Path) -> None:
    """Opted in but the manifest .json is absent → LOUD SpecInvalid naming the path."""
    _write_interview(experiment)  # no pack files on disk
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_pack_receipts_current(experiment)
    assert "manifest.json" in str(ei.value)  # names the (absolute) manifest path


def test_opted_in_without_bind_is_loud(experiment: Path) -> None:
    """Manifest on disk but never bound → dangling reference → loud SpecInvalid."""
    _build_pack(experiment)  # manifest present, NO bind journal
    _write_interview(experiment)
    with pytest.raises(errors.SpecInvalid, match="no CURRENT bind"):
        assert_pack_receipts_current(experiment)


def test_drifted_manifest_is_loud(experiment: Path) -> None:
    """Editing the manifest on disk after binding is drift → loud SpecInvalid."""
    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)
    _write_receipt(experiment, manifest, manifest_sha)
    _write_interview(experiment)
    # Re-write the manifest with different bytes → its raw sha no longer matches.
    (experiment / "packs" / "toy" / "manifest.json").write_text(
        json.dumps(manifest) + "  ", encoding="utf-8"
    )
    with pytest.raises(errors.SpecInvalid, match="no longer"):
        assert_pack_receipts_current(experiment)


def test_slot_bound_to_unopted_pack_is_loud(experiment: Path) -> None:
    """A receipt slot bound to a pack that is not opted in → loud SpecInvalid."""
    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)
    _write_interview(experiment, bindings=[{"slot": _SLOT, "pack": "some-other-pack"}])
    with pytest.raises(errors.SpecInvalid, match="some-other-pack"):
        assert_pack_receipts_current(experiment)


# ── SEAT: resolve-submit-inputs (pre-sidecar, S1 human boundary) ──────────────

_RESOLVE_SEAM = "hpc_agent.ops.resolve_submit_inputs"


def _resolve_atom_mocks(tmp_path: Path):
    """Mock the laptop-side atoms so resolve-submit-inputs reaches its pre-sidecar
    gate seat (mirrors tests/ops/test_notebook_gate.py)."""
    from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
    from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
    from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec

    (tmp_path / ".hpc").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hpc" / "tasks.py").write_text("# stub\n", encoding="utf-8")

    spec = ResolveSubmitInputsSpec(
        run_name="pi",
        submit=BuildSubmitSpecInput(
            profile="pi",
            cluster="h2",
            ssh_target="me@login.h2",
            remote_path="/scratch/me/exp",
            run_id="pi-abcd1234",
            cmd_sha="a" * 64,
            total_tasks=1,
            backend="sge",
        ),
        sidecar=WriteRunSidecarInput(
            run_id="pi-placeholder",
            cmd_sha="0" * 8,
            executor="python -m src.pi",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=1,
        ),
    )
    cr = {
        "run_id": "pi-abcd1234",
        "cmd_sha": "a" * 64,
        "trial_tokens": None,
        "trial_params": [{"x": 1}],
        "total": 1,
    }
    fp = {"found": False, "is_orphan": False, "status": None, "prior_run_id": None, "cluster": None}
    return spec, cr, fp


def test_resolve_seat_gate_fires_before_sidecar(tmp_path: Path) -> None:
    """Ordering proof (S1 seat): an opted-in pack with a MISSING receipt refuses at
    resolve BEFORE the per-run sidecar is written — the write is never reached."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    spec, cr, fp = _resolve_atom_mocks(tmp_path)
    manifest_sha, manifest = _build_pack(tmp_path)
    _write_bind(tmp_path, manifest, manifest_sha)  # bound, NO receipt
    _write_interview(tmp_path)

    def _no_sidecar(*_a: Any, **_k: Any) -> None:
        raise AssertionError("write_run_sidecar must not run — the gate is pre-sidecar")

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=cr),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=fp),
        mock.patch(f"{_RESOLVE_SEAM}.build_submit_spec", return_value={"run_id": "pi-abcd1234"}),
        mock.patch(f"{_RESOLVE_SEAM}.write_run_sidecar", _no_sidecar),
        pytest.raises(errors.PackReceiptsMissing, match=_SLOT),
    ):
        resolve_submit_inputs(tmp_path, spec=spec)


def test_resolve_seat_passes_when_receipts_current(tmp_path: Path) -> None:
    """The companion: a current+passed receipt clears the S1 gate and the resolved
    terminal writes the sidecar (the gate passed → the flow proceeds)."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    spec, cr, fp = _resolve_atom_mocks(tmp_path)
    manifest_sha, manifest = _build_pack(tmp_path)
    _write_bind(tmp_path, manifest, manifest_sha)
    _write_receipt(tmp_path, manifest, manifest_sha)
    _write_interview(tmp_path)

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=cr),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=fp),
        mock.patch(f"{_RESOLVE_SEAM}.build_submit_spec", return_value={"run_id": "pi-abcd1234"}),
        mock.patch(
            f"{_RESOLVE_SEAM}.write_run_sidecar", return_value={"path": "/x/pi-abcd1234.json"}
        ) as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=spec)

    assert res.stage_reached == "resolved"
    ws.assert_called_once()  # gate passed → the sidecar was written


# ── SEAT: submit-flow (pre-staging, before any rsync/SSH) ─────────────────────


def _submit_flow_spec(run_id: str = "pi-abcd1234"):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

    return SubmitFlowSpec(
        profile="pi",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/u/scratch/exp",
        job_name="pi",
        run_id=run_id,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        canary=False,
        job_env={"K": "v"},
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hpc_agent.state import run_record

    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


def test_submit_flow_seat_gate_fires_before_staging(
    experiment: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering proof (pre-staging seat): an opted-in pack with a MISSING receipt
    refuses in submit-flow BEFORE any rsync/deploy — the shared prelude is never
    reached."""
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.ops.submit_flow import submit_flow

    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)  # bound, NO receipt
    _write_interview(experiment)

    def _no_staging(*_a: Any, **_k: Any) -> None:
        raise AssertionError("_run_shared_prelude must not run — the gate is pre-staging")

    monkeypatch.setattr(sf, "_run_shared_prelude", _no_staging)

    with pytest.raises(errors.PackReceiptsMissing, match=_SLOT):
        submit_flow(experiment, spec=_submit_flow_spec())


def test_submit_flow_seat_passes_proceeds(
    experiment: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The companion: a current+passed receipt clears the pre-staging gate and the
    flow proceeds to staging (a sentinel raised from _run_shared_prelude)."""
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.ops.submit_flow import submit_flow
    from hpc_agent.state.runs import write_run_sidecar

    manifest_sha, manifest = _build_pack(experiment)
    _write_bind(experiment, manifest, manifest_sha)
    _write_receipt(experiment, manifest, manifest_sha)
    _write_interview(experiment)
    write_run_sidecar(
        experiment,
        run_id="pi-abcd1234",
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=10,
        tasks_py_sha="",
        remote_path="/u/scratch/exp",
    )

    class _ReachedStaging(RuntimeError):
        pass

    def _sentinel(*_a: Any, **_k: Any) -> None:
        raise _ReachedStaging("reached staging")

    monkeypatch.setattr(sf, "_run_shared_prelude", _sentinel)

    with pytest.raises(_ReachedStaging):
        submit_flow(experiment, spec=_submit_flow_spec())
