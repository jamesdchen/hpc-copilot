"""``worker-log-digest`` — a code-rendered digest of a LOCAL worker log.

A read-only ``query`` primitive (run-#10 finding G2). The premortem told the LLM
to open raw worker logs and eye them for ``[throttle]`` / ``[fatal]`` markers —
an unmechanized reading of untrusted log text, the run-#9 judgment-in-prose
strike class. This verb mechanizes that scan: given a local log path (a
detached-worker log lands under ``.hpc/_detached/``), it counts the lines
carrying each KNOWN engine marker, reports the total, echoes the last N lines
VERBATIM, and renders a markdown projection the caller relays without
interpreting.

No SSH: a local file only. Fail-open on a missing/unreadable file — a clear
diagnostic in the envelope, never a traceback.

Where the marker vocabulary comes from
--------------------------------------
The markers are DERIVED from what the engine actually emits — one definition
here, cited to the emitting modules (the library-knowledge boundary: this is
OUR engine's own vocabulary, not a third party's):

* The per-task **dispatcher** (:mod:`hpc_agent.execution.mapreduce.dispatch`)
  tags every line it prints with the ``[dispatch]`` prefix and a severity word:
  ``FATAL`` (~L814, stale-WIP clean failure), ``FAILED`` (~L991 / ~L1035, the
  task-failure verdicts), ``ERROR`` (the sidecar / contract guards), and
  ``WARN``. This is the compute-node worker whose per-task cluster log is the
  primary "worker log".
* The SSH **connection engine** (:mod:`hpc_agent.infra.ssh_engine`, ~L490) tags
  a throttled connect with ``[throttle]`` — the exact marker the premortem's G2
  named. Detached workers dial through this engine, so their logs carry it.

Counting lines that CONTAIN each marker turns the premortem's manual grep into a
deterministic reduction. The list lives in :data:`KNOWN_MARKERS`; matched
case-sensitively because the engine emits these exact spellings.

This file lives at the ``ops/`` role root (sibling to ``trace.py`` /
``notebook_status.py``): it is a self-contained local-file read that composes no
subject internals.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.worker_log_digest import (
    WorkerLogDigestResult,
    WorkerLogDigestSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef

__all__ = ["KNOWN_MARKERS", "worker_log_digest"]

#: The engine's own bracket vocabulary, derived from the emitting modules (see
#: the module docstring for the one-definition citation). More specific markers
#: are listed first, but each is counted independently, so order is cosmetic.
KNOWN_MARKERS: tuple[str, ...] = (
    "[throttle]",
    "[dispatch] FATAL",
    "[dispatch] FAILED",
    "[dispatch] ERROR",
    "[dispatch] WARN",
)


def _resolve_log_path(experiment_dir: Path, log_path: str) -> Path:
    """Resolve *log_path* to an absolute path WITHIN *experiment_dir*.

    A relative path is joined onto the experiment dir; an absolute path is taken
    as given. Either way the resolved path must stay under the experiment dir —
    a path that escapes it is a caller-input error (:class:`errors.SpecInvalid`),
    not a fail-open miss: the verb reads only this experiment's worker logs, and
    refusing traversal keeps an errant/hostile relpath from tailing arbitrary
    files.
    """
    base = experiment_dir.resolve()
    candidate = Path(log_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    if not resolved.is_relative_to(base):
        raise errors.SpecInvalid(
            f"worker-log-digest: log_path {log_path!r} resolves to {resolved}, which is "
            f"outside the experiment dir {base}. Pass a path under the experiment dir "
            "(detached-worker logs live under .hpc/_detached/)."
        )
    return resolved


def _render(
    *,
    log_path: Path,
    exists: bool,
    readable: bool,
    error: str | None,
    total_lines: int,
    marker_counts: dict[str, int],
    tail: list[str],
    tail_lines_requested: int,
) -> str:
    """Build the deterministic markdown digest (relayed verbatim)."""
    lines = [f"### worker-log digest: `{log_path}`", ""]
    if not readable:
        lines.append(f"**unreadable** — {error}")
        return "\n".join(lines) + "\n"

    lines.append(f"- total lines: {total_lines}")
    lines.append("- markers (lines containing each):")
    for marker in KNOWN_MARKERS:
        lines.append(f"  - `{marker}`: {marker_counts.get(marker, 0)}")
    lines.append("")
    shown = len(tail)
    if tail_lines_requested == 0:
        lines.append("_(tail_lines=0: no verbatim tail requested)_")
    else:
        lines.append(f"last {shown} line(s), verbatim:")
        lines.append("")
        # A ~~~~ fence (four tildes) so a triple-backtick INSIDE the log can't
        # prematurely close the block when the markdown is relayed.
        lines.append("~~~~text")
        lines.extend(tail)
        lines.append("~~~~")
    return "\n".join(lines) + "\n"


@primitive(
    name="worker-log-digest",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Code-rendered digest of a LOCAL worker log (no SSH). Counts lines "
            "carrying each known engine marker ([throttle], [dispatch] "
            "FATAL/FAILED/ERROR/WARN), reports the total line count, and echoes "
            "the last tail_lines lines verbatim in a fenced block. Fails open on "
            "a missing/unreadable file (a clear diagnostic, never a traceback). "
            "Mechanizes the premortem's manual raw-log scan (run-#10 G2). The "
            "`render` field is relayed verbatim."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=WorkerLogDigestSpec,
        schema_ref=SchemaRef(input="worker_log_digest"),
    ),
    agent_facing=True,
)
def worker_log_digest(*, experiment_dir: Path, spec: WorkerLogDigestSpec) -> WorkerLogDigestResult:
    """Digest a local worker log deterministically.

    Resolves ``spec.log_path`` to a path within the experiment dir, reads it
    (UTF-8, undecodable bytes replaced so a partly-binary log never crashes the
    read), counts the lines containing each :data:`KNOWN_MARKERS` entry, and
    echoes the last ``spec.tail_lines`` lines verbatim. Returns a stable-shaped
    :class:`WorkerLogDigestResult` whose ``render`` markdown the caller relays
    verbatim.

    Fail-open: a missing file or an OS read error does NOT raise — it returns a
    result with ``readable=False`` and an ``error`` string. Only a caller-input
    error (a ``log_path`` that escapes the experiment dir) raises
    :class:`errors.SpecInvalid`.
    """
    resolved = _resolve_log_path(Path(experiment_dir), spec.log_path)
    tail_n = int(spec.tail_lines)

    def _fail_open(*, exists: bool, error: str) -> WorkerLogDigestResult:
        return WorkerLogDigestResult(
            log_path=str(resolved),
            exists=exists,
            readable=False,
            error=error,
            total_lines=0,
            tail_lines_requested=tail_n,
            marker_counts={},
            tail=[],
            render=_render(
                log_path=resolved,
                exists=exists,
                readable=False,
                error=error,
                total_lines=0,
                marker_counts={},
                tail=[],
                tail_lines_requested=tail_n,
            ),
        )

    if not resolved.exists():
        return _fail_open(exists=False, error=f"no such file: {resolved}")
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _fail_open(exists=True, error=f"could not read log: {exc}")

    # splitlines() drops the trailing newline and never yields a phantom empty
    # final line, so the count matches what a human sees in an editor.
    all_lines = text.splitlines()
    marker_counts = {
        marker: sum(1 for line in all_lines if marker in line) for marker in KNOWN_MARKERS
    }
    tail = all_lines[-tail_n:] if tail_n > 0 else []
    return WorkerLogDigestResult(
        log_path=str(resolved),
        exists=True,
        readable=True,
        error=None,
        total_lines=len(all_lines),
        tail_lines_requested=tail_n,
        marker_counts=marker_counts,
        tail=tail,
        render=_render(
            log_path=resolved,
            exists=True,
            readable=True,
            error=None,
            total_lines=len(all_lines),
            marker_counts=marker_counts,
            tail=tail,
            tail_lines_requested=tail_n,
        ),
    )
