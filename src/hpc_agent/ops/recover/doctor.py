"""``doctor`` — driver-watchdog scan (§5 dead-man's switch).

A read-only ``query`` primitive. Scans live (``in_flight``) runs for a missed
driver-tick deadline — a ``next_tick_due`` stamped by
:func:`hpc_agent.state.journal.stamp_tick` that is now in the past — and surfaces
each as a DRAFTED recovery proposal plus the detection evidence.

Detection is the watchdog's *whole* job. It NEVER restarts or re-arms anything
(design §5: "The watchdog never restarts anything") — safe recovery is already
guaranteed by tick idempotency, so the human just decides *whether* to re-arm.
This is the deterministic verb an OS-scheduled task (Task Scheduler / cron) runs
out-of-session; the watch-the-watcher recursion bottoms out at the OS scheduler.

Pure local filesystem read — the per-run journal records under
``~/.claude/hpc/<repo>/``. No SSH, no scheduler. The only subprocess is the
version-skew check's bounded (2 s timeout, fail-open) local ``git rev-parse``.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._build_info import full_version, git_output, runtime_sha
from hpc_agent._kernel.lifecycle.detached import pid_alive
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.doctor import (
    AdvanceRunProposal,
    AlertRecord,
    DoctorResult,
    DoctorSpec,
    ParkedRunNote,
    StalledRunProposal,
    VersionSkew,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.env_flags import active_env_overrides
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.state.block_terminal import read_terminal_with_fallback
from hpc_agent.state.decision_journal import (
    is_committed_greenlight_for_boundary,
    latest_decision,
)
from hpc_agent.state.index import find_parked_runs, find_stalled_runs
from hpc_agent.state.journal import read_pending_decision
from hpc_agent.state.run_record import current_homedir

__all__ = ["doctor", "scan_dead_detached_workers"]


def _overdue_seconds(next_tick_due: str | None, now: str) -> int | None:
    """Whole seconds by which *next_tick_due* precedes *now*, or ``None``."""
    due_dt = parse_iso_utc_or_none(next_tick_due)
    now_dt = parse_iso_utc_or_none(now)
    if due_dt is None or now_dt is None:
        return None
    return max(0, int((now_dt - due_dt).total_seconds()))


def _draft_proposal(stalled: dict[str, Any], *, now: str) -> StalledRunProposal:
    """Turn one ``find_stalled_runs`` hit into a drafted (never-enacted) proposal."""
    last_tick_at = stalled.get("last_tick_at")
    next_tick_due = stalled.get("next_tick_due")
    overdue = _overdue_seconds(next_tick_due, now)
    since = last_tick_at or "an unknown time"
    proposal = (
        f"driver stalled since {since}, status {stalled.get('status')}: next tick was due "
        f"{next_tick_due} but has not fired (now {now}). Re-arm the driver? "
        f"Re-running is safe — tick idempotency loses nothing."
    )
    return StalledRunProposal(
        run_id=stalled["run_id"],
        status=stalled.get("status", "in_flight"),
        last_tick_at=last_tick_at,
        next_tick_due=next_tick_due,
        cluster=stalled.get("cluster"),
        ssh_target=stalled.get("ssh_target"),
        proposal=proposal,
        evidence={
            "last_tick_at": last_tick_at,
            "next_tick_due": next_tick_due,
            "now": now,
            "overdue_seconds": overdue,
        },
    )


def _draft_parked_note(parked: dict[str, Any]) -> ParkedRunNote:
    """Turn one ``find_parked_runs`` hit into a parked-run note (never a proposal)."""
    awaiting_since = parked.get("awaiting_since")
    since = awaiting_since or "an unknown time"
    block = parked.get("block")
    where = f" at block {block}" if block else ""
    note = (
        f"awaiting your decision since {since}{where}: the driver parked at a "
        "y/nudge boundary and is not stalled — answer the proposal to advance it."
    )
    return ParkedRunNote(
        run_id=parked["run_id"],
        status=parked.get("status", "in_flight"),
        block=block,
        workflow=parked.get("workflow"),
        awaiting_since=awaiting_since,
        note=note,
    )


def _draft_advance_proposal(
    parked: dict[str, Any], *, decision: dict[str, Any] | None, now: str
) -> AdvanceRunProposal:
    """Turn a parked-and-decided run into a DRAFTED re-arm proposal (never enacted).

    Reached only for a run whose ``pending_decision`` marker is still set AND
    whose latest committed decision is a ``y`` — the §5 Phase-5 case where the
    human already decided but the driver died before consuming it.
    """
    run_id = parked["run_id"]
    block = parked.get("block")
    workflow = parked.get("workflow")
    awaiting_since = parked.get("awaiting_since")
    wf = workflow or "<workflow>"
    blk = block or "<block>"
    proposal = (
        f"approved spec committed for {run_id} block {blk} but the driver has not "
        f"advanced — re-arm: `hpc-agent block-drive --run-id {run_id} --workflow {wf}`. "
        "Re-running is safe — tick idempotency loses nothing."
    )
    return AdvanceRunProposal(
        run_id=run_id,
        status=parked.get("status", "in_flight"),
        block=block,
        workflow=workflow,
        awaiting_since=awaiting_since,
        proposal=proposal,
        evidence={
            "awaiting_since": awaiting_since,
            "committed_decision_ts": decision.get("ts") if decision else None,
            "committed_response": decision.get("response") if decision else None,
            "now": now,
        },
    )


def _shas_agree(a: str, b: str) -> bool:
    """Prefix-tolerant short-sha equality (git may widen a short sha on collision)."""
    return a.startswith(b) or b.startswith(a)


def _resolve_source_repo(experiment_dir: Path) -> tuple[str, str] | None:
    """``(repo_root, short_sha)`` when *experiment_dir* sits inside an hpc-agent
    source repo, else ``None``.

    Cheap and fail-open: one bounded ``git rev-parse --show-toplevel`` (2 s
    timeout) plus a marker-file check that the repo IS hpc-agent's source
    (``src/hpc_agent/__init__.py`` at the root) — an experiment repo that
    merely *uses* hpc-agent never matches. No git, not a repo, timeout →
    ``None``, silently.
    """
    if not experiment_dir.is_dir():
        return None
    top = git_output(["rev-parse", "--show-toplevel"], cwd=experiment_dir)
    if not top:
        return None
    root = Path(top)
    if not (root / "src" / "hpc_agent" / "__init__.py").is_file():
        return None
    sha = git_output(["rev-parse", "--short=8", "HEAD"], cwd=root)
    if not sha:
        return None
    return str(root), sha


def _detect_version_skew(experiment_dir: Path) -> VersionSkew | None:
    """Compare the running CLI's build sha against the source repo's HEAD.

    Returns a :class:`VersionSkew` warning when both shas resolve and differ
    (the incident class: an installed wheel and the repo tip both claiming the
    same version number while diverging by days of commits). Every unresolvable
    input — no embedded sha, no git binary, experiment_dir not in the hpc-agent
    source repo — returns ``None``: the check is advisory and must never make
    ``doctor`` slower than one bounded git call or louder than one field.
    """
    cli_sha = runtime_sha()
    if not cli_sha:
        return None
    resolved = _resolve_source_repo(experiment_dir)
    if resolved is None:
        return None
    repo_root, repo_sha = resolved
    if _shas_agree(cli_sha, repo_sha):
        return None
    cli_version = full_version()
    return VersionSkew(
        cli_version=cli_version,
        cli_sha=cli_sha,
        repo_sha=repo_sha,
        repo_root=repo_root,
        warning=(
            f"version_skew: running hpc-agent CLI is {cli_version} (code {cli_sha}) "
            f"but the hpc-agent repo at {repo_root} is at {repo_sha} — the installed "
            "tool and the source tree have diverged; reinstall the CLI from the repo "
            "(e.g. `uv tool install --reinstall .`) or rerun the release install flow."
        ),
    )


def _lease_experiment_dir(lease: dict[str, Any]) -> Path | None:
    """The experiment dir a detached lease was launched FOR, or ``None``.

    ``launch_submit_block_detached`` — the one lease writer
    (:mod:`hpc_agent._kernel.lifecycle.detached`) — always stamps the child's
    ``argv`` with ``--experiment-dir <dir>``; the lease carries no dedicated
    field, so the flag is read back out of the stamped ``argv``. Returns
    ``None`` for a lease whose argv carries no such flag (torn / hand-written).
    """
    argv = lease.get("argv")
    if not isinstance(argv, list):
        return None
    for flag, value in zip(argv, argv[1:], strict=False):
        if flag == "--experiment-dir" and isinstance(value, str) and value:
            return Path(value)
    return None


def _same_dir(a: Path, b: Path) -> bool:
    """Symlink/relative-tolerant path equality (``resolve()`` both sides)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def scan_dead_detached_workers(experiment_dir: Path, *, now: str) -> list[dict[str, Any]]:
    """Detached-worker liveness scan — the §5 stalled-run scan's blind spot.

    A detached submit block (S2/S3/S4/speculate,
    :mod:`hpc_agent._kernel.lifecycle.detached`) runs its SSH work in a
    subprocess whose lease lives under the journal home's ``_detached/`` dir as
    ``<block>-<run_id>.lease.json`` (carrying ``run_id``/``block``/``pid``). The
    stalled-driver scan only walks *in-flight* runs
    (:func:`find_stalled_runs`), so it is blind to a worker that dies on a run
    whose journal is ALREADY terminal — most sharply the S4 *harvest*, which
    runs AFTER the run is terminal: a dead harvest worker leaves no metrics and
    nothing else flags it.

    The ``_detached/`` lease dir is GLOBAL (one journal home serves every
    experiment) while the terminal store consulted below is PER-EXPERIMENT, so
    the scan first scopes each lease to *experiment_dir* via the
    ``--experiment-dir`` flag its stamped ``argv`` carries
    (:func:`_lease_experiment_dir`). Without that scoping, another experiment's
    normally-FINISHED worker (its lease is never cleaned up — only reclaimed on
    re-spawn) read as dead-with-no-terminal in every OTHER project's doctor run,
    permanently flipping ``needs_attention``. A lease naming no experiment dir
    is skipped (conservative: never draft a NEEDS-ATTENTION proposal that
    cannot be scoped to this experiment).

    For each of THIS experiment's leases whose ``pid`` is DEAD
    (:func:`pid_alive` false), consult the block terminal-result store
    (:func:`read_terminal_with_fallback`, keyed by the lease's own ``block``
    verb — the canonical key — with a legacy short-key fallback so a
    pre-2026-07-07 record still counts): a dead pid WITH a recorded terminal is
    normal completion (the worker finished, wrote its terminal, exited) and is
    skipped. A dead pid with NO recorded terminal is a worker that died
    mid-flight — surfaced with a DRAFTED re-invoke proposal (the
    recorded-terminal replay makes re-running idempotent). Detection only:
    ``doctor`` NEVER re-invokes anything.

    Pure local filesystem read — the global ``_detached/`` lease dir plus the
    per-experiment ``.hpc/runs/`` terminal store. No SSH. Fail-open: an absent
    dir, an unreadable/pid-less/foreign-experiment lease, or a lease still
    naming a LIVE pid yields nothing surfaced.

    FUTURE (not implemented): this scan only catches a worker whose pid is DEAD.
    A worker that is ALIVE but wedged mid-flight is invisible here. Each lease has
    a sibling ``_detached/*.log`` into which
    :func:`hpc_agent._kernel.lifecycle.heartbeat.detached_heartbeat` appends an
    ``[hb] alive Ns …`` line every ~30s; reading that log's last ``[hb]`` line
    beside a LIVE lease — a stale elapsed stamp, or a ``frozen-at-birth suspect``
    flag — would extend this scan from "dead pid, no terminal" to "alive but
    frozen", the finding-16 signature. Left as a note so the seam is discoverable.
    """
    detached_dir = current_homedir() / "_detached"
    if not detached_dir.is_dir():
        return []
    findings: list[dict[str, Any]] = []
    for lease_path in sorted(detached_dir.glob("*.lease.json")):
        try:
            lease = json.loads(lease_path.read_text(encoding="utf-8"))
            run_id = lease["run_id"]
            block = lease["block"]
            pid = int(lease.get("pid", -1))
        except (OSError, ValueError, TypeError, KeyError):
            # Unreadable / malformed / pid-less lease: nothing to probe. Fail
            # open — a torn lease must never crash the watchdog scan.
            continue
        if not (isinstance(run_id, str) and isinstance(block, str) and run_id and block):
            continue
        lease_exp = _lease_experiment_dir(lease)
        if lease_exp is None or not _same_dir(lease_exp, experiment_dir):
            # Another experiment's worker (the lease dir is global; its
            # terminal store lives under ITS experiment dir, not ours), or a
            # lease naming no experiment dir at all. Either way, no proposal —
            # a foreign finished worker must never flip THIS experiment's
            # needs_attention.
            continue
        if pid <= 0:
            # A legit lease is stamped with the launched pid (> 0) only AFTER a
            # successful Popen; a non-positive pid is a never-stamped / torn
            # lease, not a mid-flight death. Fail open — do not surface it.
            continue
        if pid_alive(pid):
            continue  # a live worker owns the lease — not our concern
        if read_terminal_with_fallback(experiment_dir, run_id, block) is not None:
            # dead pid WITH a recorded terminal = normal completion. Read by the
            # canonical VERB key (== the lease's ``block``) with the legacy short
            # "s2" fallback (2026-07-07 key fix): a FINISHED submit worker records
            # its terminal under the verb key now, but a run whose terminal predates
            # the fix sits under the short key — either way it is NOT a dead worker,
            # so no spurious re-invoke is drafted.
            continue
        proposal = (
            f"detached {block} worker for run {run_id} died with no recorded terminal "
            f"(pid {pid} is not running, and state/block_terminal holds no {block} result "
            f"for this run) — its SSH work (e.g. the S4 harvest, which runs after the run "
            f"is already terminal) never completed and no in-flight scan covers it. Re-invoke "
            f"{block} for {run_id}: the recorded-terminal replay makes this idempotent. "
            f"doctor never enacts this — you decide whether to re-run."
        )
        findings.append({"run_id": run_id, "block": block, "pid": pid, "proposal": proposal})
    return findings


