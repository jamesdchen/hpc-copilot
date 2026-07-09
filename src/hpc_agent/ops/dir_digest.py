"""``dir-digest`` — a BOUNDED, code-rendered digest of a directory tree.

A read-only ``query`` primitive (run-#11 mechanization queue: dir-digest +
context-budget). The premortem's raw-``ls`` habit — "list the results dir and
eyeball it" — is a context-budget hazard: the moment a tree holds thousands of
shards, the listing blows the window and there is nothing bounded to relay. This
verb replaces the listing with NUMBERS computed at the source:

* file/dir counts and total byte size,
* the newest ``N`` entries as ``(relpath, size, mtime)`` (bounded by ``N``),
* an extension histogram capped at the top ~10 groups,
* opt-in per-marker line-hit counts across ``*.log`` / ``*.err`` files, reusing
  :data:`hpc_agent.ops.worker_log_digest.KNOWN_MARKERS` (the engine's own
  vocabulary, one definition) with a bounded per-file read.

Two arms, same bounded shape:

* **LOCAL** (no ``cluster``) — the first-class path. Walks the tree with
  ``os.walk`` under a path confined to the experiment dir (the same containment
  ``worker-log-digest`` enforces). Fail-open on a missing/unreadable root.
* **REMOTE** (``cluster`` set) — ONE throttled ``ssh_run`` carrying a fixed,
  read-only ``find``/``awk``/``sort``/``grep`` pipeline that computes the SAME
  numbers server-side and ships only the digest, never a listing. The probed
  path is confined strictly under the cluster's scratch root (reusing
  :func:`hpc_agent.infra.ssh_validation.validate_remote_path_under_scratch`),
  and the command is wrapped in ``bash -lc`` (login, non-interactive) to match
  the repo's ssh discipline. There is NO caller-supplied command string.

This file lives at the ``ops/`` role root (sibling to ``worker_log_digest.py``
and ``inspect_deployment.py``); it composes no subject internals.
"""

from __future__ import annotations

