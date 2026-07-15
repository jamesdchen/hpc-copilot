"""Pre-transfer disclosure lines + the byte-counting progress pump.

Every ``[transport]`` stderr line that names what a push is about to do — the
ship-size WARN, the no-rsync full-copy cost, the content-hash delta saving, the
auto-prune outcome — lives here, alongside the ``tar | ssh`` progress pump that
makes an otherwise-silent multi-hour transfer observable. All disclosures are
best-effort and fail-open: a disclosure error never blocks a push.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, Final

from ._excludes import _path_excluded

#: Payload size above which the pre-push disclosure escalates to a WARN line —
#: run-#10 finding F-E: a 3.8G artifact tree rode a deploy silently into a
#: 30-minute timeout. Disclosure only (never blocking): the no-silent-caps rule.
_PAYLOAD_WARN_BYTES = 200 * 1024 * 1024
#: Walk bound so disclosure itself stays cheap on pathological trees.
_PAYLOAD_WALK_CAP = 50_000


def _disclose_payload(local_path: str | Path, exclude: list[str]) -> int:
    """One stderr line naming what this push is about to ship (F-E).

    Approximates the transfer's own filtering: a path is skipped when ANY
    path part (or its relpath) fnmatch-es an exclude pattern — the same
    bare-name-at-any-depth semantics tar/rsync apply (the semantics whose
    misreading cost the run-#10 src/data drop; the disclosure makes them
    VISIBLE before the bytes move). Best-effort and fail-open: a disclosure
    error never blocks a push.

    Returns the total payload size in bytes (0 on any error) so the caller can
    reuse it as the transfer-progress denominator (queue item 10) without a
    second tree walk. A walk-capped total is a lower bound; the progress line's
    ``~`` prefix already reads as an estimate.
    """
    try:
        pats = [p.rstrip("/") for p in exclude]
        total = 0
        count = 0
        capped = False
        # Bare-pattern collision detector (run-#10 F-H): a bare name matches
        # at ANY depth, so excluding "data" also drops "src/data" from the
        # ship. Record every DISTINCT subtree each bare pattern hits; >1
        # subtree = the collision warning below.
        bare_hits: dict[str, set[str]] = {}
        root = Path(local_path)
        for p in root.rglob("*"):
            if count >= _PAYLOAD_WALK_CAP:
                capped = True
                break
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            excluded = _path_excluded(parts, pats)
            if excluded:
                # Record which bare pattern(s) hit which subtree, for the
                # anchor-collision WARN below (only bare names alias across
                # subtrees; anchored patterns are top-level by construction).
                for i, part in enumerate(parts):
                    for pat in pats:
                        if pat.startswith("./") or pat.startswith("^"):
                            continue
                        if fnmatch.fnmatch(part, pat) and "/" not in pat and "\\" not in pat:
                            bare_hits.setdefault(pat, set()).add("/".join(parts[: i + 1]))
                continue
            if p.is_file():
                count += 1
                with contextlib.suppress(OSError):
                    total += p.stat().st_size
        mb = total / (1024 * 1024)
        prefix = "WARN deploy payload" if total > _PAYLOAD_WARN_BYTES else "deploy payload"
        suffix = " (walk capped; true size is larger)" if capped else ""
        print(
            f"[transport] {prefix}: {count} files, {mb:.1f} MB{suffix}; "
            f"excludes: {', '.join(sorted(pats)) or '(none)'}",
            file=sys.stderr,
        )
        for pat, subtrees in sorted(bare_hits.items()):
            if len(subtrees) > 1:
                named = ", ".join(sorted(subtrees)[:4])
                print(
                    f"[transport] WARN bare exclude {pat!r} matches {len(subtrees)} "
                    f"distinct subtrees ({named}) — a bare name excludes at ANY "
                    f"depth; anchor it (e.g. './{pat}') if you meant only the "
                    "top-level one.",
                    file=sys.stderr,
                )
        return total
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        return 0


def _disclose_no_rsync(total_bytes: int, *, reason: str = "") -> None:
    """One WARN naming the tar full-copy fallback's cost (queue item 6a).

    Fired at transfer start whenever the push takes the full-copy tar path,
    alongside the :func:`_disclose_payload` WARN. The run-#11 evidence: an 8.4 GB
    tree silently re-shipped to CARC in full because no rsync was on PATH — the
    tar fallback has NO delta, so every byte crosses the wire even when the
    remote is byte-identical, and nothing said so. This makes the cause visible
    before the multi-hour transfer, in the same ``[transport]`` style as the
    payload WARN.

    *reason* (queue item 6b) names WHY the full copy ran rather than the
    content-hash delta — a first deploy, a pre-delta cluster runtime, or the
    kill-switch — so the disclosure says which mode ran and why. Fail-open
    (ASCII arrows so a cp1252 console can't raise): disclosure never blocks a
    push.
    """
    try:
        mb = total_bytes / (1024 * 1024)
        why = f" ({reason})" if reason else ""
        print(
            f"[transport] WARN no rsync on PATH -> tar full-copy fallback -> NO DELTA "
            f"-> the full {mb:.1f} MB re-ships even if the remote is identical{why} "
            f"(install rsync, or WSL/MSYS rsync on Windows, to ship only changed bytes).",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        pass


def _disclose_delta_mode(
    *, shipped_bytes: int, total_bytes: int, n_ship: int, n_local: int, n_reused: int
) -> None:
    """One line naming the content-hash DELTA the rsync-less push took (item 6b).

    Fired when a remote hash manifest WAS available, so the tar fallback ships
    only the changed/new files instead of the whole tree (the run-#11 8.4 GB
    re-ship). Says which mode ran (delta) and its saving, and that the delta is
    additive — stale remote files are not pruned (deletion is out of scope; an
    rsync ``--delete`` is the tool for that). Fail-open like the sibling
    disclosures.
    """
    try:
        if n_ship == 0:
            print(
                f"[transport] no rsync on PATH -> content-hash DELTA: the remote is "
                f"already identical for all {n_local} files; shipping 0 bytes.",
                file=sys.stderr,
            )
            return
        mb_ship = shipped_bytes / (1024 * 1024)
        mb_total = total_bytes / (1024 * 1024)
        print(
            f"[transport] no rsync on PATH -> content-hash DELTA: {n_reused}/{n_local} files "
            f"already on the remote by content-hash; shipping {n_ship} changed/new "
            f"({mb_ship:.1f} MB of {mb_total:.1f} MB). Additive only: stale remote files are "
            f"NOT pruned (install rsync for --delete).",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        pass


def _disclose_prune(plan: Any, *, remote_path: str) -> None:
    """One ``[transport]`` line per prune outcome (disclosure, never blocking).

    Names the manifest-known deletes, the refusal (over-bound), and every
    ANOMALY the push refuses to touch — the "surface to ask" half of the ruling.
    Fail-open like the sibling delta disclosures.
    """
    try:
        if plan.refused:
            print(
                f"[transport] WARN deploy prune REFUSED: {plan.refuse_reason} "
                f"({len(plan.prunable)} manifest-known extras, {plan.prune_bytes} bytes, "
                f"on {remote_path}). Nothing pruned — review and re-push, or raise the cap "
                f"(HPC_DEPLOY_PRUNE_MAX_FILES / HPC_DEPLOY_PRUNE_MAX_BYTES).",
                file=sys.stderr,
            )
        elif plan.to_prune:
            print(
                f"[transport] deploy prune: deleting {len(plan.to_prune)} manifest-known "
                f"remote extra(s) ({plan.prune_bytes} bytes) no longer in the deploy set "
                f"(journaled to .hpc/deploy_prune.jsonl).",
                file=sys.stderr,
            )
        if plan.anomalies:
            named = ", ".join(plan.anomalies[:5])
            more = "" if len(plan.anomalies) <= 5 else f" (+{len(plan.anomalies) - 5} more)"
            print(
                f"[transport] WARN deploy prune ANOMALY: {len(plan.anomalies)} remote file(s) "
                f"not manifest-known — NOT deleted, needs a human decision: {named}{more}.",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        pass


#: Transfer-progress heartbeat cadence (queue item 10). The tar|ssh pipe emits
#: nothing until it exits, so a multi-hour full re-ship looked hung; a line every
#: ~15s to the detached-worker log makes the transfer observable. Override for
#: tests via the ``interval_sec`` arg on :func:`_pump_with_progress`.
_PROGRESS_INTERVAL_SEC: Final[float] = 15.0
#: Pump read/write granularity. 1 MiB balances syscall overhead against the
#: heartbeat's byte-count resolution; binary-safe regardless of value.
_PUMP_CHUNK_BYTES: Final[int] = 1024 * 1024


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*, looping over partial ``os.write``s.

    ``os.write`` may write fewer bytes than offered (a full pipe buffer), so a
    single call can silently truncate the stream. The memoryview slice avoids
    re-copying the tail on each iteration.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


#: Bound on the child stderr tail folded into a failure disclosure. An ssh/scp
#: death can spew (host-key prompts, per-file errors); the tail carries the
#: actual "Connection reset" / "lost connection" story a post-mortem needs.
_CHILD_STDERR_TAIL_CHARS: Final[int] = 4000


def disclose_child_failure(*, what: str, returncode: int, stderr: str | None) -> None:
    """One ``[transport]`` line recording a dead ssh/scp child (run-#13 finding 2).

    The child-process runner captures ssh/scp stderr into the returned
    ``CompletedProcess``, but on a non-zero death that stderr was never written to
    the worker log — a VPN-severed ``scp`` left a "lost connection" story in
    ``proc.stderr`` that nobody recorded, so the log's last line was a stale
    progress line and the failure was undiagnosable. This flushes the child's exit
    status + a bounded stderr tail to the log at the moment the child dies, so the
    story is on the tail-able surface. Best-effort and fail-open: disclosure never
    blocks or re-raises on the transport path.
    """
    try:
        tail = (stderr or "").strip()
        truncated = len(tail) > _CHILD_STDERR_TAIL_CHARS
        if truncated:
            tail = tail[-_CHILD_STDERR_TAIL_CHARS:]
        head = f"[transport] child {what} exited {returncode}; stderr tail"
        head += " (truncated)" if truncated else ""
        body = f"\n{tail}" if tail else " (no stderr captured)"
        print(f"{head}:{body}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        pass


def _pump_with_progress(
    src: IO[bytes],
    dst_fd: int,
    *,
    total_bytes: int,
    interval_sec: float = _PROGRESS_INTERVAL_SEC,
    chunk_size: int = _PUMP_CHUNK_BYTES,
    now: Callable[[], float] = time.monotonic,
) -> int:
    """Copy *src* to *dst_fd* in chunks, emitting a progress heartbeat (item 10).

    Interposed on the ``tar c | ssh tar x`` pipe so the otherwise-silent transfer
    reports ``[transport] progress: X MB / ~Y MB (Z%), elapsed Ts`` every
    ~*interval_sec* to stderr (the detached-worker log — the tail-able surface).
    *total_bytes* is the estimate :func:`_disclose_payload` already computed; a 0
    total prints ``0%`` rather than dividing by zero.

    Transfer-semantics-preserving: reads/writes raw bytes (binary-safe), and
    :func:`_write_all` blocks on a full pipe so backpressure flows to ``tar``
    exactly as a direct fd hand-off would. Returns the byte count forwarded.
    Always closes *dst_fd* on exit — that EOF is what tells the remote ``tar x``
    the stream is complete.
    """
    start = now()
    last_emit = start
    sent = 0
    try:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                break
            _write_all(dst_fd, chunk)
            sent += len(chunk)
            current = now()
            if current - last_emit >= interval_sec:
                _emit_progress(sent, total_bytes, start, current)
                last_emit = current
    finally:
        with contextlib.suppress(OSError):
            os.close(dst_fd)
    return sent


def _emit_progress(sent: int, total_bytes: int, start: float, current: float) -> None:
    """Print one ``[transport] progress: ...`` heartbeat line to stderr."""
    sent_mb = sent / (1024 * 1024)
    total_mb = total_bytes / (1024 * 1024)
    pct = (100 * sent / total_bytes) if total_bytes > 0 else 0.0
    elapsed = current - start
    print(
        f"[transport] progress: {sent_mb:.0f} MB / ~{total_mb:.0f} MB "
        f"({pct:.0f}%), elapsed {elapsed:.0f}s",
        file=sys.stderr,
    )