def _attention_summary(
    *,
    stalled: int,
    awaiting_advance: int,
    parked: int,
    alerts: int,
    dead_workers: int = 0,
    open_circuits: list[str] | None = None,
) -> tuple[bool, str]:
    """The unmistakable top-of-envelope digest: ``(needs_attention, one-liner)``.

    Attention = a stalled driver, a committed-but-unadvanced run (both mean a
    dead driver the human must re-arm), a dead detached worker with no recorded
    terminal (T3: a mid-flight submit-block crash — esp. the post-terminal S4
    harvest — that the in-flight stalled scan cannot see), or an OPEN ssh circuit
    (discovery to that host is dark by design — the 2026-07-05 incident: an agent
    holding this very output improvised ssh probes and mis-diagnosed a VPN outage
    because the breaker state was recorded but not surfaced). Parked runs and
    unacknowledged alert-log entries are appended to the line for delivery but
    do not flip the flag on their own — parked is a valid wait, and an alert's
    underlying condition is re-detected live by this very scan (proving run
    #3: the alert LOG is the audit trail; the live scan is the truth).
    """
    open_circuits = open_circuits or []
    parts: list[str] = []
    if stalled:
        parts.append(f"{stalled} stalled driver(s)")
    if awaiting_advance:
        parts.append(f"{awaiting_advance} approved-but-unadvanced run(s)")
    if dead_workers:
        parts.append(f"{dead_workers} dead detached worker(s) with no harvest")
    if open_circuits:
        parts.append(f"{len(open_circuits)} open ssh circuit(s)")
    needs_attention = bool(parts)
    if needs_attention:
        line = f"NEEDS ATTENTION: {' and '.join(parts)}."
        if stalled or awaiting_advance or dead_workers:
            line += " Drafted re-arm proposal(s) below."
    elif parked:
        line = f"no stalled drivers; {parked} run(s) parked awaiting your decision."
    else:
        line = "all clear: no stalled drivers."
    for circuit_line in open_circuits:
        line += f" {circuit_line}"
    if alerts:
        line += f" {alerts} unacknowledged alert(s) in doctor.alerts.log."
    return needs_attention, line


