"""Bounded auto-prune of manifest-known remote extras (data-manifest ruling 6).

The rsync-less delta push is additive: it never pruned the remote's ``extra``
(files present remotely, absent locally), so a file we shipped in a PRIOR push
and later dropped from the deploy set lingered on the cluster forever. The
ruling (docs/design/data-manifest.md foot, 2026-07-10) lets us auto-delete the
only class we can PROVE is ours — a remote extra recorded in the prior push
manifest — under a disclosed twin cap (count + bytes). Anything NOT
manifest-known is an ANOMALY: never deleted, surfaced to ask.

This rides the SAME delete=True delta push that already holds the dial (the
zero-unattended-cold-SSH discipline: prune never opens a new cold connection).
"""

from __future__ import annotations

import base64
import contextlib
import json
import shlex
from pathlib import Path
from typing import Any, Final

from hpc_agent.infra.remote import _env_int

#: Remote-relative path of the push manifest — the record of what THIS control
#: plane last shipped to ``remote_path``. Read at the start of the next delta
#: push to decide which remote extras are manifest-known (ours to prune) vs
#: anomalies (foreign, never touched). Lives under ``.hpc/`` beside the deploy
#: cache; it is our own bookkeeping, so it is never itself treated as an extra.
_PUSH_MANIFEST_REL: Final[str] = ".hpc/.push_manifest.json"

#: Schema version of the push-manifest doc. v2 (rank 5) carries a per-entry
#: ``(path, size, mtime_ns, sha256)`` ``entries`` list BESIDE the ``paths`` prune
#: bookkeeping — the remote quick-check cache the deployed hash snippet reads and
#: writes (:data:`hpc_agent.infra.transport._delta._REMOTE_MANIFEST_SNIPPET`) so a
#: re-push re-hashes only files whose ``(size, mtime_ns)`` moved. A v1 doc (older
#: wheels wrote ``{paths, pkg_version}`` with no ``manifest_schema``/``entries``)
#: is read fine: the prune still reads ``paths``, and the snippet sees no cache
#: and full-re-hashes (graceful downgrade, never a refusal). The snippet hardcodes
#: this integer (it runs stdlib-only cluster-side and cannot import); the lockstep
#: is pinned by ``tests/infra/test_transport_delta_cache_checkpoint.py``.
_PUSH_MANIFEST_SCHEMA: Final[int] = 2

#: Env kill-switch: ``HPC_NO_DEPLOY_PRUNE=1`` disables the auto-prune entirely
#: (the push stays additive, as it was before the ruling). Mirrors the
#: ``HPC_NO_DEPLOY_DELTA`` / ``HPC_NO_DEPLOY_CACHE`` opt-outs.
_PRUNE_ENV_KILL = "HPC_NO_DEPLOY_PRUNE"


def _prune_max_files() -> int:
    from hpc_agent.infra.prune import DEFAULT_PRUNE_MAX_FILES

    return _env_int("HPC_DEPLOY_PRUNE_MAX_FILES", DEFAULT_PRUNE_MAX_FILES)


def _prune_max_bytes() -> int:
    from hpc_agent.infra.prune import DEFAULT_PRUNE_MAX_BYTES

    return _env_int("HPC_DEPLOY_PRUNE_MAX_BYTES", DEFAULT_PRUNE_MAX_BYTES)


