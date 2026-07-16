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
import io
import json
import logging
import os
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from hpc_agent._wire.workflows.block_drive import BlockDriveResult
from hpc_agent.infra import block_chain
from hpc_agent.infra.time import parse_iso_utc_or_none
from hpc_agent.ops import field_ownership
from hpc_agent.state.journal import (
    clear_pending_decision,
    mark_pending_decision,
    read_pending_decision,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from hpc_agent.ops.overnight import ConsumptionOutcome

__all__ = [
    "plan_block_action",
    "run_tick",
    "block_drive_once",
    "greenlight_targets_boundary",
    "committed_greenlight_for_boundary",
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
# The subset whose spec accepts an ABSENT run_id: only ``status-snapshot``
# (``{}`` is its fleet digest). ``aggregate-check`` REQUIRES ``run_id``
# (``AggregateCheckSpec.run_id`` is a required ``RunIdStrict``), so a bare tick
# without one gets the clear skip, never a doomed ``SpecInvalid`` span.
_FRESH_ENTRY_OPTIONAL_RUN_ID_BLOCKS: frozenset[str] = frozenset({"status-snapshot"})


# ── in-process span eligibility (WS-INPROC) ─────────────────────────────────────
#
# The DECLARED, ENUMERATED set of block verbs a driver span may dispatch
# IN-PROCESS (no ``python -m hpc_agent`` subprocess) — reusing the warm registry
# instead of re-paying interpreter cold-start + the registry walk on every span.
#
# The carve-out is ENCODED, not blanket: a verb qualifies ONLY when it is a
# LOCAL, decision/state-only block that never shells ssh and never blocks on a
# watch/wait. Membership is pinned by
# ``tests/contracts/test_src_subprocess_timeout_discipline.py`` — a member that
# declares an ``ssh`` / scheduler side-effect, sets ``requires_ssh``, or joins
# ``block_chain.WATCH_VERBS`` turns that contract RED (the planted-violation
# guard). So the KEEP-subprocess seam holds for:
#   * ssh-shelling children — ``submit-s1/s2/s4``, ``aggregate-check``/``-run``,
#     ``status-snapshot`` (all declare a conditional ssh side-effect + require_ssh),
#     and check-preflight / resolve-resources (preflight sub-calls);
#   * WATCH_VERBS — ``submit-s3`` / ``status-watch`` / ``campaign-watch``;
#   * 2.6's per-host census waiter (a WATCH_VERB subprocess, named so it cannot be
#     inlined here).
# Only ``campaign-greenlight`` (writes-campaign-state, local journal) and
# ``campaign-complete`` (side_effects=[]) clear the bar today: both are cluster-free
# state/decision blocks.
_IN_PROCESS_ELIGIBLE_VERBS: frozenset[str] = frozenset({"campaign-greenlight", "campaign-complete"})


@contextlib.contextmanager
def _shield_stdin_for_span() -> Iterator[None]:
    """Swap the REAL ``sys.stdin`` out for an empty buffer during an in-process span.

    The block-drive tick itself may be running IN-PROCESS inside the MCP server
    (``mcp_server._in_process_cli_runner``), whose reader thread is blocked in
    ``readline()`` on the real ``sys.stdin`` (the JSON-RPC transport). A verb that
    reads stdin in-process — or any code that reconfigures it — must never touch
    that stream: a reconfigure-under-read returns a false EOF on Windows and kills
    the reader thread (regression 17243a17). Swap in an empty ``StringIO`` for the
    span's duration so an in-process verb sees EOF instead of eating the transport's
    bytes; restore on exit. The same shielding the MCP in-proc runner seam applies,
    reproduced locally (the runner is package-private to ``_kernel/extension`` and
    cannot be imported across the package boundary).
    """
    import sys

    prev = sys.stdin
    sys.stdin = io.StringIO()
    try:
        yield
    finally:
        sys.stdin = prev


def _in_process_eligible(verb: str) -> bool:
    """Whether *verb* may run as an in-process span (enumerated set + fast-path gate).

    Members of :data:`_IN_PROCESS_ELIGIBLE_VERBS` run in-process UNLESS the single
    -verb dispatch surface is unavailable for them — the same conditions the CLI
    fast path defers on: the ``HPC_AGENT_NO_FAST_CLI`` kill switch, or an installed
    plugin that can reshape this core verb's CLI (then only the full parser walk,
    reached via the subprocess, honours the reshaping). Reuses the SAME
    plugin-reshaping verdict the CLI fast path gates on so the two never diverge;
    any hiccup falls back to the subprocess (byte-identical, just slower).
    """
    if verb not in _IN_PROCESS_ELIGIBLE_VERBS:
        return False
    if os.environ.get("HPC_AGENT_NO_FAST_CLI") == "1":
        return False
    if os.environ.get("HPC_AGENT_DISABLE_PLUGINS") == "1":
        return True
    try:
        from hpc_agent.cli._fast_path_cache import cached_cli_reshaping_verdict

        conservative, reshaped = cached_cli_reshaping_verdict()
        if conservative:
            return False
        return verb not in reshaped
    except Exception:  # noqa: BLE001 — a metadata hiccup falls back to the subprocess
        return False


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
    * ``"advance"`` (journal-derived, run #9) — no pending decision, but the
      journal's latest committed greenlight names a ``next_block`` verb → resume
      there. An interview-driven chain starts its first blocks by DIRECT
      invocation (``submit-s1`` is not bare-startable), so no marker was ever
      parked — but the committed ``resolved`` IS a durable cursor. Without this,
      every later tick reports nothing drivable and the agent hand-sequences the
      whole chain (proving run #9: "no parked cursor → invoking submit-s2
      directly"). Re-entering an already-finished detached block is idempotent
      (the recorded-terminal replay), and the next park writes the marker, so one
      tick re-onboards a hand-started chain onto the driver.
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
    # ── FRESH start: no pending decision. ──────────────────────────────────────
    if not pending_decision:
        # Journal-derived resume first (run #9): the latest committed greenlight's
        # ``next_block`` is a durable cursor even when no marker was parked (the
        # interview-driven direct-invocation start). Scoped to the journal verb's
        # own workflow family — an explicit mismatching ``workflow`` request
        # (e.g. a status tick against a run mid-submit) still fresh-starts.
        next_block = (committed_resolved or {}).get("next_block")
        if (
            isinstance(next_block, str)
            and next_block in block_chain.WORKFLOW_OF
            and (workflow is None or workflow == block_chain.WORKFLOW_OF[next_block])
        ):
            return {
                "action": "advance",
                "verb": next_block,
                "workflow": block_chain.WORKFLOW_OF[next_block],
                "current_verb": None,
                "next_verb": next_block,
                "carry_fields": {},
                "changed_fields": [],
                "reason": (
                    f"no pending marker, but the journal's latest committed greenlight "
                    f"names {next_block} — resuming the chain from the journal"
                ),
            }
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


def _block_verb_argv(verb: str, spec_path: str, experiment_dir: Path) -> list[str]:
    """The argv one block span runs (a seam so tests can substitute a child)."""
    return ["hpc-agent", verb, "--spec", spec_path, "--experiment-dir", str(experiment_dir)]


# Exit code the driver reports for a span whose child exceeded its deadline and
# was killed — the coreutils ``timeout(1)`` convention, distinguishable from any
# CLI envelope exit code so ``_chain`` can name the deadline in its reason.
_TIMEOUT_EXIT_CODE = 124


def _run_block_verb_in_process(
    verb: str, spec: dict[str, Any], experiment_dir: Path
) -> tuple[dict[str, Any], int] | None:
    """Dispatch an in-process-eligible block verb WITHOUT a subprocess (WS-INPROC).

    Reuses the server's warm ``@primitive`` registry via the single-verb dispatch
    surface (``register_single_module`` + ``build_single_verb_parser`` →
    ``dispatch_primitive``), the SAME code path the CLI fast dispatch takes — so the
    ``(exit_code, stdout-envelope)`` contract, and therefore the block Result, is
    reproduced exactly, just without re-paying interpreter cold-start + the registry
    walk. Routes through the registry/dispatch surface, never a direct
    ``_kernel.lifecycle → ops/meta`` import.

    Deliberately does NOT go through ``cli.dispatch.main`` (regression 17243a17): the
    in-process seam is ``_shield_stdin_for_span`` + a NESTED stdout/stderr capture, so
    the span's output never leaks into the driver's own envelope and the JSON-RPC
    transport's real ``sys.stdin`` is never reconfigured under the blocked reader
    thread. The detached-worker heartbeat + ``_record_detached_failure_terminal``
    machinery ``main`` wraps is bypassed (it reads ``os.environ`` for a worker
    identity an in-process span never carries).

    Returns ``None`` — signalling the caller to fall back to the subprocess — only
    for a STRUCTURAL ineligibility (verb absent from the fast-path map, a stale map
    miss, no single-verb parser). A verb that actually RAN but FAILED returns
    ``({}, code)`` with a non-zero ``code`` exactly like the subprocess path: an
    in-process failure (a crash mid-span included) must produce the identical failed
    -span envelope so the F14 re-park fires — a swallowed exception must never convert
    a park into a silent skip.
    """
    from hpc_agent._kernel.registry.primitive import register_single_module
    from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP
    from hpc_agent.cli.parser import build_single_verb_parser

    entry = VERB_MODULE_MAP.get(verb)
    if entry is None:
        return None  # not fast-path-mapped → subprocess
    primitive_name, module_name = entry
    try:
        register_single_module(module_name)
        parser = build_single_verb_parser(primitive_name)
    except ImportError:
        return None  # stale map (module renamed/deleted) → subprocess
    if parser is None:
        return None  # grouped verb / non-safe handler → subprocess

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix=f"{verb}-spec-", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(spec, handle)
        spec_path = handle.name
    out, err = io.StringIO(), io.StringIO()
    try:
        # The single-verb parser expects argv WITHOUT the ``hpc-agent`` prog token.
        ns = parser.parse_args([verb, "--spec", spec_path, "--experiment-dir", str(experiment_dir)])
        try:
            with (
                _shield_stdin_for_span(),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                # ``ns.func`` routes to ``dispatch_primitive`` (the parser binds it),
                # which already maps a raised ``HpcError`` to an ``ok:false`` envelope
                # + its exit code — the same translation the subprocess CLI applies.
                code = ns.func(ns)
        except SystemExit as exc:  # argparse / an explicit sys.exit inside a verb
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        except Exception:  # noqa: BLE001 — F14: a crashed span is a FAILED span, never a silent skip
            _log.warning(
                "in-process block verb %s crashed mid-span — treating as a failed span", verb
            )
            return {}, 1
    finally:
        with contextlib.suppress(OSError):
            os.unlink(spec_path)

    if code != 0:
        _log.warning("in-process block verb %s failed (exit %s)", verb, code)
        return {}, int(code)
    try:
        envelope = json.loads(out.getvalue())
    except (json.JSONDecodeError, ValueError):
        _log.warning("in-process block verb %s produced an unparseable envelope", verb)
        return {}, 1
    data = envelope.get("data")
    return (data if isinstance(data, dict) else {}), 0


def _run_block_verb(
    verb: str, spec: dict[str, Any], experiment_dir: Path
) -> tuple[dict[str, Any], int]:
    """Run one ``hpc-agent <verb>`` block and return its ``(result, code)``.

    An in-process-eligible verb (:func:`_in_process_eligible` — a local,
    decision/state-only block: ``campaign-greenlight`` / ``campaign-complete``) is
    dispatched IN-PROCESS via :func:`_run_block_verb_in_process`, reusing the warm
    registry. Every OTHER verb — ssh-shelling children, ``block_chain.WATCH_VERBS``,
    2.6's per-host census waiter — CAPTURES stdout from a ``python -m hpc_agent``
    SUBPROCESS so the driver can read the block's Result: the ``{block,
    stage_reached, needs_decision, next_block, …}`` ``data`` block of the JSON
    envelope. Both paths return an empty dict on a non-zero exit or an unparseable
    envelope (the caller treats that as a failed span → the F14 re-park).

    This is where the WS-INPROC path DIVERGES from its twin ``drive._run_cli_step``:
    that loop drives ssh-reaching flow verbs (``monitor-flow`` / ``aggregate-flow``)
    and stays wholly on the subprocess seam.

    The subprocess wait is BOUNDED by the per-verb deadline from the block registry
    (:func:`block_chain.verb_deadline_seconds` — watch-class blocks get their
    spec's own wall-clock budget + slack; everything else a class ceiling). The
    capture routes through ``infra.remote.capture_via_select``, the S2-wedge-fix
    seam whose deadline can actually fire on Windows (kill on expiry, then a
    bounded post-kill drain — see ``_capture_windows``). On expiry the child is
    killed and the span reports ``({}, _TIMEOUT_EXIT_CODE)``.
    """
    if _in_process_eligible(verb):
        in_proc = _run_block_verb_in_process(verb, spec, experiment_dir)
        if in_proc is not None:
            return in_proc
        # Structural fall-through (stale map / no single-verb parser): subprocess.

    from hpc_agent.infra.remote import capture_via_select

    deadline = block_chain.verb_deadline_seconds(verb, spec)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix=f"{verb}-spec-", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(spec, handle)
        spec_path = handle.name
    try:
        proc = capture_via_select(
            _block_verb_argv(verb, spec_path, experiment_dir), timeout=deadline
        )
    except subprocess.TimeoutExpired:
        _log.warning(
            "block verb %s exceeded its %.0fs driver deadline — child killed", verb, deadline
        )
        return {}, _TIMEOUT_EXIT_CODE
    except OSError as exc:
        # F14: a spawn failure (fork exhaustion, ENOMEM, EMFILE — the documented
        # fork-exhaustion night) raised UNCAUGHT here bypassed ``on_first_failure``,
        # so a cleared resume marker was never re-parked and the human's edit was
        # silently downgraded to a journal-derived advance. Return an empty result +
        # non-zero code exactly like a failed span so the re-park guard FIRES.
        _log.warning("block verb %s could not spawn its capture subprocess (%s)", verb, exc)
        return {}, 1
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
    the driver materializes ``{"run_id": run_id}``. With NO run_id, only
    ``status-snapshot`` is still buildable (``{}`` — a fleet digest);
    ``aggregate-check``'s spec REQUIRES ``run_id``, so it is as unbuildable as
    ``submit-s1`` / ``campaign-greenlight`` (inputs a bare tick can't supply).
    In every unbuildable case the driver returns ``None`` and :func:`run_tick`
    reports a clear ``skip`` naming the missing inputs rather than running a
    span the block would reject with ``SpecInvalid``.
    """
    if verb in _FRESH_ENTRY_RUN_ID_BLOCKS:
        if run_id:
            return {"run_id": run_id}
        if verb in _FRESH_ENTRY_OPTIONAL_RUN_ID_BLOCKS:
            return {}
    return None


def _spec_model_field_names(verb: str) -> frozenset[str] | None:
    """The declared field names of *verb*'s input spec model, or ``None``.

    Resolves the block's spec model through the primitive registry — the same
    ``CliShape.spec_model`` the CLI validates ``--spec`` against — via the
    single-verb fast-path map, so the acting-spec filter can never drift from
    what the block actually accepts. Returns ``None`` when the verb is not in
    the map or carries no spec model; the caller then falls back to
    metadata-only stripping.
    """
    from hpc_agent._kernel.registry.primitive import get_meta, register_single_module
    from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP

    entry = VERB_MODULE_MAP.get(verb)
    if entry is None:
        return None
    primitive_name, module_name = entry
    try:
        register_single_module(module_name)
        model = getattr(get_meta(primitive_name).cli, "spec_model", None)
    except Exception:  # noqa: BLE001 — a stale map entry must not crash the tick
        return None
    fields = getattr(model, "model_fields", None)
    if not isinstance(fields, dict):
        return None
    return frozenset(fields)


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
    if run_id:
        scope_wf = (pending.get("workflow") if pending else None) or workflow
        scope_kind = "campaign" if scope_wf == "campaign" else "run"
        if pending:
            # RESUME path — BOUNDARY-SCOPED (bug-sweep #1 / run-12 finding 21).
            # The approval of a parked boundary is ONLY the greenlight that targets
            # THIS boundary. A prior boundary's already-consumed ``y`` (its
            # ``resolved`` names an earlier verb) or a same-boundary re-park's stale
            # ``y`` (older than the new ``awaiting_since``) must NOT be replayed as
            # this decision's approval: doing so either re-runs the block every tick
            # or force-advances into the gated successor whose gate then refuses —
            # a spurious "block failed" masking the true "awaiting the human" state.
            # When nothing targets the boundary the reader returns ``None`` →
            # :func:`plan_block_action` returns ``awaiting_decision`` (exit 0).
            cursor = pending.get("resume_cursor") or {}
            next_verb = cursor.get("next_verb") if isinstance(cursor, dict) else None
            current_verb = cursor.get("current_verb") if isinstance(cursor, dict) else None
            committed_resolved = _boundary_scoped_committed_resolved(
                experiment_dir,
                scope_kind,
                run_id,
                block=current_verb if isinstance(current_verb, str) else None,
                next_verb=next_verb,
                awaiting_since=pending.get("awaiting_since"),
            )
        else:
            # NO-MARKER journal-derived resume (run #9): an interview-driven chain
            # starts by direct invocation (nothing ever parked), so the latest
            # committed greenlight's own ``next_block`` is the only durable cursor.
            # There is no parked boundary to scope to → the UNSCOPED latest-``y``
            # scan is correct here (that path must not regress).
            committed_resolved = _latest_committed_resolved(experiment_dir, scope_kind, run_id)
    if pending:
        cursor = pending.get("resume_cursor", {})
        last_run_inputs = cursor.get("input_spec") if isinstance(cursor, dict) else None

    plan = plan_block_action(
        workflow=workflow,
        pending_decision=pending,
        committed_resolved=committed_resolved,
        last_run_inputs=last_run_inputs,
    )
    action = plan["action"]

    # F12: a standing consent recorded AFTER the driver parked at a gated boundary must
    # still auto-advance it. The awaiting_decision resume path never consulted the consent
    # (only _chain's FRESH gated-successor site did), so a consent typed after the park was
    # stranded — the night lost and the morning brief silent on why. Consult it HERE on the
    # resume path, but only when the human is NOT mid-redraft: a same-boundary nudge
    # journaled after the park (F13) means the human is engaging THIS boundary, so the
    # standing consent must not steamroll the un-redrafted spec.
    if action == "awaiting_decision" and not dry_run and pending and run_id:
        f12 = _consume_parked_boundary_under_consent(experiment_dir, run_id, pending, workflow)
        if f12 is not None:
            return f12

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
    # If the FIRST resumed span then FAILS, the approval was NOT consumed: the
    # marker is re-parked verbatim (see :func:`_chain`'s ``on_first_failure``)
    # so the next tick retries the SAME route instead of replaying the resume
    # as a bare journal-derived advance (dropping a nudge's rerun + the §4
    # ``input_spec`` diff base).
    resume_action = action in ("advance", "rerun", "advance_carrying")
    on_first_failure: Callable[[], None] | None = None
    if pending and resume_action and run_id:
        clear_pending_decision(run_id, experiment_dir=experiment_dir)
        marker = dict(pending)
        rid = run_id

        def _repark() -> None:
            _repark_marker(experiment_dir, rid, marker)

        on_first_failure = _repark

    verb: str | None = plan["verb"]
    wf = plan.get("workflow") or workflow

    # Materialize the FIRST span's spec (§3, correct spec-materialization — no
    # blind top-level ``run_id`` injection).
    #
    # * RESUME (advance / rerun / advance_carrying): the approved ``resolved`` spec
    #   the LLM committed IS the correctly-shaped acting spec for the routed block
    #   (the gated block's nested-object spec, or the current block's for a rerun).
    #   The downstream edits an ``advance_carrying`` folds are already inside
    #   ``resolved``. Stripped as non-inputs: the ``next_block`` routing token,
    #   plus any journal-sanctioned identity echo (``cmd_sha`` / ``run_id`` /
    #   ``total_tasks`` — ops/decision/journal.py) the TARGET block's
    #   ``extra="forbid"`` spec model does not declare. The filter is the
    #   model's own declared fields, so a block whose spec genuinely takes
    #   ``run_id`` (aggregate-check) keeps it.
    # * FRESH: the entry block's minimal spec, or ``None`` when the block is not
    #   bare-startable (submit-s1 / campaign-greenlight, or aggregate-check
    #   without a run_id) → a clear skip.
    if resume_action:
        accepted = _spec_model_field_names(verb) if verb else None
        first_spec: dict[str, Any] = {
            k: v
            for k, v in (committed_resolved or {}).items()
            if k not in _META_KEYS and (accepted is None or k in accepted)
        }
    else:
        built = _fresh_entry_spec(verb, run_id)
        if built is None:
            if verb in _FRESH_ENTRY_RUN_ID_BLOCKS:
                reason = (
                    f"cannot fresh-start {verb} without a run_id — its spec requires "
                    "one. Re-run with --run-id <id>."
                )
            else:
                reason = (
                    f"cannot fresh-start {verb} from a bare block-drive tick — it "
                    "needs inputs beyond (run_id, workflow) "
                    "(submit-s1: goal/task_generator/walk; campaign-greenlight: "
                    "campaign_id). Drive its fresh start via the interview skill / "
                    "campaign reconcile driver."
                )
            return (
                BlockDriveResult(
                    action="skip",
                    run_id=run_id,
                    workflow=wf,
                    next_verb=verb,
                    reason=reason,
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
        on_first_failure=on_first_failure,
    )


def _chain(
    experiment_dir: Path,
    *,
    run_id: str,
    workflow: str | None,
    first_verb: str | None,
    first_spec: dict[str, Any],
    first_label: str,
    on_first_failure: Callable[[], None] | None = None,
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

    ``on_first_failure`` (a niladic callable) fires ONLY when the FIRST span
    fails: :func:`run_tick` uses it to re-park a pending marker it cleared for
    a resume that never consumed its approval. Later chained spans do not fire
    it — by then the first span succeeded, so the approval WAS consumed.
    """
    from hpc_agent._kernel.lifecycle.drive import _stamp_driver_tick

    verb = first_verb
    spec: dict[str, Any] = dict(first_spec)
    last_action = first_label
    last_result: dict[str, Any] = {}
    first_span = True

    while verb is not None:
        result, code = _run_block_verb(verb, spec, experiment_dir)
        if run_id:
            _stamp_driver_tick(experiment_dir, run_id)
        if not result:
            if first_span and on_first_failure is not None:
                on_first_failure()
            if code == _TIMEOUT_EXIT_CODE:
                deadline = block_chain.verb_deadline_seconds(verb, spec)
                reason = (
                    f"block {verb} exceeded its {deadline:.0f}s driver deadline "
                    f"and was killed (exit {code})"
                )
            else:
                reason = f"block {verb} failed or returned no result (exit {code})"
            return (
                BlockDriveResult(
                    action="skip",
                    run_id=run_id or None,
                    workflow=workflow,
                    current_verb=verb,
                    reason=reason,
                ),
                code or 1,
            )

        first_span = False
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
        #
        # OVERNIGHT AUTO-ADVANCE (item 8 seam 1): before parking, consult the run's
        # standing consent. A LIVE consent covering this named boundary consumes the
        # greenlight — the auto-advance is recorded to the consumption ledger in the
        # same breath (:func:`_consume_overnight`), and the driver chains into the
        # gated block (whose own consent-aware gate re-verifies the same consent). A
        # not-live / not-named boundary parks exactly as today, carrying the refusal
        # reason so the park brief says WHY the overnight consent did not carry.
        if block_chain.is_gated(successor):
            overnight = _consume_overnight(experiment_dir, run_id, successor)
            if overnight is not None and overnight.consumed:
                _log.info(
                    "overnight consent for %s consumed the %s greenlight — auto-advancing",
                    run_id,
                    successor,
                )
                last_action = "chained"
                spec = _next_spec_hint(result)
                verb = successor
                continue
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
            park_reason = result.get("reason") or (
                f"{verb} complete; greenlight required before {successor} — parked."
            )
            if overnight is not None and not overnight.consumed:
                park_reason = (
                    f"{park_reason} (no live standing consent to auto-advance overnight: "
                    f"{overnight.decision.reason})"
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
                    reason=park_reason,
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


def _consume_overnight(
    experiment_dir: Path, run_id: str, successor: str
) -> ConsumptionOutcome | None:
    """Consult the run's standing consent at a gated boundary (item 8 seam 1).

    Returns ``None`` when there is no run to key on (a bare tick) — the caller then
    parks as usual. Otherwise returns the substrate's :class:`ConsumptionOutcome`:
    ``consumed=True`` means a LIVE consent covered this named boundary and the
    auto-advance was ledgered in the same breath, so the driver may chain into
    ``successor``; ``consumed=False`` carries the refusal leg for the park brief.

    ``current_cmd_sha`` is the run's sidecar tree fingerprint — the identity a spec
    change moves — read here so the consent's spec-identity binding is checked against
    the SAME token the S3 gate uses. A fail-safe read: any sidecar surprise yields an
    empty identity, which the substrate treats as a spec mismatch (not live) → parks.
    """
    if not run_id:
        return None
    from hpc_agent.ops.overnight import consume_boundary_under_consent
    from hpc_agent.state.runs import read_run_sidecar

    current_cmd_sha = ""
    spent_walltime: float | None = None
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
        current_cmd_sha = str((sidecar or {}).get("cmd_sha") or "")
        spent_walltime = _run_requested_walltime(sidecar)
    except Exception:  # noqa: BLE001 — a bad sidecar must not crash the tick; park instead
        current_cmd_sha = ""
    return consume_boundary_under_consent(
        experiment_dir,
        scope_kind="run",
        scope_id=run_id,
        boundary_block=successor,
        current_cmd_sha=current_cmd_sha,
        # F16: meter the walltime cap against the fallout THIS boundary authorizes —
        # the main array's requested wall-seconds (walltime_sec × task_count) — passed
        # explicitly so a consent whose walltime_cap is below the launch's cost REFUSES
        # the auto-advance (the mandatory cap the ledger-fed meter, which nothing writes,
        # could never fire). ``None`` when unavailable ⇒ the ledger auto-meter (0 today).
        spent_walltime=spent_walltime,
    )


def _run_requested_walltime(sidecar: dict[str, Any] | None) -> float | None:
    """The run's requested total wall-seconds (``walltime_sec`` × ``task_count``), or ``None``.

    The cost the ``submit-s3`` main-array launch would burn — the fallout a standing
    consent's ``walltime_cap`` exists to bound (F16). Reads the sidecar's
    ``resources.walltime_sec`` (an int) and ``task_count``; ``None`` when either is
    absent/non-numeric so the caller falls back to the ledger auto-meter rather than
    fabricating a cost.
    """
    if not isinstance(sidecar, dict):
        return None
    resources = sidecar.get("resources")
    walltime = resources.get("walltime_sec") if isinstance(resources, dict) else None
    if not (isinstance(walltime, (int, float)) and not isinstance(walltime, bool) and walltime > 0):
        return None
    try:
        tasks = int(sidecar.get("task_count") or 0)
    except (TypeError, ValueError):
        tasks = 0
    return float(walltime) * max(tasks, 1)


def _consume_parked_boundary_under_consent(
    experiment_dir: Path,
    run_id: str,
    pending: dict[str, Any],
    workflow: str | None,
) -> tuple[BlockDriveResult, int] | None:
    """Auto-advance a PARKED gated boundary under a standing consent, or ``None`` (F12).

    The resume-path counterpart of ``_chain``'s fresh gated-successor consult: when the
    tick is awaiting a human at a gated boundary and a live standing consent covers it, the
    consent (typically recorded AFTER the driver parked — a natural "I see it's awaiting a
    decision; run it overnight" flow) auto-advances it instead of losing the night. Returns
    the advanced ``_chain`` result, or ``None`` to fall through to the normal awaiting stop
    when: the marker's successor is not gated; the human is mid-redraft (a same-boundary
    nudge journaled at/after the park — the consent must not steamroll it); there is no run
    to key on; or no live consent covers the boundary.

    Transactional (F14 posture): the marker is cleared BEFORE the resumed span and re-parked
    verbatim if the first span fails, so a crash/OSError leg does not lose the parked state.
    """
    cursor = pending.get("resume_cursor") or {}
    if not isinstance(cursor, dict):
        return None
    gated_next = cursor.get("next_verb")
    parked_block = cursor.get("current_verb")
    if not (isinstance(gated_next, str) and gated_next and block_chain.is_gated(gated_next)):
        return None
    scope_kind = "campaign" if (pending.get("workflow") or workflow) == "campaign" else "run"
    if _boundary_has_post_park_nudge(
        experiment_dir,
        scope_kind,
        run_id,
        block=parked_block if isinstance(parked_block, str) else None,
        awaiting_since=pending.get("awaiting_since"),
    ):
        return None  # the human is redrafting this boundary — do not steamroll (F13)
    overnight = _consume_overnight(experiment_dir, run_id, gated_next)
    if overnight is None or not overnight.consumed:
        return None
    _log.info(
        "overnight consent for %s consumed the parked %s greenlight on the resume path "
        "— auto-advancing (F12)",
        run_id,
        gated_next,
    )
    clear_pending_decision(run_id, experiment_dir=experiment_dir)
    marker = dict(pending)

    def _repark() -> None:
        _repark_marker(experiment_dir, run_id, marker)

    hint = cursor.get("next_spec_hint")
    return _chain(
        experiment_dir,
        run_id=run_id,
        workflow=(pending.get("workflow") or workflow),
        first_verb=gated_next,
        first_spec=dict(hint) if isinstance(hint, dict) else {},
        first_label="advanced",
        on_first_failure=_repark,
    )


def _boundary_has_post_park_nudge(
    experiment_dir: Path,
    scope_kind: str,
    scope_id: str,
    *,
    block: str | None,
    awaiting_since: str | None,
) -> bool:
    """True when a same-boundary nudge was journaled at/after the park (F12/F13).

    The "the human is mid-redraft" signal the F12 resume-path consent consult must respect:
    a non-greenlight decision on the parked block, journaled ``ts >= awaiting_since``
    (:func:`_same_boundary_nudge`). Fail-safe: an unreadable scope reads False (no nudge
    seen), so a journal surprise never blocks a legitimate auto-advance.
    """
    from hpc_agent.state.decision_journal import read_decisions

    if not scope_id:
        return False
    try:
        records = read_decisions(experiment_dir, scope_kind, scope_id)
    except Exception:  # noqa: BLE001 — a bad scope must not crash the tick
        return False
    return any(
        _same_boundary_nudge(rec, block=block, awaiting_since=awaiting_since) for rec in records
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
    # A park is a DISCLOSURE, not a mutation entitled to assume journal state.
    # The journal RunRecord is minted by ``submit_and_record`` INSIDE the gated
    # submit-s2 (the qsub) — S1's resolve leg writes only the per-run sidecar.
    # So the FIRST park (the S1→S2 greenlight gate) is reached before any record
    # exists, and ``mark_pending_decision`` → ``update_run_status`` raises
    # FileNotFoundError for a sidecar-only run. That crashed the driver tick at
    # the rendezvous for BOTH of run #11's runs, pushing the agent off-pipeline
    # to per-block CLI (notebook-audit.md Addendum 13.0). The BlockDriveResult
    # the caller returns already carries the brief to the human, so the human
    # still sees the decision; only the DURABLE journal marker (the §5 "parked ≠
    # stalled" flag + resume_cursor) is skipped here — and the §5 watchdog keys
    # off journal records anyway, so a record-less run is unwatched regardless.
    # Warn + continue, mirroring ``_repark_marker``'s OSError guard and
    # ``_stamp_driver_tick``'s "warn, don't vanish" philosophy (drive.py).
    try:
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
    except FileNotFoundError:
        _log.warning(
            "no run record for %s at park (%s → %s) — the sidecar-only run has "
            "no journal record yet (minted at submit-s2/qsub); the decision brief "
            "is still disclosed to the human but the durable pending-decision "
            "marker is skipped until the record is minted",
            run_id,
            verb,
            successor,
        )


def _repark_marker(experiment_dir: Path, run_id: str, marker: dict[str, Any]) -> None:
    """Re-write a cleared pending marker verbatim after a FAILED resume span.

    :func:`run_tick` clears the marker BEFORE the resumed span runs (so a
    re-entry cannot double-consume the approval); when that first span fails
    the approval was NOT consumed, so the marker — the resume cursor plus the
    §4 ``input_spec`` diff base — must survive: the next tick then retries the
    SAME route (a nudge's ``rerun`` stays a rerun) instead of degrading to a
    journal-derived ``advance``. A missing run record is logged, not raised —
    the tick is already on its failure path and must still report it.
    """
    brief = marker.get("brief")
    cursor = marker.get("resume_cursor")
    try:
        mark_pending_decision(
            run_id,
            block=marker.get("block") or "",
            workflow=marker.get("workflow") or "",
            brief=brief if isinstance(brief, dict) else {},
            resume_cursor=cursor if isinstance(cursor, dict) else {},
            awaiting_since=marker.get("awaiting_since") or "",
            cmd_sha=marker.get("cmd_sha"),
            experiment_dir=experiment_dir,
        )
    except OSError:
        _log.warning("failed to re-park the pending marker for %s after a failed span", run_id)


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


def greenlight_targets_boundary(
    record: dict[str, Any],
    *,
    next_verb: str | None,
    awaiting_since: str | None,
) -> bool:
    """True iff a committed decision *record* is the greenlight for THIS parked boundary.

    The SINGLE predicate the driver (:func:`run_tick` via
    :func:`_boundary_scoped_committed_resolved`) and the ``block-drive`` Stop guard
    (:func:`hpc_agent._kernel.hooks.decision_rendezvous_stop_guard.find_committed_unadvanced`)
    both key on, so a CONSUMED greenlight can never masquerade as a fresh one and
    the two surfaces cannot drift. A committed ``y`` counts as the approval of the
    boundary a marker is parked at only when it:

    * is a greenlight (``response == "y"`` — never a nudge), AND
    * NAMES the parked successor — ``resolved["next_block"]`` (block_gate
      ``_journaled_target`` semantics: the verb string or the ``{"verb": ...}``
      hint) equals the marker's ``resume_cursor["next_verb"]``. This rejects a
      PREVIOUS boundary's already-consumed greenlight, whose target names an
      earlier verb: nothing is appended when a ``y`` is consumed, so a shared run
      journal's latest ``y`` may belong to a prior boundary (the exact pitfall
      ``ops/block_gate.py`` documents), AND
    * was journaled AT OR AFTER the marker was (re-)parked
      (``ts >= awaiting_since``). This rejects a SAME-boundary re-park's stale
      ``y`` — a greenlight that DID name this boundary but was already consumed by
      a tick that ran the block and re-parked, stamping a newer ``awaiting_since``
      (run-12 finding 21). The 2026-06-10 stall class stays closed: a genuinely
      unconsumed ``y`` is always newer than the marker it answers, so it passes.

    The timestamp leg only REFUSES when both stamps parse and ``ts`` is strictly
    older than ``awaiting_since``; a missing/unparseable stamp falls back to the
    boundary-name test alone (fail toward the pre-fix behavior rather than
    over-refusing a live greenlight).
    """
    from hpc_agent.ops.block_gate import _journaled_target

    if str(record.get("response") or "") != "y":
        return False
    if _journaled_target(record.get("resolved")) != next_verb:
        return False
    parked_at = parse_iso_utc_or_none(awaiting_since)
    ts = record.get("ts")
    recorded_at = parse_iso_utc_or_none(ts if isinstance(ts, str) else None)
    consumed_before_park = (
        parked_at is not None and recorded_at is not None and recorded_at < parked_at
    )
    return not consumed_before_park


def _same_boundary_nudge(
    record: dict[str, Any], *, block: str | None, awaiting_since: str | None
) -> bool:
    """True when *record* is a non-greenlight decision on THIS parked boundary (F13).

    A nudge (``response != "y"``) whose ``block`` names the parked block and whose ``ts``
    is AT OR AFTER the marker's ``awaiting_since`` is the human still redrafting THIS
    boundary — journaled after the park, it SUPERSEDES any earlier greenlight for the
    same boundary (the driver must not launch the retracted spec; the Stop guard must not
    fire on it). An UNRELATED later record — a different block's touchpoint, an
    overnight-consent, a sign-off — has a different ``block`` and is skipped, so it neither
    silences the guard nor blocks the driver (the "y then an unrelated later record" half of
    the disagreement).

    Keys on the record's ``block`` field (a nudge carries no ``next_block`` target) plus the
    ``ts >= awaiting_since`` anchor. A missing/unparseable stamp is NOT treated as
    superseding (fail toward the pre-fix behaviour rather than over-refusing a live
    greenlight), mirroring :func:`greenlight_targets_boundary`'s timestamp leg.
    """
    if str(record.get("response") or "") == "y":
        return False
    if not block or str(record.get("block") or "") != block:
        return False
    parked_at = parse_iso_utc_or_none(awaiting_since)
    ts = record.get("ts")
    recorded_at = parse_iso_utc_or_none(ts if isinstance(ts, str) else None)
    if parked_at is None or recorded_at is None:
        return False
    return recorded_at >= parked_at


def committed_greenlight_for_boundary(
    records: list[dict[str, Any]],
    *,
    block: str | None,
    next_verb: str | None,
    awaiting_since: str | None,
) -> dict[str, Any] | None:
    """The ``resolved`` of the greenlight answering THIS parked boundary, or ``None`` (F13).

    THE single scan the driver (:func:`_boundary_scoped_committed_resolved`) and the
    ``block-drive`` Stop guard
    (:func:`hpc_agent._kernel.hooks.decision_rendezvous_stop_guard.find_committed_unadvanced`)
    both share, so the two surfaces apply ONE rule to the SAME record set and cannot drift
    (the docstring's "cannot drift" claim was false — they applied the shared predicate to
    DIFFERENT record sets: the driver scanned newest-first skipping a trailing nudge, the
    guard tested only ``records[-1]``). Scans *records* newest-first and stops at the FIRST
    record that CONCERNS this boundary:

    * a greenlight that TARGETS it (:func:`greenlight_targets_boundary`) → return its
      ``resolved`` (the driver advances / the guard forces continue);
    * a SAME-BOUNDARY nudge journaled at/after the park (:func:`_same_boundary_nudge`) → the
      human retracted / is mid-redraft → return ``None`` (the driver stays awaiting; the
      guard stays silent). This closes BOTH directions of the disagreement: the driver
      consuming a retracted ``y`` behind a trailing nudge, and the guard stalling a genuine
      ``y`` behind an unrelated later record.

    Unrelated records (a different block, a later overnight-consent / sign-off) are skipped.
    ``None`` when nothing concerns the boundary yet — still awaiting the human.

    OVERRIDE-BOUNDARY MAP (run-13 ``causal_tune_linear-de448128`` wedge): a block that
    parks a *decision* with no code-determined auto-successor records ``next_verb=None``
    in its marker (``aggregate-check``'s ``not_ready`` / ``integrity_review`` parks —
    ``SUCCESSORS`` is ``None`` there — hit while the run is non-terminal). But the human's
    ``y`` at such a boundary is an OVERRIDE that greenlights the block's chain-forward
    successor (``aggregate-run``), so the greenlight's ``resolved["next_block"]`` names a
    verb the ``None`` marker target could never equal → ``greenlight_targets_boundary``
    rejected every greenlight → a PERMANENT "awaiting" wedge (the driver kept reporting
    "pending decision not yet committed" after the ``y`` was journaled). Map a ``None``
    marker target through :func:`block_chain.chain_successor` HERE — the ONE seam the driver
    and the Stop guard share — so both agree on the single greenlight target for the
    boundary. A genuinely terminal park (no chain successor) keeps ``None`` and its
    pre-existing behavior. ``aggregate-run``'s own greenlight gate still backstops a
    premature advance (it raises for a non-terminal run).
    """
    # BOTH vocabularies are accepted at a None-marker boundary: the raw None
    # target (a greenlight with EMPTY resolved — the shape record 8 used and
    # the attention queue pins) AND the chain-forward successor (the override
    # shape records 4-7 used, which the raw predicate could never match — the
    # run-13 wedge). Mapping ONLY to the successor would just invert the wedge.
    boundary_targets: list[str | None] = [next_verb]
    if next_verb is None and block:
        mapped = block_chain.chain_successor(block)
        if mapped is not None:
            boundary_targets.append(mapped)
    for record in reversed(records):
        if any(
            greenlight_targets_boundary(record, next_verb=t, awaiting_since=awaiting_since)
            for t in boundary_targets
        ):
            resolved = record.get("resolved")
            return dict(resolved) if isinstance(resolved, dict) else {}
        if _same_boundary_nudge(record, block=block, awaiting_since=awaiting_since):
            return None
    return None


def _boundary_scoped_committed_resolved(
    experiment_dir: Path,
    scope_kind: str,
    scope_id: str,
    *,
    block: str | None,
    next_verb: str | None,
    awaiting_since: str | None,
) -> dict[str, Any] | None:
    """The ``resolved`` of the latest greenlight that TARGETS the parked boundary.

    The boundary-scoped counterpart of :func:`_latest_committed_resolved`, used on
    the RESUME path (a pending marker exists). Reads the decision journal and delegates
    to the shared :func:`committed_greenlight_for_boundary` scan (F13 — the ONE predicate
    the driver and Stop guard both key on). ``None`` means no committed greenlight answers
    THIS boundary yet (or a same-boundary nudge superseded an earlier one): the tick is
    still awaiting the human (a valid parked stop, exit 0), never a spurious advance/rerun.
    """
    from hpc_agent.state.decision_journal import read_decisions

    if not scope_id:
        return None
    try:
        records = read_decisions(experiment_dir, scope_kind, scope_id)
    except Exception:  # noqa: BLE001 — a bad scope must not crash the tick
        return None
    return committed_greenlight_for_boundary(
        records, block=block, next_verb=next_verb, awaiting_since=awaiting_since
    )


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