def _jsonschema_importable_probe(now: str) -> list[AlertRecord]:
    """G5 preflight surface: is ``jsonschema`` importable at all (no SSH, local).

    Latency plan B1 moved ``import jsonschema`` out of module scope and into the
    functions that validate a ``--spec`` payload (the campaign atoms, the
    contract schema loader). That kills the cold-startup cost, but it also moves
    a broken / absent ``jsonschema`` install's ``ImportError`` from *import time*
    (loud, immediate, on every CLI turn) to *first-validate time* (deep inside a
    submit / campaign flow, after the human has already committed to a run). This
    probe restores an early, local, no-SSH surface: attempt the import here and,
    on failure, surface an alert so the operator sees the missing dependency at
    ``doctor`` time instead of mid-run. Success returns ``[]`` (no noise). The
    alert rides the envelope's ``alerts`` list for delivery; it does not flip
    ``needs_attention`` on its own (a missing dep is a preflight advisory, not a
    stalled driver) — consistent with the log-alert delivery contract.
    """
    try:
        import jsonschema  # noqa: F401
    except ImportError as exc:
        return [
            AlertRecord(
                ts=now,
                message=(
                    "jsonschema is not importable "
                    f"({exc}) — spec validation is lazy now (latency plan B1), so this "
                    "would otherwise only surface at first --spec validation deep inside "
                    "a submit/campaign flow. Reinstall hpc-agent's dependencies "
                    "(e.g. `uv tool install --reinstall .` or `pip install -e '.[dev]'`)."
                ),
            )
        ]
    return []