def _read_prior_push_manifest(
    *, ssh_target: str, remote_path: str, timeout: float | None
) -> set[str]:
    """The set of paths our LAST push recorded at :data:`_PUSH_MANIFEST_REL`.

    One bounded ssh ``cat`` (rides the push's dial). Returns an EMPTY set on any
    problem — a first push (no manifest), a read/parse error, a wrong shape — so
    a manifest we cannot prove routes every remote extra to the ANOMALY branch
    (never deleted). The safe direction: only a path we can PROVE we shipped is
    ever prunable.

    NOTE (delta-push round-trip Option 1, 2026-07-17): the delta push no longer
    calls this — the ``paths`` bookkeeping is now folded into the remote hash
    read (leg A) and threaded through as ``known`` (see
    :func:`hpc_agent.infra.transport._delta._remote_push_manifest`), saving a
    dial. This standalone reader is retained for back-compat: it defines the
    exact fail-open shape the folded read reproduces (absent/garbled ``paths`` ->
    empty set) and remains available to any non-delta / older caller.
    """
    # ``_guarded_ssh_bounded`` is defined in the engine package (``__init__``),
    # which imports THIS module in its re-export block — import it call-time to
    # keep the package's own initialization free of an import cycle. U5
    # breaker/slot uniformity: this back-compat reader rides the breaker + slot
    # like every other dial (was a bare ``_ssh_bounded`` — AUDIT §6 un-guarded).
    from hpc_agent.errors import SshCircuitOpen, SshSlotWaitTimeout
    from hpc_agent.infra.transport import _guarded_ssh_bounded

    quoted = shlex.quote(f"{remote_path.rstrip('/')}/{_PUSH_MANIFEST_REL}")
    try:
        proc = _guarded_ssh_bounded(
            ssh_target,
            f"cat {quoted} 2>/dev/null",
            timeout=timeout,
            what=f"read push manifest of {remote_path}",
        )
    except (TimeoutError, OSError, SshCircuitOpen, SshSlotWaitTimeout):
        # A breaker-open / slot give-up degrades to the SAME empty-set contract
        # as any other read problem: an unprovable manifest routes every remote
        # extra to the ANOMALY branch (never deleted) — fail-open preserved.
        return set()
    raw = (getattr(proc, "stdout", "") or "").strip()
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list):
        return set()
    return {str(p) for p in paths}


#: Remote temp sibling the manifest is written to before the atomic
#: ``os.replace`` swap (run-13 finding 3). Never a prune candidate (filtered
#: beside :data:`_PUSH_MANIFEST_REL`) so a temp left by a torn write never nags as
#: an anomaly extra.
_PUSH_MANIFEST_TMP_REL: Final[str] = _PUSH_MANIFEST_REL + ".tmp"

#: The stdlib-only python the control plane pipes cluster-side to write the push
#: manifest as a read-modify-write that PRESERVES the ``entries`` remote
#: quick-check cache (rank 5) the hash snippet persisted. The snippet (step 1 of a
#: delta push) writes the ``entries``; this writer (the per-batch checkpoints +
#: the final seal, all AFTER the snippet) owns ``paths``/``pkg_version`` and must
#: not clobber ``entries`` or the NEXT push's snippet loses its cache. It reads
#: the payload (``{paths, pkg_version, manifest_schema}``, base64 in
#: ``HPC_PM_PAYLOAD``), folds in any existing ``entries`` list, and swaps
#: atomically (temp + ``os.replace``) — the same crash-safety the shell ``mv``
#: gave, so a severed connection can never leave a corrupt manifest. Kept under
#: the deploy Python floor (stdlib ``os``/``sys``/``json``/``base64`` only).
_PUSH_MANIFEST_MERGE_PY: Final[str] = (
    "import os,sys,json,base64\n"
    "d='.hpc/.push_manifest.json'\n"
    "t=d+'.tmp'\n"
    "new=json.loads(base64.b64decode(os.environ['HPC_PM_PAYLOAD']))\n"
    "try:\n"
    "    with open(d) as f:\n"
    "        cur=json.load(f)\n"
    "except Exception:\n"
    "    cur={}\n"
    "if isinstance(cur,dict) and isinstance(cur.get('entries'),list):\n"
    "    new['entries']=cur['entries']\n"
    "with open(t,'w') as f:\n"
    "    json.dump(new,f)\n"
    "os.replace(t,d)\n"
)


#: Positive-evidence sentinel the FOLDED per-batch checkpoint (delta-push
#: round-trip Option 2) echoes on stdout IFF the batch's ``tar x`` landed AND its
#: push-manifest checkpoint merge recorded. Its PRESENCE proves "batch landed and
#: checkpoint committed"; its ABSENCE — a drop after ``tar x`` before the ack, or
#: a best-effort merge hiccup — is NOT a batch failure (the batch is durable; the
#: ``tar x`` rc stays authoritative), only an un-committed checkpoint the NEXT
#: push re-derives from the live remote hash (fail-open prune lag, Invariant 2).
#: Read back per the positive-evidence discipline: an absent ack is never trusted
#: as "committed" (house discipline 4, run-12 finding 24).
_PUSH_CP_SENTINEL: Final[str] = "__HPC_PUSH_CP_OK__"


