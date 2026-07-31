"""Structured terminal causes for detached workers — "failures are product surface".

Pillar 5 of ``docs/design/s2-readiness.md``: *a human reading a worker log to
learn WHY is a design defect*. On the night of 2026-07-30 two 16-second worker
corpses produced four distinct failure classes and every diagnosis was human
archaeology through ``_detached/*.log`` — because the exit path's only structured
artefact was a block terminal whose ``error_code`` is the literal constant
``"detached_worker_exit"``. Everything the system actually KNEW at that moment —
the discriminated ``PathCause``, the transport-flap identity stamped on the
exception, the breaker state, the record's own dispatch evidence — reached the
log as prose, or not at all.

This module is the seam that fixes that. It:

* **classifies** a terminal detached-worker exit into a discriminated
  :class:`TerminalCause` — ``error_code`` / ``category`` / ``retry_safe`` taken
  from the typed exception when one survived, plus the S2-hardening
  discriminants (``path_cause``, transport-flap identity, breaker state, the
  never-actuated dispatch evidence) read from the evidence that already exists;
* **keys that cause to the recoveries registry** (``recovery_kind``) so the
  remediation is COMPOSED from one definition rather than authored at the call
  site — the same string ``hpc-agent recoveries show --kind <kind>`` prints;
* **journals it** as an append-only record beside the run's decisions/briefs,
  carrying ``failed_at`` (when the worker died) and ``recorded_at`` (when the
  machine wrote the disclosure) so the attention queue and the morning brief can
  render the disclosure LATENCY honestly instead of implying the human learned
  at the instant of failure.

The ``[fatal]`` block in the worker log STAYS. Logs remain the forensic tier —
the traceback, the child stderr, the heartbeat trail. What changes is that
nothing a human NEEDS in order to decide is only in there.

Pure local I/O: no SSH, no probing. Every field is HARVESTED from evidence the
system already produced (the standing "harvest, never probe" rule pillar 1
writes down); a reading that is not already available is recorded as ``None``,
never sensed on the exit path — a dying worker must not open a connection.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = [
    "CAUSE_RECORD_KIND",
    "TERMINAL_CAUSE_SUFFIX",
    "TerminalCause",
    "classify_worker_exit",
    "iter_experiment_terminal_causes",
    "read_terminal_causes",
    "record_worker_terminal_cause",
    "terminal_cause_path",
]

_log = logging.getLogger(__name__)

#: The journal record's ``kind`` discriminator — one token so a reader can tell a
#: terminal-cause line from anything else that might ever share the file.
CAUSE_RECORD_KIND = "detached-worker-terminal"

#: Sidecar filename suffix. Lands in the per-experiment run sidecar tree beside
#: ``<run_id>.decisions.jsonl`` / ``<run_id>.overnight.jsonl`` / the block
#: terminals, so the whole per-run evidence set lives in one directory.
TERMINAL_CAUSE_SUFFIX = ".terminal-causes.jsonl"

#: How the cause was learned. ``exit-path`` is the worker disclosing on its own
#: way out (the catchable paths); the vocabulary is open so a later scanner-side
#: mint (a hard kill that flushed nothing) can name itself without redefining the
#: record.
DETECTED_BY_EXIT_PATH = "exit-path"

#: The two shapes ``ops/submit_flow.py::_stage_exhausted_error`` actually
#: composes, quoted from its own source rather than paraphrased:
#:
#: * ``"staging against {t} exhausted {n} bounded attempt(s): …"`` — the ladder
#:   used its whole budget;
#: * ``"staging against {t} STOPPED after {n} of {m} allowed attempt(s): …"`` —
#:   the ladder stopped early because the breaker's cooldown outlasted its
#:   patience (a fence, not a failure to try).
#:
#: These are matched because that error's own text is the ONLY channel the class
#: has once ``_err_from_hpc`` has collapsed the exception to an int: the
#: exhaustion envelope is a fresh ``SshUnreachable`` and carries no flap stamp,
#: and its ``path_cause`` prefix is present only when a readiness reading
#: happened to be in the window (the composer is consult-only and says "cause not
#: named" otherwise). Verified against the composer, not assumed — an earlier
#: draft of this tuple matched three strings that appear nowhere in the codebase,
#: which is a dead arm that silently classifies nothing.
_STAGE_EXHAUSTED_MARKERS = (
    "bounded attempt(s)",
    "STOPPED after",
)

#: The canary verdict token. ``ops/verify_canary.py`` sets it as a literal
#: ``failure_kind`` in the envelope, which the worker prints on its way out.
_REPORTER_UNREACHABLE_TOKEN = "reporter_unreachable"

#: ``PathCause`` arms that mean "the ProxyJump hop is the break".
_DEAD_HOP_CAUSES = frozenset(
    {"hop_down_direct_ok", "hop_down_direct_dead", "hop_down_direct_unprobed"}
)


@dataclass(frozen=True)
class TerminalCause:
    """One detached worker's terminal cause — the structure the log never had.

    ``recovery_kind`` is a :data:`hpc_agent.recovery.registry.RecoveryKind` value
    or ``None``. ``None`` is an HONEST outcome, not a gap to paper over: it means
    the evidence did not positively discriminate a known class, and the record
    says so rather than guessing a remediation that would send the reader
    somewhere wrong (the 2026-07-30 lesson — the failure was not a missing probe,
    it was a message that steered confidently at the wrong host).
    """

    run_id: str
    block: str
    #: The envelope vocabulary, taken from the typed exception when one reached
    #: the exit path; the literal ``detached_worker_exit`` otherwise (the
    #: pre-existing block-terminal constant, kept so the two agree).
    error_code: str
    #: ``user`` / ``cluster`` / ``network`` / ``internal`` — the exception's own.
    category: str
    retry_safe: bool
    exit_code: int | None
    #: When the worker died (ISO-8601 UTC). The age everything is measured from.
    failed_at: str
    #: When THIS record was written. ``recorded_at - failed_at`` is the machine's
    #: own disclosure delay; the reader's delay is measured against the surface
    #: that renders it (see ``ops/attention_queue.py``).
    recorded_at: str
    detected_by: str = DETECTED_BY_EXIT_PATH
    recovery_kind: str | None = None
    #: The discriminated ``infra/readiness_sensors.PathCause`` when the worker's
    #: own disclosure named one; ``None`` when nothing named a path cause.
    path_cause: str | None = None
    #: Transport-flap identity — the stamped verdict, not a re-derivation from
    #: the message (re-deriving is what made a known flap read as a hard failure).
    transport_flap: bool = False
    #: Effective circuit-breaker state for the run's host at record time, read
    #: from the durable breaker doc (a file read; never a dial).
    breaker_state: str | None = None
    #: Whether the run record proves its dispatch never actuated (rung 0's
    #: evidence class): ``True`` / ``False`` / ``None`` when the record is a
    #: pre-evidence one and the question is genuinely UNKNOWN.
    dispatch_never_actuated: bool | None = None
    ssh_target: str | None = None
    cluster: str | None = None
    log_path: str | None = None
    #: Whether the worker log carries a flushed ``[fatal]`` block. The forensic
    #: tier is still there; this says so rather than asserting it.
    log_disclosed: bool = False
    last_log_line: str = ""
    message: str = ""
    #: The composed remediation from the recoveries registry for
    #: :attr:`recovery_kind`, placeholders already substituted. ``None`` when no
    #: kind was discriminated.
    remediation: str | None = None

    def as_record(self) -> dict[str, Any]:
        """The append-only journal-record shape (JSON-able, stable key order)."""
        return {
            "kind": CAUSE_RECORD_KIND,
            "run_id": self.run_id,
            "block": self.block,
            "error_code": self.error_code,
            "category": self.category,
            "retry_safe": self.retry_safe,
            "exit_code": self.exit_code,
            "failed_at": self.failed_at,
            "recorded_at": self.recorded_at,
            "detected_by": self.detected_by,
            "recovery_kind": self.recovery_kind,
            "remediation": self.remediation,
            "path_cause": self.path_cause,
            "transport_flap": self.transport_flap,
            "breaker_state": self.breaker_state,
            "dispatch_never_actuated": self.dispatch_never_actuated,
            "ssh_target": self.ssh_target,
            "cluster": self.cluster,
            "log_path": self.log_path,
            "log_disclosed": self.log_disclosed,
            "last_log_line": self.last_log_line,
            "message": self.message,
        }


# ── the store ────────────────────────────────────────────────────────────────


def terminal_cause_path(experiment_dir: Path, run_id: str) -> Path:
    """The per-run terminal-cause journal path.

    ``<experiment_dir>/.hpc/runs/<run_id>.terminal-causes.jsonl`` — the run
    sidecar tree, beside the decision journal and the block terminals. Its OWN
    file so a code-authored failure record never pollutes the y/nudge journal the
    block gate and the Stop guard scan (the ``overnight.jsonl`` precedent).
    """
    from hpc_agent import errors
    from hpc_agent._kernel.contract.layout import RepoLayout

    # ``:`` is refused alongside the separators: on Windows a ``C:foo`` run_id is
    # a DRIVE-RELATIVE path that escapes the sidecar tree without containing a
    # separator at all (and an NTFS ``name:stream`` writes an alternate data
    # stream). The two existing guards in the tree do not carry it; this one is
    # written by a DYING process against a run_id that came off the environment,
    # so it takes the stricter side.
    if not run_id or set(run_id) & {"/", "\\", ":"} or run_id in (".", ".."):
        raise errors.SpecInvalid(f"run_id must be filesystem-safe; got {run_id!r}")
    return RepoLayout(experiment_dir).runs / f"{run_id}{TERMINAL_CAUSE_SUFFIX}"


def read_terminal_causes(experiment_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Every terminal-cause record for *run_id*, in append order (``[]`` if none).

    Fail-open by construction: an absent file, an unreadable one, or a torn line
    yields fewer records rather than an exception. A failure surface that can
    itself crash the read is not a failure surface.
    """
    try:
        path = terminal_cause_path(experiment_dir, run_id)
    except Exception:  # noqa: BLE001 — a bad run_id yields no records, never a raise
        return []
    return _read_jsonl(path)


