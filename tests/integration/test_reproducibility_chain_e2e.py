"""End-to-end ACCEPTANCE test for the whole reproducibility chain.

Every link of the chain is covered by its own unit test elsewhere
(``tests/ops/test_extract_recipe.py``, ``test_cite_check.py``,
``test_provenance_manifest.py``, ``test_export_dossier.py``,
``tests/ops/aggregate/test_reduce_provenance_fields.py``,
``test_verify_reproduction.py``). What NO unit test does is walk the FULL chain
over ONE scenario and assert the links COHERE — the exact gap the settle_run /
BR-9 arity mismatch fell into (each unit green, the seam between them broken).

This test builds ONE synthetic-but-realistic campaign and drives the shipped
verbs across the whole "a stranger re-derives the citable table" standard,
asserting the numbers and identities stay consistent end to end:

    a campaign is reduced (REAL ``aggregate_flow``) to a citable table with real
    ``contributing_run_ids`` provenance  ->  extract-recipe walks it BACK to the
    minimal clean run-set (canary / superseded / dead-end excluded)  ->  a signed
    provenance manifest v3 carries the wheel + env-lock and REFUSES a tamper  ->
    cite-check matches a manuscript's numbers to the SEALED table and flags a
    typo  ->  the recipe seals into a dossier whose signature COVERS it  ->  the
    numbers are the SAME object at every hop  ->  a fresh reproduction of a
    contributing run verifies as a match.

Fixtures reuse the SAME real writers the per-unit tests use (``write_run_sidecar``
with campaign_id / env-lock, ``upsert_run``, the harvest-marker ledger) and the
SAME ``aggregate_flow`` transport-seam stubs as
``test_reduce_provenance_fields`` — no parallel harness. Hermetic: no SSH, no
scheduler, no real cluster binary (the three transport/reduce seams are stubbed
in-process, so ``aggregate_flow`` never leaves the box).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hpc_agent._wire.actions.export_dossier import ExportDossierSpec
from hpc_agent._wire.queries.cite_check import CiteCheckInput
from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput
from hpc_agent._wire.queries.verify_reproduction import VerifyReproductionSpec
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.aggregate_flow import AggregateFlowResult, aggregate_flow
from hpc_agent.ops.cite_check import cite_check
from hpc_agent.ops.export_dossier import export_dossier
from hpc_agent.ops.extract_recipe import extract_recipe
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.ops.provenance_manifest import (
    manifest_signature,
    verify_provenance_manifest,
    write_provenance_manifest,
)
from hpc_agent.ops.verify_reproduction import verify_reproduction
from hpc_agent.state.env_lock import STATUS_CAPTURED
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import stamp_run_sidecar_env_lock, write_run_sidecar

# ── the campaign shape (the proven ``_seed_campaign`` topology) ────────────────
_CAMPAIGN = "camp"
_GOOD = "exp-good-aaaaaaaa"  # contributing; THE citable run (reduced for real)
_CANARY = "exp-good-aaaaaaaa-canary"  # canary family sibling -> EXCLUDED
_OLD = "exp-old-bbbbbbbb"  # superseded lineage member -> EXCLUDED
_NEW = "exp-new-cccccccc"  # supersedes _OLD; contributing (harvested)
_DEAD = "exp-dead-dddddddd"  # never harvested -> dead-end EXCLUDED

_TS = "2026-07-17T12:00:00+00:00"
_WHEEL_SHA = "0.11.0+gfeedface"  # the wheel that PRODUCED the citable run
_ENV_LOCK_SHA = "e" * 64  # the resolved-environment lock (v3 signed field)
_CMD_SHA_GOOD = "a" * 64

# The FINAL reduced numbers the citable table seals — the reducer's own output.
_QLIKE = 0.9427
_N_SAMPLES = 1536
_SEALED_METRICS: dict[str, dict[str, Any]] = {"linear": {"qlike": _QLIKE, "n_samples": _N_SAMPLES}}


@dataclass(frozen=True)
class Chain:
    """The built scenario: the shared substrate every link assertion reads."""

    exp: Path
    campaign_id: str
    citable_run: str
    contributing: frozenset[str]
    table_path: Path
    sealed_on_disk: dict[str, Any]
    flow_result: AggregateFlowResult
    manifest: dict[str, Any]
    wheel_sha: str
    env_lock_sha: str


# ── fixture builders (the REAL writers, mirroring the per-unit tests) ──────────
def _record(run_id: str, *, campaign_id: str, supersedes: str = "") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/u/scratch/exp",
        job_name="job",
        job_ids=["12345678"],
        total_tasks=4,
        submitted_at=_TS,
        experiment_dir="/exp",
        campaign_id=campaign_id,
        supersedes=supersedes,
        status="complete",
    )


def _sidecar(exp: Path, run_id: str, *, campaign_id: str, **over: Any) -> None:
    kwargs: dict[str, Any] = dict(
        run_id=run_id,
        cmd_sha=f"cmd-{run_id}"[:64].ljust(64, "0"),
        hpc_agent_version="9.9.9",
        submitted_at=_TS,
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=4,
        tasks_py_sha=f"tsha-{run_id}"[:64].ljust(64, "1"),
        wave_map={},
        remote_path="/u/scratch/exp",
        cluster="hoffman2",
        profile="p",
        campaign_id=campaign_id,
    )
    kwargs.update(over)
    write_run_sidecar(exp, **kwargs)


def _harvest(exp: Path, run_id: str) -> None:
    """Write a durable harvest receipt so the run reads harvested (not dead-end)."""
    append_jsonl_line(
        harvest_marker_path(exp, run_id),
        {"run_id": run_id, "harvested_at": _TS, "harvest_ok": True},
    )


def _seed(
    exp: Path,
    run_id: str,
    *,
    campaign_id: str,
    supersedes: str = "",
    harvested: bool = True,
    **sidecar_over: Any,
) -> None:
    _sidecar(exp, run_id, campaign_id=campaign_id, **sidecar_over)
    upsert_run(exp, _record(run_id, campaign_id=campaign_id, supersedes=supersedes))
    if harvested:
        _harvest(exp, run_id)


def _pull_ok(**kw: Any) -> subprocess.CompletedProcess[str]:
    """Stub the ONE aggregate transport funnel (``af_module._pull``) — a local mkdir.

    Patching ``_pull`` (rather than ``rsync_pull``) is deliberate: ``_pull``
    branches between the legacy ``rsync_pull`` and O2's ``tar_ssh_pull`` engine, so
    a ``rsync_pull``-only patch is bypassed whenever the tar engine is active and
    the REAL SSH pull runs (an 11-minute timeout against a nonexistent cluster).
    Every reduce path (combiner-only + per-task fallback) funnels through ``_pull``
    and reads ``.returncode`` / ``.stderr``, both of which a CompletedProcess
    carries — so this one stub makes the reduce hermetic regardless of engine."""
    Path(kw["local_dir"]).mkdir(parents=True, exist_ok=True)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _build_chain(exp: Path, monkeypatch: pytest.MonkeyPatch) -> Chain:
    """Build the whole scenario end to end (seed -> reduce -> sign)."""
    # (1) Seed the campaign through the real writers: two contributing runs (one
    #     of them the head of a supersession pair), a canary sibling, the
    #     superseded old member, and a dead-end that never harvested.
    _seed(
        exp,
        _GOOD,
        campaign_id=_CAMPAIGN,
        cmd_sha=_CMD_SHA_GOOD,
        hpc_agent_version=_WHEEL_SHA,
        data_sha="d" * 64,
        data_manifest_sha="f" * 64,
        env_hash="c" * 64,
    )
    _seed(exp, _CANARY, campaign_id=_CAMPAIGN)
    _seed(exp, _OLD, campaign_id=_CAMPAIGN)
    _seed(exp, _NEW, campaign_id=_CAMPAIGN, supersedes=_OLD)
    _seed(exp, _DEAD, campaign_id=_CAMPAIGN, harvested=False)

    # (2) Reduce the citable run for REAL. Stub only the three transport/reduce
    #     seams (identical to test_reduce_provenance_fields) so aggregate_flow's
    #     OWN provenance walk + persistence run unchanged, in-process, no SSH.
    monkeypatch.delenv("HPC_CLUSTER_FINAL_REDUCE", raising=False)
    monkeypatch.setattr(af_module, "_pull", _pull_ok)
    monkeypatch.setattr(af_module, "reduce_partials", lambda _dir, **_kw: dict(_SEALED_METRICS))
    monkeypatch.setattr(af_module, "collect_wave_errors", lambda _dir, **_kw: [])
    flow_result = aggregate_flow(exp, spec=AggregateFlowSpec(run_id=_GOOD))

    table_path = exp / "_aggregated" / _GOOD / "metrics_aggregate.json"
    sealed_on_disk = json.loads(table_path.read_text(encoding="utf-8"))

    # (3) Stamp the resolved-environment lock, then SIGN the per-campaign
    #     provenance manifest (v3 carries wheel + env-lock).
    stamp_run_sidecar_env_lock(
        exp, _GOOD, env_lock_sha=_ENV_LOCK_SHA, env_lock_status=STATUS_CAPTURED
    )
    _target, manifest = write_provenance_manifest(exp, _CAMPAIGN)

    return Chain(
        exp=exp,
        campaign_id=_CAMPAIGN,
        citable_run=_GOOD,
        contributing=frozenset({_GOOD, _NEW}),
        table_path=table_path,
        sealed_on_disk=sealed_on_disk,
        flow_result=flow_result,
        manifest=manifest,
        wheel_sha=_WHEEL_SHA,
        env_lock_sha=_ENV_LOCK_SHA,
    )


@pytest.fixture
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journal_home: Path) -> Chain:
    return _build_chain(tmp_path, monkeypatch)


# ── LINK 1: extract-recipe walks the campaign back to the clean run-set ────────
def test_extract_recipe_returns_the_clean_contributing_set(chain: Chain) -> None:
    """The campaign -> minimal-run-set link: canary / superseded / dead-end are
    mechanically EXCLUDED (each disclosed + counted). Catches a break in the
    journal-walk -> exclusion seam (a canary suffix rule or lineage read that
    stopped firing would let a non-citable run into the recipe)."""
    recipe = extract_recipe(chain.exp, spec=ExtractRecipeInput(campaign_id=chain.campaign_id))

    assert set(recipe["minimal_run_ids"]) == set(chain.contributing)
    reasons = {e["run_id"]: e["reason"] for e in recipe["excluded"]}
    assert reasons[_CANARY] == "canary"
    assert reasons[_OLD] == "superseded"
    assert reasons[_DEAD].startswith("dead-end")
    assert len(recipe["excluded"]) == 3


def test_recipe_over_the_citable_table_has_no_run_set_gap(chain: Chain) -> None:
    """The aggregate-seed link: extract-recipe over the SEALED table reads the
    reducer's real ``contributing_run_ids`` (a first-class table->run-set link),
    so NO G4a 'link-absent' gap is disclosed. Catches the pre-Task-1 regression
    where a table kept no record of which runs fed it."""
    recipe = extract_recipe(
        chain.exp, spec=ExtractRecipeInput(aggregate_path=str(chain.table_path))
    )
    assert recipe["seed_kind"] == "aggregate"
    assert recipe["minimal_run_ids"] == [chain.citable_run]
    assert recipe["gaps"] == []  # first-class contributing_run_ids present


def test_recipe_fingerprints_carry_the_signed_wheel_and_env(chain: Chain) -> None:
    """Cross-unit: extract-recipe PREFERS the provenance manifest's SIGNED wheel
    + env-lock over the sidecar projection, disclosing the source per run.
    Catches a break in the extract-recipe <-> provenance-manifest seam (a stale
    sidecar silently overriding the signed value)."""
    recipe = extract_recipe(
        chain.exp, spec=ExtractRecipeInput(aggregate_path=str(chain.table_path))
    )
    (good,) = [r for r in recipe["runs"] if r["run_id"] == chain.citable_run]
    assert good["hpc_agent_version_source"] == "signed-manifest"
    assert good["hpc_agent_version"] == chain.wheel_sha
    assert good["env_lock_sha_source"] == "signed-manifest"
    assert good["env_lock_sha"] == chain.env_lock_sha
    assert len(recipe["recipe_signature"]) == 64


# ── LINK 2: the signed provenance manifest verifies and refuses a tamper ───────
def test_provenance_manifest_v3_verifies_and_a_tamper_fails(chain: Chain) -> None:
    """The manifest is self-attesting at schema v3 and SIGNS the wheel + env-lock:
    a genuine manifest verifies, and flipping EITHER signed field breaks the
    signature. This is the seam extract-recipe trusts above the sidecar."""
    assert chain.manifest["manifest_schema_version"] == 3
    assert verify_provenance_manifest(chain.manifest) is True

    good = next(r for r in chain.manifest["runs"] if r["run_id"] == chain.citable_run)
    assert good["hpc_agent_version"] == chain.wheel_sha
    assert good["env_lock_sha"] == chain.env_lock_sha

    flipped_wheel = json.loads(json.dumps(chain.manifest))
    for r in flipped_wheel["runs"]:
        if r["run_id"] == chain.citable_run:
            r["hpc_agent_version"] = "9.9.9-evil"
    assert verify_provenance_manifest(flipped_wheel) is False

    flipped_env = json.loads(json.dumps(chain.manifest))
    for r in flipped_env["runs"]:
        if r["run_id"] == chain.citable_run:
            r["env_lock_sha"] = "f" * 64
    assert verify_provenance_manifest(flipped_env) is False


# ── LINK 3: cite-check audits a manuscript against the SEALED table ────────────
def test_cite_check_matches_sealed_numbers_and_flags_a_typo(chain: Chain) -> None:
    """A manuscript citing the sealed numbers buckets them ``matched``; a typo'd
    digit no sealed value backs is ``uncitable``. The cited numbers are built
    FROM the on-disk sealed table, so this is the number->paper transcription
    link exercised against the real citing authority."""
    sealed = chain.sealed_on_disk["aggregated_metrics"]["linear"]
    qlike = str(sealed["qlike"])
    n = int(sealed["n_samples"])

    faithful = f"Our estimator attains a QLIKE of {qlike} across {n:,} held-out windows."
    ok = cite_check(
        chain.exp,
        spec=CiteCheckInput(manuscript_text=faithful, aggregate_path=str(chain.table_path)),
    )
    assert ok["clean"] is True
    kinds = {f["claim"]: f["kind"] for f in ok["findings"]}
    assert kinds.get(qlike) == "matched"
    assert kinds.get(f"{n:,}") == "matched"  # comma-grouped render reconciles

    # A fat-fingered transcription (0.9472 vs the sealed 0.9427).
    typo = "We report a QLIKE of 0.9472 on the held-out set."
    bad = cite_check(
        chain.exp, spec=CiteCheckInput(manuscript_text=typo, aggregate_path=str(chain.table_path))
    )
    assert bad["clean"] is False
    finding = next(f for f in bad["findings"] if f["claim"] == "0.9472")
    assert finding["kind"] == "uncitable"
    assert finding["nearest_chain_value"] == qlike  # offered as CONTEXT


# ── LINK 4: the recipe seals into a dossier whose signature covers it ──────────
def test_recipe_seals_into_the_dossier_and_the_signature_covers_it(chain: Chain) -> None:
    """The dossier bundles the derived recipe as a first-class SEALED member, and
    ``bundle_sha256`` is computed OVER that member — tampering the recipe entry
    moves the whole bundle signature. Catches a break in the
    export-dossier <-> extract-recipe seam (a recipe forked from a second walk,
    or a member left outside the seal)."""
    result = export_dossier(
        experiment_dir=chain.exp, spec=ExportDossierSpec(run_id=chain.citable_run)
    )

    entries = result.manifest["entries"]
    (recipe_entry,) = [e for e in entries if e["source"] == "recipe"]

    import zipfile

    with zipfile.ZipFile(result.archive_path) as zf:
        member = zf.read("recipe/recipe.json")
    sealed_recipe = json.loads(member)

    # Parity: the sealed recipe IS a direct extract-recipe on the same seed —
    # the dossier composes the shipped walk, never a divergent second recipe.
    direct = extract_recipe(chain.exp, spec=ExtractRecipeInput(run_id=chain.citable_run))
    assert sealed_recipe == direct
    assert sealed_recipe["seed_kind"] == "run"
    assert sealed_recipe["seed_ref"] == chain.citable_run
    # The dossier's recipe reflects the SIGNED manifest too (dossier ∘ recipe ∘
    # provenance-manifest all agree on the wheel source).
    (good,) = [r for r in sealed_recipe["runs"] if r["run_id"] == chain.citable_run]
    assert good["hpc_agent_version_source"] == "signed-manifest"

    # The entry sha binds the sealed bytes, and the member rides bundle_sha256.
    assert recipe_entry["sha256"] == hashlib.sha256(member).hexdigest()
    assert manifest_signature(entries) == result.bundle_sha256  # type: ignore[arg-type]
    tampered = [dict(e) for e in entries]
    for e in tampered:
        if e["source"] == "recipe":
            e["sha256"] = "0" * 64
    assert manifest_signature(tampered) != result.bundle_sha256  # type: ignore[arg-type]


# ── LINK 5: the numbers are the SAME object at every hop ───────────────────────
def test_numbers_are_consistent_across_the_whole_chain(chain: Chain) -> None:
    """The consistency spine — the value the reducer RETURNED, the value SEALED on
    disk, and the value cite-check MATCHES against are one and the same. Catches
    the streaming-vs-final divergence class: a plausible intermediate/streaming
    estimate that differs from the final reduce is ``uncitable``, never a false
    pass."""
    # (a) reducer output == the persisted citable table (no persist-time drift).
    assert chain.flow_result.aggregated_metrics == chain.sealed_on_disk["aggregated_metrics"]

    # (b) the sealed value on disk IS what cite-check pools + matches.
    sealed_qlike = str(chain.sealed_on_disk["aggregated_metrics"]["linear"]["qlike"])
    matched = cite_check(
        chain.exp,
        spec=CiteCheckInput(
            manuscript_text=f"The headline QLIKE is {sealed_qlike}.",
            aggregate_path=str(chain.table_path),
        ),
    )
    assert {f["claim"]: f["kind"] for f in matched["findings"]}.get(sealed_qlike) == "matched"

    # (c) a STREAMING/partial number that differs from the final reduce is caught
    #     — the "cite the number the machinery sealed, not an intermediate" rule.
    streaming = "An early streaming pass reported a QLIKE of 0.9315."
    diverged = cite_check(
        chain.exp,
        spec=CiteCheckInput(manuscript_text=streaming, aggregate_path=str(chain.table_path)),
    )
    assert diverged["clean"] is False
    assert {f["claim"]: f["kind"] for f in diverged["findings"]}.get("0.9315") == "uncitable"

    # (d) the recipe's contributing set and the citable table point at ONE
    #     artifact — cite-check + extract-recipe share the same seed_ref.
    recipe = extract_recipe(
        chain.exp, spec=ExtractRecipeInput(aggregate_path=str(chain.table_path))
    )
    assert recipe["seed_ref"] == matched["seed_ref"] == str(chain.table_path)


# ── LINK 6: a fresh reproduction of a contributing run verifies as a match ─────
def test_reproducing_the_citable_run_verifies_as_a_match(chain: Chain) -> None:
    """The terminal link of 'a stranger re-derives the citable table': a fresh run
    that reproduces the citable run and yields the SAME sealed numbers agrees with
    NO human needed, while a run that diverges ROUTES to a human — never a false
    auto-pass and never a raised error. Closes the recipe's own re-derivation step
    (reproduce-run -> aggregate -> verify) end to end.

    The deterministic invariant is ``needs_decision`` — an agreeing re-derivation
    is False, a diverging one is True — asserted directly (the exact
    ``stage_reached`` label, match / auto_cleared / mismatch / needs_verdict,
    depends on the determinism-fingerprint envelope tier and is not the contract
    the chain rests on)."""
    repro = _GOOD + "-repro"
    _sidecar(chain.exp, repro, campaign_id=chain.campaign_id, reproduces=chain.citable_run)
    upsert_run(chain.exp, _record(repro, campaign_id=chain.campaign_id))
    # The reproduction seals the SAME numbers the citable table did.
    repro_table = chain.exp / "_aggregated" / repro / "metrics_aggregate.json"
    repro_table.parent.mkdir(parents=True, exist_ok=True)
    repro_table.write_text(
        json.dumps({"aggregated_metrics": chain.sealed_on_disk["aggregated_metrics"]}),
        encoding="utf-8",
    )

    agree = verify_reproduction(
        chain.exp,
        spec=VerifyReproductionSpec(original_run_id=chain.citable_run, repro_run_id=repro),
    )
    assert agree.needs_decision is False  # an agreeing re-derivation needs no human
    assert agree.stage_reached in {"match", "auto_cleared"}

    # A divergent reproduction ROUTES to a human (exit-0 finding), never a raise.
    repro_table.write_text(
        json.dumps({"aggregated_metrics": {"linear": {"qlike": 0.5000, "n_samples": _N_SAMPLES}}}),
        encoding="utf-8",
    )
    diverge = verify_reproduction(
        chain.exp,
        spec=VerifyReproductionSpec(original_run_id=chain.citable_run, repro_run_id=repro),
    )
    assert diverge.needs_decision is True  # a diverging re-derivation is routed
    assert diverge.stage_reached in {"mismatch", "needs_verdict"}