import heapq
import os
import shlex
from collections import Counter
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.dir_digest import (
    DirDigestResult,
    DirDigestSpec,
    DirEntry,
    HistogramBucket,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.worker_log_digest import KNOWN_MARKERS

__all__ = ["dir_digest"]

#: Cap the number of top extension/name-group buckets in the histogram so the
#: result stays bounded no matter how many distinct extensions a tree holds.
_HISTOGRAM_TOP = 10

#: Bounds on the marker scan so a huge log tree can never unbound the digest:
#: at most this many *.log/*.err files are read, each up to this many bytes.
_MARKER_SCAN_MAX_FILES = 200
_MARKER_SCAN_MAX_BYTES = 1_000_000

#: Echoed cluster-side when the probed path does not exist, so a missing tree is
#: distinguishable from an empty one in the same single ssh round-trip.
_MISSING_SENTINEL = "__HPC_DIRDIGEST_MISSING__"

#: Section headers in the remote pipeline's stdout. Parsing is defensive: a
#: missing section yields zeros/empties, never a crash.
_SEC_COUNTS = "===COUNTS==="
_SEC_NEWEST = "===NEWEST==="
_SEC_HIST = "===HIST==="
_SEC_MARKERS = "===MARKERS==="


def _extension_group(name: str) -> str:
    """Return the histogram group key for a file *name*.

    A lowercased trailing extension incl. the dot (``.json``) when the basename
    has one; ``(noext)`` otherwise. A leading-dot dotfile with no further suffix
    (``.gitignore``) counts as ``(noext)`` — its "extension" is the whole name.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or not ext:
        return "(noext)"
    return f".{ext.lower()}"


def _histogram(names: Counter[str]) -> list[HistogramBucket]:
    """Top ``_HISTOGRAM_TOP`` groups, count-descending then name-ascending (stable)."""
    ordered = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))
    return [HistogramBucket(name=n, count=c) for n, c in ordered[:_HISTOGRAM_TOP]]


def _render(
    *,
    path: str,
    scope: str,
    cluster: str | None,
    readable: bool,
    error: str | None,
    file_count: int,
    dir_count: int,
    total_size_bytes: int,
    newest: list[DirEntry],
    histogram: list[HistogramBucket],
    marker_scan: bool,
    marker_counts: dict[str, int],
    files_scanned_for_markers: int,
) -> str:
    """Build the deterministic, bounded markdown digest (relayed verbatim)."""
    where = f"{cluster}:{path}" if scope == "remote" else path
    lines = [f"### dir digest ({scope}): `{where}`", ""]
    if not readable:
        lines.append(f"**unreadable** — {error}")
        return "\n".join(lines) + "\n"

    lines.append(f"- files: {file_count}")
    lines.append(f"- dirs: {dir_count}")
    lines.append(f"- total size: {total_size_bytes} bytes")
    lines.append("")
    if newest:
        lines.append(f"newest {len(newest)} entr{'y' if len(newest) == 1 else 'ies'} (mtime desc):")
        for e in newest:
            lines.append(f"  - `{e.relpath}` — {e.size} bytes, mtime {e.mtime:.0f}")
    else:
        lines.append("_(newest=0: no entry list requested)_")
    lines.append("")
    if histogram:
        lines.append(f"top {len(histogram)} extension group(s):")
        for b in histogram:
            lines.append(f"  - `{b.name}`: {b.count}")
    else:
        lines.append("_(no files to histogram)_")
    lines.append("")
    if marker_scan:
        lines.append(f"failure markers across *.log/*.err ({files_scanned_for_markers} file(s)):")
        for marker in KNOWN_MARKERS:
            lines.append(f"  - `{marker}`: {marker_counts.get(marker, 0)}")
    else:
        lines.append("_(marker_scan=false: no marker scan requested)_")
    return "\n".join(lines) + "\n"


def _resolve_local_path(experiment_dir: Path, path: str) -> Path:
    """Resolve *path* to an absolute path WITHIN *experiment_dir*.

    Mirrors ``worker_log_digest._resolve_log_path``: a relative path joins onto
    the experiment dir; an absolute path is taken as given; either way the
    resolved path must stay under the experiment dir. A path that escapes is a
    caller-input error (:class:`errors.SpecInvalid`), never a fail-open miss.
    """
    base = experiment_dir.resolve()
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    if not resolved.is_relative_to(base):
        raise errors.SpecInvalid(
            f"dir-digest: path {path!r} resolves to {resolved}, which is outside the "
            f"experiment dir {base}. Pass a path under the experiment dir (or set "
            "--cluster to digest a scratch-confined remote tree)."
        )
    return resolved


def _scan_markers_local(root: Path) -> tuple[dict[str, int], int]:
    """Bounded per-file marker scan over *root*'s ``*.log`` / ``*.err`` files.

    Reuses ``worker-log-digest``'s reading approach — UTF-8, undecodable bytes
    replaced, count lines CONTAINING each known marker — but bounds it: at most
    :data:`_MARKER_SCAN_MAX_FILES` files, each read up to
    :data:`_MARKER_SCAN_MAX_BYTES` bytes. Returns ``(marker_counts, files_read)``
    with every known marker present (0 when absent) so the shape is stable.
    """
    counts = {marker: 0 for marker in KNOWN_MARKERS}
    files_read = 0
    # Deterministic order so the (bounded) file selection is reproducible.
    logs = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".log", ".err")
    )
    for log in logs[:_MARKER_SCAN_MAX_FILES]:
        try:
            with log.open("rb") as fh:
                raw = fh.read(_MARKER_SCAN_MAX_BYTES)
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace")
        files_read += 1
        for line in text.splitlines():
            for marker in KNOWN_MARKERS:
                if marker in line:
                    counts[marker] += 1
    return counts, files_read


def _digest_local(*, resolved: Path, newest_n: int, marker_scan: bool) -> DirDigestResult:
    """Walk *resolved* and compute the bounded digest locally (fail-open)."""

    def _fail_open(*, exists: bool, error: str) -> DirDigestResult:
        return DirDigestResult(
            path=str(resolved),
            cluster=None,
            scope="local",
            exists=exists,
            readable=False,
            error=error,
            newest_requested=newest_n,
            marker_scan=marker_scan,
            render=_render(
                path=str(resolved),
                scope="local",
                cluster=None,
                readable=False,
                error=error,
                file_count=0,
                dir_count=0,
                total_size_bytes=0,
                newest=[],
                histogram=[],
                marker_scan=marker_scan,
                marker_counts={},
                files_scanned_for_markers=0,
            ),
        )

    if not resolved.exists():
        return _fail_open(exists=False, error=f"no such directory: {resolved}")
    if not resolved.is_dir():
        return _fail_open(exists=True, error=f"not a directory: {resolved}")

    file_count = 0
    dir_count = 0
    total_size = 0
    ext_counter: Counter[str] = Counter()

    def _entries() -> Any:
        """Yield ``(mtime, size, relpath)`` for every file/dir under the root."""
        nonlocal file_count, dir_count, total_size
        for dirpath, dirnames, filenames in os.walk(resolved):
            dp = Path(dirpath)
            for d in dirnames:
                dir_count += 1
                full = dp / d
                try:
                    st = full.stat()
                except OSError:
                    continue
                yield (st.st_mtime, st.st_size, str(full.relative_to(resolved)))
            for f in filenames:
                file_count += 1
                ext_counter[_extension_group(f)] += 1
                full = dp / f
                try:
                    st = full.stat()
                except OSError:
                    total_size += 0
                    continue
                total_size += st.st_size
                yield (st.st_mtime, st.st_size, str(full.relative_to(resolved)))

    # heapq.nlargest keeps only N in memory — bounded for an arbitrarily large
    # tree while the counters accumulate over the full walk.
    top = heapq.nlargest(newest_n, _entries()) if newest_n > 0 else _drain(_entries())
    newest = [DirEntry(relpath=rp, size=sz, mtime=mt) for mt, sz, rp in top]
    histogram = _histogram(ext_counter)

    if marker_scan:
        marker_counts, files_scanned = _scan_markers_local(resolved)
    else:
        marker_counts, files_scanned = {}, 0

    return DirDigestResult(
        path=str(resolved),
        cluster=None,
        scope="local",
        exists=True,
        readable=True,
        error=None,
        file_count=file_count,
        dir_count=dir_count,
        total_size_bytes=total_size,
        newest_requested=newest_n,
        newest=newest,
        histogram=histogram,
        marker_scan=marker_scan,
        marker_counts=marker_counts,
        files_scanned_for_markers=files_scanned,
        render=_render(
            path=str(resolved),
            scope="local",
            cluster=None,
            readable=True,
            error=None,
            file_count=file_count,
            dir_count=dir_count,
            total_size_bytes=total_size,
            newest=newest,
            histogram=histogram,
            marker_scan=marker_scan,
            marker_counts=marker_counts,
            files_scanned_for_markers=files_scanned,
        ),
    )


def _drain(it: Any) -> list[tuple[float, int, str]]:
    """Consume *it* fully (drives the counters) but keep nothing — newest=0 case."""
    for _ in it:
        pass
    return []


# --------------------------------------------------------------------------- #
# Remote arm                                                                   #
# --------------------------------------------------------------------------- #


def _resolve_cluster(cluster: str) -> tuple[str, str]:
    """Return ``(ssh_target, scratch)`` for *cluster* — mirrors inspect-deployment.

    Reads ``ssh_target``/``scratch`` from the raw config dict without forcing
    full ``ClusterConfig`` validation (a read must not be blocked by an
    unrelated missing field). Raises :class:`errors.ClusterUnknown` when absent,
    :class:`errors.SpecInvalid` when no ssh_target is derivable.
    """
    from hpc_agent.infra.clusters import load_clusters_config

    clusters = load_clusters_config()
    if cluster not in clusters:
        raise errors.ClusterUnknown(f"unknown cluster {cluster!r}; run `hpc-agent clusters list`")
    cfg = clusters[cluster] or {}
    if not isinstance(cfg, dict):
        raise errors.SpecInvalid(
            f"cluster {cluster!r} entry in clusters.yaml must be a mapping, got "
            f"{type(cfg).__name__}"
        )
    ssh_target = cfg.get("ssh_target")
    if not ssh_target:
        host, user = cfg.get("host"), cfg.get("user")
        ssh_target = f"{user}@{host}" if host and user else None
    if not ssh_target:
        raise errors.SpecInvalid(
            f"cluster {cluster!r} has no derivable ssh_target (host/user unset); "
            "dir-digest --cluster needs an SSH-reachable cluster."
        )
    return str(ssh_target), str(cfg.get("scratch") or "")


def _build_remote_script(*, target: str, newest_n: int, marker_scan: bool) -> str:
    """Compose the fixed, read-only pipeline that computes the digest server-side.

    The ONLY interpolated value is *target* (scratch-confined, shape-validated,
    shell-quoted). No caller command string. Emits sentinel-delimited sections
    that :func:`_parse_remote` reads defensively. All output is BOUNDED: counts
    are a single line, newest is ``head -n N``, the histogram is ``head -n 10``,
    and each marker is one summed line-count.
    """
    q = shlex.quote(target)
    parts = [
        f"if [ ! -e {q} ]; then printf '%s\\n' {shlex.quote(_MISSING_SENTINEL)}; exit 0; fi",
        f"printf '%s\\n' {shlex.quote(_SEC_COUNTS)}",
        # files<TAB>dirs<TAB>bytes over the whole subtree (excluding the root).
        (
            f"find {q} -mindepth 1 -printf '%y\\t%s\\n' 2>/dev/null | "
            "awk -F'\\t' '{ if ($1==\"d\") d++; else { f++; b+=$2 } } "
            'END { printf "%d\\t%d\\t%d\\n", f+0, d+0, b+0 }\''
        ),
        f"printf '%s\\n' {shlex.quote(_SEC_NEWEST)}",
    ]
    if newest_n > 0:
        # mtime<TAB>size<TAB>relpath, newest first, capped at N cluster-side.
        parts.append(
            f"find {q} -mindepth 1 -printf '%T@\\t%s\\t%P\\n' 2>/dev/null | "
            f"sort -rn -k1,1 | head -n {int(newest_n)}"
        )
    parts.append(f"printf '%s\\n' {shlex.quote(_SEC_HIST)}")
    # count<TAB>ext over regular files, top-10 cluster-side.
    parts.append(
        f"find {q} -mindepth 1 -type f -printf '%f\\n' 2>/dev/null | "
        'awk \'{ n=$0; e="(noext)"; p=match(n, /\\.[^./]+$/); '
        "if (p>1) { e=tolower(substr(n,p)) } c[e]++ } "
        'END { for (k in c) printf "%d\\t%s\\n", c[k], k }\' | '
        f"sort -rn -k1,1 | head -n {_HISTOGRAM_TOP}"
    )
    parts.append(f"printf '%s\\n' {shlex.quote(_SEC_MARKERS)}")
    if marker_scan:
        # First line: how many *.log/*.err files exist (bounded report only).
        parts.append(
            f"find {q} -mindepth 1 -type f \\( -name '*.log' -o -name '*.err' \\) "
            f"2>/dev/null | head -n {_MARKER_SCAN_MAX_FILES} | wc -l | "
            "awk '{ printf \"FILES\\t%d\\n\", $1+0 }'"
        )
        # One summed line-hit count per marker (grep -F: brackets are literal).
        for marker in KNOWN_MARKERS:
            qm = shlex.quote(marker)
            count_expr = (
                f"find {q} -mindepth 1 -type f \\( -name '*.log' -o -name '*.err' \\) "
                f"2>/dev/null | head -n {_MARKER_SCAN_MAX_FILES} | tr '\\n' '\\0' | "
                f"xargs -0 grep -hF -e {qm} 2>/dev/null | wc -l"
            )
            parts.append(f"printf '%s\\t%s\\n' {qm} \"$({count_expr})\"")
    return "\n".join(parts)


def _parse_remote_int(field: str, default: int = 0) -> int:
    try:
        return int(field.strip())
    except (ValueError, AttributeError):
        return default


def _parse_remote(
    stdout: str, *, newest_n: int, marker_scan: bool
) -> tuple[int, int, int, list[DirEntry], list[HistogramBucket], dict[str, int], int]:
    """Parse the sentinel-delimited remote stdout into the bounded digest fields.

    Defensive: a missing/garbled section degrades to zeros/empties. Returns
    ``(files, dirs, bytes, newest, histogram, marker_counts, files_scanned)``.
    """
    section: str | None = None
    counts_line: str | None = None
    newest_rows: list[str] = []
    hist_rows: list[str] = []
    marker_rows: list[str] = []
    for raw in stdout.splitlines():
        if raw in (_SEC_COUNTS, _SEC_NEWEST, _SEC_HIST, _SEC_MARKERS):
            section = raw
            continue
        if section == _SEC_COUNTS and counts_line is None and raw.strip():
            counts_line = raw
        elif section == _SEC_NEWEST and raw.strip():
            newest_rows.append(raw)
        elif section == _SEC_HIST and raw.strip():
            hist_rows.append(raw)
        elif section == _SEC_MARKERS and raw.strip():
            marker_rows.append(raw)

    file_count = dir_count = total_size = 0
    if counts_line:
        fields = counts_line.split("\t")
        if len(fields) >= 3:
            file_count = _parse_remote_int(fields[0])
            dir_count = _parse_remote_int(fields[1])
            total_size = _parse_remote_int(fields[2])

    newest: list[DirEntry] = []
    for row in newest_rows[: max(newest_n, 0)]:
        fields = row.split("\t")
        if len(fields) >= 3:
            try:
                mt = float(fields[0])
            except ValueError:
                continue
            newest.append(DirEntry(relpath=fields[2], size=_parse_remote_int(fields[1]), mtime=mt))

    histogram: list[HistogramBucket] = []
    for row in hist_rows[:_HISTOGRAM_TOP]:
        fields = row.split("\t")
        if len(fields) >= 2:
            histogram.append(HistogramBucket(name=fields[1], count=_parse_remote_int(fields[0])))

    marker_counts: dict[str, int] = {}
    files_scanned = 0
    if marker_scan:
        marker_counts = {marker: 0 for marker in KNOWN_MARKERS}
        for row in marker_rows:
            key, _, val = row.partition("\t")
            if key == "FILES":
                files_scanned = _parse_remote_int(val)
            elif key in marker_counts:
                marker_counts[key] = _parse_remote_int(val)

    return file_count, dir_count, total_size, newest, histogram, marker_counts, files_scanned


def _digest_remote(*, cluster: str, path: str, newest_n: int, marker_scan: bool) -> DirDigestResult:
    """Digest a REMOTE tree over ONE throttled ssh read (fail-open on a miss)."""
    from hpc_agent.infra import remote
    from hpc_agent.infra.ssh_validation import validate_remote_path_under_scratch

    ssh_target, scratch = _resolve_cluster(cluster)
    if not scratch:
        raise errors.SpecInvalid(
            f"cluster {cluster!r} declares no scratch root, so a remote --path cannot "
            "be confined; dir-digest --cluster needs a scratch-confined path."
        )
    # Confine strictly under scratch AND shape-check (no metachars / leading dash)
    # so the value is safe to interpolate — the same guard inspect-deployment uses.
    validate_remote_path_under_scratch(path, scratch)

    script = _build_remote_script(target=path, newest_n=newest_n, marker_scan=marker_scan)
    remote_cmd = f"bash -lc {shlex.quote(script)}"
    proc = remote.ssh_run(remote_cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"dir-digest probe to {ssh_target} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )

    stdout = proc.stdout
    if stdout.splitlines()[:1] == [_MISSING_SENTINEL]:
        error = f"no such directory: {path}"
        return DirDigestResult(
            path=path,
            cluster=cluster,
            scope="remote",
            exists=False,
            readable=False,
            error=error,
            newest_requested=newest_n,
            marker_scan=marker_scan,
            render=_render(
                path=path,
                scope="remote",
                cluster=cluster,
                readable=False,
                error=error,
                file_count=0,
                dir_count=0,
                total_size_bytes=0,
                newest=[],
                histogram=[],
                marker_scan=marker_scan,
                marker_counts={},
                files_scanned_for_markers=0,
            ),
        )

    (file_count, dir_count, total_size, newest, histogram, marker_counts, files_scanned) = (
        _parse_remote(stdout, newest_n=newest_n, marker_scan=marker_scan)
    )
    return DirDigestResult(
        path=path,
        cluster=cluster,
        scope="remote",
        exists=True,
        readable=True,
        error=None,
        file_count=file_count,
        dir_count=dir_count,
        total_size_bytes=total_size,
        newest_requested=newest_n,
        newest=newest,
        histogram=histogram,
        marker_scan=marker_scan,
        marker_counts=marker_counts,
        files_scanned_for_markers=files_scanned,
        render=_render(
            path=path,
            scope="remote",
            cluster=cluster,
            readable=True,
            error=None,
            file_count=file_count,
            dir_count=dir_count,
            total_size_bytes=total_size,
            newest=newest,
            histogram=histogram,
            marker_scan=marker_scan,
            marker_counts=marker_counts,
            files_scanned_for_markers=files_scanned,
        ),
    )


@primitive(
    name="dir-digest",
    verb="query",
    side_effects=[
        SideEffect("ssh", "<cluster> when set: one read-only bounded digest probe"),
    ],
    error_codes=[errors.SpecInvalid, errors.RemoteCommandFailed, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Bounded, code-rendered digest of a directory tree — replaces raw "
            "`ls`/`find` in agent prose (a context-budget hazard on large trees). "
            "Reports file/dir counts, total size, the newest N entries, a top-~10 "
            "extension histogram, and (opt-in) per-marker line-hit counts across "
            "*.log/*.err files. LOCAL by default (path confined to the experiment "
            "dir); set --cluster to digest a scratch-confined REMOTE tree over ONE "
            "throttled ssh read (never ships a listing). Fails open on a "
            "missing/unreadable root. The `render` field is relayed verbatim."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=DirDigestSpec,
        schema_ref=SchemaRef(input="dir_digest"),
        requires_ssh=True,
    ),
    agent_facing=True,
)
def dir_digest(*, experiment_dir: Path, spec: DirDigestSpec) -> DirDigestResult:
    """Digest a directory tree deterministically and BOUNDED.

    LOCAL (``spec.cluster is None``): resolves ``spec.path`` within the
    experiment dir and walks it. REMOTE (``spec.cluster`` set): confines
    ``spec.path`` under the cluster scratch root and computes the same numbers
    over one throttled ssh read.

    Fail-open: a missing/unreadable root returns ``readable=False`` with an
    ``error`` string, never a traceback. Only caller-input errors raise —
    :class:`errors.SpecInvalid` (path escapes the sanctioned root / no scratch),
    :class:`errors.ClusterUnknown` (unknown cluster), or
    :class:`errors.RemoteCommandFailed` (ssh transport failure).
    """
    newest_n = int(spec.newest)
    if spec.cluster:
        return _digest_remote(
            cluster=spec.cluster,
            path=spec.path,
            newest_n=newest_n,
            marker_scan=spec.marker_scan,
        )
    resolved = _resolve_local_path(Path(experiment_dir), spec.path)
    return _digest_local(resolved=resolved, newest_n=newest_n, marker_scan=spec.marker_scan)