def _transport_drift_routing(now: str) -> list[AlertRecord]:
    """Route live transport-env drift to its heal class (detection + routing ONLY).

    The classifier front-end's doctor-seat wiring (overnight-repair.md §8 item 3).
    Reads ONLY local state — the live healable transport overrides
    (:func:`infra.env_flags.active_transport_overrides`) — and, for each live
    override, asks the classifier (:func:`ops.recover.heal_taxonomy.classify_crash_cause`)
    for its route. Surfaces each as a routing alert. It NEVER opens SSH and NEVER
    unsets a var — a drifted transport var is C1 (elicit-then-mint the env-pin anchor)
    or, once an anchor exists, B; the ENACTMENT is a spawned detached child, never the
    doctor process. Returns ``[]`` when no transport override is live.
    """
    from hpc_agent.infra.env_flags import active_transport_overrides
    from hpc_agent.ops.recover.heal_taxonomy import classify_crash_cause

    live = active_transport_overrides()
    if not live:
        return []
    routing = classify_crash_cause("env-drift", context={"anchored": False})
    names = ", ".join(sorted(live))
    return [
        AlertRecord(
            ts=now,
            message=(
                f"transport-env drift routed to Class {routing.heal_class} "
                f"({routing.arm}): live healable transport override(s) [{names}]. "
                f"{routing.reason} doctor ROUTES only — it never unsets a var; the "
                "heal is enacted by a spawned detached child on the human's `y`."
            ),
        )
    ]


