"""Parse ``sshare -P`` output into ``{user: fairshare_value}``.

SLURM's ``sshare`` exposes per-account / per-user fair-share. The
``-P`` (parsable) flag pipe-delimits the columns. Default headers
include ``Account``, ``User``, ``RawShares``, ``NormShares``,
``RawUsage``, ``EffectvUsage``, ``FairShare``.

For the wait-predictor, we want ``FairShare`` (a value in [0, 1])
keyed by user. Aggregate-account rows (where ``User`` is empty) are
skipped — we want individual users only, so ``competitor_count_by_tier``
in :mod:`wait_features` can bucket each pending job's user.

Permissive parser: header order is detected by name; rows with
unparseable FairShare are skipped; missing columns surface as a
single empty dict so callers can fall back gracefully.
"""

from __future__ import annotations


def parse_sshare(text: str) -> dict[str, float]:
    """Parse ``sshare -P`` output into ``{user: fairshare}``.

    Empty input → empty dict. The default ``sshare -P`` shape::

        Account|User|RawShares|NormShares|RawUsage|EffectvUsage|FairShare
        root|||1.000000|...
        labA|alice|1|0.500000|...|0.823456
        labB|bob|1|0.250000|...|0.412300

    Aggregate rows (no User) are dropped. Returns the per-user
    FairShare values; the wait-features extractor buckets these
    into quintile tiers downstream.
    """
    if not text:
        return {}
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    header = [col.strip() for col in lines[0].split("|")]
    name_to_idx = {col: i for i, col in enumerate(header)}
    if "User" not in name_to_idx or "FairShare" not in name_to_idx:
        return {}
    user_idx = name_to_idx["User"]
    fs_idx = name_to_idx["FairShare"]
    out: dict[str, float] = {}
    for raw in lines[1:]:
        cells = raw.split("|")
        if user_idx >= len(cells) or fs_idx >= len(cells):
            continue
        user = cells[user_idx].strip()
        if not user:
            continue  # aggregate-account row
        fs_text = cells[fs_idx].strip()
        try:
            fs_value = float(fs_text)
        except ValueError:
            continue
        out[user] = fs_value
    return out


__all__ = ["parse_sshare"]