def _push_manifest_payload_b64(paths: list[str]) -> str:
    """Base64 JSON payload (``{paths, pkg_version, manifest_schema}``) for the
    push-manifest merge script (:data:`_PUSH_MANIFEST_MERGE_PY`).

    Shared by the standalone :func:`_write_push_manifest` seal/checkpoint dial and
    the FOLDED per-batch checkpoint that rides the tar-push leg (delta-push
    round-trip Option 2, :func:`_folded_checkpoint_cmd`), so the two writers emit a
    byte-identical payload for the same path set — a folded checkpoint and a
    standalone one are indistinguishable on the remote. ``_pkg_version`` is
    re-exported by the engine package; imported call-time so this module never
    depends on the engine at its own import time (no cycle).
    """
    from hpc_agent.infra.transport import _pkg_version

    payload = json.dumps(
        {
            "paths": sorted(paths),
            "pkg_version": _pkg_version(),
            "manifest_schema": _PUSH_MANIFEST_SCHEMA,
        }
    )
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _folded_checkpoint_cmd(remote_path: str, payload_b64: str) -> str:
    """The ack-gated remote-command SUFFIX that rides a delta batch's tar-push leg
    to checkpoint the push manifest IN THE SAME ssh dial (delta-push round-trip
    Option 2 — eliminates the separate per-batch :func:`_write_push_manifest`
    dial).

    Appended by :func:`hpc_agent.infra.transport._tar_ssh_push` to its
    ``delete=False`` extract command (``mkdir -p <r> && tar x -C <r>``) so the
    whole batch leg becomes::

        mkdir -p <r> && tar x -C <r> && { ( cd <r> && mkdir -p .hpc
            && <merge> && printf %s __HPC_PUSH_CP_OK__ ) || true; }

    The shell shape is load-bearing:

    * The checkpoint is ``&&``-GATED on ``tar x`` — a failed extract
      short-circuits it, so ``tar x``'s rc stays AUTHORITATIVE for the batch. The
      caller's rc≠0 early-return / resume path is unchanged, and the checkpoint
      can never MASK a batch failure (the two signals — batch-landed vs
      checkpoint-committed — stay orthogonal).
    * The checkpoint block is wrapped ``{ ( … ) || true; }`` — a merge hiccup can
      NEVER fail an otherwise-good batch (best-effort, exactly like the standalone
      fail-open :func:`_write_push_manifest`); on ``tar x`` success the leg exits
      0 whether or not the checkpoint recorded.
    * The sentinel :data:`_PUSH_CP_SENTINEL` is printed LAST and ONLY after the
      merge ``python3`` succeeds (``… python3 && printf %s <sentinel>``): its
      presence in the leg's stdout is positive evidence the checkpoint committed;
      its absence is a safe re-derive (Invariant 2), never a batch failure.

    Reuses :data:`_PUSH_MANIFEST_MERGE_PY` (the same crash-safe temp+``os.replace``,
    ``entries``-preserving read-modify-write the standalone writer runs) inside a
    ``cd <remote_path>`` subshell so the relative ``.hpc/.push_manifest.json`` it
    writes lands in the deploy tree — matching :func:`_write_push_manifest`'s
    ``cd``. Base64-piped so neither the merge source nor any path needs shell
    quoting; no new cold SSH (rides the batch's dial) and no raw ssh.
    """
    merge_b64 = base64.b64encode(_PUSH_MANIFEST_MERGE_PY.encode("utf-8")).decode("ascii")
    root = shlex.quote(remote_path.rstrip("/"))
    checkpoint = (
        f"cd {root} && mkdir -p .hpc && printf %s {shlex.quote(merge_b64)} | base64 -d | "
        f"HPC_PM_PAYLOAD={shlex.quote(payload_b64)} python3 && "
        f"printf %s {shlex.quote(_PUSH_CP_SENTINEL)}"
    )
    return f" && {{ ( {checkpoint} ) || true; }}"


