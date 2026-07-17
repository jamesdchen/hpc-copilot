"""Cluster-side jobmap marker — the submit-once id↔identity binding (U3).

The scheduler submit leg is the codebase's one genuinely non-idempotent
actuation: ``qsub``/``sbatch`` and the id-read are one round-trip whose two
halves the network can split, leaving an array LIVE on the cluster with **no
job id anywhere the client can read** and **no journal record** — an orphan no
existing verb can reconcile (``SUBMIT-ONCE-DESIGN.md`` §1).

This module owns the *cluster-durable* half of the fix: a **jobmap marker** the
dispatching shell itself writes, so the scheduler-assigned id survives a severed
response channel. The control plane, on a drop, re-dials and reads the marker
instead of re-``qsub``-ing (the recovery read is U3-d; this module only produces
the marker and the ack-gated *reader helper* U3-d consumes).

Layout, under ``<remote_path>/.hpc/submit/`` (sibling of ``.hpc/announce/`` /
``.hpc/runs/``), all written **by the remote shell**, all atomic (temp + ``mv``):

* ``<run_id>.jobmap`` — the JSON pending marker written BEFORE dispatch:
  ``{"token": "<run_id>#<attempt>", "state": "pending", "attempt": N,
  "at": <epoch>, "waves": {}}``. It carries the run+attempt-unique correlation
  token (Δ2: ``run_id#attempt``, NOT a job-name hash — ``job_name`` is consumed
  byte-for-byte by log paths and canary naming and must stay untouched).
* ``<run_id>.jobmap.<wave_key>.id`` — ONE file per submitted wave, written
  AFTER the ``qsub`` captured its stdout + rc server-side, containing
  ``"<rc> <raw scheduler stdout>"`` (rc FIRST as a clean single integer token;
  the raw stdout blob follows and may carry spaces — e.g. SGE ``"Your job 987654
  (…) has been submitted"``, Slurm ``"Submitted batch job 12345"``). The job id
  is extracted from that blob by the SAME ``JOB_ID_REGEX`` the client applies to
  the happy-path response (one id-parsing source; the recovery reader does not
  re-implement it). Δ4: the ``rc`` rides the marker so the U3-d adopt rung can
  require ``rc==0`` before adopting an id — a ``qsub`` that failed but still
  printed garbage to stdout is a confirmed failed dispatch, never an adopt.

**Why per-wave id-FILES instead of a JSON ``waves`` read-modify-write.** The
design (§3.2) sketches appending the id into ``jobmap.waves`` atomically. A pure
``sh`` read-modify-write of a JSON object across independent wave dispatches has
a lost-update race no ``mv`` closes — exactly the class of atomicity gap the
premortem's Δ1 exists to eliminate. Writing one write-once id-file per wave
(the proven ``.hpc/announce`` filename-encoding discipline) is atomic per file,
carries the same ``{wave_key: (job_id, rc)}`` information losslessly, keeps the
canary (its own ``run_id`` → its own ``<canary_run_id>.jobmap``) and each main
wave on DISTINCT keys (Δ5: a canary is never confused with wave-0), and the
reader reconstructs the logical ``waves`` map. Premortem-wins-over-design: this
honors every stated invariant (durable token+attempt+state, per-wave id+rc,
atomic writes, ack-gated read) without the RMW race.

Ack discipline (house rule, cloned verbatim from
:data:`hpc_agent.ops.monitor.announce._ANNOUNCE_ACK`): every read echoes
:data:`_JOBMAP_ACK` only after a successful ``cd`` into ``.hpc/submit/``, so an
ABSENT ack — a ``cd`` that failed (dir never created ⇒ never dispatched) or a
truncated/severed read — reads as ``present=False`` / UNKNOWN, **never** as a
spurious "no marker" that could mis-settle a run.

The mint/marker WRITES here are gated behind the opt-in :func:`submit_once_enabled`
capability flag (Δ3, default OFF); flag off ⇒ the submit atom is byte-identical
to today. The reader helper is always safe to call (it only reads).
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

__all__ = [
    "JOBMAP_SUBPATH",
    "JOBMAP_STATE_PENDING",
    "SUBMIT_ONCE_FLAG",
    "CANARY_WAVE_KEY",
    "submit_once_enabled",
    "wave_key",
    "jobmap_token",
    "jobmap_dir",
    "jobmap_marker_path",
    "wave_id_marker_path",
    "build_pre_dispatch_shell",
    "build_post_dispatch_shell",
    "build_read_shell",
    "parse_jobmap_read",
    "JobmapRead",
]

# ``.hpc/submit/`` — sibling of ``.hpc/announce`` / ``.hpc/runs`` (design §3.2).
JOBMAP_SUBPATH = ".hpc/submit"
JOBMAP_STATE_PENDING = "pending"

# Opt-in capability flag (Δ3). House convention: ``HPC_<NAME>=1`` read via a
# ``.strip() == "1"`` predicate (the same shape as HPC_FORCE_RERUN /
# HPC_RESUME_FROM_CHECKPOINT — an independent convention, not a value twin).
# Default OFF ⇒ the mint + jobmap-marker WRITES are dormant and the submit atom
# is byte-identical to current behavior. The phase-1 recovery READERS
# (``find_submitting_runs``, the ``prune`` guard, the render surfaces) are
# always-on; ONLY the writes gate here.
SUBMIT_ONCE_FLAG = "HPC_SUBMIT_ONCE"

# The wave key the canary rides (Δ5). The canary is a DISTINCT run_id
# (``<run_id>-canary``), so it already gets its own ``<canary_run_id>.jobmap``
# file for free; this distinct key is belt-and-suspenders so a canary marker can
# never be read as the main array's wave-0 even if a future refactor co-located
# them.
CANARY_WAVE_KEY = "canary"

# Positive-evidence ack sentinel, SAME discipline as ``announce.py``'s
# ``_ANNOUNCE_ACK`` (echoed only after a successful ``cd`` into ``.hpc/submit/``).
# A DISTINCT token so a grep never confuses a jobmap read with an announce read.
_JOBMAP_ACK = "__HPC_JOBMAP_ACK__"

# Prefix on each reconstructed wave-id line the reader emits (``build_read_shell``)
# and parses (``parse_jobmap_read``). Distinct, greppable, never collides with the
# JSON marker line.
_WAVE_ID_LINE = "__HPC_JOBMAP_WAVE__"


def submit_once_enabled() -> bool:
    """True iff the opt-in submit-once capability flag (Δ3) is set to ``1``.

    Gates ONLY the mint + jobmap-marker writes. Default OFF ⇒ byte-identical
    current behavior. Read live from the environment on every call (never an
    import-time snapshot) so a test / a proving-run operator can flip it without
    a re-import.

    CAUTION (U3-b scope): ON is currently inert-for-correctness — markers are
    written but no ``submitting`` record is minted (the live submit_flow wiring
    is deferred with U3-d's recovery reader) and nothing consumes the markers
    yet, so every marker carries ``attempt=0``/``wave-0``. Do NOT enable in a
    proving run until the mint wiring + per-wave key plumbing + U3-d land.
    """
    return os.environ.get(SUBMIT_ONCE_FLAG, "").strip() == "1"


def wave_key(wave: int) -> str:
    """The jobmap key for main-array wave *wave* (0-based) — ``wave-<n>`` (Δ5).

    DISTINCT from :data:`CANARY_WAVE_KEY` so the canary's id-marker and the main
    array's wave-0 id-marker never collide.
    """
    return f"wave-{int(wave)}"


def jobmap_token(run_id: str, attempt: int) -> str:
    """The run+attempt-unique correlation token ``<run_id>#<attempt>`` (Δ2/§3.1).

    Carried in the marker (and, in U3-c, in the scheduler ``--comment``/``-ac``
    context field) — NOT in ``job_name`` (SGE ≤15 chars, and ``job_name`` is
    consumed byte-for-byte by log paths and canary naming). ``run_id`` is
    deterministic on swept params (#207); ``attempt`` discriminates a legitimate
    later resubmit's marker from an orphan's marker of the same run_id.
    """
    return f"{run_id}#{int(attempt)}"


def jobmap_dir(remote_path: str) -> str:
    """The remote ``.hpc/submit/`` directory for *remote_path* (no trailing slash)."""
    return f"{remote_path.rstrip('/')}/{JOBMAP_SUBPATH}"


def jobmap_marker_path(remote_path: str, run_id: str) -> str:
    """Absolute remote path of the ``<run_id>.jobmap`` pending marker."""
    return f"{jobmap_dir(remote_path)}/{run_id}.jobmap"


def wave_id_marker_path(remote_path: str, run_id: str, wkey: str) -> str:
    """Absolute remote path of a wave's ``<run_id>.jobmap.<wave_key>.id`` file."""
    return f"{jobmap_dir(remote_path)}/{run_id}.jobmap.{wkey}.id"


def build_pre_dispatch_shell(
    *,
    remote_path: str,
    run_id: str,
    attempt: int,
    wkey: str,
) -> str:
    """Shell fragment that writes the ``pending`` jobmap BEFORE ``qsub`` (§3.2 step 1).

    Folded into the SAME ``bash -lc`` / direct round-trip as the dispatch by
    :meth:`RemoteHPCBackend._execute_command`, so it survives a severed client.
    ``mkdir -p`` rides the same leg (OPEN-4). Atomic temp + ``mv`` (the transfer
    plane's manifest-writer discipline). Uses ``$$``-suffixed temp names and
    ``__hpc_``-prefixed shell vars so nothing collides with the qsub command.

    *wkey* is accepted for symmetry / future per-wave pending state but the
    pending marker itself is per-run (one ``<run_id>.jobmap``); the id lands in a
    per-wave file (:func:`build_post_dispatch_shell`).
    """
    _ = wkey  # pending marker is per-run; wave keying lives in the id-file name
    submit_dir = jobmap_dir(remote_path)
    marker = jobmap_marker_path(remote_path, run_id)
    token = jobmap_token(run_id, attempt)
    # printf format is single-quoted ⇒ the JSON braces are literal shell text; the
    # three ``%s`` are filled by shlex-quoted literals + an integer epoch. ``at``
    # is unquoted in the JSON (numeric) — ``$(date +%s)`` yields a bare integer.
    return (
        f"mkdir -p {shlex.quote(submit_dir)} 2>/dev/null; "
        f"__hpc_jm={shlex.quote(marker)}; "
        f'__hpc_jmt="$__hpc_jm.tmp.$$"; '
        f'printf \'{{"token":"%s","state":"{JOBMAP_STATE_PENDING}",'
        f'"attempt":%s,"at":%s,"waves":{{}}}}\\n\' '
        f'{shlex.quote(token)} {shlex.quote(str(int(attempt)))} "$(date +%s)" '
        f'> "$__hpc_jmt" 2>/dev/null && mv -f "$__hpc_jmt" "$__hpc_jm" 2>/dev/null'
    )


def build_post_dispatch_shell(
    *,
    remote_path: str,
    run_id: str,
    wkey: str,
    jid_var: str = "__hpc_jid",
    rc_var: str = "__hpc_rc",
) -> str:
    """Shell fragment that persists ``"<JID> <rc>"`` AFTER ``qsub`` (§3.2 step 2, Δ4).

    Runs after the dispatch captured the id + rc server-side
    (``__hpc_jid=$(qsub …); __hpc_rc=$?``) and BEFORE the id is echoed to the
    client — so the id is cluster-durable before the response channel carries it.
    Atomic temp + ``mv``. The ``rc`` is persisted alongside the id so the U3-d
    adopt rung can require ``rc==0`` (Δ4: an ``rc≠0`` marker is a confirmed failed
    dispatch, never an adopt).
    """
    wid = wave_id_marker_path(remote_path, run_id, wkey)
    # rc FIRST (clean single integer token), then the raw scheduler stdout blob —
    # so the reader can peel a well-delimited rc off the front and treat the rest
    # (which may carry spaces) as the id blob.
    return (
        f"__hpc_wid={shlex.quote(wid)}; "
        f'__hpc_widt="$__hpc_wid.tmp.$$"; '
        f'printf \'%s %s\\n\' "${rc_var}" "${jid_var}" '
        f'> "$__hpc_widt" 2>/dev/null && mv -f "$__hpc_widt" "$__hpc_wid" 2>/dev/null'
    )


def build_read_shell(*, remote_path: str, run_id: str) -> str:
    """Ack-gated read command for the recovery path (U3-d consumes this).

    ONE bounded ssh exec, cloned from ``announce.read_announcements``: ``cd`` into
    ``.hpc/submit/`` and, ONLY on success, echo :data:`_JOBMAP_ACK`, ``cat`` the
    ``<run_id>.jobmap`` pending marker, then emit one ``__HPC_JOBMAP_WAVE__`` line
    per ``<run_id>.jobmap.<wave_key>.id`` file. An absent ack (``cd`` failed ⇒ dir
    never created ⇒ never dispatched, or a truncated read) ⇒ ``present=False``
    (:func:`parse_jobmap_read`), never a spurious "no marker".

    The ``{run_id}.jobmap.*.id`` glob keeps ``*`` unquoted (only ``run_id`` is
    shell-quoted) so it expands; a no-match glob stays literal and the
    ``[ -e ]`` test skips it. ``; true`` keeps rc==0 so a transport rc!=0 is the
    (separate) severed-transport signal, exactly like the announce reader.
    """
    submit_dir = jobmap_dir(remote_path)
    marker_name = f"{run_id}.jobmap"
    # Only ``run_id`` is quoted; ``.jobmap.*.id`` stays literal so ``*`` globs.
    glob = f"{shlex.quote(run_id)}.jobmap.*.id"
    # For each id-file, emit ``__HPC_JOBMAP_WAVE__ <wave_key> <JID> <rc>``. The
    # wave_key is carved out of the basename via a ``sh`` parameter-expansion:
    # strip the ``<run_id>.jobmap.`` prefix and the ``.id`` suffix.
    prefix = f"{run_id}.jobmap."
    return (
        f"cd {shlex.quote(submit_dir)} 2>/dev/null "
        f"&& printf '%s\\n' {shlex.quote(_JOBMAP_ACK)} "
        f"&& cat {shlex.quote(marker_name)} 2>/dev/null; "
        f"for __hpc_f in {glob}; do "
        f'[ -e "$__hpc_f" ] || continue; '
        f'__hpc_wk="${{__hpc_f#{prefix}}}"; __hpc_wk="${{__hpc_wk%.id}}"; '
        f"printf '%s %s %s\\n' {shlex.quote(_WAVE_ID_LINE)} \"$__hpc_wk\" "
        f"\"$(cat \"$__hpc_f\" 2>/dev/null | tr '\\n' ' ')\"; "
        f"done; true"
    )


@dataclass(frozen=True)
class JobmapRead:
    """Parsed result of a jobmap read (U3-d's recovery input).

    ``present`` is the capability/ack signal — ``True`` iff the positive ack was
    seen (``.hpc/submit/`` exists and the read was not severed). ``present ==
    False`` carries empty fields and means UNKNOWN / never-dispatched (the caller
    must NOT read it as a settle). ``waves`` maps ``wave_key -> (stdout_blob, rc)``
    where ``stdout_blob`` is the RAW scheduler stdout captured server-side (the
    U3-d adopt rung applies the backend ``JOB_ID_REGEX`` to it, gated on
    ``rc == 0`` — Δ4); it is not a pre-parsed id.
    """

    present: bool
    token: str | None
    attempt: int | None
    state: str | None
    waves: dict[str, tuple[str, int]]


def parse_jobmap_read(stdout: str) -> JobmapRead:
    """Parse :func:`build_read_shell` stdout under the ack discipline.

    No :data:`_JOBMAP_ACK` in the output ⇒ ``present=False`` with empty fields
    (severed/absent — UNKNOWN, never "no marker"). Otherwise parse the pending
    marker JSON line (``token``/``attempt``/``state``) and each
    ``__HPC_JOBMAP_WAVE__ <wave_key> <rc> <blob>`` line (rc FIRST, then the
    raw stdout blob) into ``waves``.
    """
    import json

    lines = [ln.strip() for ln in (stdout or "").splitlines()]
    if _JOBMAP_ACK not in lines:
        return JobmapRead(present=False, token=None, attempt=None, state=None, waves={})
    token: str | None = None
    attempt: int | None = None
    state: str | None = None
    waves: dict[str, tuple[str, int]] = {}
    for line in lines:
        if line == _JOBMAP_ACK:
            continue
        if line.startswith(_WAVE_ID_LINE):
            parts = line.split()
            # ``__HPC_JOBMAP_WAVE__ <wave_key> <rc> <raw stdout blob...>`` — rc is
            # the clean single token at [2]; the id blob (may be empty on a failed
            # dispatch, may carry spaces) is everything after it.
            if len(parts) >= 3:
                wkey, rc_str = parts[1], parts[2]
                blob = " ".join(parts[3:])
                try:
                    waves[wkey] = (blob, int(rc_str))
                except ValueError:
                    continue
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                token = obj.get("token") if isinstance(obj.get("token"), str) else token
                state = obj.get("state") if isinstance(obj.get("state"), str) else state
                raw_attempt = obj.get("attempt")
                if isinstance(raw_attempt, int):
                    attempt = raw_attempt
    return JobmapRead(present=True, token=token, attempt=attempt, state=state, waves=waves)
