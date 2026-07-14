"""Content-hash push delta: local + remote manifests for the rsync-less path.

The tar full-copy fallback has no delta, so it re-ships the whole tree even when
the remote is byte-identical (the run-#11 8.4 GB re-ship). This module builds the
two content manifests the delta diffs: the local one (over the exclude-filtered
push tree) and the remote one (the deployed runtime hashes its own tree in one
bounded ssh round-trip, via :data:`_REMOTE_MANIFEST_SNIPPET`). Both sides use the
same :func:`_path_excluded` file-set test so they describe the same tree.
"""

from __future__ import annotations

import base64
import json
import shlex
import sys
import textwrap
from pathlib import Path
from typing import Any, Final

from ._excludes import _pushable_relpaths

#: Cap on the remote hash manifest's file count. A delta needs one sha per
#: file shipped back over the (slow) link; past this the manifest stops being
#: "bounded output" and the push falls back to the full tar (disclosed) rather
#: than pull a pathological payload back. The pushable code/data tree is small
#: (run output dirs are excluded), so a real push never approaches this.
_DELTA_MANIFEST_FILE_CAP: Final[int] = 100_000

#: Env kill-switch: set ``HPC_NO_DEPLOY_DELTA=1`` to force the whole-tree tar
#: copy on rsync-less hosts even when a remote manifest is available (mirrors
#: ``HPC_NO_DEPLOY_CACHE`` for :func:`deploy_runtime`). The full-copy disclosure
#: then names this as the reason.
_DELTA_ENV_KILL = "HPC_NO_DEPLOY_DELTA"

#: The self-contained python the DEPLOYED runtime runs cluster-side to hash its
#: own tree — the "remote side hashes its deployed tree, shipped back as a hash
#: manifest" half of item 6b. Stdlib-only so it runs under any cluster ``python3``
#: without the framework installed; base64-piped over one ssh round-trip so no
#: quoting of the source is needed. It mirrors :func:`_path_excluded` and
#: :class:`Manifest`'s content-hash exactly, so local and remote agree on both
#: the file set and each file's identity. Emits ``{"files": [...]}`` (the
#: :meth:`Manifest.from_dict` shape); prints nothing — routing the caller to the
#: full-copy fallback — on any error, a first/absent tree, or a file count past
#: the cap.
_REMOTE_MANIFEST_SNIPPET = textwrap.dedent(
    """
    import os, sys, json, hashlib, fnmatch
    try:
        pats = [str(p).rstrip('/') for p in json.loads(os.environ.get('HPC_DELTA_EXCLUDES', '[]'))]
        cap = int(os.environ.get('HPC_DELTA_CAP', '100000'))

        def excluded(parts):
            rel = '/'.join(parts)
            for pat in pats:
                if pat.startswith('./') or pat.startswith('^'):
                    a = pat[2:] if pat.startswith('./') else pat[1:]
                    if parts and fnmatch.fnmatch(parts[0], a):
                        return True
                    continue
                if '/' in pat:
                    if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + '/*'):
                        return True
                    continue
                for part in parts:
                    if fnmatch.fnmatch(part, pat):
                        return True
            return False

        files = []
        for dp, dirs, names in os.walk('.'):
            rel = '' if dp == '.' else os.path.relpath(dp, '.').replace(os.sep, '/')
            base = tuple(rel.split('/')) if rel else ()
            dirs[:] = [d for d in dirs if not excluded(base + (d,))]
            for n in names:
                parts = base + (n,)
                if excluded(parts):
                    continue
                full = os.path.join(dp, n)
                if not os.path.isfile(full):
                    continue
                try:
                    h = hashlib.sha256()
                    with open(full, 'rb') as fh:
                        for chunk in iter(lambda: fh.read(1048576), b''):
                            h.update(chunk)
                    size = os.path.getsize(full)
                except OSError:
                    continue
                files.append({'path': '/'.join(parts), 'size': size, 'sha256': h.hexdigest()})
                if len(files) > cap:
                    sys.exit(0)  # too big -> no output -> caller ships the whole tree
        sys.stdout.write(json.dumps({'files': files}))
    except Exception:
        pass
    """
).strip()


