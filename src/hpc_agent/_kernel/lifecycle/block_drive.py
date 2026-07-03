"""``block-drive`` — the stateless resumable tick that drives a block chain.

Wave 4 of the block architecture (``docs/design/block-drive.md`` §2–§5). The
LLM no longer executes the deterministic transition between blocks: this driver
does. One invocation:

* **chains the deterministic spans in code** — a block that returns without a
  human decision and with a code-determined successor is followed immediately,
  in the same tick, with no LLM round-trip (§2, the whole point);
* **at a decision point, parks and exits** — writes ``{brief, pending marker,
  resume cursor}`` to durable state (:func:`journal.mark_pending_decision`) and
  returns. Nothing is held open between decisions; the journal + filesystem are
  the only state carried, exactly like the campaign tick this generalizes
  (``_kernel/lifecycle/drive.py``);
* **on resume, consumes an approved SPEC — never a nudge string** (§3). When a
  ``pending_decision`` exists the tick reads the decision journal for the latest
  committed ``response=="y"`` record; if none is committed it is still awaiting
  the human — a valid parked stop, exit 0. If committed, it routes by IDENTITY +
  OWNERSHIP (§4): diff the approved ``resolved`` spec against the inputs the
  block last ran under, then :func:`field_ownership.route` picks
  ``advance`` / ``rerun`` / ``advance_carrying``. The code keys on the spec, not
  the sentiment — the "code never interprets raw NL" invariant at the rendezvous.

The mechanism mirrors ``drive.py``'s one-step-per-invocation, durable-state-only,
cadence-stamping discipline. :func:`plan_block_action` is the pure planner (no
I/O, unit-testable, mirroring ``drive.plan_action``); :func:`run_tick` does the
I/O + chaining; :func:`block_drive_once` is the int-returning console /
detach-child entry; :func:`main` is its argparse shell.

THE CODE NEVER READS A NUDGE STRING. The digestion of natural language into a
spec (including every nudge redraft) happens entirely in chat *before* approval;
the driver's only input on resume is the approved ``resolved`` spec (§3).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from hpc_agent._wire.workflows.block_drive import BlockDriveResult
from hpc_agent.infra import block_chain
from hpc_agent.ops import field_ownership
from hpc_agent.state.journal import (
    clear_pending_decision,
    mark_pending_decision,
    read_pending_decision,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "plan_block_action",
    "run_tick",
    "block_drive_once",
    "main",
]

_log = logging.getLogger(__name__)

# Keys on a ``resolved`` / input spec that are METADATA, not experiment inputs —
# excluded from the §4 changed-field diff so a routing pointer never registers as
# an edit. ``next_block`` is the greenlit successor pointer the LLM commits
# alongside the approved inputs (design §6/§8); it is a routing token, not a
# field the ownership map attributes.
_META_KEYS: frozenset[str] = frozenset({"next_block"})

# The FRESH entry blocks whose spec the driver CAN materialize from a bare
# ``(run_id, workflow)`` — both take a top-level ``run_id`` (§3). The other two
# workflow entry blocks are NOT bare-startable: ``submit-s1`` needs the
# ``goal``/``task_generator``/``walk`` inputs and ``campaign-greenlight`` needs a
# ``campaign_id``, none of which live on ``BlockDriveSpec``. A fresh start of
# those is driven by the interview skill / campaign reconcile driver, not a bare
# ``block-drive`` tick — the driver returns a clear ``skip`` rather than running a
# doomed span (see :func:`_fresh_entry_spec` / :func:`run_tick`).
_FRESH_ENTRY_RUN_ID_BLOCKS: frozenset[str] = frozenset({"status-snapshot", "aggregate-check"})


# ── pure planning (no I/O) ─────────────────────────────────────────────────────


def _spec_sha(spec: dict[str, Any]) -> str:
    """A deterministic identity for an input *spec* (the §4 identity fast-path).

    SHA-256 over the sorted-keys JSON of the spec's non-metadata fields — the
    same "identity, not sentiment" idea ``cmd_sha`` encodes for a task list, but
    over a block's input spec. Equal shas ⇒ unchanged ⇒ advance; unequal ⇒ fall
    back to the field-level diff to attribute the edit (§4).
    """
    payload = {k: v for k, v in spec.items() if k not in _META_KEYS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _changed_fields(
    last_run_inputs: dict[str, Any],
    approved: dict[str, Any],
    *,
    last_cmd_sha: str | None = None,
    approved_cmd_sha: str | None = None,
) -> set[str]:
    """Which input fields the approved spec changed vs the last-run inputs (§4).

    Identity fast-path first: when both ``cmd_sha`` identities are present and
    equal, the spec is unchanged → empty set (a plain ``y`` → advance). Otherwise
    diff the two spec dicts field-by-field (metadata keys excluded), returning the
    set of keys whose values differ or that exist on only one side. This is the
    field-level attribution :func:`field_ownership.route` needs to pick the route.
    """
    if last_cmd_sha and approved_cmd_sha and last_cmd_sha == approved_cmd_sha:
        return set()
    last = {k: v for k, v in last_run_inputs.items() if k not in _META_KEYS}
    new = {k: v for k, v in approved.items() if k not in _META_KEYS}
    return {k for k in set(last) | set(new) if last.get(k) != new.get(k)}


def _carry(approved: dict[str, Any], changed_fields: set[str]) -> dict[str, Any]:
    """The edited field VALUES to fold into the next/re-run spec (§4).

    Projects the approved ``resolved`` spec onto just the changed, non-metadata
    fields — what an ``advance_carrying`` folds into the downstream block's spec,
    or a ``rerun`` feeds back into the current block.
    """
    return {k: approved[k] for k in changed_fields if k in approved and k not in _META_KEYS}


def plan_block_action(
    *,
    workflow: str | None,
    pending_decision: dict[str, Any],
    committed_resolved: dict[str, Any] | None,
    last_run_inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Map the driver's position to a concrete first-action intent (pure).

    Mirrors ``drive.plan_action``: no I/O, so the §4 routing is unit-testable with
    the journal + block-verb calls faked. The caller (:func:`run_tick`) has
    already read the durable state and passes it in. Returns a dict whose
    ``action`` is one of:

    * ``"fresh"`` — no pending decision → begin ``workflow``'s chain at its first
      block (``block_chain.ORDER[workflow][0]``).
    * ``"awaiting_decision"`` — a pending decision exists but no ``response=="y"``
      is committed yet → a valid PARKED stop (exit 0, do nothing).
    * ``"advance"`` — approved spec unchanged (§4) → run the resume cursor's
      code-determined ``next_verb``.
    * ``"rerun"`` — a changed field owned by the current block (§4) → re-run the
      current block, carrying the edited inputs so it recomputes derived fields.
    * ``"advance_carrying"`` — all changed fields owned strictly downstream (§4) →
      run ``next_verb`` carrying the edit (no needless re-run).
    * ``"terminal"`` — committed, routed to advance, but no successor (end of chain).
    * ``"skip"`` — nothing drivable (no workflow to start / unrecoverable position).

    Routing NEVER reads a nudge string — only ``committed_resolved`` (an approved
    spec) and the ownership map (§3/§4).
    """
    # ── FRESH start: no pending decision. Begin the workflow's chain. ──────────
    if not pending_decision:
        if not workflow or workflow not in block_chain.ORDER:
            return {
                "action": "skip",
                "reason": (
                    f"no pending decision and no known workflow to start "
                    f"(got workflow={workflow!r})"
                ),
            }
        first = block_chain.ORDER[workflow][0]
        return {
            "action": "fresh",
            "verb": first,
            "workflow": workflow,
            "current_verb": None,
            "next_verb": first,
            "carry_fields": {},
            "reason": f"fresh {workflow} chain — start at {first}",
        }

    # ── RESUME: a pending decision exists. Recover the parked position. ────────
    cursor = pending_decision.get("resume_cursor", {})
    current_verb = cursor.get("current_verb")
    wf = cursor.get("workflow") or pending_decision.get("workflow") or workflow
    next_verb = cursor.get("next_verb")

    # Not committed yet → still awaiting the human. A valid parked stop (§5).
    if committed_resolved is None:
        return {
            "action": "awaiting_decision",
            "workflow": wf,
            "current_verb": current_verb,
            "next_verb": next_verb,
            "reason": (
                "pending decision not yet committed (no response=='y') — awaiting the human"
            ),
        }

    if wf is None or current_verb is None:
        return {
            "action": "skip",
            "reason": "pending decision is missing its resume cursor position; cannot route",
        }

    # Committed → route by IDENTITY + OWNERSHIP (§4), never by the nudge text.
    changed = _changed_fields(
        last_run_inputs or {},
        committed_resolved,
        last_cmd_sha=pending_decision.get("cmd_sha"),
        approved_cmd_sha=committed_resolved.get("cmd_sha"),
    )
    # ``route`` accepts stage_reached for call-shape symmetry only; it does not
    # branch on it (the advance target is the cursor's stored ``next_verb``).
    route = field_ownership.route(wf, current_verb, changed, stage_reached="")

    if route == "rerun":
        return {
            "action": "rerun",
            "verb": current_verb,
            "workflow": wf,
            "current_verb": current_verb,
            "next_verb": next_verb,
            # Re-run the block under the edited inputs so it recomputes its
            # derived fields and emits a fresh brief (§4).
            "carry_fields": {k: v for k, v in committed_resolved.items() if k not in _META_KEYS},
            "changed_fields": sorted(changed),
            "reason": (
                f"nudge edits {sorted(changed)} owned by {current_verb} — re-run to recompute"
            ),
        }

    # advance / advance_carrying both target the stored successor.
    if next_verb is None:
        return {
            "action": "terminal",
            "workflow": wf,
            "current_verb": current_verb,
            "next_verb": None,
            "reason": f"{current_verb} approved with no successor — end of the {wf} chain",
        }

    if route == "advance_carrying":
        return {
            "action": "advance_carrying",
            "verb": next_verb,
            "workflow": wf,
            "current_verb": current_verb,
            "next_verb": next_verb,
            "carry_fields": _carry(committed_resolved, changed),
            "changed_fields": sorted(changed),
            "reason": (
                f"edits {sorted(changed)} owned downstream of {current_verb} — "
                f"advance to {next_verb} carrying them"
            ),
        }

    # route == "advance"
    return {
        "action": "advance",
        "verb": next_verb,
        "workflow": wf,
        "current_verb": current_verb,
        "next_verb": next_verb,
        "carry_fields": {},
        "changed_fields": sorted(changed),
        "reason": f"approved spec unchanged — advance {current_verb} → {next_verb}",
    }


