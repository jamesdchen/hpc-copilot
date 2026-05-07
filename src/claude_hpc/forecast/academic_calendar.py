"""Academic deadline calendar for queue-wait forecasting.

On academic clusters, conference deadlines are first-order predictors
of queue contention. The week before NeurIPS abstract registration
typically saturates the queue; the week before ICML camera-ready
spikes again. Multiple simultaneous deadlines (e.g. Jan-Feb
ICLR/ICML/AAAI overlap) compound the load multiplicatively.

This module supplies the deadline list + a feature extractor. The
calendar is configurable per project via ``.hpc/deadlines.yaml`` so
NLP labs can swap in ACL/EMNLP, CV labs can swap in CVPR/ICCV/ECCV,
etc. The default ships major ML venues only.

Maintenance: deadline dates shift slightly year to year. Update the
defaults annually; project overrides absorb the in-year volatility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Deadline:
    """One conference / workshop deadline.

    * ``venue`` — short identifier including year (``"NeurIPS-2026"``).
    * ``kind`` — milestone type. Common: ``"abstract"`` (early),
      ``"paper"`` (main submission), ``"camera_ready"`` (post-accept),
      ``"rebuttal"`` (response window). Open string so projects can
      add custom milestones (e.g. ``"workshop_proposal"``).
    * ``date_iso`` — ``YYYY-MM-DD``; treated as 23:59 UTC of that day
      (deadline-end semantics).
    """

    venue: str
    kind: str
    date_iso: str


# Major ML venues. Annual update required. The dates below are
# approximate seasons; refer to each venue's CFP for exact deadlines.
DEFAULT_DEADLINES: tuple[Deadline, ...] = (
    # 2026 cycle
    Deadline("NeurIPS-2026", "paper", "2026-05-22"),
    Deadline("NeurIPS-2026", "camera_ready", "2026-10-25"),
    Deadline("ICLR-2027", "paper", "2026-09-25"),
    Deadline("ICLR-2027", "rebuttal", "2026-11-15"),
    Deadline("AAAI-2027", "paper", "2026-08-15"),
    Deadline("CVPR-2027", "paper", "2026-11-08"),
    Deadline("EMNLP-2026", "paper", "2026-06-20"),
    Deadline("EMNLP-2026", "camera_ready", "2026-09-30"),
    # 2027 cycle
    Deadline("ICML-2027", "abstract", "2027-01-23"),
    Deadline("ICML-2027", "paper", "2027-01-30"),
    Deadline("ICML-2027", "camera_ready", "2027-06-10"),
    Deadline("ACL-2027", "paper", "2027-02-15"),
    Deadline("KDD-2027", "paper", "2027-02-08"),
    Deadline("ECCV-2026", "paper", "2026-03-07"),
    Deadline("ICCV-2027", "paper", "2027-03-08"),
    Deadline("AISTATS-2027", "paper", "2026-10-15"),
    Deadline("UAI-2027", "paper", "2027-02-19"),
)


def _coerce_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def load_project_deadlines(experiment_dir: Path) -> tuple[Deadline, ...]:
    """Read ``<experiment_dir>/.hpc/deadlines.yaml`` if present.

    Returns the project's deadlines or :data:`DEFAULT_DEADLINES` if no
    file. Malformed files surface as ``ValueError`` so the caller can
    flag the config rather than silently using stale data.

    Schema::

        deadlines:
          - venue: ICLR-2027
            kind: paper
            date: 2026-09-25
          - venue: NeurIPS-2026
            kind: camera_ready
            date: 2026-10-25
    """
    path = experiment_dir / ".hpc" / "deadlines.yaml"
    if not path.is_file():
        return DEFAULT_DEADLINES
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"deadlines.yaml parse error: {exc}") from exc
    if raw is None:
        return DEFAULT_DEADLINES
    if not isinstance(raw, dict) or not isinstance(raw.get("deadlines"), list):
        raise ValueError("deadlines.yaml must have top-level key 'deadlines: [...]'")
    out: list[Deadline] = []
    for i, entry in enumerate(raw["deadlines"]):
        if not isinstance(entry, dict):
            raise ValueError(f"deadlines[{i}] must be a mapping")
        try:
            out.append(
                Deadline(
                    venue=str(entry["venue"]),
                    kind=str(entry["kind"]),
                    date_iso=str(entry["date"]),
                )
            )
        except KeyError as exc:
            raise ValueError(f"deadlines[{i}] missing key {exc.args[0]!r}") from None
    return tuple(out)


def features_at(
    now: datetime,
    *,
    deadlines: tuple[Deadline, ...] = DEFAULT_DEADLINES,
) -> dict[str, Any]:
    """Compute deadline-aware features for *now*.

    Returns a dict with:

    * ``min_days_to_deadline`` — days until the next upcoming deadline.
      ``None`` when no future deadlines remain in the calendar.
    * ``is_within_deadline_week`` — boolean, ``min_days_to_deadline <= 7``.
    * ``is_within_deadline_month`` — boolean, ``min_days_to_deadline <= 30``.
    * ``deadline_density_30d`` — count of deadlines in the next 30
      days. Captures the multi-deadline pile-up effect (Jan-Feb ML
      season, etc.).

    Past deadlines are ignored. Project-specific calendars from
    :func:`load_project_deadlines` plug in via the *deadlines* arg.
    """
    today = now.astimezone(timezone.utc).date()
    upcoming = []
    for d in deadlines:
        d_date = _coerce_date(d.date_iso)
        if d_date is None:
            continue
        delta = (d_date - today).days
        if delta < 0:
            continue
        upcoming.append(delta)
    if not upcoming:
        return {
            "min_days_to_deadline": None,
            "is_within_deadline_week": False,
            "is_within_deadline_month": False,
            "deadline_density_30d": 0,
        }
    min_days = min(upcoming)
    return {
        "min_days_to_deadline": min_days,
        "is_within_deadline_week": min_days <= 7,
        "is_within_deadline_month": min_days <= 30,
        "deadline_density_30d": sum(1 for d in upcoming if d <= 30),
    }


__all__ = [
    "DEFAULT_DEADLINES",
    "Deadline",
    "features_at",
    "load_project_deadlines",
]
