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

    python -m scripts.extract_sacct_history \\
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

from claude_hpc.infra.parsing import parse_sacct_pipe_row, parse_walltime_to_sec

_SACCT_FORMAT = "JobID,Submit,Start,Priority,Partition,User,TimeLimit"


def _coerce_iso(s: str) -> str | None:
    """SLURM emits ``2026-09-22T10:00:00`` (no timezone). Treat as UTC."""
    if not s or s in {"Unknown", "None", "N/A"}:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


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
    starttime = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime(
        "%Y-%m-%dT00:00:00"
    )
    return (
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
        from claude_hpc.infra.remote import ssh_run

        cmd = _build_sacct_command(since_days=args.since_days)
        cp = ssh_run(cmd, ssh_target=args.ssh_target)
        if cp.returncode != 0:
            print(f"sacct failed (exit {cp.returncode}): {cp.stderr[:500]}", file=sys.stderr)
            return 1
        text = cp.stdout

    rows = parse_sacct_lines(text)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2))
    print(f"wrote {len(rows)} completed-job rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
