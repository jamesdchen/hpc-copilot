"""Snapshot writer for the queue-wait predictor's training data.

Run via cron every 5 minutes::

    */5 * * * *  python -m scripts.snapshot_squeue \
                     --ssh-target alice@cluster \
                     --experiment-dir /home/alice/exp

Writes a column-projected, gzipped squeue snapshot to
``<experiment_dir>/.hpc/squeue_snapshots/<YYYYMMDDTHHMMSS>.tsv.gz``.

The columns ingested match exactly what the parser
:func:`claude_hpc.forecast.squeue_priority_field.parse_squeue_priority_field`
expects, so historical snapshots and live ones are interchangeable.

Storage cost: ~30 KB per snapshot for a ~500-job cluster, ~3 KB
gzipped → ~1 MB / day → ~365 MB / year. Manageable.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from datetime import datetime, timezone
from pathlib import Path

# Standard column set for the predictor's training pipeline. Order
# doesn't matter (the parser keys by column name) but stability does
# — adding columns later is fine, removing is a wire-shape break.
_SQUEUE_FORMAT = "JOBID|PRIORITY|PARTITION|USERNAME|STATE|TIME_LEFT|TIME_LIMIT"


def _build_squeue_command() -> str:
    """Compose the ``squeue`` invocation we want the cluster to run."""
    return f"squeue --user='*' -O '{_SQUEUE_FORMAT}'"


def write_snapshot(
    *,
    text: str,
    experiment_dir: Path,
    at: datetime | None = None,
) -> Path:
    """Persist a squeue snapshot. Returns the path written.

    Pure I/O; the caller fetches *text* via SSH (or reads from
    stdin for testing).
    """
    if at is None:
        at = datetime.now(timezone.utc)
    out_dir = experiment_dir / ".hpc" / "squeue_snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = at.strftime("%Y%m%dT%H%M%S") + ".tsv.gz"
    target = out_dir / fname
    with gzip.open(target, "wt", encoding="utf-8") as f:
        f.write(text)
    return target


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ssh-target", help="user@host or OpenSSH alias.")
    p.add_argument(
        "--experiment-dir", type=Path, default=Path("."), help="Where to write snapshots."
    )
    p.add_argument(
        "--from-stdin",
        action="store_true",
        help="Read squeue text from stdin instead of running ssh (test path).",
    )
    args = p.parse_args(argv)

    if args.from_stdin:
        text = sys.stdin.read()
    else:
        if not args.ssh_target:
            p.error("--ssh-target required when --from-stdin is not set")
        from claude_hpc.infra.remote import ssh_run

        cmd = _build_squeue_command()
        cp = ssh_run(cmd, ssh_target=args.ssh_target)
        if cp.returncode != 0:
            print(
                f"squeue failed (exit {cp.returncode}): {cp.stderr[:500]}",
                file=sys.stderr,
            )
            return 1
        text = cp.stdout

    target = write_snapshot(text=text, experiment_dir=args.experiment_dir)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
