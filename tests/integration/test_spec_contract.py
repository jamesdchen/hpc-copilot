"""Layer A of the integration tier — **spec-contract** tests (block-drive.md §9).

Every orchestrator in this tree builds a ``dict`` spec and hands it across the
``hpc-agent <verb> --spec`` subprocess seam. A unit test that fakes that seam
never learns whether the dict is the *shape* the target verb's pydantic model
accepts — the exact miss that broke ``block-drive`` (it injected a top-level
``{"run_id": ...}`` into a block whose Spec nests ``run_id`` under a required
``monitor`` / ``aggregate`` / ``submit`` sub-object with ``extra="forbid"``).

This file LOCKS the invariant across the whole tree: **every spec an
orchestrator constructs must validate against the target verb's LIVE pydantic
model** (``spec_model_for`` / ``assert_valid_spec`` from the tier conftest). It
generalizes ``tests/_kernel/lifecycle/test_block_drive_specs.py`` (which pins
block-drive's own hops) to every orchestrator→verb spec construction:

1. the campaign driver's ``_run_cli_step`` dict (drive.py);
2. every ``block_chain`` chain hop's successor spec (all four families);
3. the detached-drive spec builders (detached.py);
4. a registry-wide net over the verb↔model↔schema triangle.

Layer A is SSH-free: pure model validation plus driving journal-only blocks
with fabricated on-disk state (a manifest / mocked run-state reads).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from pydantic import BaseModel

from hpc_agent._kernel.lifecycle import detached, drive
from hpc_agent._kernel.registry.primitive import get_registry
from hpc_agent.cli._dispatch import CliShape
from hpc_agent.infra import block_chain

from .conftest import assert_valid_spec, spec_model_for

pytestmark = pytest.mark.integration

# A run_id / campaign_id satisfying RunIdStrict / CampaignId (``^[A-Za-z0-9._\\-]+$``).
_RUN_ID = "ml_run_abcd1234"
_CAMPAIGN_ID = "camp_test_1"

# Repo root (…/tests/integration/test_spec_contract.py -> repo root).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS_DIR = _REPO_ROOT / "src" / "hpc_agent" / "schemas"


# ── 1. campaign driver: the {"run_id": ...} spec _run_cli_step builds ──────────


def test_run_cli_step_builds_a_bare_run_id_dict() -> None:
    """Pin the EXACT dict ``drive._run_cli_step`` constructs for a cli step.

    The driver builds ``json.dump({"run_id": run_id})`` — a bare top-level
    ``run_id``. If a future refactor nested it (or the verbs grew a required
    field), the assertions below would still need to reflect the real dict, so
    this source pin is the tripwire that the constructed shape is what the next
    tests validate.
    """
    src = inspect.getsource(drive._run_cli_step)
    assert '{"run_id": run_id}' in src, (
        "drive._run_cli_step no longer builds a bare {'run_id': run_id} dict; "
        "update the campaign-driver spec-contract tests to pin the new shape."
    )


def test_drive_cli_step_specs_validate() -> None:
    """The tick-loop's per-step ``{"run_id": ...}`` validates for both flow verbs.

    ``drive._run_cli_step`` hands the mapped verb the bare ``{"run_id": run_id}``
    spec pinned above. A step table maps ``monitor -> monitor-flow`` and
    ``aggregate -> aggregate-flow`` (the shape the deleted campaign driver used;
    an external loop supplies its own). Both models require only ``run_id``
    today — LOCK it so a future required field on either model is caught here
    instead of at a live tick.
    """
    for verb in ("monitor-flow", "aggregate-flow"):
        assert spec_model_for(verb) is not None, f"{verb} lost its spec_model"
        assert_valid_spec(verb, {"run_id": _RUN_ID})


# ── 2. block_chain chain hops → successor spec (all four families) ─────────────

# The successor hops the DRIVER chains in code (needs_decision=False + UNgated):
# it passes the predecessor's ``next_block.spec_hint`` VERBATIM, so the hint MUST
# validate against the successor's model. These are the reachable ungated hops
# (block-drive.md §2). The remaining ungated hops (``*_timeout`` keep-watching
# self-loops) carry needs_decision=True → the driver PARKS, so their spec_hint is
# a human-facing skeleton the LLM rebuilds into a ``resolved`` — never chained
# verbatim (asserted in ``test_ungated_hop_partition`` below).
_CHAINED_UNGATED_HOPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("status-snapshot", "snapshot_clean"),
        ("campaign-greenlight", "greenlit"),
        ("campaign-greenlight", "already_greenlit"),
        ("campaign-watch", "watching_complete"),
    }
)
# Ungated but needs_decision=True → the driver parks; spec_hint is human-facing.
_PARKING_UNGATED_HOPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("submit-s3", "watching_timeout"),
        ("status-watch", "watch_timeout"),
    }
)


def _non_none_hops() -> dict[tuple[str, str], str]:
    return {k: v for k, v in block_chain.SUCCESSORS.items() if v is not None}


def test_ungated_hop_partition() -> None:
    """Every ungated hop is either chained-verbatim or parks — no third bucket.

    Locks the partition so a NEW ungated successor hop cannot slip in without a
    reviewer deciding which bucket it lands in (and, if chained, adding its
    spec_hint validation below).
    """
    ungated = {k for k, v in _non_none_hops().items() if not block_chain.is_gated(v)}
    assert ungated == _CHAINED_UNGATED_HOPS | _PARKING_UNGATED_HOPS


def _drive_status_snapshot_next_block(tmp_path: Path) -> dict[str, Any]:
    """Drive the REAL ``status_snapshot`` on a live single run; return its next_block.

    Journal-only block — the only cluster surface is the run-state read, faked
    here (a fabricated sidecar keyed by ``load_run``) so the op runs SSH-free.
    """
    from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec
    from hpc_agent.ops import status_blocks

    rec = SimpleNamespace(
        run_id=_RUN_ID,
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        status="in_flight",
        last_status={"running": 4, "pending": 6},
        last_tick_at=None,
        last_seen_by_human_at=None,
        total_tasks=10,
    )
    with (
        mock.patch.object(status_blocks, "load_run", return_value=rec),
        mock.patch.object(status_blocks, "find_stalled_runs", return_value=[]),
        mock.patch.object(status_blocks, "mark_seen_by_human"),
    ):
        result = status_blocks.status_snapshot(
            tmp_path, spec=StatusSnapshotSpec(run_id=_RUN_ID, mark_seen=False)
        )
    assert result.stage_reached == "snapshot_clean"
    assert result.next_block is not None
    return dict(result.next_block)


def test_status_snapshot_chained_spec_hint_validates(tmp_path: Path) -> None:
    """status-snapshot → status-watch: the emitted spec_hint validates VERBATIM.

    The block nests ``run_id`` under ``monitor`` (``{"monitor": {"run_id": ...}}``);
    a bare top-level ``run_id`` would be a ``SpecInvalid`` the instant the driver
    chained it. Drives the real block to capture exactly what it emits.
    """
    nb = _drive_status_snapshot_next_block(tmp_path)
    assert nb["verb"] == "status-watch"
    assert nb["spec_hint"] == {"monitor": {"run_id": _RUN_ID}}
    assert_valid_spec("status-watch", nb["spec_hint"])


def _write_campaign_manifest(experiment_dir: Path, *, greenlit: bool) -> None:
    from hpc_agent.meta.campaign.manifest import write_manifest

    write_manifest(
        experiment_dir,
        campaign_id=_CAMPAIGN_ID,
        goal="spec-contract probe",
        greenlit=greenlit,
        greenlit_at="2026-07-03T00:00:00+00:00" if greenlit else None,
    )


def test_campaign_greenlight_chained_spec_hint_validates(tmp_path: Path) -> None:
    """campaign-greenlight → campaign-watch (both greenlit stages) validates VERBATIM.

    Drives the REAL greenlight block (a pure manifest read — SSH-free) on both
    ungated terminators that chain: an already-greenlit manifest (idempotent
    re-read) and a fresh confirm. Each emits ``{"campaign_id": ...}``, which must
    validate against CampaignWatchSpec.
    """
    # already_greenlit: a manifest already carrying the marker → idempotent re-read.
    from hpc_agent._wire.workflows.campaign_blocks import CampaignGreenlightSpec
    from hpc_agent.meta.campaign import blocks as campaign_blocks

    _write_campaign_manifest(tmp_path, greenlit=True)
    ag = campaign_blocks.campaign_greenlight(
        tmp_path, spec=CampaignGreenlightSpec(campaign_id=_CAMPAIGN_ID)
    )
    assert ag.stage_reached == "already_greenlit"
    assert ag.next_block is not None
    ag_nb = dict(ag.next_block)
    assert ag_nb["verb"] == "campaign-watch"
    assert_valid_spec("campaign-watch", ag_nb["spec_hint"])

    # greenlit: a non-greenlit manifest + confirm=True → the block stamps + chains.
    _write_campaign_manifest(tmp_path, greenlit=False)
    gl = campaign_blocks.campaign_greenlight(
        tmp_path,
        spec=CampaignGreenlightSpec(campaign_id=_CAMPAIGN_ID, confirm=True, journal=False),
    )
    assert gl.stage_reached == "greenlit"
    assert gl.next_block is not None
    gl_nb = dict(gl.next_block)
    assert gl_nb["verb"] == "campaign-watch"
    assert_valid_spec("campaign-watch", gl_nb["spec_hint"])


def test_campaign_watch_complete_spec_hint_validates() -> None:
    """campaign-watch → campaign-complete: the hint the block emits validates VERBATIM.

    The ``watching_complete`` terminator fires only under a live convergence
    decision (``campaign_advance`` → ``stop_converged``), which needs cluster run
    state — so per block-drive.md §2 we build the hint the way the block does,
    through its own ``_next_block`` helper with the same ``campaign_id`` kwarg the
    terminator passes, and validate that.
    """
    from hpc_agent.meta.campaign import blocks as campaign_blocks

    nb = campaign_blocks._next_block(
        "campaign-watch",
        "watching_complete",
        "a stop criterion fired; build the completion brief.",
        campaign_id=_CAMPAIGN_ID,
    )
    assert nb is not None
    assert nb["verb"] == "campaign-complete"
    assert_valid_spec("campaign-complete", nb["spec_hint"])


def test_parking_ungated_hops_are_decision_terminators() -> None:
    """The ungated ``*_timeout`` hops PARK — their spec_hint is not chained verbatim.

    Both keep-watching self-loops carry ``needs_decision=True`` in their block
    source, so ``block_drive._chain`` parks (the human rebuilds a ``resolved``)
    rather than passing the spec_hint verbatim. Their hint is therefore a
    human-facing skeleton, not a chain-critical spec — documented here so the
    partition in ``test_ungated_hop_partition`` has a rationale for this bucket.
    """
    from hpc_agent.ops import status_blocks, submit_blocks

    for fn in (submit_blocks.submit_s3, status_blocks.status_watch):
        src = inspect.getsource(fn)
        assert 'stage_reached="watching_timeout"' in src or 'stage_reached="watch_timeout"' in src
        # The timeout terminator is a decision point (needs_decision=True).
        assert "needs_decision=True" in src


def test_gated_successors_go_through_resolved() -> None:
    """Gated hops pass a committed ``resolved`` (not spec_hint) — pin the gated set.

    For every hop whose successor is greenlight-gated, the driver PARKS for the
    human ``y`` (``block_drive._chain``); the LLM commits a correctly-shaped
    ``resolved`` spec, so there is no verbatim spec_hint to validate. We instead
    pin that the successor IS gated and that a valid instance of its model CAN
    exist (a real BaseModel with a live spec_model), documenting the resolved
    path. No fake ``resolved`` is constructed (block-drive.md §3).
    """
    gated_successors = {v for k, v in _non_none_hops().items() if block_chain.is_gated(v)}
    assert gated_successors == set(block_chain.GATED_BLOCKS)
    for successor in gated_successors:
        assert block_chain.is_gated(successor)
        model = spec_model_for(successor)
        assert model is not None, f"gated {successor} has no spec_model to build a resolved against"
        assert issubclass(model, BaseModel)


def test_every_successor_verb_is_dispatchable() -> None:
    """Completeness net: every non-None successor has a live spec_model.

    The driver can only validate/dispatch a successor it has a model for; a
    successor verb without one would fail at the ``hpc-agent <verb> --spec`` seam.
    Also pins ``GATED_BLOCKS ⊆ {verbs with a spec_model}`` so a gated block can
    never be routed without a model to validate its ``resolved`` against.
    """
    successors = set(_non_none_hops().values())
    for successor in successors:
        assert spec_model_for(successor) is not None, (
            f"successor {successor} has no spec_model — the driver cannot "
            "validate or dispatch a spec for it"
        )
    for gated in block_chain.GATED_BLOCKS:
        assert spec_model_for(gated) is not None, f"gated {gated} has no spec_model"


# ── 3. detached spec builders (detached.py) ────────────────────────────────────


def test_build_status_pipeline_spec_validates() -> None:
    """``detached.build_status_pipeline_spec`` builds a valid ``status-pipeline`` spec.

    The detached status runner maps ``run --workflow status`` fields to a
    ``status-pipeline`` spec in code (no LLM renders it): ``run_id`` nests under a
    ``monitor`` MonitorFlowSpec. The built dict must validate against
    ``status-pipeline``'s model — the same seam the LLM-rendered path used to own.
    """
    spec = detached.build_status_pipeline_spec({"run_id": _RUN_ID})
    assert spec == {"monitor": {"run_id": _RUN_ID}}
    assert_valid_spec("status-pipeline", spec)

    # Optional pass-throughs land inside the nested monitor spec and still validate.
    spec2 = detached.build_status_pipeline_spec(
        {"run_id": _RUN_ID, "poll_interval_seconds": 30, "wall_clock_budget_seconds": 600}
    )
    assert spec2["monitor"]["poll_interval_seconds"] == 30
    assert_valid_spec("status-pipeline", spec2)


def test_submit_block_detach_is_a_pass_through_writer() -> None:
    """The submit-block detach path WRITES the caller's spec — it builds no dict.

    ``launch_submit_block_detached`` does not construct a spec; it persists the
    already-resolved spec the parent verb hands it (with ``detach`` forced off)
    and re-invokes the SAME verb via ``--spec``. So there is no orchestrator-built
    dict to validate here beyond what the block itself validates on dispatch. We
    pin the contract it DOES enforce — the run_id it digs out for the poll handle
    lives at ``spec.submit.submit.run_id`` — using a minimal valid SubmitS2Spec so
    a shape drift in that nesting is caught.
    """
    from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
    from hpc_agent._wire.workflows.submit_blocks import SubmitS2Spec
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

    flow = SubmitFlowSpec(
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=_RUN_ID,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        job_env={"K": "v"},
        canary=True,
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )
    resolved = SubmitS2Spec(
        submit=SubmitAndVerifySpec(submit=flow, poll_interval_sec=1, wait_budget_sec=5),
        detach=False,
    ).model_dump(mode="json")
    # The spec the parent hands the detach writer validates against submit-s2, and
    # the writer's run_id digger finds it at the nested path it expects.
    assert_valid_spec("submit-s2", resolved)
    assert detached._block_spec_run_id(resolved) == _RUN_ID
    # And every detach-supported block verb is a live, dispatchable verb.
    for verb in detached.SUPPORTED_DETACHED_BLOCK_VERBS:
        assert spec_model_for(verb) is not None, f"detach verb {verb} has no spec_model"


# ── 4. registry-wide guard: the verb↔model↔schema triangle ─────────────────────


def _spec_model_verbs() -> list[tuple[str, type[BaseModel], str]]:
    """Every registered verb with a live spec_model, plus its schema basename.

    Schema basename = ``schema_ref.input`` when the CliShape declares one, else
    the verb name with hyphens→underscores (the convention the schema files
    follow). Returns ``(verb, model, schema_basename)`` triples.
    """
    out: list[tuple[str, type[BaseModel], str]] = []
    for name, meta in sorted(get_registry().items()):
        cli = getattr(meta, "cli", None)
        model = getattr(cli, "spec_model", None) if cli is not None else None
        if model is None:
            continue
        schema_ref = getattr(cli, "schema_ref", None) if isinstance(cli, CliShape) else None
        ref_input = getattr(schema_ref, "input", None) if schema_ref is not None else None
        basename = ref_input if ref_input else name.replace("-", "_")
        out.append((name, model, basename))
    return out


def test_every_spec_model_verb_has_a_schema_and_is_a_basemodel() -> None:
    """Pin the verb↔model↔schema triangle for every spec-bearing verb.

    For each registered verb whose CliShape carries a ``spec_model``:

    * the model is a real ``BaseModel`` subclass, and ``spec_model_for(verb)``
      returns that exact class (the object a live dispatch validates against);
    * a matching ``schemas/<basename>.input.json`` exists.

    Catches a model added without a schema (or a verb whose model went missing) —
    the light mapping check, distinct from the JSON-roundtrip in
    ``tests/contract/test_schema_roundtrip.py``.
    """
    verbs = _spec_model_verbs()
    assert verbs, "no spec_model verbs found — registry not populated?"
    missing: list[tuple[str, str]] = []
    for verb, model, basename in verbs:
        assert issubclass(model, BaseModel), f"{verb}: spec_model {model!r} is not a BaseModel"
        assert spec_model_for(verb) is model, (
            f"{verb}: spec_model_for disagrees with the registry CliShape model"
        )
        if not (_SCHEMAS_DIR / f"{basename}.input.json").exists():
            missing.append((verb, basename))
    assert not missing, f"spec_model verbs missing a schemas/<name>.input.json: {missing}"
