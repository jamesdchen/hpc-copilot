"""``submit-s1``..``submit-s4`` — the submit workflow as human-amplification blocks.

The submit flow, decomposed (docs/design/human-amplification-blocks.md §3) into
four blocks, each a THIN orchestrator that composes existing rings and TERMINATES
at a human decision point carrying code-digested evidence (a *brief*). No decision
is resolved by the LLM: code chains deterministically as far as it can, then hands
back the brief for the ``y``/nudge propose loop (§2).

* ``submit-s1`` (resolve) — preflight + walk-submit-ambiguities. Surfaces each
  ambiguity's ``safe_default`` as a PRE-FILLED RECOMMENDATION (§6 line 181),
  never auto-applied into ``resolved`` — ``apply-safe-defaults`` is the silent
  actor this kills, so S1 does NOT call it. When the walk is clean and a
  ``resolve`` spec is supplied, chains ``resolve-submit-inputs`` to a
  ``resolved`` / ``prior_run_found`` terminator.
* ``submit-s2`` (stage & canary) — ``submit-and-verify`` stopped after a verified
  canary + the ``estimate-core-hours`` footprint. Brief: "canary green, est N
  core-hours".
* ``submit-s3`` (submit & watch) — Phase-2 main-array launch + ``monitor-flow``
  to terminal/anomaly + ``decide-monitor-arm``. Runs unattended.
* ``submit-s4`` (harvest) — ``aggregate-flow`` → a code-extracted results table
  + a slot for proposed interpretations.

Each block owns its invariants at the boundary (adding-a-primitive.md): it
validates the wire spec (the embedded models do the shape work) and fails loudly
via the composed rings. The block bodies stay THIN — they never reimplement ring
logic, only sequence it and digest the evidence into a brief.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.decide_monitor_arm import DecideMonitorArmSpec
from hpc_agent._wire.workflows.submit_blocks import (
    SubmitBlockResult,
    SubmitS1Spec,
    SubmitS2Spec,
    SubmitS3Spec,
    SubmitS4Spec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.block_chain import next_block_hint
from hpc_agent.infra.cost import CostEstimate, estimate_core_hours
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.ops.block_gate import assert_greenlit_target
from hpc_agent.ops.data_manifest import render_manifest_disclosure
from hpc_agent.ops.monitor.arm import decide_monitor_arm, summary_from_last_status
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.ops.relay_render import render_relay
from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs
from hpc_agent.ops.scope_gate import assert_scopes_unlocked
from hpc_agent.ops.submit_and_verify import launch_main_array, submit_and_verify
from hpc_agent.ops.submit_preflight import submit_preflight
from hpc_agent.ops.walk_submit_ambiguities import walk_submit_ambiguities

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

__all__ = ["submit_s1", "submit_s2", "submit_s3", "submit_s4"]


# The detached submit blocks (design §3): each spawns a durable worker and
# returns a handle, so a naive re-invocation after the worker FINISHED would
# re-spawn (the single-lease self-heals on a dead pid) — the run #7 papercut.
# These blocks record their terminal outcome and replay it on re-invoke.
_DETACHED_BLOCKS: frozenset[str] = frozenset({"s2", "s3", "s4"})


def _current_cmd_sha(experiment_dir: Path, run_id: str) -> str:
    """The run's tree fingerprint (``cmd_sha``) from its sidecar, or ``""``.

    The identity a terminal replay is keyed on: a nudge that re-resolves the run
    (revise-resolved) rewrites the sidecar ``cmd_sha``, so a mismatch is exactly
    "the tree moved → do not replay a stale outcome". An unreadable/absent
    sidecar yields ``""`` — unprovable identity → the replay refuses (re-execute).
    """
    from hpc_agent.state.runs import read_run_sidecar

    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (OSError, ValueError, errors.HpcError):
        return ""
    return str((sidecar or {}).get("cmd_sha") or "")


def _replay_recorded_terminal(
    experiment_dir: Path, *, block: str, run_id: str
) -> SubmitBlockResult | None:
    """Return a prior detached worker's recorded terminal for ``(run_id, block)``
    when the tree still matches, else ``None`` (run #7 idempotent re-invoke).

    Replays ONLY when the current sidecar ``cmd_sha`` equals the one recorded with
    the terminal — proof the outcome still applies. A moved ``cmd_sha`` (a nudge),
    an absent record, an unprovable identity (empty sha on either side), or a
    corrupt record all return ``None`` so the caller re-executes (never replays a
    possibly-stale brief). The replayed result was already finalized on first
    completion (relay rendered, brief appended), so the caller returns it as-is.
    """
    from hpc_agent.state.block_terminal import read_terminal_with_fallback

    # Read by the canonical VERB key with a legacy short-key fallback (2026-07-07
    # key-mismatch fix): a run recorded pre-fix sits under the short "s2" key, so a
    # mid-flight re-invoke must still find and replay it.
    record = read_terminal_with_fallback(experiment_dir, run_id, block)
    if record is None:
        return None
    current_sha = _current_cmd_sha(experiment_dir, run_id)
    if not current_sha or str(record.get("cmd_sha") or "") != current_sha:
        return None
    try:
        return SubmitBlockResult.model_validate(record["result"])
    except (KeyError, TypeError, ValueError):
        return None


def _persist_brief(experiment_dir: Path, result: SubmitBlockResult) -> SubmitBlockResult:
    """Durably persist a decision-point brief so the provenance gate can diff it.

    Conduct rule 9 (docs/design/history/proving-run-2-hardening.md §6): ``append-decision``
    refuses a greenlight whose ``resolved`` diverts a field the brief never
    recommended — but only if the brief the block emitted is on disk. CODE
    persists it here, at the moment a block returns a decision-point Result, in
    BOTH driving modes (block-drive AND direct MCP invocation) — the v1
    ``next_block`` lesson forbids keying this on block-drive-only state.

    Persists only when the block ends at a human boundary (``needs_decision``) AND
    a ``run_id`` exists to scope the file (S1's pre-resolve ambiguity branch has no
    run_id yet — its greenlight then legitimately fails open). Detached / clean-
    terminal returns (``needs_decision=False``) carry nothing to greenlight, so
    nothing is persisted. Pass-through: returns *result* unchanged.

    Also renders the human-facing ``relay`` from the block's OWN structured
    evidence (design §5.3, finding 15): the single wire point every S1–S4 return
    (including the detached handle) routes through, so the agent relays a
    code-authored line VERBATIM instead of reconstructing numbers/state from
    memory. The relay is NOT written into the persisted brief — a stale relay
    string in the durable record would poison the relay-audit source pool
    (``verify-relay``); the audit must diff the agent's relay against the
    STRUCTURED facts, never against a prior rendering of itself.
    """
    result.relay = render_relay(result.block, result.stage_reached, result.brief)
    # Idempotent-terminal record (run #7): persist the FULL terminal result of a
    # detached block, keyed by (run_id, block, cmd_sha), so a re-invoke after the
    # worker finished REPLAYS it (see _replay_recorded_terminal) instead of
    # re-spawning. The detached HANDLE (stage_reached="detached") is not terminal
    # and is skipped. The provenance brief is appended only on a FRESH terminal
    # (first record, or a moved cmd_sha = a nudge's genuinely new outcome) — a
    # replay returns the same result and must not double-append (the append-only
    # briefs journal stays honest; two identical s2 briefs were seen live).
    if result.run_id and result.block in _DETACHED_BLOCKS and result.stage_reached != "detached":
        from hpc_agent.state.block_terminal import (
            read_terminal_with_fallback,
            record_terminal,
            terminal_block_key,
        )

        # Canonical VERB key (2026-07-07 key-mismatch fix): record under
        # "submit-s2"/"submit-s3"/"submit-s4" — the SAME string the detached lease
        # stamps and the doctor dead-worker scan reads off it — so a FINISHED submit
        # worker's terminal is found (no spurious re-invoke). The prior-check read
        # falls back to the legacy short "s2" key so a mid-flight run recorded
        # pre-fix is still seen as a prior terminal (no double brief append).
        block_key = terminal_block_key(result.block)
        cmd_sha = _current_cmd_sha(experiment_dir, result.run_id)
        prior = read_terminal_with_fallback(experiment_dir, result.run_id, block_key)
        is_fresh_terminal = prior is None or str(prior.get("cmd_sha") or "") != cmd_sha
        record_terminal(
            experiment_dir,
            run_id=result.run_id,
            block=block_key,
            cmd_sha=cmd_sha,
            result_dump=result.model_dump(mode="json"),
        )
        if is_fresh_terminal and result.needs_decision and result.brief:
            from hpc_agent.state.decision_briefs import append_brief

            append_brief(
                experiment_dir,
                run_id=result.run_id,
                block=result.block,
                brief=result.brief,
            )
        return result
    if result.needs_decision and result.run_id and result.brief:
        from hpc_agent.state.decision_briefs import append_brief

        append_brief(
            experiment_dir,
            run_id=result.run_id,
            block=result.block,
            brief=result.brief,
        )
    return result


def _watchdog_brief(experiment_dir: Path) -> dict[str, Any]:
    """The §5 watchdog install-status field for a brief arming a long wait.

    ``doctor-install`` is decided opt-in ("never auto-installed" — design §5,
    2026-07-03): the design-consistent close for the crash-durability gap is a
    *decision brief*, not a silent default. So the block that arms an
    unattended wait reports whether the OS-scheduler dead-man's switch exists
    on this machine, and — when it doesn't — carries the recommendation for
    the human's ``y``/nudge. Proving run #2 ran with no watchdog armed; a dead
    session would have stranded the run undetected.
    """
    from hpc_agent.ops.recover.doctor_install import watchdog_installed

    installed = watchdog_installed(experiment_dir)
    field: dict[str, Any] = {"installed": installed}
    if not installed:
        field["recommendation"] = (
            "§5 watchdog not installed on this machine — if this session dies, "
            "the run strands undetected until a human runs doctor. Recommend "
            "`hpc-agent doctor-install` (one idempotent OS-scheduler entry; "
            "`uninstall:true` reverses it)."
        )
    return field


def _detached_block_result(block: str, verb: str, launch: Any) -> SubmitBlockResult:
    """Build the immediate-return handle for a detached block (design §3).

    The parent already ran its synchronous gate + drift guards (gate → drift →
    detach) and spawned the durable detached worker; this is the ``{started,
    watch: journal, detached_pid}`` handle it hands back so the chat is never
    held. ``needs_decision`` is False (nothing to decide yet — the brief arrives
    on completion, read from the journal) and ``next_block`` is null (the journal,
    not this process, carries the next-block suggestion once the block finishes).
    """
    return SubmitBlockResult(
        block=block,  # type: ignore[arg-type]  # caller passes the block's own literal
        stage_reached="detached",
        needs_decision=False,
        reason=(
            f"{verb} detached — the scheduler poll runs in a durable background "
            "worker; its brief arrives on completion (read the journal). The gate "
            "and drift guards already passed synchronously before the detach."
        ),
        run_id=launch.run_id,
        started=True,
        watch="journal",
        detached_pid=launch.pid,
        brief={"run_id": launch.run_id, "log_path": launch.log_path},
    )


def _detached_spec_dict(spec: Any) -> dict[str, Any]:
    """Serialize *spec* with ``detach`` forced OFF for the detached child.

    The child runs the SAME verb body synchronously (its poll is the point), so
    its spec must carry ``detach=False`` — otherwise it would re-detach forever.
    """
    dumped: dict[str, Any] = spec.model_copy(update={"detach": False}).model_dump(mode="json")
    return dumped


def _next_block(
    current_verb: str, stage_reached: str, why: str, **spec_hint: Any
) -> dict[str, Any] | None:
    """Delegate to the ``block_chain`` successor table (design §6/§8).

    The successor VERB is re-homed into ``block_chain.SUCCESSORS`` (single source
    of truth for the deterministic chain); this thin helper keeps the emitted
    ``{verb, why, spec_hint}`` shape unchanged — ``why`` is the one-line rationale
    the LLM surfaces, ``spec_hint`` the minimal next-spec skeleton (run_id / canary
    ids). Returns ``None`` at a terminal / human-branch terminator (no
    deterministic successor for ``(current_verb, stage_reached)``).
    """
    return next_block_hint(current_verb, stage_reached, why=why, **spec_hint)


def _assert_canary_verified(experiment_dir: Path, base: SubmitFlowSpec) -> None:
    """S3 predecessor-artifact check: S2's canary is recorded validated-fresh.

    The strongest CODE-WRITTEN artifact S2 leaves is the canary TTL cache:
    ``verify-canary`` calls ``record_canary_validated`` on a green canary, keyed
    by ``(cmd_sha, framework-version)`` — the same key ``submit-flow``'s skip
    reads. So when the cache is ENABLED, a ``submit-s3`` whose ``(cmd_sha,
    version)`` is NOT validated-fresh means either S2 never verified a canary or
    the spec's ``cmd_sha`` moved (a nudge changed the tree) — refuse, pointing
    back to ``submit-s2``.

    Bounds of the artifact (documented, not worked around):
      * cache DISABLED (``HPC_NO_CANARY_SKIP=1``) → the artifact is unavailable;
        the greenlight gate + ``_assert_no_post_greenlight_drift`` carry the
        canary-verified guarantee, so this check is a no-op.
      * no ``HPC_CMD_SHA`` on the spec → nothing to key on → no-op (same fallback).
      * TTL window: a greenlight that lands after ``HPC_CANARY_TTL_SEC`` (default
        4h) legitimately re-canaries via ``submit-s2`` — a cheap, bounded canary,
        exactly the design's "mis-speculation is bounded" stance.
    """
    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.state import canary_cache

    if canary_cache.cache_disabled():
        return
    cmd_sha = (base.job_env or {}).get("HPC_CMD_SHA") or ""
    if not cmd_sha:
        return
    key = canary_cache.canary_cache_key(
        cmd_sha=cmd_sha, version=_pkg_version or "", cluster=base.cluster
    )
    if not canary_cache.is_canary_validated_fresh(key):
        raise errors.SpecInvalid(
            "submit-s3: no validated-fresh canary for this (cmd_sha, version, cluster) — "
            "re-run submit-s2 so the canary verifies the current tree before the "
            f"main array launches (run_id={base.run_id!r})."
        )


# ── S1 helpers ──────────────────────────────────────────────────────────────


def _recommendations_from_ambiguities(
    ambiguities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Surface each ambiguity's ``safe_default`` as a ``recommendation`` (§6).

    The design kills ``apply-safe-defaults`` as a SILENT ACTOR: the old
    auto-applied default survives ONLY as a pre-filled recommendation inside the
    brief. So each ambiguity is passed through verbatim (``safe_default`` intact)
    with an added ``recommendation`` mirror the LLM proposes and the human
    greenlights — but nothing is written into ``resolved`` here.
    """
    out: list[dict[str, Any]] = []
    for amb in ambiguities:
        entry = dict(amb)
        # The pre-filled recommendation is exactly the (never-auto-applied)
        # safe_default. A REQUIRED_CALLER_FIELDS ambiguity has none (the
        # partition guard forbids it) → recommendation stays None: genuine
        # judgment the human must supply.
        entry["recommendation"] = amb.get("safe_default")
        out.append(entry)
    return out


# ── S2 helpers ──────────────────────────────────────────────────────────────


def _estimate_for_submit(base: SubmitFlowSpec) -> CostEstimate:
    """Compute the pre-dispatch core-hours footprint from the submit spec.

    ``total_tasks × walltime × cores`` — all three already live on the submit
    spec (``total_tasks`` + ``resources.{walltime_sec,cpus}``). Delegates to the
    single ``estimate_core_hours`` kernel. A missing walltime yields the
    kernel's defensive zero-cost estimate rather than raising; the returned
    estimate's ``footprint_unknown`` property is what the brief/reason
    renderers branch on to say "unknown core-hours" instead of a false "0"
    (run #6: the human read a cold-start's defensive 0.0 as literal).
    """
    resources = base.resources
    walltime_s = resources.walltime_sec if (resources and resources.walltime_sec) else 0
    cores = resources.cpus if (resources and resources.cpus) else None
    return estimate_core_hours(
        total_tasks=base.total_tasks,
        walltime_s=walltime_s or 0,
        cores_per_task=cores,
    )


# ── S4 helpers ──────────────────────────────────────────────────────────────


def _results_table(aggregated_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Digest the reduced metrics into a code-extracted results table.

    ``aggregated_metrics`` maps a run_id / grid-point key → its metric dict. The
    table is a stable, row-per-key projection the LLM renders and proposes
    interpretations over — it never interprets the raw metrics itself (§2, the
    #355 doctrine extended from computing to concluding).
    """
    rows: list[dict[str, Any]] = []
    for key, metrics in sorted(aggregated_metrics.items()):
        row: dict[str, Any] = {"key": key}
        if isinstance(metrics, dict):
            row["metrics"] = metrics
        else:
            row["value"] = metrics
        rows.append(row)
    return rows


# ── S1 ──────────────────────────────────────────────────────────────────────


@primitive(
    name="submit-s1",
    verb="workflow",
    composes=["submit-preflight", "walk-submit-ambiguities", "resolve-submit-inputs"],
    side_effects=[SideEffect("ssh", "<cluster> (preflight probe, when run_preflight)")],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="walk.experiment_dir",
    cli=CliShape(
        help=(
            "Submit block S1 (resolve): preflight + walk-submit-ambiguities to "
            "the decision boundary. Brief = the {resolved, ambiguities} envelope "
            "with each ambiguity's safe_default surfaced as a RECOMMENDATION "
            "(never auto-applied). When the walk is clean and a resolve spec is "
            "supplied, chains resolve-submit-inputs. Terminates → y/nudge."
        ),
        spec_arg=True,
        spec_model=SubmitS1Spec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_s1", output="submit_block"),
    ),
    agent_facing=True,
)
def submit_s1(experiment_dir: Path, *, spec: SubmitS1Spec) -> SubmitBlockResult:
    """Resolve block: preflight → walk ambiguities → (optional) resolve inputs.

    Ends at the FIRST decision point with a full brief for the ``y``/nudge loop:
    ``needs_resolution`` (the walk surfaced ambiguities, each with a pre-filled
    ``recommendation``), or — when the walk is clean and ``spec.resolve`` was
    supplied — the ``resolve-submit-inputs`` terminator (``resolved`` /
    ``prior_run_found`` / ``needs_scaffold_interview``). ``needs_decision`` is
    True in every case: S1 always ends at a human greenlight (§3).

    The emitted brief is persisted (``_persist_brief``) so the provenance gate
    (conduct rule 9) can later diff an S1→S2 greenlight's ``resolved`` against it.
    """
    return _persist_brief(experiment_dir, _submit_s1_impl(experiment_dir, spec=spec))


def _submit_s1_impl(experiment_dir: Path, *, spec: SubmitS1Spec) -> SubmitBlockResult:
    brief: dict[str, Any] = {}

    # 1. Preflight (optional) — fold pass/fail into the brief.
    if spec.run_preflight:
        pf = submit_preflight(experiment_dir=experiment_dir, cluster=spec.walk.cluster)
        brief["preflight"] = {"overall": pf.get("overall")}

    # 2. Walk ambiguities — the envelope machinery, unchanged. Accumulates ALL
    #    decision points in one pass (never early-returns on the first miss).
    walk = walk_submit_ambiguities(spec=spec.walk)
    brief["resolved"] = dict(walk.resolved)
    brief["provenance"] = dict(walk.provenance)
    # 3. Surface each safe_default as a pre-filled recommendation — NOT applied
    #    into resolved (apply-safe-defaults is the silent actor this kills, §6).
    brief["ambiguities"] = _recommendations_from_ambiguities(walk.ambiguities)

    # Consumer #2 (data-manifest): the VERDICT-FREE, code-rendered data-drift
    # disclosure rides the greenlight brief. Never gates, never raises (the
    # accept-with-disclosure rule); None → nothing declared/minted → the brief
    # stays byte-identical for a repo not using the manifest.
    disclosure = render_manifest_disclosure(experiment_dir)
    if disclosure is not None:
        brief["data_manifest"] = disclosure

    if walk.ambiguities:
        return SubmitBlockResult(
            block="s1",
            stage_reached="needs_resolution",
            needs_decision=True,
            reason=(
                f"{len(walk.ambiguities)} field(s) need a decision; each carries a "
                "pre-filled recommendation the human greenlights or nudges."
            ),
            brief=brief,
        )

    # 4. Walk clean. If a resolve spec was supplied, chain the deterministic
    #    input-resolution ring to its own terminator; else stop at the clean
    #    brief for the human to greenlight the resolved plan.
    if spec.resolve is None:
        # PRE-RESOLVE boundary: the walk is clean but no run_id / submit-flow spec
        # exists yet — resolve needs caller inputs the walk cannot supply
        # (``remote_path`` + the build-submit-spec fields). ``next_block`` stays
        # submit-s2 — the code-driven table target the resolve leg feeds (the
        # ``("submit-s1","resolved") -> submit-s2`` invariant; do NOT special-case
        # it to None, that breaks the block↔SUCCESSORS agreement contract). The
        # REASON flags that run_id is UNMINTED so the caller supplies the resolve
        # spec FIRST (run #7: the agent read the submit-s2 pointer as "advance
        # now", jumped ahead of the resolve leg, and improvised a direct
        # submit-s2; the fix is this honest reason + the hpc-submit skill's
        # pre-resolve-boundary step, not a routing change).
        return SubmitBlockResult(
            block="s1",
            stage_reached="resolved",
            needs_decision=True,
            reason=(
                "plan resolved (no ambiguities) — PRE-RESOLVE boundary: run_id is "
                "UNMINTED. Supply the resolve inputs (remote_path + the "
                "build-submit-spec fields) so S1's resolve leg mints run_id and "
                "builds the sidecar; only then does submit-s2 (the next_block "
                "target) have a resolved run to stage."
            ),
            brief=brief,
            next_block=_next_block(
                "submit-s1",
                "resolved",
                "inputs resolved; stage & canary the run for review.",
            ),
        )

    rr = resolve_submit_inputs(experiment_dir, spec=spec.resolve)
    brief["resolve"] = {
        "stage_reached": rr.stage_reached,
        "reason": rr.reason,
        "run_id": rr.run_id,
        "submit_spec": rr.submit_spec,
        "sidecar_path": rr.sidecar_path,
        "prior_run_id": rr.prior_run_id,
        "prior_status": rr.prior_status,
    }
    # Only a CLEAN resolve (submit-flow spec built) has a single deterministic
    # successor (submit-s2). ``prior_run_found`` (resume-vs-fresh) and
    # ``needs_scaffold_interview`` are genuine human branches → next_block null.
    # The resolve leg passes ``rr.stage_reached`` straight into the table: a clean
    # ``resolved`` chains to submit-s2, while ``prior_run_found`` /
    # ``needs_scaffold_interview`` are human branches (table → None).
    next_block = _next_block(
        "submit-s1",
        rr.stage_reached,
        "inputs resolved; stage & canary the run for review.",
        run_id=rr.run_id,
    )
    return SubmitBlockResult(
        block="s1",
        stage_reached=rr.stage_reached,
        needs_decision=True,
        reason=rr.reason,
        run_id=rr.run_id,
        brief=brief,
        next_block=next_block,
    )


# ── S2 ──────────────────────────────────────────────────────────────────────


@primitive(
    name="submit-s2",
    verb="workflow",
    composes=["submit-and-verify"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster> (canary only)"),
        SideEffect("ssh", "<cluster> (canary poll + log scan)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="submit.submit.run_id",
    cli=CliShape(
        help=(
            "Submit block S2 (stage & canary): submit-and-verify STOPPED after a "
            "verified canary (the main array does NOT launch), plus the "
            "estimate-core-hours footprint. Brief = 'canary green, est N "
            "core-hours'. Terminates → y/nudge; S3 launches the main array."
        ),
        spec_arg=True,
        spec_model=SubmitS2Spec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_s2", output="submit_block"),
    ),
    agent_facing=True,
)
def submit_s2(experiment_dir: Path, *, spec: SubmitS2Spec) -> SubmitBlockResult:
    """Stage & canary block: canary-only submit + verify, STOP, estimate cost.

    Runs ``submit-and-verify`` with ``stop_after_canary=True`` so the main array
    never launches, then attaches the pre-dispatch core-hours estimate. Ends at
    the "canary green, est N core-hours" brief for the ``y``/nudge loop. A failed
    or deduped canary is its own terminator — S2 surfaces it (an anomaly is a
    block terminator too, §5) rather than sailing into the main array.

    Precondition gate (design §2): the latest journaled decision for this run must
    be a greenlight naming ``submit-s2`` — the human greenlit S1's resolved brief.
    A missing/mismatched greenlight fails loudly (``assert_greenlit_target``).

    The emitted brief is persisted (``_persist_brief``) for the provenance gate
    (conduct rule 9): an S2→S3 greenlight's ``resolved`` is diffed against it.
    """
    return _persist_brief(experiment_dir, _submit_s2_impl(experiment_dir, spec=spec))


def _submit_s2_impl(experiment_dir: Path, *, spec: SubmitS2Spec) -> SubmitBlockResult:
    assert_greenlit_target(
        experiment_dir,
        run_id=spec.submit.submit.run_id,
        verb="submit-s2",
        predecessor="S1",
    )
    # Detach-by-contract (design §3): the greenlight gate above fired
    # SYNCHRONOUSLY (gate → detach — a gate failure surfaces here, loudly, never
    # inside a detached child). With detach ON (default), spawn a durable
    # background worker to own the canary poll and return the handle immediately.
    if spec.detach:
        # Idempotent re-invoke (run #7): a prior detached worker may have already
        # driven this block to its terminal for the current tree — replay that
        # recorded outcome instead of re-spawning a redundant worker (no SSH, no
        # new canary poll). A moved cmd_sha (a nudge) or no record → fall through
        # and spawn; a still-LIVE worker is refused by the single-lease.
        replay = _replay_recorded_terminal(
            experiment_dir, block="s2", run_id=spec.submit.submit.run_id
        )
        if replay is not None:
            return replay

        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached

        launch = launch_submit_block_detached(
            verb="submit-s2",
            experiment_dir=str(experiment_dir),
            spec=_detached_spec_dict(spec),
        )
        return _detached_block_result("s2", "submit-s2", launch)

    sv = submit_and_verify(experiment_dir, spec=spec.submit, stop_after_canary=True)

    # Cost estimate from the submit spec (cost.py untouched).
    est = _estimate_for_submit(spec.submit.submit)
    brief: dict[str, Any] = {
        # run_id + cluster ride the brief so the relay renderer is self-contained
        # (design §5.3): the canonical line is rendered from the brief's OWN data.
        "run_id": sv.run_id,
        "cluster": spec.submit.submit.cluster,
        "canary_run_id": sv.canary_run_id,
        "canary_job_ids": sv.canary_job_ids,
        "verified": sv.verified,
        "failure_kind": sv.failure_kind,
        "deduped": sv.deduped,
        "est_core_hours": est.est_core_hours,
        "est_gpu_hours": est.est_gpu_hours,
        # Unknown-footprint honesty (run #6): the defensive 0.0 above must
        # never render as a literal "0 core-hours". Stamped into the brief so
        # the relay renderer (which reads est_core_hours off the brief dict,
        # not the CostEstimate) has the signal to say "unknown".
        "footprint_unknown": est.footprint_unknown,
        "cost_estimate": {
            "total_tasks": est.total_tasks,
            "walltime_s": est.walltime_s,
            "cores_per_task": est.cores_per_task,
            "gpus_per_task": est.gpus_per_task,
            "est_core_hours": est.est_core_hours,
            "est_gpu_hours": est.est_gpu_hours,
            "footprint_unknown": est.footprint_unknown,
        },
    }
    if sv.verify_result is not None:
        brief["verify_result"] = sv.verify_result.model_dump(mode="json")

    if sv.deduped:
        return SubmitBlockResult(
            block="s2",
            stage_reached="deduped",
            needs_decision=True,
            reason="the run already exists — no fresh canary fired; confirm resume-vs-fresh.",
            run_id=sv.run_id,
            brief=brief,
        )
    if not sv.verified:
        # Two distinct "not verified" conditions collapse into verified=False in
        # submit-and-verify (ops/submit_and_verify.py): (a) a canary LANDED and
        # failed its verification (failure_kind set), vs (b) NO canary ever
        # entered the queue — canary_run_id is None, failure_kind is None (the
        # canary_submit.canary_run_id-None branch). Rendering (b) as a
        # "verification failure (None)" is misleading — the canary never launched.
        # Reason-only distinction (no new stage_reached literal, so no wire/schema
        # regen): both stay canary_failed terminators (still an anomaly → human
        # decides, needs_decision/next_block unchanged), only the reason differs.
        if sv.canary_run_id is None:
            reason = (
                "canary never entered the queue (no canary_run_id) — submission "
                "did not launch; propose a fix before main."
            )
        else:
            reason = f"canary failed verification ({sv.failure_kind}); propose a fix before main."
        return SubmitBlockResult(
            block="s2",
            stage_reached="canary_failed",
            needs_decision=True,
            reason=reason,
            run_id=sv.run_id,
            brief=brief,
        )
    # An unknown footprint says so, loudly — never "est. 0 core-hours" (run #6:
    # the human read the defensive 0.0 as literal and the driving agent had to
    # caption it by hand).
    est_phrase = (
        "unknown core-hours (walltime unresolved — no history)"
        if est.footprint_unknown
        else f"{est.est_core_hours:g} core-hours"
    )
    return SubmitBlockResult(
        block="s2",
        stage_reached="canary_verified",
        needs_decision=True,
        reason=f"canary green, est. {est_phrase}; greenlight to submit & watch.",
        run_id=sv.run_id,
        brief=brief,
        next_block=_next_block(
            "submit-s2",
            "canary_verified",
            "canary verified; launch the main array and watch to terminal.",
            run_id=sv.run_id,
            canary_run_id=sv.canary_run_id,
            canary_job_ids=sv.canary_job_ids,
        ),
    )


# ── S3 ──────────────────────────────────────────────────────────────────────

# monitor-flow terminal states that are §5 anomaly terminators (human decides)
# vs a clean completion that simply suggests S4.
_S3_ANOMALY_STATES: frozenset[str] = frozenset({"failed", "abandoned"})


@primitive(
    name="submit-s3",
    verb="workflow",
    composes=["submit-flow", "monitor-flow", "decide-monitor-arm"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster> (main array)"),
        SideEffect("ssh", "<cluster> (status poll + wave combine)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="submit.submit.run_id",
    cli=CliShape(
        help=(
            "Submit block S3 (submit & watch): launch the main array (Phase-2 of "
            "the two-phase gate — canary verified in S2), monitor to "
            "terminal/anomaly, and arm the next monitor tick. Runs UNATTENDED — "
            "no human boundary inside; an anomaly is a block terminator."
        ),
        spec_arg=True,
        spec_model=SubmitS3Spec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_s3", output="submit_block"),
    ),
    agent_facing=True,
)
def submit_s3(experiment_dir: Path, *, spec: SubmitS3Spec) -> SubmitBlockResult:
    """Submit & watch block: launch main + monitor to terminal + arm next tick.

    The canary was verified and greenlit in S2, so S3 launches the main array via
    the standalone Phase-2 path (``launch_main_array``), then watches it to a
    terminal/timeout state and arms the next monitor tick (``decide-monitor-arm``).
    No human boundary inside: a clean completion flows on to S4 (harvest); an
    anomaly (failed/abandoned) or a timeout is itself the terminator that raises
    the ``y``/nudge boundary (§5).

    Precondition gates (design §2 + §3): the latest journaled decision for this
    run must be a greenlight naming ``submit-s3`` (``assert_greenlit_target``), and
    the canary S2 verified must be recorded validated-fresh
    (``_assert_canary_verified``, the TTL-cache artifact). ``launch_main_array``
    adds the tree-drift guard. All three fail loudly before any main-array submit.

    The emitted brief is persisted (``_persist_brief``) at each human-boundary
    return for the provenance gate (conduct rule 9). The unattended / detached /
    clean-terminal returns carry no greenlight, so nothing is persisted there.
    """
    return _persist_brief(experiment_dir, _submit_s3_impl(experiment_dir, spec=spec))


def _submit_s3_impl(experiment_dir: Path, *, spec: SubmitS3Spec) -> SubmitBlockResult:
    assert_greenlit_target(
        experiment_dir,
        run_id=spec.submit.submit.run_id,
        verb="submit-s3",
        predecessor="S2",
    )
    # Idempotent re-invoke (run #7): replay a prior worker's recorded terminal for
    # the current tree BEFORE the canary-TTL re-check — the run already ran, so
    # re-validating the 4h canary window on a replay would wrongly refuse a
    # completed run whose window lapsed. cmd_sha match IS the drift check. This
    # also gives the S3 CLEAN-terminal (needs_decision=False → S4) a replayable
    # record, closing the "clean-terminal persists nothing, agent scrapes the
    # worker log" sibling.
    if spec.detach:
        replay = _replay_recorded_terminal(
            experiment_dir, block="s3", run_id=spec.submit.submit.run_id
        )
        if replay is not None:
            return replay

    _assert_canary_verified(experiment_dir, spec.submit.submit)

    # Detach-by-contract (design §3), ordering PROOF: gate → drift → detach.
    # Both the greenlight gate and the canary-validated gate fired above; run the
    # tree-drift guard (N's predicate) HERE too, synchronously, so a
    # post-greenlight edit fails loudly to the caller BEFORE any detach — never
    # inside a detached child that would otherwise launch the full array on code
    # the canary never verified. Only then hand the launch+monitor to a durable
    # background worker (which re-runs all three guards harmlessly + owns the poll
    # to terminal, stamping the journal so the §5 doctor covers its death).
    if spec.detach:
        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached
        from hpc_agent.ops.submit_and_verify import _assert_no_post_greenlight_drift

        _assert_no_post_greenlight_drift(experiment_dir, spec.submit.submit)
        launch = launch_submit_block_detached(
            verb="submit-s3",
            experiment_dir=str(experiment_dir),
            spec=_detached_spec_dict(spec),
        )
        result = _detached_block_result("s3", "submit-s3", launch)
        # The detach hands the long unattended wait to a background worker —
        # exactly the moment the human should learn whether the §5 dead-man's
        # switch would catch that worker's death (see _watchdog_brief).
        result.brief["watchdog"] = _watchdog_brief(experiment_dir)
        return result

    # 1. Phase-2: launch the main array (canary already verified/greenlit in S2).
    main = launch_main_array(
        experiment_dir,
        spec=spec.submit,
        canary_run_id=spec.canary_run_id,
        canary_job_ids=spec.canary_job_ids,
    )

    # 2. Monitor to terminal-or-budget (unattended, no human boundary inside).
    mon = monitor_flow(experiment_dir, spec=spec.monitor)

    # 3. Arm the next monitor tick from the final status snapshot.
    # ``last_status`` carries the counts FLAT (no "summary" nesting) — feed it
    # through the shared both-shape projector so a 20/20-terminal run reads as
    # complete and arms "none" instead of shearing off to a running-fallback
    # cron (run #8).
    summary = summary_from_last_status(mon.last_status)
    arm = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id=main.run_id,
            summary=summary,
            total_tasks=main.total_tasks,
            invocation_argv=spec.invocation_argv,
            user_invoked_via_loop=spec.user_invoked_via_loop,
        )
    )

    brief: dict[str, Any] = {
        "main_run_id": main.run_id,
        "main_job_ids": main.job_ids,
        "total_tasks": main.total_tasks,
        # cluster rides the brief for the relay renderer (design §5.3).
        "cluster": spec.submit.submit.cluster,
        "canary_run_id": main.canary_run_id,
        "lifecycle_state": mon.lifecycle_state,
        "last_status": mon.last_status,
        "combined_waves": mon.combined_waves,
        "failed_waves": mon.failed_waves,
        "escalation_reason": mon.escalation_reason,
        "ticks": mon.ticks,
        "elapsed_seconds": mon.elapsed_seconds,
        "monitor_arm": arm,
        "watchdog": _watchdog_brief(experiment_dir),
    }

    if mon.lifecycle_state == "complete":
        return SubmitBlockResult(
            block="s3",
            stage_reached="watching_terminal",
            needs_decision=False,
            reason="main array complete; proceed to harvest (S4).",
            run_id=main.run_id,
            brief=brief,
            next_block=_next_block(
                "submit-s3",
                "watching_terminal",
                "main array complete; harvest results and propose interpretations.",
                run_id=main.run_id,
            ),
        )
    if mon.lifecycle_state == "timeout":
        return SubmitBlockResult(
            block="s3",
            stage_reached="watching_timeout",
            needs_decision=True,
            # A reporter-unreachable escalation (run #7: rc 126/127 env break)
            # carries its own diagnosis in escalation_reason — surface it as the
            # top-line reason instead of the misleading "budget hit".
            reason=(
                mon.escalation_reason
                or "monitor wall-clock budget hit; cluster jobs may run on — keep watching or stop?"
            ),
            run_id=main.run_id,
            brief=brief,
            # Still in flight; the deterministic continuation is to keep watching
            # (status-watch), which re-arms the next tick. Not S4 — nothing terminal yet.
            next_block=_next_block(
                "submit-s3",
                "watching_timeout",
                "budget elapsed but jobs may run on; keep watching to a terminal state.",
                run_id=main.run_id,
            ),
        )
    # failed / abandoned → §5 anomaly terminator. next_block is null: recovery is a
    # genuine human branch (resubmit-failed / kill / reconcile) with no single
    # deterministic successor — the anomaly brief carries the proposed actions.
    return SubmitBlockResult(
        block="s3",
        stage_reached="watching_anomaly",
        needs_decision=True,
        reason=(
            f"main array reached '{mon.lifecycle_state}' "
            f"({mon.escalation_reason or 'no escalation reason'}); propose recovery."
        ),
        run_id=main.run_id,
        brief=brief,
    )


# ── S4 ──────────────────────────────────────────────────────────────────────


@primitive(
    name="submit-s4",
    verb="workflow",
    composes=["aggregate-flow"],
    side_effects=[SideEffect("ssh", "<cluster> (wave combine + rsync pull)")],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="aggregate.run_id",
    cli=CliShape(
        help=(
            "Submit block S4 (harvest): aggregate-flow → a code-extracted results "
            "table + a slot for proposed interpretations. Terminates → y/nudge. "
            "(Calls the existing aggregate entry; Unit B's harvest_on_terminal "
            "guarantee is a parallel add.)"
        ),
        spec_arg=True,
        spec_model=SubmitS4Spec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_s4", output="submit_block"),
    ),
    agent_facing=True,
)
def submit_s4(experiment_dir: Path, *, spec: SubmitS4Spec) -> SubmitBlockResult:
    """Harvest block: aggregate → code-extracted results table → propose interp.

    Runs the existing ``aggregate-flow`` (ensure waves combined → pull partials →
    reduce) and digests the reduced metrics into a stable results table. The
    brief carries the table plus an empty ``proposed_interpretations`` slot the
    LLM fills at the ``y``/nudge boundary — code extracts the results; the human
    concludes from them (§2). Results are never interpreted raw by the LLM.

    NOTE (Unit B dependency): a ``harvest_on_terminal`` guarantee (§5) is being
    added in parallel by Unit B; S4 calls the EXISTING ``aggregate-flow`` entry
    until that lands, at which point S4 should route through the guaranteed path.

    Precondition gate (design §2): the latest journaled decision for this run must
    be a greenlight naming ``submit-s4`` — the human greenlit S3's terminal brief.
    The terminal-or-explicitly-partial invariant is NOT re-checked here: the
    composed ``aggregate-flow`` gate owns it (compose, don't duplicate).

    Detach-by-contract (design §3): with ``detach`` ON (default) the gate fires
    synchronously, then a durable detached worker owns the harvest (combine SSH
    round-trips + rsync pull + the breaker-deadline wait-and-retry) and the block
    returns a ``{started, watch: journal, detached_pid}`` handle immediately —
    the results-table brief is read from the journal on completion.

    The emitted results brief is persisted (``_persist_brief``) for the
    provenance gate (conduct rule 9).
    """
    return _persist_brief(experiment_dir, _submit_s4_impl(experiment_dir, spec=spec))


def _submit_s4_impl(experiment_dir: Path, *, spec: SubmitS4Spec) -> SubmitBlockResult:
    assert_greenlit_target(
        experiment_dir,
        run_id=spec.aggregate.run_id,
        verb="submit-s4",
        predecessor="S3",
    )
    # Scope gate (rigor-primitives T3), same gate → detach ordering PROOF as the
    # greenlight above: a locked evidence-scope refuses SYNCHRONOUSLY in the
    # parent, never inside a detached child's log where a "spent a reserved look"
    # refusal is invisible. Defense in depth — ONE definition
    # (assert_scopes_unlocked), TWO call sites: the detached CHILD re-hits the
    # very same gate inside aggregate_flow, so a scope locked in the window
    # between this parent check and the child's reduce is still caught. Fail-safe
    # by construction: a scope-less run (no sidecar `scopes`) passes silently.
    assert_scopes_unlocked(experiment_dir, spec.aggregate.run_id)
    # Detach-by-contract (design §3): the greenlight gate above fired
    # SYNCHRONOUSLY (gate → detach — a gate failure surfaces here, loudly, never
    # inside a detached child). The harvest's wall-clock is cluster-bound —
    # per-wave combine SSH round-trips, the rsync pull, and the breaker-deadline
    # wait-and-retry can each ride a throttled host for minutes — so with detach
    # ON (default) a durable background worker owns it and the parent returns
    # the handle immediately, exactly like S2/S3.
    if spec.detach:
        # Idempotent re-invoke (run #7): a prior detached worker may have already
        # driven this block to its terminal for the current tree — replay that
        # recorded outcome instead of re-spawning a redundant worker (no SSH, no
        # re-combine). A moved cmd_sha (a nudge) or no record → fall through and
        # spawn; a still-LIVE worker is refused by the single-lease.
        replay = _replay_recorded_terminal(experiment_dir, block="s4", run_id=spec.aggregate.run_id)
        if replay is not None:
            return replay

        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached

        launch = launch_submit_block_detached(
            verb="submit-s4",
            experiment_dir=str(experiment_dir),
            spec=_detached_spec_dict(spec),
        )
        return _detached_block_result("s4", "submit-s4", launch)

    agg = aggregate_flow(experiment_dir, spec=spec.aggregate)

    brief: dict[str, Any] = {
        "run_id": agg.run_id,
        "results_table": _results_table(agg.aggregated_metrics),
        "combined_waves": agg.combined_waves,
        "failed_waves": agg.failed_waves,
        "escalation_reason": agg.escalation_reason,
        "nonempty_failing_task_ids": agg.nonempty_failing_task_ids,
        "column_violations": agg.column_violations,
        # The slot the LLM fills with proposed interpretations at y/nudge — the
        # code hands over an EMPTY list; concluding is the human's decision (§2).
        "proposed_interpretations": [],
    }
    # Per-scope PRIOR look counts recorded by this reduction, copied VERBATIM
    # (the est_core_hours pattern) — plain integers the relay renders as counts;
    # core interprets nothing (rigor-primitives T3). Key ABSENT for a scope-less
    # run so an old brief stays byte-identical.
    if agg.scope_looks is not None:
        brief["scope_looks"] = agg.scope_looks

    partial = bool(agg.escalation_reason) or bool(agg.failed_waves)
    return SubmitBlockResult(
        block="s4",
        stage_reached="harvest_partial" if partial else "harvested",
        needs_decision=True,
        reason=(
            "partial harvest — some waves escalated; review the results table."
            if partial
            else "harvest complete; review the results table and choose an interpretation."
        ),
        run_id=agg.run_id,
        brief=brief,
    )
