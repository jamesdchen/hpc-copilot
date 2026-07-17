"""Candidate input-data-root detection — the shared engine for the data-leg
deepening (``docs/design/data-leg-deepening.md`` option (a)).

Scans the experiment repo for **data-shaped** top-level directories the executor
likely reads and surfaces them as UNCONFIRMED CANDIDATES — the exact
``ops/detect_entry_point.py::_scan_candidates`` posture the doctrine anchor
demands: **scan the repo, DISCLOSE candidates, let the human confirm — never
silently assume or capture.** This engine PROPOSES; it never mints a manifest,
never writes anything, and never decides a directory is data. Capture stays the
human's confirm (declaring ``audited_source.input_roots``) — so this does NOT
cross the ``state/data_manifest.py::declared_input_roots`` "core never guesses
which directories are data" line.

Composition (not re-invention): the walk is the SAME exclude-filtered,
walk-capped, fail-open repo walk the deploy disclosure uses, exposed publicly as
:func:`hpc_agent.infra.transport.iter_exclude_filtered_files` — so the exclude
vocabulary (cluster run-output dirs, the credential file, framework runtime
files, ``.venv`` / ``node_modules``) can never drift from what the push honors.
The only new logic is the data-shape CLASSIFIER over that walk.

Three data-shape signals (``docs/design/data-leg-deepening.md`` §(a)), each an
IDENTITY/COUNTING classification over opaque file names — no format is ever
parsed, no third-party library is imported (the agnosticism boundary, mirroring
``state/data_manifest.py``):

* a top-level directory with a **conventional data name**
  (:data:`CONVENTIONAL_DATA_DIR_NAMES`);
* a top-level directory containing a file with a **data-shaped suffix**
  (:data:`DATA_SHAPED_SUFFIXES`);
* a **DVC pointer** — a top-level ``<name>.dvc`` file names ``<name>`` as a
  (possibly not-yet-pulled) data target.

**Residual (named, never hidden):** a data directory that is neither
conventionally named nor holds a telltale extension, and — the standing blind
spot every repo-scan option shares — any data read from OUTSIDE the repo (an
absolute path, ``/scratch``, a network mount, an S3/DB URL) is invisible here.
The coverage disclosure (``ops/submit_blocks.py::_input_data_brief``) names that
residual at the human boundary; this engine only supplies the candidate set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hpc_agent.infra.transport import iter_exclude_filtered_files

__all__ = [
    "CONVENTIONAL_DATA_DIR_NAMES",
    "DATA_SHAPED_SUFFIXES",
    "CandidateDataRoot",
    "detect_candidate_data_roots",
]

#: Conventional data-directory names (matched case-insensitively on the
#: top-level component). The most-cited crisis patterns; a hardcoded ``data/``
#: default is REFUSED as a capture trigger by design (that is RD1's opt-out
#: flip, not this engine) — here these names only PROPOSE a candidate the human
#: confirms.
CONVENTIONAL_DATA_DIR_NAMES: frozenset[str] = frozenset({"data", "datasets", "inputs", "raw"})

#: Data-shaped file suffixes (lowercase, leading dot). A directory holding any
#: file with one of these is proposed as a candidate. Substrate knowledge (how
#: data is persisted), never experiment semantics — no format is parsed.
DATA_SHAPED_SUFFIXES: frozenset[str] = frozenset(
    {".csv", ".parquet", ".h5", ".npz", ".feather", ".arrow"}
)

#: The DVC pointer suffix (``git``-tracked stub for out-of-band data).
_DVC_SUFFIX = ".dvc"

# Reason tags carried opaquely to the human (the "why this is a candidate"
# disclosure); core never ranks or acts on them.
_REASON_CONVENTIONAL = "conventional-name"
_REASON_DATA_EXTENSION = "data-extension"
_REASON_DVC_POINTER = "dvc-pointer"


@dataclass(frozen=True)
class CandidateDataRoot:
    """One detected-but-UNCONFIRMED data-root candidate.

    ``path`` is a POSIX top-level relpath (a directory name, or a DVC target);
    ``reasons`` is the sorted set of data-shape signals that flagged it. A
    candidate is a PROPOSAL for the human to confirm into ``input_roots`` — it is
    never captured or minted by its detection.
    """

    path: str
    reasons: tuple[str, ...]

    def as_brief(self) -> dict[str, object]:
        """JSON-safe projection for the S1 coverage disclosure (code-rendered)."""
        return {"path": self.path, "reasons": list(self.reasons)}


def detect_candidate_data_roots(
    experiment_dir: Path | str, *, exclude: list[str] | None = None
) -> list[CandidateDataRoot]:
    """Scan *experiment_dir* for data-shaped top-level roots, as UNCONFIRMED candidates.

    Returns the sorted (by ``path``) list of :class:`CandidateDataRoot`. Composes
    the shared exclude-filtered walk (:func:`iter_exclude_filtered_files`) and
    classifies each top-level component by the three data-shape signals. NEVER
    mints, writes, or captures — it only proposes.

    FAIL-OPEN by contract: this feeds a DISCLOSURE path, so any error (an
    unreadable tree, a walk failure) yields ``[]`` rather than raising. A missing
    directory naturally yields ``[]`` (an empty walk). *exclude* defaults to the
    push's default exclude set; a caller rarely overrides it.
    """
    try:
        base = Path(experiment_dir)
        # top-level component -> the set of reasons it looks data-shaped
        signals: dict[str, set[str]] = {}
        for parts, _path in iter_exclude_filtered_files(base, exclude):
            if not parts:
                continue
            if len(parts) == 1:
                # A file directly at the repo root. Only a DVC pointer names a
                # data ROOT (``<name>.dvc`` tracks ``<name>``); a loose data
                # file at the root is not a directory candidate.
                name = parts[0]
                if name.endswith(_DVC_SUFFIX) and name != _DVC_SUFFIX:
                    target = name[: -len(_DVC_SUFFIX)]
                    if target:
                        signals.setdefault(target, set()).add(_REASON_DVC_POINTER)
                continue
            top = parts[0]
            if not top or top.startswith("."):
                continue  # dotdirs (the .hpc control tree, .git) are never data
            reasons = signals.setdefault(top, set())
            if top.lower() in CONVENTIONAL_DATA_DIR_NAMES:
                reasons.add(_REASON_CONVENTIONAL)
            if Path(parts[-1]).suffix.lower() in DATA_SHAPED_SUFFIXES:
                reasons.add(_REASON_DATA_EXTENSION)
        return sorted(
            (
                CandidateDataRoot(path=name, reasons=tuple(sorted(reasons)))
                for name, reasons in signals.items()
                if reasons
            ),
            key=lambda c: c.path,
        )
    except Exception:  # noqa: BLE001 — detection feeds a disclosure path; fail-open to []
        return []