def _local_push_manifest(local_path: str | Path, exclude: list[str]) -> Any:
    """Content manifest of the local push tree (exclude-filtered) — item 6b.

    Returns a :class:`hpc_agent.infra.manifest.Manifest`; imported lazily
    to keep this low-level infra module import-light.
    """
    from hpc_agent.infra.manifest import build_manifest

    root = Path(local_path)
    paths = _pushable_relpaths(root, exclude)
    # Phase disclosure (run-#12 finding 3): hashing a multi-GB tree is
    # MINUTES of silence otherwise — the 8.7GB scan read as a hang twice in
    # one night. One line in, one line out, same stderr surface as the
    # transfer heartbeat.
    print(
        f"[transport] content-hash scan: hashing {len(paths)} local file(s) "
        "for the push delta (minutes on a large tree; transfer follows)",
        file=sys.stderr,
    )
    manifest = build_manifest(root, paths=paths)
    print(
        f"[transport] content-hash scan done ({len(paths)} file(s)); "
        "comparing against the remote manifest",
        file=sys.stderr,
    )
    return manifest


def _parse_remote_push_manifest(stdout: str) -> Any | None:
    """Parse the cluster-side hash manifest, or ``None`` on any problem.

    An absent/empty tree (snippet printed nothing), corrupt JSON, a wrong shape,
    or a cap breach all collapse to ``None`` — which routes the push to the
    full-copy tar fallback (disclosed). The safe direction: never claim a remote
    file is present unless the manifest proves it.
    """
    from hpc_agent.infra.manifest import Manifest

    raw = (stdout or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not (isinstance(data, dict) and isinstance(data.get("files"), list)):
        return None
    try:
        return Manifest.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _remote_push_manifest(
    *, ssh_target: str, remote_path: str, exclude: list[str], timeout: float | None
) -> Any | None:
    """One bounded ssh round-trip: the deployed runtime hashes the remote tree.

    Ships :data:`_REMOTE_MANIFEST_SNIPPET` base64-piped into ``python3`` under
    ``remote_path`` and parses the JSON manifest it prints. Returns a
    :class:`Manifest` of the remote tree, or ``None`` when the remote can't
    produce one — a first deploy (``cd`` fails, absent tree), a pre-delta
    runtime, a python/base64 gap, a cap breach, or a timeout. ``None`` routes to
    the full-copy fallback (disclosed), so this is never worse than the prior
    whole-tree behavior. *remote_path* is ``shlex.quote``-d; the snippet is
    base64 (no shell metacharacters) so no source quoting is needed.
    """
    # ``_ssh_bounded`` is defined in the engine package (``__init__``), which
    # imports THIS module in its re-export block — import it call-time to keep
    # the package's own initialization free of an import cycle.
    from hpc_agent.infra.transport import _ssh_bounded

    b64 = base64.b64encode(_REMOTE_MANIFEST_SNIPPET.encode("utf-8")).decode("ascii")
    excludes_json = json.dumps([p.rstrip("/") for p in exclude])
    remote_cmd = (
        f"cd {shlex.quote(remote_path)} && printf %s {shlex.quote(b64)} | base64 -d | "
        f"HPC_DELTA_EXCLUDES={shlex.quote(excludes_json)} "
        f"HPC_DELTA_CAP={_DELTA_MANIFEST_FILE_CAP} python3"
    )
    try:
        proc = _ssh_bounded(
            ssh_target,
            remote_cmd,
            timeout=timeout,
            what=f"remote hash manifest of {remote_path}",
        )
    except (TimeoutError, OSError):
        return None
    return _parse_remote_push_manifest(getattr(proc, "stdout", "") or "")