def _write_push_manifest(
    *, ssh_target: str, remote_path: str, paths: list[str], timeout: float | None
) -> None:
    """Persist the current push's shipped path set at :data:`_PUSH_MANIFEST_REL`.

    Base64-piped so no path needs shell quoting (mirrors the remote-manifest
    snippet). A read-modify-write (:data:`_PUSH_MANIFEST_MERGE_PY`): it updates
    ``paths``/``pkg_version``/``manifest_schema`` while PRESERVING the ``entries``
    remote quick-check cache (rank 5) the hash snippet persisted — this writer
    runs AFTER the snippet on every delta push (per-batch checkpoints + the final
    seal), so a plain overwrite would drop the cache and force the next push's
    snippet to full-re-hash. Written CRASH-SAFELY (run-13 finding 3): the bytes
    land in a temp sibling that is atomically ``os.replace``-d into place, so a
    torn write — a connection severed mid-write — can never leave a corrupt
    manifest that a later prune (or the next snippet) would misread. This matters
    because the push checkpoints the manifest per landed batch (many writes, each
    a crash opportunity), not only once at completion. Fail-open: a write error
    only loses the NEXT push's prune ability + cache (extras degrade to anomalies,
    the tree re-hashes), never breaks this push. Runs stdlib-only ``python3`` — the
    same interpreter the delta path already required to hash the remote tree.
    """
    # ``_guarded_ssh_bounded`` (engine) is re-exported by the package; import it
    # call-time so this module never depends on the engine at its own import time
    # (no cycle). U5 breaker/slot uniformity: the checkpoint/seal write rides the
    # breaker + slot like every other dial (was a bare ``_ssh_bounded`` — AUDIT §6
    # un-guarded). The payload is built by the SHARED
    # :func:`_push_manifest_payload_b64` (byte-identical to the FOLDED per-batch
    # checkpoint, Option 2).
    from hpc_agent.errors import SshCircuitOpen, SshSlotWaitTimeout
    from hpc_agent.infra.transport import _guarded_ssh_bounded

    payload_b64 = _push_manifest_payload_b64(paths)
    merge_b64 = base64.b64encode(_PUSH_MANIFEST_MERGE_PY.encode("utf-8")).decode("ascii")
    root = shlex.quote(remote_path.rstrip("/"))
    cmd = (
        f"cd {root} && mkdir -p .hpc && printf %s {shlex.quote(merge_b64)} | base64 -d | "
        f"HPC_PM_PAYLOAD={shlex.quote(payload_b64)} python3"
    )
    # Fail-open (unchanged): a breaker-open (SshCircuitOpen) or slot-wait give-up
    # (SshSlotWaitTimeout) degrades to the SAME skip as a TimeoutError/OSError —
    # a lost checkpoint/seal only lags the NEXT push's prune ability + cache
    # (extras degrade to anomalies, the tree re-hashes), never a new raise.
    with contextlib.suppress(TimeoutError, OSError, SshCircuitOpen, SshSlotWaitTimeout):
        _guarded_ssh_bounded(
            ssh_target,
            cmd,
            timeout=timeout,
            what=f"write push manifest of {remote_path}",
        )


def _journal_deploy_prune(local_path: str | Path, record: dict[str, Any]) -> None:
    """Append one prune record to ``<experiment>/.hpc/deploy_prune.jsonl``.

    The tier-0 "what we auto-deleted from the cluster, why, and its old sha"
    timeline (the data-manifest mint-journal pattern). Fail-open — a journal
    write must never break a push.
    """
    from hpc_agent.infra.io import append_jsonl_line
    from hpc_agent.infra.time import utcnow_iso

    try:
        path = Path(local_path) / ".hpc" / "deploy_prune.jsonl"
        append_jsonl_line(path, {"ts": utcnow_iso(), **record})
    except OSError:
        pass


#: The stdlib-only python the control plane pipes cluster-side to prune the
#: manifest-known extras AND re-seal the push manifest in ONE trailing leg
#: (delta-push round-trip Option 4 — collapses the former ``rm`` leg + union-seal
#: leg). It removes each proven-ours path (``os.remove``, no shell), computes the
#: retained set REMOTE-SIDE (which paths the delete could NOT remove — a
#: raced/failed delete stays ours, fail-open per-path), then writes the manifest
#: as ``sorted(seal ∪ retained)`` while PRESERVING the ``entries`` remote
#: quick-check cache the hash snippet persisted, atomically (temp + ``os.replace``
#: — the same crash-safety as :data:`_PUSH_MANIFEST_MERGE_PY`, so a severed
#: connection can never leave a corrupt manifest). Reads its payload
#: (``{prune, seal, pkg_version, manifest_schema}``, base64 in ``HPC_PM_PAYLOAD``).
#: Kept under the deploy Python floor (stdlib ``os``/``sys``/``json``/``base64``
#: only). A delete-then-sever leaves the proven-ours extras gone (correct) and the
#: manifest lag re-derives next push; a sever before the delete removes nothing
#: and the retained set stays intact — fail-open either way (Invariant 2).
_PRUNE_RESEAL_PY: Final[str] = (
    "import os,sys,json,base64\n"
    "d='.hpc/.push_manifest.json'\n"
    "t=d+'.tmp'\n"
    "P=json.loads(base64.b64decode(os.environ['HPC_PM_PAYLOAD']))\n"
    "retained=[]\n"
    "for p in P.get('prune',[]):\n"
    "    try:\n"
    "        os.remove(p)\n"
    "    except OSError:\n"
    "        pass\n"
    "    if os.path.lexists(p):\n"
    "        retained.append(p)\n"
    "new={'paths':sorted(set(P.get('seal',[]))|set(retained)),"
    "'pkg_version':P.get('pkg_version'),'manifest_schema':P.get('manifest_schema')}\n"
    "try:\n"
    "    with open(d) as f:\n"
    "        cur=json.load(f)\n"
    "except Exception:\n"
    "    cur={}\n"
    "if isinstance(cur,dict) and isinstance(cur.get('entries'),list):\n"
    "    new['entries']=cur['entries']\n"
    "with open(t,'w') as f:\n"
    "    json.dump(new,f)\n"
    "os.replace(t,d)\n"
)


