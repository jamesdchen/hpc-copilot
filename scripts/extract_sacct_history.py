"""Extract completed-job history from ``sacct`` for the wait predictor.

Walks ``sacct -P --format=JobID,Submit,Start,Priority,Partition,User,
TimeLimit`` for a recent window and writes a JSON file in the shape
``scripts/train_wait_predictor.py`` expects::

    [
      {
        "submit_iso": "2026-09-22T10:00:00",
        "start_iso": "2026-09-22T10:30:00",
        "priority": 1234,
        "partition": "gpu",
        "user": "alice",
        "walltime_sec": 14400
      },
      ...
    ]

Run ahead of the trainer; the trainer then walks each completed job +
the saved squeue snapshots to produce ``(features, observed_overhead)``
training rows.

Usage::

    python scripts/extract_sacct_history.py \\
        --ssh-target alice@cluster \\
        --since-days 30 \\
        --out completed_jobs.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hpc_agent.infra.parsing import parse_sacct_pipe_row, parse_walltime_to_sec  # noqa: E402

_SACCT_FORMAT = "JobID,Submit,Start,Priority,Partition,User,TimeLimit"


def _coerce_iso(s: str) -> str | None:
    """Parse a SLURM sacct ``Submit``/``Start`` timestamp.

    ``SLURM_TIME_FORMAT=%Y-%m-%dT%H:%M:%S%z`` (set by ``_build_sacct_command``)
    makes sacct emit an explicit UTC offset, so we can parse strictly.
    Older sacct callers (and tests that pipe naive text via ``--from-stdin``)
    still produce naive ``YYYY-MM-DDThh:mm:ss`` — we accept those too,
    but tag them ``+00:00`` ONLY when the caller has set
    ``HPC_SACCT_NAIVE_IS_UTC=1``; otherwise we refuse them so a misset
    cluster TZ doesn't silently shift queue-wait labels by hours.
    """
    if not s or s in {"Unknown", "None", "N/A"}:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        import os as _os

        if _os.environ.get("HPC_SACCT_NAIVE_IS_UTC") != "1":
            return None
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_sacct_lines(text: str) -> list[dict]:
    """Parse pipe-separated sacct output into job dicts. Permissive:
    rows missing required fields are skipped; step rows (``JobID``
    containing ``.``) are skipped — only top-level jobs counted."""
    if not text:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [col.strip() for col in lines[0].split("|")]
    out: list[dict] = []
    for raw in lines[1:]:
        parts = raw.split("|")
        row = parse_sacct_pipe_row(parts, header)
        job_id = row.get("JobID", "")
        if not job_id or "." in job_id:  # skip ``12345.batch`` step rows
            continue
        submit_iso = _coerce_iso(row.get("Submit", ""))
        start_iso = _coerce_iso(row.get("Start", ""))
        if submit_iso is None or start_iso is None:
            continue
        try:
            priority = int(row.get("Priority", "0"))
        except ValueError:
            continue
        partition = row.get("Partition", "")
        user = row.get("User", "")
        walltime_sec = parse_walltime_to_sec(row.get("TimeLimit", ""))
        if walltime_sec <= 0:
            continue
        out.append(
            {
                "job_id": job_id,
                "submit_iso": submit_iso,
                "start_iso": start_iso,
                "priority": priority,
                "partition": partition,
                "user": user,
                "walltime_sec": walltime_sec,
            }
        )
    return out


def _build_sacct_command(*, since_days: int) -> str:
    """Compose the sacct invocation. We want completed top-level jobs
    with a real start time within the window."""
    # Stamp the explicit UTC offset so sacct doesn't reinterpret a naive
    # timestamp as cluster-local time, which would shift the cutoff by
    # the cluster's TZ offset (up to 14h either way).
    starttime = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime(
        "%Y-%m-%dT00:00:00+0000"
    )
    # SLURM_TIME_FORMAT forces sacct's row-level Submit/Start to carry an
    # explicit UTC offset (without it, sacct emits the cluster's local
    # time and the parser would silently mislabel it as UTC, shifting
    # every training row's queue-wait by the cluster's tz offset).
    return (
        f"SLURM_TIME_FORMAT='%Y-%m-%dT%H:%M:%S%z' "
        f"sacct -P -X --noheader=no --starttime={starttime} "
        f"--state=COMPLETED,FAILED,TIMEOUT --format={_SACCT_FORMAT}"
    )


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ssh-target", help="user@host or OpenSSH alias.")
    p.add_argument("--since-days", type=int, default=30, help="History window.")
    p.add_argument("--out", type=Path, required=True, help="Output JSON path.")
    p.add_argument(
        "--from-stdin",
        action="store_true",
        help="Read sacct text from stdin (test path).",
    )
    args = p.parse_args(argv)

    if args.from_stdin:
        text = sys.stdin.read()
    else:
        if not args.ssh_target:
            p.error("--ssh-target required when --from-stdin is not set")
        from hpc_agent.infra.remote import ssh_run

        cmd = _build_sacct_command(since_days=args.since_days)
        cp = ssh_run(cmd, ssh_target=args.ssh_target)
        if cp.returncode != 0:
            print(f"sacct failed (exit {cp.returncode}): {cp.stderr[:500]}", file=sys.stderr)
            return 1
        text = cp.stdout

    rows = parse_sacct_lines(text)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"wrote {len(rows)} completed-job rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
