"""Per-run journal — data layer.

Owns the RunRecord dataclass + the layout / locking / atomic-write
primitives that every higher-level helper in
``hpc_agent.state`` builds on.

Storage layout (one tree per experiment cwd)::

    ~/.claude/hpc/<repo_hash>/
    ├── repo.json                   {"experiment_dir": ..., "first_seen": ...}
    ├── index.json                  cache of run_id -> {status, updated_at}
    ├── index.lock
    └── runs/
        ├── <run_id>.json
        ├── <run_id>.lock
        └── ...

``repo_hash`` is ``sha256(experiment_dir.resolve())[:12]``. Pure I/O;
no SSH, no mapreduce imports — composition with cluster-mutating ops
lives in the per-subject runners under :mod:`hpc_agent.ops`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES as _LIFECYCLE_TERMINAL
from hpc_agent.infra.io import advisory_flock

__all__ = [
    "SCHEMA_VERSION",
    "HPC_HOMEDIR",
    "TERMINAL_STATUSES",
    "RunRecord",
    "repo_hash",
    "journal_dir",
    "runs_dir",
]

if TYPE_CHECKING:
    from collections.abc import Iterator

SCHEMA_VERSION = 1


def _current_homedir() -> Path:
    """Re-resolve the journal home on every call.

    Lookup order:
    1. ``HPC_JOURNAL_DIR`` env var — wins always, so
       ``monkeypatch.setenv("HPC_JOURNAL_DIR", tmp_path)`` alone is
       enough to redirect state writes (v3 BUG-8V3-1 root cause was a
       cached import-time snapshot that ignored the env override).
    2. The module-level ``HPC_HOMEDIR`` attribute — back-compat with the
       pre-v3 ``monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path)``
       pattern used in ~20 test files. Patching the attribute still
       redirects as long as the env var is unset.
    3. ``~/.claude/hpc`` — the default.
    """
    env_val = os.environ.get("HPC_JOURNAL_DIR")
    # ``if env_val`` previously fell through to the default branch when
    # the env var was set to the empty string; v3 precedence says env
    # wins (including the explicit empty-string case → still treat
    # empty as unset, but distinguish None from "" elsewhere).
    if env_val is not None and env_val != "":
        return Path(env_val)
    attr = globals().get("HPC_HOMEDIR")
    if isinstance(attr, Path):
        return attr
    return Path.home() / ".claude" / "hpc"


# Import-time snapshot kept as a public attribute for back-compat —
# read-mostly callers (capabilities envelope, doc-gen) that just want
# the configured location are fine with the snapshot. State-touching
# call sites (``journal_dir``, ``find_in_flight_runs``,
# ``find_runs_by_campaign``) go through ``_current_homedir()`` so
# per-test env redirection actually applies.
_env_journal_dir = os.environ.get("HPC_JOURNAL_DIR")
HPC_HOMEDIR = (
    Path(_env_journal_dir)
    if _env_journal_dir is not None and _env_journal_dir != ""
    else Path.home() / ".claude" / "hpc"
)

# Re-exported for back-compat. Derived from the canonical
# hpc_agent._kernel.contract.vocabulary.JournalStatus StrEnum so the literal can
# no longer drift from the rest of the codebase.
TERMINAL_STATUSES = _LIFECYCLE_TERMINAL

# Whitelist of fields ``update_run_status`` may overwrite on an existing
# RunRecord. Keeps the read-modify-write helper from accidentally
# clobbering identity / provenance fields (run_id, profile, cluster,
# submitted_at, etc.).
_UPDATABLE_FIELDS = frozenset(
    {
        "last_status",
        "combined_waves",
        "failed_waves",
        "retries",
        "stage",
        "job_ids",
        "last_resubmit_request_id",
        "recent_resubmit_request_ids",
        "pending_resubmit",
        "pending_verdict",
        # §5 "parked ≠ stalled": a run paused ON A DECISION (a block reached its
        # y/nudge boundary), distinct from pending_verdict (paused on an
        # escalation). Whitelisted so the value-overwriting update_run_status path
        # the mark/clear setters use may set it.
        "pending_decision",
        # Bumped by the auto-resume composite after each fired resubmit so the
        # gate's "count < cap" backstop tightens with every attempt (#299).
        "auto_resume_count",
        # Bumped by the resolve-and-recover composite after each auto-acted
        # code-verdict resubmit so its "count < cap" backstop tightens (#240).
        "auto_recover_count",
        # ── §5 driver watchdog (dead-man's switch) + kill semantics ──────────
        # Stamped by the journal setters ``stamp_tick`` / ``mark_seen_by_human``
        # / ``record_kill_request`` / ``record_kill_confirmed``; whitelisted so
        # the value-overwriting ``update_run_status`` path may also set them.
        "last_tick_at",
        "next_tick_due",
        "last_seen_by_human_at",
        "kill_requested_at",
        "kill_confirmed_at",
        "kill_requested_job_ids",
        "kill_confirmed_job_ids",
    }
)

_log = logging.getLogger(__name__)


@dataclasses.dataclass
class RunRecord:
    """One submitted run as seen by the agent layer."""

    run_id: str
    profile: str
    cluster: str
    ssh_target: str
    remote_path: str
    job_name: str
    job_ids: list[str]
    total_tasks: int
    submitted_at: str
    experiment_dir: str
    last_status: dict = dataclasses.field(default_factory=dict)
    combined_waves: list[int] = dataclasses.field(default_factory=list)
    failed_waves: list[int] = dataclasses.field(default_factory=list)
    retries: dict[str, dict] = dataclasses.field(default_factory=dict)
    stage: str = "monitor"
    status: str = "in_flight"
    last_resubmit_request_id: str = ""
    # Bounded list of recent resubmit request_ids so an A→B→A replay
    # sequence is correctly recognised as a duplicate of A (the
    # ``last_resubmit_request_id`` alone only catches back-to-back
    # replays and double-increments retry counters otherwise). Newest
    # last; capped via ``_MAX_RECENT_RESUBMIT_IDS`` at write time.
    recent_resubmit_request_ids: list[str] = dataclasses.field(default_factory=list)
    # Closed-loop campaign tag. Empty string for open-loop submits.
    # Populated when /submit was invoked with --campaign-id (or with
    # campaign_id set on the submit spec). The asyncio campaign loop
    # uses ``find_runs_by_campaign`` to discover its in-flight set on
    # resume.
    campaign_id: str = ""
    # Resume marker for a multi-batch resubmit that failed partway. When
    # non-empty: ``{"request_id": <rid>, "job_ids": [<ids landed so
    # far>]}``. resubmit_flow uses it to continue from the next
    # un-submitted batch instead of re-running the whole plan (double
    # submit) or skipping the remainder. Cleared once the resubmit
    # completes fully.
    pending_resubmit: dict = dataclasses.field(default_factory=dict)
    # Holding state for an escalated cluster awaiting a verdict (#231/#234).
    # Empty dict = not held. When non-empty it carries the Escalation block
    # (decision-as-data: failure_features + candidate_actions + the affected
    # cluster) that the deterministic resolver could not resolve. The run is
    # *parked* — neither resubmitted (the verdict picks the fix) nor treated
    # as terminal-done by the campaign loop — so the campaign keeps making
    # progress on unaffected work. Stored as a plain dict to keep this layer
    # pure I/O (no _wire import); the caller passes ``Escalation.model_dump()``.
    # Cleared back to ``{}`` when the verdict is applied; the exit is
    # resubmit_flow with the chosen overrides (resubmit-on-verdict). HPC
    # schedulers can't cheaply freeze a *running* job, so a live task is left
    # to run to terminal and only then enters this hold — there is no
    # live-freeze state by design.
    pending_verdict: dict = dataclasses.field(default_factory=dict)
    # ── §5 durable pending-DECISION marker ("parked ≠ stalled") ────────────────
    # Set when a driver span reaches a block's y/nudge boundary and PARKS awaiting
    # a human decision (block-drive.md §5). This is the "parked ON A DECISION"
    # state — distinct from ``pending_verdict`` above, which is "parked ON AN
    # ESCALATION" (the deterministic resolver could not act). A parked-on-decision
    # run is neither stalled (the §5 watchdog reads a non-empty pending_decision as
    # "awaiting your decision since <awaiting_since>", not "driver stalled — re-arm")
    # nor terminal. Empty dict = not parked. Shape (caller-assembled to keep this
    # layer pure I/O — no _wire import):
    #   {
    #     "block": str,          # the block verb that parked (e.g. "submit-s2")
    #     "workflow": str,       # its workflow family ("submit"/"status"/...)
    #     "brief": dict,         # the code-digested evidence for the y/nudge loop
    #     "resume_cursor": {     # enough for a STATELESS tick to resume the driver
    #        "workflow": str,    #   the workflow family
    #        "run_id": str,      #   the run being driven
    #        "next_verb": str | None,  # the deterministic successor, or None at a
    #                                  #   human branch (block_chain.successor_verb)
    #        "current_verb": str,      # the block that parked
    #     },
    #     "awaiting_since": <iso8601>,  # when the park began (the watchdog's clock)
    #     "cmd_sha": str | None,        # the tree identity at park (§4 routing key)
    #   }
    # Cleared back to ``{}`` by ``clear_pending_decision`` once the driver advances
    # (the human answered y/nudge and the next span consumed the resolved spec).
    pending_decision: dict = dataclasses.field(default_factory=dict)
    # Append-only audit log of judgement verdicts enacted on this run (#234).
    # Each entry records the control-flow branch a non-deterministic decision
    # took AND why — the rationale the deterministic resolver could not supply.
    # Written by ``clear_pending_verdict`` as the hold is released (which resets
    # ``pending_verdict`` to {}, otherwise discarding the reasoning), so the
    # *why* survives enactment. Entry shape is caller-assembled to keep this
    # layer pure I/O (no _wire import) — typically
    # ``{decided_by, chosen, rejected, why, applied_at}`` — with ``applied_at``
    # auto-stamped when omitted. Feeds the audit trail ("why did this campaign
    # take branch X") and the ``source="history"`` recall input the resolver
    # consults before re-escalating the same fingerprint. Never cleared.
    verdict_history: list[dict] = dataclasses.field(default_factory=list)
    # ── #294 Layer-2 auto-resume keystone (#299) ──────────────────────────────
    # The inputs a monitor-side auto-resume needs to re-submit *with*. Before
    # this, ``script`` / ``backend`` / ``job_env`` only existed on the
    # agent-supplied resubmit spec, so a read-only monitor path had nothing to
    # resubmit from. Populated at submit (submit-flow's record-creation path).
    # All three carry harmless empty defaults so a pre-#299 record loads
    # unchanged (``from_dict`` filters to known fields → dataclass defaults).
    script: str = ""
    backend: str = ""
    job_env: dict[str, str] = dataclasses.field(default_factory=dict)
    # Opt-in, default OFF: a run that did not set this is NEVER auto-resubmitted
    # (zero blast radius for everyone else — the #294 safety posture). The
    # monitor's terminal-FAILED hook consults the auto-resume gate only when
    # this is True.
    auto_resume_on_kill: bool = False
    # Hard cap + running counter — the ultimate backstop. Even total
    # misclassification can waste at most ``max_auto_resumes`` resubmits before
    # the gate escalates with "cap reached". ``auto_resume_count`` is bumped by
    # the auto-resume composite each time it fires.
    max_auto_resumes: int = 2
    auto_resume_count: int = 0
    # ── #240 resolve-and-recover auto-act (realizes #234's buildable wiring) ──
    # Opt-in, default OFF, mirroring the auto-resume posture above: a run that
    # did not set this is NEVER auto-recovered (zero blast radius). When True,
    # the resolve-and-recover composite may auto-resubmit a ``decided_by="code"``
    # verdict with the resolver's refined overrides; a ``decided_by="judgement"``
    # verdict is always parked (``mark_pending_verdict``), never auto-acted.
    # This is the broader-than-preempted general-resolver counterpart to
    # ``auto_resume_on_kill`` (which stays preempted-only); the two opt-ins are
    # independent so enabling general auto-recovery is a deliberate separate
    # choice. Held to the #283 motto: no agent-facing field bypasses a safety
    # step — when OFF the composite still computes and surfaces the verdict, it
    # just takes no side effect.
    auto_recover_on_failure: bool = False
    # Hard cap + running counter — the ultimate backstop for code-verdict
    # auto-resubmits. Even total misclassification can waste at most
    # ``max_auto_recovers`` resubmits before the composite refuses with "cap
    # reached". ``auto_recover_count`` is bumped each time a code-verdict
    # resubmit actually fires.
    max_auto_recovers: int = 2
    auto_recover_count: int = 0
    # ── #351 sub-bug #5 (layer-1 companion): durable code-drift provenance ────
    # The per-task ``executor`` command and the ``tasks.py`` drift sha this run
    # was submitted with. These are otherwise SIDECAR-only fields — but a
    # same-run_id in-place redo (unchanged swept params) re-writes the per-run
    # sidecar with the NEW code at Step 6d, BEFORE the dedup gate runs, so the
    # sidecar cannot tell the layer-1 COMPLETE-dedup gate what the PRIOR run
    # actually ran. Mirroring them onto the journal record — which is rewritten
    # only by ``upsert_run`` AFTER the dedup decision — gives
    # ``submit_and_record`` the one durable prior-vs-new signal that survives
    # the redo's destructive sidecar overwrite (see ``_layer1_code_drift``).
    # Harmless empty defaults: a pre-#351 record loads unchanged (``from_dict``
    # filters to known fields) and an empty recorded value is treated as
    # "cannot prove drift" — never a false invalidation.
    executor: str = ""
    tasks_py_sha: str = ""
    # ── §5 driver watchdog (dead-man's switch) ────────────────────────────────
    # Stamped by ``stamp_tick`` every driver tick: when this tick ran
    # (``last_tick_at``) and the absolute deadline the NEXT tick must run by
    # (``next_tick_due``, computed from the cadence the tick itself chose). A
    # ``next_tick_due`` in the past on a live (in_flight) run is a STALLED driver
    # — ``find_stalled_runs`` / the ``doctor`` verb surface it for a human re-arm
    # decision. The watchdog never restarts anything (design §5). Both are
    # ISO-8601 UTC strings (same format as ``submitted_at``); None until the
    # first tick. Harmless None defaults: a pre-watchdog record loads unchanged.
    last_tick_at: str | None = None
    next_tick_due: str | None = None
    # Set by ``mark_seen_by_human`` when the human last looked at this run, so
    # the journal can answer "what changed since the human last looked" (§5).
    last_seen_by_human_at: str | None = None
    # ── §5 first-class kill semantics: request → journaled → verified ──────────
    # ``record_kill_request`` stamps the intent (when + which job_ids were
    # targeted) BEFORE any scheduler mutation, so a crash mid-kill still leaves a
    # durable record of what was asked. ``record_kill_confirmed`` stamps the
    # subset verified gone against the scheduler afterwards. The pair backs the
    # "N requested, N confirmed gone" honesty contract (§5). None / [] until a
    # kill is requested.
    kill_requested_at: str | None = None
    kill_confirmed_at: str | None = None
    kill_requested_job_ids: list[str] = dataclasses.field(default_factory=list)
    kill_confirmed_job_ids: list[str] = dataclasses.field(default_factory=list)
    # ── Supersession conduct (proving run #4, findings e/g/h) ─────────────────
    # Minting a NEW run_id while a SIBLING prior run_id (same cmd_sha, same
    # journal home) still has live state must be an explicit, closure-triggering
    # act — a fresh run_id must never make the lease / provenance gates forget
    # (`ops/supersession.py`). ``superseded_by`` + ``superseded_at`` are
    # stamped on the OLD record as the durable evidence of WHY it was closed
    # (the verdict is revisable; the evidence is durable — engineering-
    # principles); ``supersedes`` is stamped on the NEW record so the old→new
    # audit link is queryable in both directions without a scan. The status
    # write itself goes through ``mark_run`` with the reason recorded in
    # ``last_status.verdict_reason`` (the same centralized idiom reconcile's
    # settle arms use), never an ad-hoc status flip. Harmless empty defaults:
    # a pre-supersession record loads unchanged (``from_dict`` filters).
    superseded_by: str = ""
    superseded_at: str | None = None
    supersedes: str = ""
    # Non-empty when the superseded run's scheduler jobs could NOT be confirmed
    # gone at supersession time (no backend cancel affordance, partial kill, or
    # an unreachable cluster / open circuit breaker). Shape (caller-assembled,
    # pure I/O layer): {"job_ids": [...], "reason": str, "recorded_at": iso8601}.
    # Surfaced by status-snapshot (and the doctor's journal reads) rather than
    # blocking the superseding submit; cleared when a later reconcile confirms
    # the jobs terminal.
    pending_closure: dict = dataclasses.field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> RunRecord:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})


# Path-form translators applied on Windows BEFORE resolve() so the same
# logical drive expressed under Bash MINGW (/c/...) or WSL (/mnt/c/...)
# canonicalizes into the Windows drive-letter form. Without this, the same
# experiment dir under different shells gets distinct namespace hashes and
# submitter / reconcile silently miss each other (#296).
_BASH_MINGW_DRIVE_RE = re.compile(r"^/([a-zA-Z])(/.*)?$")
_WSL_DRIVE_RE = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$")


def _canonicalize_for_hash(experiment_dir: Path) -> str:
    """Path-form-invariant string for hashing.

    On Windows, translates Bash MINGW (``/c/...``) and WSL (``/mnt/c/...``)
    prefixes into the Windows drive-letter form before ``resolve()``, then
    folds ``/`` → ``\\`` and uppercases the drive letter. Existing canonical
    Windows-form paths (``C:\\...``) hash unchanged, preserving every
    journal namespace dir already on disk.

    On non-Windows, returns ``str(Path(p).resolve())`` unchanged.
    """
    if sys.platform != "win32":
        return str(Path(experiment_dir).resolve())

    # On Windows, ``Path('/c/Users/...')`` normalizes immediately to
    # ``\c\Users\...`` — the forward slashes are gone before this function
    # runs. Fold to forward slashes BEFORE the regex check so the prefix
    # patterns match regardless of which slash flavor the caller passed.
    raw = str(experiment_dir).replace(chr(92), "/")

    m = _WSL_DRIVE_RE.match(raw)
    if m:
        drive, rest = m.groups()
        raw = f"{drive.upper()}:{rest or '/'}"
    else:
        m = _BASH_MINGW_DRIVE_RE.match(raw)
        if m:
            drive, rest = m.groups()
            raw = f"{drive.upper()}:{rest or '/'}"

    resolved = str(Path(raw).resolve()).replace("/", chr(92))
    if len(resolved) >= 2 and resolved[1] == ":":
        resolved = resolved[0].upper() + resolved[1:]
    return resolved


def repo_hash(experiment_dir: Path) -> str:
    """Stable 12-char hex digest of the resolved experiment directory.

    Path-form-invariant on Windows (#296): the same logical dir under
    backslash, Bash MINGW ``/c/...``, and WSL ``/mnt/c/...`` produces the
    same hash so the submitter writes the journal under the same namespace
    the reconcile / monitor reader looks up.
    """
    return hashlib.sha256(_canonicalize_for_hash(experiment_dir).encode()).hexdigest()[:12]


def journal_dir(experiment_dir: Path) -> Path:
    """Return ``~/.claude/hpc/<repo_hash>/`` for *experiment_dir* (created)."""
    from hpc_agent.infra.time import utcnow_iso

    d = _current_homedir() / repo_hash(experiment_dir)
    d.mkdir(parents=True, exist_ok=True)
    repo_meta = d / "repo.json"
    if not repo_meta.exists():
        _atomic_write_json(
            repo_meta,
            {
                "experiment_dir": str(Path(experiment_dir).resolve()),
                "first_seen": utcnow_iso(),
            },
        )
    (d / "runs").mkdir(exist_ok=True)
    return d


def runs_dir(experiment_dir: Path) -> Path:
    """Deprecated forwarder for ``JournalLayout(experiment_dir).runs``.

    NOTE: this is the **journal** runs directory under
    ``~/.claude/hpc/<repo_hash>/runs/``, NOT the cluster sidecar runs
    directory under ``<experiment_dir>/.hpc/runs/`` — that one is
    :attr:`hpc_agent._kernel.contract.layout.RepoLayout.runs`. The
    pre-B1 collision between these two ``runs_*`` names was a P0 bug
    source; the ``RepoLayout`` / ``JournalLayout`` type split makes
    it a type
    error.
    """
    from hpc_agent._kernel.contract.layout import JournalLayout

    return JournalLayout(experiment_dir).runs


def _run_path(experiment_dir: Path, run_id: str) -> Path:
    """Deprecated alias for ``JournalLayout(experiment_dir).run_record(run_id)``."""
    from hpc_agent._kernel.contract.layout import JournalLayout

    return JournalLayout(experiment_dir).run_record(run_id)


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


@contextlib.contextmanager
def _locked(target: Path) -> Iterator[None]:
    """Acquire an exclusive flock on a sibling ``.lock`` file for *target*.

    Thin wrapper around :func:`hpc_agent.infra.io.advisory_flock`
    that derives the lock path via :func:`_lock_path`. No-op on platforms
    without ``fcntl`` (e.g. Windows). The lock file is created on demand
    and never deleted — flock semantics handle reuse.
    """
    with advisory_flock(_lock_path(target), timeout_sec=120.0):
        yield


def _atomic_write_json(path: Path, payload: dict, *, fsync: bool = True) -> None:
    """Deprecated forwarder for :func:`hpc_agent.infra.io.atomic_write_json`.

    Kept so callers that import this module-level name don't break;
    new code should import ``atomic_write_json`` from
    ``hpc_agent.infra.io`` directly. This forwarder will be
    removed in a future release.

    The optional ``fsync`` kwarg is forwarded so the hot-path monitor
    tick can write a non-authoritative cache without a redundant fsync
    — see the canonical helper's docstring for the durability tradeoff.
    """
    from hpc_agent.infra.io import atomic_write_json

    atomic_write_json(path, payload, fsync=fsync)


def _read_json(path: Path) -> dict | None:
    try:
        result: dict = json.loads(path.read_text(encoding="utf-8"))
        return result
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log.warning("session: skipping unreadable %s (%s)", path, exc)
        return None