# ── I/O: running a block verb + inspecting its Result ──────────────────────────


def _run_block_verb(
    verb: str, spec: dict[str, Any], experiment_dir: Path
) -> tuple[dict[str, Any], int]:
    """Run one ``hpc-agent <verb>`` block via the CLI and return ``(result, code)``.

    Mirrors ``drive._run_cli_step`` (a temp spec file + subprocess), but CAPTURES
    stdout so the driver can read the block's Result: the ``{block, stage_reached,
    needs_decision, next_block, …}`` ``data`` block of the JSON envelope. Returns
    an empty dict on a non-zero exit or an unparseable envelope (the caller treats
    that as a failed span).
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix=f"{verb}-spec-", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(spec, handle)
        spec_path = handle.name
    try:
        proc = subprocess.run(
            ["hpc-agent", verb, "--spec", spec_path, "--experiment-dir", str(experiment_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(spec_path)
    if proc.returncode != 0:
        _log.warning(
            "block verb %s failed (exit %s): %s", verb, proc.returncode, proc.stderr.strip()
        )
        return {}, proc.returncode
    try:
        envelope = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        _log.warning("block verb %s produced an unparseable envelope", verb)
        return {}, 1
    data = envelope.get("data")
    return (data if isinstance(data, dict) else {}), 0


def _is_detached(result: dict[str, Any]) -> bool:
    """True when a block handed the poll to a detached, scheduler-bound child.

    Such a block returns a handle (``started`` / a ``detached_pid`` / a
    ``stage_reached`` of ``"detached"``) rather than a decision: the child owns
    the poll, so the tick exits returning the handle and does NOT block (§2, the
    detach-by-contract terminators).
    """
    if result.get("started") or result.get("detached_pid") or result.get("watch"):
        return True
    return result.get("stage_reached") == "detached"


def _next_verb_of(result: dict[str, Any]) -> str | None:
    """The block's code-determined successor VERB, honoring its runtime gate.

    Reads the block Result's own ``next_block`` (the ``{verb, why, spec_hint}``
    hint, or null at a terminal / human branch). Using the Result — not a raw
    :func:`block_chain.successor_verb` lookup — respects a block's runtime gate
    (e.g. status-snapshot emits ``next_block=None`` when no live run exists even
    though the table lists status-watch).
    """
    nb = result.get("next_block")
    if isinstance(nb, dict):
        verb = nb.get("verb")
        return verb if isinstance(verb, str) else None
    return None


def _next_spec_hint(result: dict[str, Any]) -> dict[str, Any]:
    """The minimal next-spec skeleton the block attached to its ``next_block``."""
    nb = result.get("next_block")
    if isinstance(nb, dict):
        hint = nb.get("spec_hint")
        if isinstance(hint, dict):
            return dict(hint)
    return {}


def _fresh_entry_spec(verb: str | None, run_id: str | None) -> dict[str, Any] | None:
    """Build the FIRST span's spec for a FRESH chain start, or ``None`` if unbuildable.

    ``status-snapshot`` / ``aggregate-check`` accept a top-level ``run_id`` (§3), so
    the driver materializes ``{"run_id": run_id}`` (or ``{}`` — a fleet digest — when
    no run_id is supplied to ``status-snapshot``). ``submit-s1`` and
    ``campaign-greenlight`` need inputs a bare tick can't supply, so the driver
    returns ``None`` and :func:`run_tick` reports a clear ``skip`` naming the missing
    inputs rather than running a span the block would reject with ``SpecInvalid``.
    """
    if verb in _FRESH_ENTRY_RUN_ID_BLOCKS:
        return {"run_id": run_id} if run_id else {}
    return None


# ── the tick ───────────────────────────────────────────────────────────────────


def run_tick(
    experiment_dir: Path,
    *,
    run_id: str | None,
    workflow: str | None,
    dry_run: bool = False,
) -> tuple[BlockDriveResult, int]:
    """Advance the block chain by one tick; return ``(result, exit_code)``.

    The I/O half (the pure routing lives in :func:`plan_block_action`). Reads the
    durable position, plans the first action, then — for an executable action —
    runs block verbs, CHAINING deterministic spans in code until it parks on a
    decision, hits a detached child, or reaches a terminal. Stamps the §5
    watchdog dead-man's-switch after every executed span (reusing
    ``drive._stamp_driver_tick``).

    Used by the :func:`block_drive_once` console entry (which prints the result +
    returns the exit code) and by the ``block-drive`` primitive wrapper (which
    returns the :class:`BlockDriveResult` directly).
    """
    pending = read_pending_decision(run_id, experiment_dir=experiment_dir) if run_id else {}
    committed_resolved: dict[str, Any] | None = None
    last_run_inputs: dict[str, Any] | None = None
    if pending:
        scope_wf = pending.get("workflow") or workflow
        scope_kind = "campaign" if scope_wf == "campaign" else "run"
        committed_resolved = _latest_committed_resolved(experiment_dir, scope_kind, run_id or "")
        cursor = pending.get("resume_cursor", {})
        last_run_inputs = cursor.get("input_spec") if isinstance(cursor, dict) else None

    plan = plan_block_action(
        workflow=workflow,
        pending_decision=pending,
        committed_resolved=committed_resolved,
        last_run_inputs=last_run_inputs,
    )
    action = plan["action"]

    # Non-executing outcomes: report and exit 0. ``awaiting_decision`` carries the
    # parked brief so the caller can re-surface it.
    if dry_run or action in ("skip", "awaiting_decision", "terminal"):
        brief = pending.get("brief") if action == "awaiting_decision" else None
        if dry_run and action not in ("skip", "awaiting_decision", "terminal"):
            # A dry run of an executable plan: report what WOULD run, don't run it.
            return (
                BlockDriveResult(
                    action="skip",
                    run_id=run_id,
                    workflow=plan.get("workflow") or workflow,
                    current_verb=plan.get("current_verb"),
                    next_verb=plan.get("verb"),
                    reason=f"dry-run: would {action} → {plan.get('verb')}",
                ),
                0,
            )
        return (
            BlockDriveResult(
                action=action,  # type: ignore[arg-type]
                run_id=run_id,
                workflow=plan.get("workflow") or workflow,
                current_verb=plan.get("current_verb"),
                next_verb=plan.get("next_verb"),
                brief=brief if isinstance(brief, dict) else None,
                reason=plan.get("reason", ""),
            ),
            0,
        )

    # Executable resume actions consumed the approved ``resolved`` — clear the
    # pending marker before running so a re-entry does not double-consume it.
    resume_action = action in ("advance", "rerun", "advance_carrying")
    if pending and resume_action and run_id:
        clear_pending_decision(run_id, experiment_dir=experiment_dir)

    verb: str | None = plan["verb"]
    wf = plan.get("workflow") or workflow

    # Materialize the FIRST span's spec (§3, correct spec-materialization — no
    # blind top-level ``run_id`` injection).
    #
    # * RESUME (advance / rerun / advance_carrying): the approved ``resolved`` spec
    #   the LLM committed IS the correctly-shaped acting spec for the routed block
    #   (the gated block's nested-object spec, or the current block's for a rerun).
    #   The downstream edits an ``advance_carrying`` folds are already inside
    #   ``resolved``; the ``next_block`` routing token is stripped as metadata.
    # * FRESH: the entry block's minimal spec, or ``None`` when the block is not
    #   bare-startable (submit-s1 / campaign-greenlight) → a clear skip.
    if resume_action:
        first_spec: dict[str, Any] = {
            k: v for k, v in (committed_resolved or {}).items() if k not in _META_KEYS
        }
    else:
        built = _fresh_entry_spec(verb, run_id)
        if built is None:
            return (
                BlockDriveResult(
                    action="skip",
                    run_id=run_id,
                    workflow=wf,
                    next_verb=verb,
                    reason=(
                        f"cannot fresh-start {verb} from a bare block-drive tick — it "
                        "needs inputs beyond (run_id, workflow) "
                        "(submit-s1: goal/task_generator/walk; campaign-greenlight: "
                        "campaign_id). Drive its fresh start via the interview skill / "
                        "campaign reconcile driver."
                    ),
                ),
                0,
            )
        first_spec = built

    # The action label of the FIRST span (how the tick entered); subsequent
    # deterministic spans are ``chained``.
    first_label = {
        "fresh": "chained",
        "advance": "advanced",
        "advance_carrying": "advanced",
        "rerun": "reran",
    }[action]

    return _chain(
        experiment_dir,
        run_id=run_id or "",
        workflow=wf,
        first_verb=verb,
        first_spec=first_spec,
        first_label=first_label,
    )


def _chain(
    experiment_dir: Path,
    *,
    run_id: str,
    workflow: str | None,
    first_verb: str | None,
    first_spec: dict[str, Any],
    first_label: str,
) -> tuple[BlockDriveResult, int]:
    """Run block spans, chaining deterministic successors until a stop (§2).

    Loops: run ``verb`` under ``spec``, stamp the watchdog, then branch on the
    Result — ``needs_decision`` parks (writes the pending marker, exits), a
    detached handle exits, a code-determined UNGATED successor chains on in-code
    (its spec is the predecessor's ``spec_hint`` passed VERBATIM), a
    greenlight-GATED successor PARKS for the human ``y`` the gate requires (an
    in-code chain never journals it), and a terminal exits. The FIRST span's spec
    is materialized by the caller (:func:`run_tick`); chained spans take the
    predecessor's ``spec_hint`` — never a blind top-level ``run_id`` injection (§3).
    """
    from hpc_agent._kernel.lifecycle.drive import _stamp_driver_tick

    verb = first_verb
    spec: dict[str, Any] = dict(first_spec)
    last_action = first_label
    last_result: dict[str, Any] = {}

    while verb is not None:
        result, code = _run_block_verb(verb, spec, experiment_dir)
        if run_id:
            _stamp_driver_tick(experiment_dir, run_id)
        if not result:
            return (
                BlockDriveResult(
                    action="skip",
                    run_id=run_id or None,
                    workflow=workflow,
                    current_verb=verb,
                    reason=f"block {verb} failed or returned no result (exit {code})",
                ),
                code or 1,
            )

        last_result = result
        stage = result.get("stage_reached")
        successor = _next_verb_of(result)

        # A detached, scheduler-bound child now owns the poll — exit with the handle.
        if _is_detached(result):
            return (
                BlockDriveResult(
                    action="detached",
                    run_id=result.get("run_id") or (run_id or None),
                    workflow=workflow,
                    current_verb=verb,
                    next_verb=successor,
                    stage_reached=stage,
                    brief=result.get("brief") if isinstance(result.get("brief"), dict) else None,
                    reason=f"{verb} detached a child that owns the poll — tick exits",
                ),
                0,
            )

        # A human decision point — park and exit (the rendezvous).
        if result.get("needs_decision"):
            _park(
                experiment_dir,
                run_id=run_id,
                workflow=workflow,
                verb=verb,
                stage=stage,
                successor=successor,
                spec=spec,
                result=result,
            )
            return (
                BlockDriveResult(
                    action="awaiting_decision",
                    run_id=run_id or None,
                    workflow=workflow,
                    current_verb=verb,
                    next_verb=successor,
                    stage_reached=stage,
                    brief=result.get("brief") if isinstance(result.get("brief"), dict) else None,
                    reason=result.get("reason") or f"{verb} reached a decision point — parked",
                ),
                0,
            )

        # No decision, no successor → a clean terminal.
        if successor is None:
            return (
                BlockDriveResult(
                    action="terminal",
                    run_id=run_id or None,
                    workflow=workflow,
                    current_verb=verb,
                    next_verb=None,
                    stage_reached=stage,
                    reason=result.get("reason") or f"{verb} reached a terminal — chain complete",
                ),
                0,
            )

        # A greenlight-GATED successor (block_chain.is_gated is the SoT): an
        # in-code chain never journals the human ``y`` the gate requires, so PARK
        # here exactly as a decision does — surface the predecessor's brief, store
        # a resume cursor whose ``next_verb`` is the gated block, and exit. A later
        # tick advances into it once the human's greenlight is journaled and its
        # gate finds it (design: needs_decision + gate agree, zero gate re-scoping).
        if block_chain.is_gated(successor):
            _park(
                experiment_dir,
                run_id=run_id,
                workflow=workflow,
                verb=verb,
                stage=stage,
                successor=successor,
                spec=spec,
                result=result,
            )
            return (
                BlockDriveResult(
                    action="awaiting_decision",
                    run_id=run_id or None,
                    workflow=workflow,
                    current_verb=verb,
                    next_verb=successor,
                    stage_reached=stage,
                    brief=result.get("brief") if isinstance(result.get("brief"), dict) else None,
                    reason=(
                        result.get("reason")
                        or f"{verb} complete; greenlight required before {successor} — parked."
                    ),
                ),
                0,
            )

        # Deterministic continuation into an UNGATED successor: chain it IN CODE
        # (no LLM). The predecessor's spec_hint is the successor's VALID minimal
        # spec — passed VERBATIM (no top-level run_id injection, §3).
        last_action = "chained"
        spec = _next_spec_hint(result)
        verb = successor

    # Loop only exits via a return above unless the first verb was None.
    return (
        BlockDriveResult(
            action=last_action,  # type: ignore[arg-type]
            run_id=run_id or None,
            workflow=workflow,
            stage_reached=last_result.get("stage_reached"),
            reason="no block verb to run",
        ),
        0,
    )


def _park(
    experiment_dir: Path,
    *,
    run_id: str,
    workflow: str | None,
    verb: str,
    stage: Any,
    successor: str | None,
    spec: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Write the §5 pending-decision marker for a parked span (the rendezvous).

    Assembles the ``resume_cursor`` (the STATELESS-tick position: workflow,
    run_id, current_verb, the code-determined ``next_verb``, plus the driver-only
    ``input_spec`` the block ran under, so a resume tick can diff the approved
    ``resolved`` against it for §4 routing) and stamps ``awaiting_since`` +
    ``cmd_sha`` (the input-spec identity, the §4 fast-path key). This marker is
    what flips the doctor's read from "stalled" to "parked" (§5).
    """
    from hpc_agent.infra.time import utcnow_iso

    if not run_id:
        _log.warning("cannot park %s without a run_id; brief not persisted", verb)
        return
    wf = workflow or block_chain.WORKFLOW_OF.get(verb)
    resume_cursor: dict[str, Any] = {
        "workflow": wf,
        "run_id": run_id,
        "next_verb": successor,
        "current_verb": verb,
        # Driver-only additive keys (§4 routing): the inputs the block ran under
        # and the successor's minimal spec skeleton, so the resume tick can diff
        # and rebuild the next spec without re-reading the block.
        "input_spec": spec,
        "next_spec_hint": _next_spec_hint(result),
    }
    brief = result.get("brief")
    mark_pending_decision(
        run_id,
        block=verb,
        workflow=wf or "",
        brief=brief if isinstance(brief, dict) else {},
        resume_cursor=resume_cursor,
        awaiting_since=utcnow_iso(),
        cmd_sha=_spec_sha(spec),
        experiment_dir=experiment_dir,
    )


def _latest_committed_resolved(
    experiment_dir: Path, scope_kind: str, scope_id: str
) -> dict[str, Any] | None:
    """Return the ``resolved`` spec of the latest committed (``response=="y"``) decision.

    The commit-is-the-approval sentinel (§3/§5): the driver keys resume on the
    LATEST ``response=="y"`` record's ``resolved`` (the approved input spec).
    Returns ``None`` when nothing is committed yet — the tick is still awaiting the
    human (a valid parked stop). Never reads the nudge text of any record.
    """
    from hpc_agent.state.decision_journal import read_decisions

    if not scope_id:
        return None
    try:
        records = read_decisions(experiment_dir, scope_kind, scope_id)
    except Exception:  # noqa: BLE001 — a bad scope must not crash the tick
        return None
    for record in reversed(records):
        if record.get("response") == "y":
            resolved = record.get("resolved")
            return dict(resolved) if isinstance(resolved, dict) else {}
    return None


def block_drive_once(
    experiment_dir: Path,
    *,
    run_id: str | None,
    workflow: str | None,
    dry_run: bool = False,
) -> int:
    """Advance the block chain by one tick; print the result, return an exit code.

    The programmatic / console / detach-child entry (block-drive.md §7 "the CLI is
    the invariant substrate"). Delegates the work to :func:`run_tick`, prints the
    :class:`BlockDriveResult` as the per-tick record (like ``drive_once``), and
    returns the process exit code.
    """
    result, code = run_tick(experiment_dir, run_id=run_id, workflow=workflow, dry_run=dry_run)
    print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    return code


def main(argv: list[str] | None = None) -> int:
    """Console-script shell over :func:`block_drive_once` (mirrors ``drive.drive``).

    Exposes ``--experiment-dir`` / ``--run-id`` / ``--workflow`` / ``--dry-run``.
    This is the out-of-session / detach-child entry: cron, ``schtasks``, a
    ``doctor`` re-arm, or ``Popen(["hpc-block-drive", …])`` can all drive a chain
    without an LLM in the loop.
    """
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="hpc-block-drive",
        description="Advance one block-drive tick: chain deterministic spans, park at a decision.",
    )
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Experiment repo root (default: cwd).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="The run being driven (recovers the parked position on a resume).",
    )
    parser.add_argument(
        "--workflow",
        default=None,
        help="The workflow family to drive (submit / status / aggregate / campaign).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned action and exit without executing any span.",
    )
    args = parser.parse_args(argv)
    return block_drive_once(
        args.experiment_dir,
        run_id=args.run_id,
        workflow=args.workflow,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main())
