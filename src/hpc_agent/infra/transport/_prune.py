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
    """
    # ``_ssh_bounded`` is defined in the engine package (``__init__``), which
    # imports THIS module in its re-export block — import it call-time to keep
    # the package's own initialization free of an import cycle.
    from hpc_agent.infra.transport import _ssh_bounded

    quoted = shlex.quote(f"{remote_path.rstrip('/')}/{_PUSH_MANIFEST_REL}")
    try:
        proc = _ssh_bounded(
            ssh_target,
            f"cat {quoted} 2>/dev/null",
            timeout=timeout,
            what=f"read push manifest of {remote_path}",
        )
    except (TimeoutError, OSError):
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
    # ``_ssh_bounded`` (engine) and ``_pkg_version`` (``_deploy_items``) are both
    # re-exported by the package; import them call-time so this module never
    # depends on the engine at its own import time (no cycle).
    from hpc_agent.infra.transport import _pkg_version, _ssh_bounded

    payload = json.dumps(
        {
            "paths": sorted(paths),
            "pkg_version": _pkg_version(),
            "manifest_schema": _PUSH_MANIFEST_SCHEMA,
        }
    )
    payload_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    merge_b64 = base64.b64encode(_PUSH_MANIFEST_MERGE_PY.encode("utf-8")).decode("ascii")
    root = shlex.quote(remote_path.rstrip("/"))
    cmd = (
        f"cd {root} && mkdir -p .hpc && printf %s {shlex.quote(merge_b64)} | base64 -d | "
        f"HPC_PM_PAYLOAD={shlex.quote(payload_b64)} python3"
    )
    with contextlib.suppress(TimeoutError, OSError):
        _ssh_bounded(
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


def _execute_prune(
    *, ssh_target: str, remote_path: str, paths: list[str], timeout: float | None
) -> bool:
    """Delete exactly *paths* under *remote_path* via one bounded ssh ``rm``.

    Each path is ``shlex.quote``-d and the list is the vetted manifest-known set
    (never anomalies, never over-bound). ``rm -f --`` is 0 even if a path already
    vanished. Returns True on a clean delete. Fail-open on any transport error —
    the manifest-known extra simply survives to the next push.
    """
    from hpc_agent.infra.transport import _ssh_bounded

    if not paths:
        return False
    root = shlex.quote(remote_path.rstrip("/"))
    quoted_paths = " ".join(shlex.quote(p) for p in paths)
    try:
        proc = _ssh_bounded(
            ssh_target,
            f"cd {root} && rm -f -- {quoted_paths}",
            timeout=timeout,
            what=f"prune {len(paths)} manifest-known extra(s) from {remote_path}",
        )
    except (TimeoutError, OSError):
        return False
    return getattr(proc, "returncode", 1) == 0