def iter_experiment_terminal_causes(experiment_dir: Path) -> Iterator[dict[str, Any]]:
    """Every terminal-cause record in *experiment_dir*, run by run.

    The read side the attention queue's collector uses. Globs the run sidecar
    tree — the queue is a PROJECTION recomputed on every read, so there is no
    index to keep in step and nothing here writes.
    """
    from hpc_agent._kernel.contract.layout import RepoLayout

    try:
        runs_dir = RepoLayout(experiment_dir).runs
    except Exception:  # noqa: BLE001 — an unresolvable layout yields nothing
        return
    try:
        paths = sorted(runs_dir.glob(f"*{TERMINAL_CAUSE_SUFFIX}"))
    except OSError:
        return
    for path in paths:
        yield from _read_jsonl(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into dicts, skipping torn lines (never raising)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("terminal-cause journal unreadable %s (%s)", path, exc)
        return []
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("kind") == CAUSE_RECORD_KIND:
            out.append(obj)
    return out


# ── the classifier ───────────────────────────────────────────────────────────


def classify_worker_exit(
    experiment_dir: Path,
    *,
    run_id: str,
    block: str,
    exit_code: int | None,
    exc: BaseException | None = None,
    log_path: str | None = None,
    now_iso: str | None = None,
) -> TerminalCause:
    """Discriminate a terminal detached-worker exit into a :class:`TerminalCause`.

    Evidence, in the order it is trusted — STRUCTURE before prose:

    1. the typed exception, when one survived to the exit path (``error_code`` /
       ``category`` / ``retry_safe``, and the ``mark_transport_flap`` IDENTITY,
       which is a stamped attribute rather than a message match);
    2. the run record's own durable ``dispatch_evidence`` (rung 0's class: proof
       that no dispatch was ever issued — provable offline, so it outranks
       everything the transport might say);
    3. the worker log's bounded tail, scanned for the discriminated cause
       VOCABULARY. Two different provenances, deliberately not conflated: the
       ``PathCause`` arms and the canary's ``reporter_unreachable`` are STABLE
       TOKENS — a closed Literal and a literal ``failure_kind`` field, emitted
       verbatim by design so one word crosses the fire-time gate, the worker log
       and triage — whereas the staging ladder's exhaustion is matched on
       composed MESSAGE TEXT (:data:`_STAGE_EXHAUSTED_MARKERS`), which is a
       weaker contract quoted from that composer's source and re-pinned by test;
    4. the breaker's durable state file for the run's host — a file read.

    Never dials, never probes, never raises: a dying worker must not open a
    connection, and the disclosure path must not gain a new crash. Anything
    unreadable degrades to ``None`` and the record says so.
    """
    from hpc_agent._kernel.lifecycle.crash_disclosure import log_has_fatal_marker, read_log_tail
    from hpc_agent.infra.time import utcnow_iso

    now = now_iso or utcnow_iso()
    tail = read_log_tail(log_path)
    disclosed, last_line = log_has_fatal_marker(log_path)

    error_code, category, retry_safe = _envelope_facts(exc)
    flap = _is_transport_flap(exc)
    record = _load_run_record(experiment_dir, run_id)
    never_actuated = _dispatch_never_actuated(record)
    ssh_target = getattr(record, "ssh_target", None) if record is not None else None
    cluster = getattr(record, "cluster", None) if record is not None else None

    path_cause = _path_cause_from(exc, tail)
    if path_cause == "transport_flap":
        flap = True

    recovery_kind = _recovery_kind(
        never_actuated=never_actuated,
        record_status=str(getattr(record, "status", "") or ""),
        path_cause=path_cause,
        flap=flap,
        tail=tail,
        exc=exc,
    )

    cause = TerminalCause(
        run_id=run_id,
        block=block,
        error_code=error_code,
        category=category,
        retry_safe=retry_safe,
        exit_code=exit_code,
        failed_at=now,
        recorded_at=now,
        recovery_kind=recovery_kind,
        path_cause=path_cause,
        transport_flap=flap,
        breaker_state=_breaker_state(ssh_target or cluster),
        dispatch_never_actuated=never_actuated,
        ssh_target=ssh_target,
        cluster=cluster,
        log_path=str(log_path) if log_path else None,
        log_disclosed=disclosed,
        last_log_line=last_line,
        message=_compose_message(
            run_id=run_id,
            block=block,
            exit_code=exit_code,
            recovery_kind=recovery_kind,
            path_cause=path_cause,
            exc=exc,
        ),
    )
    remediation = _remediation_for(cause, experiment_dir)
    return TerminalCause(**{**cause.__dict__, "remediation": remediation})


def _envelope_facts(exc: BaseException | None) -> tuple[str, str, bool]:
    """``(error_code, category, retry_safe)`` from the exception, or the default.

    The default is the block terminal's own literal ``detached_worker_exit`` so
    the two structured artefacts of one death agree by construction. A typed
    :class:`hpc_agent.errors.HpcError` carries the real triple; anything else is
    an unguarded internal bug and reads as ``internal`` (the dispatch layer's own
    classification rule, mirrored here rather than re-invented).
    """
    from hpc_agent import errors

    if isinstance(exc, errors.HpcError):
        return (
            str(getattr(exc, "error_code", "internal")),
            str(getattr(exc, "category", "internal")),
            bool(getattr(exc, "retry_safe", False)),
        )
    if exc is not None:
        return "internal", "internal", False
    return "detached_worker_exit", "internal", False


def _is_transport_flap(exc: BaseException | None) -> bool:
    """The STAMPED flap identity (``__cause__``-chain aware), never a text match."""
    if exc is None:
        return False
    try:
        from hpc_agent.infra.ssh_options import is_transport_flap

        return bool(is_transport_flap(exc))
    except Exception:  # noqa: BLE001 — an import surprise must not wedge the exit path
        return False


def _load_run_record(experiment_dir: Path, run_id: str) -> Any:
    """The run record, or ``None`` when absent/unreadable (never raises)."""
    try:
        from hpc_agent.state.journal import load_run

        return load_run(experiment_dir, run_id)
    except Exception:  # noqa: BLE001 — a torn record yields no facts, never a crash
        return None


def _dispatch_never_actuated(record: Any) -> bool | None:
    """Rung 0's evidence class, tri-state.

    ``True`` / ``False`` from the record's durable ``dispatch_evidence``; ``None``
    when there is no record or the evidence is EMPTY — a pre-evidence record, for
    which the question is genuinely unknown. Collapsing unknown to ``False`` would
    be the same lie rung 0 exists to refuse.
    """
    if record is None:
        return None
    evidence = getattr(record, "dispatch_evidence", None)
    if not isinstance(evidence, dict) or not evidence:
        return None
    try:
        from hpc_agent.state.journal import dispatch_never_actuated

        return bool(dispatch_never_actuated(record))
    except Exception:  # noqa: BLE001
        return None


def _path_cause_from(exc: BaseException | None, tail: str) -> str | None:
    """The discriminated ``PathCause`` named by the worker's own disclosure.

    Scans the exception message first (the gate refusal and the staging-exhaustion
    error both LEAD with ``readiness.cause``), then the bounded log tail. Only
    FAILING arms are reported: ``path_ok`` / ``path_unproven`` / ``route_unresolved``
    are the passing set, and recording one of those as "the cause" would invent a
    failure the readiness layer explicitly declined to assert.
    """
    try:
        from typing import get_args

        from hpc_agent.infra.readiness_sensors import PathCause, PathReadiness

        arms: list[str] = [
            str(arm)
            for arm in get_args(PathCause)
            if arm not in PathReadiness.PASSING_CAUSES  # only positive failures
        ]
    except Exception:  # noqa: BLE001 — no vocabulary available means no claim
        return None
    haystacks = [str(exc)] if exc is not None else []
    haystacks.append(tail)
    for haystack in haystacks:
        for arm in arms:
            if arm in haystack:
                return arm
    return None


def _recovery_kind(
    *,
    never_actuated: bool | None,
    record_status: str,
    path_cause: str | None,
    flap: bool,
    tail: str,
    exc: BaseException | None,
) -> str | None:
    """Key the cause to ONE recoveries-registry kind, or ``None`` honestly.

    Precedence is by EVIDENCE CLASS, strongest first — the same ordering rung 0
    established: a fact the record proves about itself, offline, outranks
    anything sensed over a wire that may itself be the thing that broke.
    """
    # (1) The record proves its own dispatch never actuated — provable offline.
    if never_actuated and record_status in ("submitting", ""):
        return "zombie_submitting_record"
    # (2) A dead ProxyJump hop: the class whose WRONG remediation (host-retarget
    #     through the same dead hop) is the 2026-07-30 exhibit.
    if path_cause in _DEAD_HOP_CAUSES:
        return "dead_hop_route"
    # (3) A transport flap that outlasted the staging ladder.
    if flap or path_cause == "transport_flap" or _mentions(tail, _STAGE_EXHAUSTED_MARKERS):
        return "flap_exhausted_staging"
    # (4) The canary could not be read — never a pass, so it is terminal.
    message = str(exc) if exc is not None else ""
    if _REPORTER_UNREACHABLE_TOKEN in tail or _REPORTER_UNREACHABLE_TOKEN in message:
        return "canary_reporter_unreachable"
    return None


def _mentions(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def _breaker_state(host: str | None) -> str | None:
    """The host's EFFECTIVE breaker state from its durable doc — a file read.

    ``None`` when there is no host to key on or the breaker cannot be read. This
    is a harvest, not a probe: the breaker's state file already exists because
    real connections recorded into it.
    """
    if not host:
        return None
    try:
        from hpc_agent.infra.ssh_circuit import effective_state_for_host

        return str(effective_state_for_host(host.rsplit("@", 1)[-1].strip()))
    except Exception:  # noqa: BLE001 — an unreadable breaker is no claim, not a crash
        return None


def _compose_message(
    *,
    run_id: str,
    block: str,
    exit_code: int | None,
    recovery_kind: str | None,
    path_cause: str | None,
    exc: BaseException | None,
) -> str:
    """The one-line WHY — composed from the fields, never authored prose."""
    head = f"detached {block} worker for run {run_id} died terminal"
    if exit_code is not None:
        head += f" (exit {exit_code})"
    if recovery_kind == "zombie_submitting_record" and path_cause in _DEAD_HOP_CAUSES:
        # Precedence collision, stated ROUTE-FIRST on purpose. The dispatch
        # evidence legitimately wins the classification (it proves offline that
        # nothing was sent, so resubmitting is safe), but that menu's rank-1
        # option is `/submit-hpc` — and read after a "cause: zombie_submitting_
        # record" headline, with the dead hop never mentioned, it reads as
        # "resubmit now" straight back through the hop that killed the worker.
        # Leading with the route fact keeps BOTH truths in the order that makes
        # the second one safe to act on.
        return (
            f"{head}; the route is DOWN ({path_cause}) — fix the path FIRST; "
            f"separately, cause: {recovery_kind} (the record proves no dispatch "
            f"was ever issued, so nothing is duplicated by a resubmit once the "
            f"route is back)" + (f"; {type(exc).__name__}: {exc}" if exc is not None else "")
        )
    if recovery_kind:
        head += f"; cause: {recovery_kind}"
        # The readiness layer's OWN word rides alongside the registry key: it is
        # the vocabulary the fire-time gate, the worker log, and triage all quote,
        # so a reader can grep one token across every surface.
        if path_cause:
            head += f" (path_cause: {path_cause})"
    elif path_cause:
        head += f"; cause: {path_cause} (no registry kind keyed to it)"
    else:
        head += "; cause NOT discriminated — no known failure class matched the evidence"
    if exc is not None:
        head += f"; {type(exc).__name__}: {exc}"
    return head


def _remediation_for(cause: TerminalCause, experiment_dir: Path) -> str | None:
    """The registry's composed remediation for the cause's kind, or ``None``.

    Routes through :func:`hpc_agent.recovery.registry.remediation_for` — the one
    chokepoint — so the string an attention item carries is byte-identical to
    what ``hpc-agent recoveries show --kind <kind>`` prints. An unknown kind
    yields ``None`` rather than a generic fallback: a generic string is exactly
    the drift the registry exists to eliminate.
    """
    if not cause.recovery_kind:
        return None
    placeholders = {
        "run_id": cause.run_id,
        "experiment_dir": str(experiment_dir),
    }
    if cause.ssh_target:
        placeholders["ssh_target"] = cause.ssh_target
    if cause.cluster:
        placeholders["cluster_host"] = cause.cluster
    if cause.log_path:
        placeholders["log_path"] = cause.log_path
    try:
        from hpc_agent.recovery.registry import remediation_for

        return str(remediation_for(cause.recovery_kind, placeholders=placeholders))
    except Exception:  # noqa: BLE001 — a registry miss is no remediation, not a crash
        return None


# ── the write path ───────────────────────────────────────────────────────────


def record_worker_terminal_cause(
    experiment_dir: Path,
    *,
    run_id: str,
    block: str,
    exit_code: int | None,
    exc: BaseException | None = None,
    log_path: str | None = None,
    now_iso: str | None = None,
) -> dict[str, Any] | None:
    """Classify and journal one terminal detached-worker death.

    The seam the exit path calls. Returns the written record, or ``None`` when
    nothing could be written — this runs while the process is dying, so it is
    fail-open end to end: any surprise is swallowed and the pre-existing
    disclosure (the ``[fatal]`` block, the block terminal) still stands.

    Append-only via the canonical JSONL discipline, with ``fsync_required=False``:
    a disclosure record must never turn a failed worker into a worker that ALSO
    fails to exit. The forensic tier (the log) is already durable.
    """
    try:
        cause = classify_worker_exit(
            experiment_dir,
            run_id=run_id,
            block=block,
            exit_code=exit_code,
            exc=exc,
            log_path=log_path,
            now_iso=now_iso,
        )
        record = cause.as_record()
        from hpc_agent.infra.io import append_jsonl_line

        append_jsonl_line(
            terminal_cause_path(experiment_dir, run_id),
            record,
            fsync_required=False,
        )
        return record
    except Exception:  # noqa: BLE001 — the exit path must never gain a new crash
        _log.debug("terminal-cause record failed for %s/%s", run_id, block, exc_info=True)
        return None