@primitive(
    name="doctor",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Driver watchdog (dead-man's switch). Scan live runs for a missed "
            "driver-tick deadline and surface each as a DRAFTED recovery proposal "
            "plus the evidence. Read-only, no SSH, no scheduler. It NEVER restarts "
            "or re-arms anything — detection is its whole job; safe recovery is "
            "guaranteed by tick idempotency. Run it out-of-session from an OS "
            "scheduler (Task Scheduler / cron)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=DoctorSpec,
        schema_ref=SchemaRef(input="doctor"),
    ),
    agent_facing=True,
)
def doctor(*, experiment_dir: Path, spec: DoctorSpec) -> dict[str, Any]:
    """Scan for stalled drivers under *experiment_dir*; return drafted proposals.

    *spec.now* optionally overrides the evaluation instant (for deterministic
    testing); it defaults to the current UTC time. Each live run whose stamped
    ``next_tick_due`` is before that instant is a stalled driver — returned with
    a drafted recovery proposal the human decides on. Runs *parked on a human
    decision* (a ``pending_decision`` marker, §5 "parked ≠ stalled") are split by
    the decision journal: while still genuinely awaiting the human they are
    surfaced in ``parked`` as an "awaiting your decision since T" read; once the
    human's ``y`` is committed but the driver died before advancing (§5 Phase-5)
    they are surfaced in ``awaiting_advance`` as a drafted re-arm proposal — a
    stalled driver. Neither ever appears in ``stalled``. No side effects.

    The envelope leads with delivery: ``needs_attention`` (True iff any stalled
    or approved-but-unadvanced driver was found) and ``attention_summary`` (a
    one-line human digest) make the verdict unmistakable without reading the
    per-run lists, and ``alerts`` carries the unacknowledged entries from the
    ``doctor.alerts.log`` audit trail (fail-open; doctor never acknowledges or
    truncates — the status snapshot's watermark owns acknowledgment).

    ``open_ssh_circuits`` carries one line per host whose SSH circuit breaker
    is OPEN (read from the local ``_ssh_circuit`` state files — still no SSH):
    an open breaker means discovery to that host is dark by design, flips
    ``needs_attention``, and joins the summary line so no agent concludes a
    network cause without seeing it (run ``net-triage`` for the differential).

    Additionally surfaces ``version_skew`` when the running CLI's embedded
    build sha differs from the HEAD of the hpc-agent *source repo* that
    *experiment_dir* belongs to (stale install — reinstall). Fail-open: no
    git, no embedded sha, or not that repo → the field is simply null.

    Raises :class:`errors.SpecInvalid` if *spec.now* is a non-ISO-8601 string.
    """
    experiment_dir = Path(experiment_dir)
    now = (spec.now or "").strip() or utcnow_iso()
    if parse_iso_utc_or_none(now) is None:
        raise errors.SpecInvalid(f"doctor: now override {spec.now!r} is not ISO-8601 UTC")

    stalled = find_stalled_runs(now, experiment_dir=experiment_dir)
    proposals = [_draft_proposal(hit, now=now) for hit in stalled]

    # Parked ≠ stalled (§5): runs awaiting a human decision are surfaced as a
    # distinct read, never a re-arm proposal. find_stalled_runs already excludes
    # them, so a parked run can never appear in the stalled list.
    #
    # But a parked run splits into two sub-states (§5 Phase-5): a run is only
    # genuinely *awaiting the human* while its latest committed decision is NOT
    # the greenlight for THIS parked boundary. Once the human commits that `y`
    # (marker still set, driver dead — no OS scheduler consumed it), it is a
    # STALLED driver that must be re-armed. BOUNDARY-SCOPED (bug-sweep #1/#23,
    # run-12 finding 21): a consumed `y` stays the journal's latest record after
    # the driver re-parks, so the bare latest-is-y read would re-arm a driver
    # that is genuinely waiting — is_committed_greenlight_for_boundary is the
    # same shared rule the in-session Stop guard
    # (decision_rendezvous_stop_guard.find_committed_unadvanced) keys on, so the
    # hook and the doctor agree on "committed-but-unadvanced".
    parked_hits = find_parked_runs(now, experiment_dir=experiment_dir)
    parked_notes: list[ParkedRunNote] = []
    advance_proposals: list[AdvanceRunProposal] = []
    for hit in parked_hits:
        run_id = hit["run_id"]
        marker = read_pending_decision(run_id, experiment_dir=experiment_dir) or {}
        cursor = marker.get("resume_cursor") or {}
        next_verb = cursor.get("next_verb") if isinstance(cursor, dict) else None
        if is_committed_greenlight_for_boundary(
            experiment_dir,
            "run",
            run_id,
            next_verb=next_verb,
            awaiting_since=marker.get("awaiting_since") or hit.get("awaiting_since"),
            # F13: the parked block so a same-boundary retraction nudge supersedes an
            # earlier `y` (stays parked), while an unrelated later record does not stall it.
            block=marker.get("block") if isinstance(marker.get("block"), str) else None,
        ):
            decision = latest_decision(experiment_dir, "run", run_id)
            advance_proposals.append(_draft_advance_proposal(hit, decision=decision, now=now))
        else:
            parked_notes.append(_draft_parked_note(hit))

    # Delivery, not just detection (proving run #3): carry the unacknowledged
    # alert-log entries in the envelope too, and lead with an unmistakable
    # needs-attention line. Read-only — doctor never moves the acknowledgment
    # watermark (that is the status snapshot's job) and never truncates the log.
    from hpc_agent.ops.recover.net_triage import open_circuit_lines
    from hpc_agent.ops.recover.notify import read_unacknowledged_alerts

    log_alerts = [AlertRecord(**a) for a in read_unacknowledged_alerts(experiment_dir)]

    # Dead detached workers (T3): the stalled-run scan above only walks
    # IN-FLIGHT runs, so a detached submit block (esp. the S4 harvest, which runs
    # AFTER the run is terminal) that dies mid-flight is invisible to it. Scan the
    # `_detached/` leases for a DEAD pid with NO recorded block-terminal and
    # surface each as a drafted re-invoke proposal — detection only, still no SSH.
    dead_worker_findings = scan_dead_detached_workers(experiment_dir, now=now)
    dead_worker_alerts = [AlertRecord(ts=now, message=f["proposal"]) for f in dead_worker_findings]

    # Overnight self-heal (item 8 ruling, 2026-07-09): the OS-scheduled scan is the
    # ONE failure domain that survives when every in-session process died, so it is
    # the seat that can revive a campaign reconcile chain nobody is left to re-arm.
    # Opt-in (spec.self_heal, mirroring spec.notify's opt-in side effect) so the
    # plain in-session detection verb is byte-unchanged. Under a LIVE standing
    # consent it respawns the sanctioned WATCHER (never the scheduler) and, on
    # exhaustion, flips the consent dead + fires the fail-loud alert. Reads only
    # local state — no SSH. Fail-open: a heal-scan error never breaks detection.
    heal_alerts: list[AlertRecord] = []
    if spec.self_heal:
        try:
            from hpc_agent.ops.overnight import self_heal_scan

            for outcome in self_heal_scan(experiment_dir, now_iso=now):
                if outcome.status in {"exhausted", "structurally-impossible"}:
                    line = outcome.attempt_line if isinstance(outcome.attempt_line, dict) else {}
                    raw_detail = line.get("detail")
                    detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
                    msg = detail.get("text") or f"overnight self-heal failed: {outcome.reason}"
                    heal_alerts.append(AlertRecord(ts=now, message=str(msg)))
        except Exception:  # noqa: BLE001 — self-heal must never break the watchdog scan
            heal_alerts = []
        # Classifier front-end on the doctor seat (overnight-repair.md §8 item 3,
        # §10 doctor-seat row): crash-cause → class ROUTING only. Detection +
        # routing, NEVER actuation — the doctor process opens no SSH; a heal is
        # enacted by a spawned detached child (the stray reaper / watcher re-arm).
        # The one cause the doctor can classify from pure LOCAL state is transport
        # ENV DRIFT (a live healable transport override, finding 24d) — routed to
        # C1 (elicit-then-mint) or, once an env-pin anchor exists, B. Surfaced as a
        # routing alert; the doctor never unsets a var.
        with contextlib.suppress(Exception):
            heal_alerts.extend(_transport_drift_routing(now))

        # Leaked ssh-slot reaper (run-14 slot starvation, 2026-07-16): a watcher
        # or detached worker hard-killed mid-run leaks its per-host
        # ``_ssh_throttle`` slot (its finally/atexit release never ran). The
        # contention-time reaper in ssh_slots.acquire_slot only reclaims it when a
        # NEW acquirer contends on that EXACT host, so until then a dead holder's
        # slot keeps eating one of the N=2 per-host slots. The OS-scheduled
        # self-heal seat is the right place to sweep it proactively: reap every
        # DEAD-pid slot (pid-liveness only — a LIVE holder is never touched) so a
        # leaked slot cannot starve a concurrent harvest/retry. Best-effort and
        # local-only (no SSH); never breaks the watchdog scan.
        with contextlib.suppress(Exception):
            from hpc_agent.infra.ssh_slots import reap_stale_slots

            reclaimed_slots = reap_stale_slots()
            if reclaimed_slots:
                heal_alerts.append(
                    AlertRecord(
                        ts=now,
                        message=(
                            f"reclaimed {reclaimed_slots} leaked ssh connection slot(s) "
                            "under <journal home>/_ssh_throttle — a hard-killed holder's "
                            "slot the contention-time reaper had not yet reclaimed (it "
                            "was eating one of the per-host burst-limiter slots)."
                        ),
                    )
                )

    # G5 preflight surface (latency plan B1): jsonschema is imported lazily now,
    # so a broken/absent install would only fail at first --spec validation deep
    # inside a flow. Probe it here (local, no SSH) and ride any failure on the
    # `alerts` list for delivery — like the dead-worker drafts, it does not flip
    # needs_attention (a missing dep is a preflight advisory, not a stalled run).
    jsonschema_alerts = _jsonschema_importable_probe(now)

    # Both the log audit-trail entries and the dead-worker drafts ride the
    # envelope's `alerts` list for delivery; only the log entries feed the
    # "in doctor.alerts.log" suffix (the dead-worker drafts are live-scan output,
    # not log lines), while the dead workers get their own attention part.
    alerts = log_alerts + dead_worker_alerts + heal_alerts + jsonschema_alerts

    # Open ssh circuits (2026-07-05 incident): a breaker-dark host must be
    # visible on the surface the agent already reads — read-only, fail-open,
    # still no SSH (the breaker state is a local file).
    circuit_lines = open_circuit_lines()

    needs_attention, attention_summary = _attention_summary(
        stalled=len(proposals),
        awaiting_advance=len(advance_proposals),
        parked=len(parked_notes),
        alerts=len(log_alerts),
        dead_workers=len(dead_worker_findings),
        open_circuits=circuit_lines,
    )

    result = DoctorResult(
        now=now,
        needs_attention=needs_attention,
        attention_summary=attention_summary,
        alerts=alerts,
        open_ssh_circuits=circuit_lines,
        stalled_count=len(proposals),
        stalled=proposals,
        parked_count=len(parked_notes),
        parked=parked_notes,
        awaiting_advance_count=len(advance_proposals),
        awaiting_advance=advance_proposals,
        version_skew=_detect_version_skew(experiment_dir),
        active_env_overrides=active_env_overrides(),
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")

    # Opt-in (§5): the OS-scheduled scan surfaces stalls as an OS notification
    # instead of printing JSON no one reads. Notify only — never acts. Default
    # spec.notify is False, so the plain in-session verb is unchanged.
    if spec.notify and proposals:
        from hpc_agent.ops.recover.notify import raise_stall_notification

        raise_stall_notification(dumped["stalled"], experiment_dir=experiment_dir)

    return dumped