def _prune_and_reseal(
    *,
    ssh_target: str,
    remote_path: str,
    prune_paths: list[str],
    seal_paths: list[str],
    timeout: float | None,
) -> None:
    """Delete *prune_paths* AND reseal the push manifest in ONE bounded ssh leg.

    The delta-push round-trip Option 4 fold: the prune ``rm`` and the
    retained-union manifest seal — formerly two separate un-guarded dials
    (``_execute_prune`` + :func:`_write_push_manifest`) — collapse into a single
    trailing leg fired ONLY when a prune actually has paths to delete. One
    stdlib-floor ``python3`` script (:data:`_PRUNE_RESEAL_PY`) removes each
    proven-ours path, computes the retained set REMOTE-SIDE (which paths the
    delete could not remove — a raced/failed delete stays ours), and writes the
    manifest as ``sorted(seal_paths ∪ retained)`` while preserving the ``entries``
    remote quick-check cache, atomically.

    Fail-open (Invariant 2): a severed leg after the delete leaves the
    proven-ours extras gone (correct) and the manifest lag re-derives next push; a
    severed leg before the delete removes nothing and the retained set stays
    intact. Every delete target is the vetted manifest-known set (never an
    anomaly). Base64-piped so no path needs shell quoting. Rides the same dial
    discipline as the writer it replaces (no new cold SSH).
    """
    # ``_guarded_ssh_bounded`` (engine) and ``_pkg_version`` (``_deploy_items``)
    # are both re-exported by the package; import them call-time so this module
    # never depends on the engine at its own import time (no cycle). U5
    # breaker/slot uniformity: the prune+reseal tail rides the breaker + slot
    # like every other dial (was a bare ``_ssh_bounded`` — AUDIT §6 un-guarded).
    from hpc_agent.errors import SshCircuitOpen, SshSlotWaitTimeout
    from hpc_agent.infra.transport import _guarded_ssh_bounded, _pkg_version

    payload = json.dumps(
        {
            "prune": list(prune_paths),
            "seal": sorted(set(seal_paths)),
            "pkg_version": _pkg_version(),
            "manifest_schema": _PUSH_MANIFEST_SCHEMA,
        }
    )
    payload_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    reseal_b64 = base64.b64encode(_PRUNE_RESEAL_PY.encode("utf-8")).decode("ascii")
    root = shlex.quote(remote_path.rstrip("/"))
    cmd = (
        f"cd {root} && mkdir -p .hpc && printf %s {shlex.quote(reseal_b64)} | base64 -d | "
        f"HPC_PM_PAYLOAD={shlex.quote(payload_b64)} python3"
    )
    # Fail-open (Invariant 2, unchanged): a breaker-open (SshCircuitOpen) or a
    # slot-wait give-up (SshSlotWaitTimeout) degrades to the SAME skip as a
    # TimeoutError/OSError — a skipped prune leaves the extras un-pruned (they
    # stay manifest-known for the next push) and the manifest lag re-derives from
    # the live remote hash, never a new raise.
    with contextlib.suppress(TimeoutError, OSError, SshCircuitOpen, SshSlotWaitTimeout):
        _guarded_ssh_bounded(
            ssh_target,
            cmd,
            timeout=timeout,
            what=f"prune {len(prune_paths)} manifest-known extra(s) + reseal manifest "
            f"of {remote_path}",
        )
