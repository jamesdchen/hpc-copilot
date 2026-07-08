# evidence-period

The evidence **period digest** — a time-window projection over the one evidence
collector (`docs/design/evidence-memory.md` E5). Given a `since` (required
window start) and an optional `until` (window end, default open/now), it
projects the sealed records — conclusions, per-tag activity, determinism
envelopes — over the window and renders a dated timeline that **ends with the
unconcluded-campaigns list**: every terminal campaign no current conclusion
names, dated by its completion ts. That list is the standing invitation to close
the conclusion loop — a list, never a verdict (a missing conclusion blocks
nothing).

It is the point-query sibling of `evidence-brief`: same collector, same sealed
records, a different lead. Where `evidence-brief` answers "what do we know under
tag X" for a greenlight embed, `evidence-period` answers "what happened between
these two dates, and what did we never conclude about".

## Window semantics

The collector owns the **upper** bound: `evidence-period` calls it with
`as_of=until`, so every store is time-filtered to `ts <= until` in one shared
definition. The **lower** bound is a projection filter kept in the verb:
conclusions, activity, and the unconcluded list are kept only when
`ts >= since`. `until` defaults to open-ended (up to now); `since` is required (a
window has a start). Both are ISO-8601 timestamps — never named periods
("2025H1", "post-vol-spike" are caller vocabulary the caller translates to
timestamps).

## Fleet

`fleet: true` runs the identical per-namespace walk over every experiment this
machine has journaled (non-creating `*/repo.json` discovery); a wiped, torn, or
unreadable namespace is skipped and counted in `skipped`. Default scope is the
single `--experiment-dir`.

## Cache

A content-keyed cache (`state/evidence_cache.py`) memoizes the projection, keyed
by the package version, the spec fields, and the `(relpath, mtime_ns, size)`
fingerprint of every file the collector would walk. Any append to any journal,
ledger, or sidecar moves the key and forces a recompute. `cache` discloses
`hit` / `miss` / `disabled` (`HPC_NO_EVIDENCE_CACHE=1` opts out). The cache is
disposable: **deleting it changes no output** — the digest recomputes
byte-identically from the live stores.

## Boundary

A pure projection: no SSH, no scheduler, no write, no store. It never interprets
what a tag means or what a `finding` says — both are opaque, echoed verbatim,
never parsed. Every field is identity, a count, a date, a sha, or a verbatim
fingerprint-evidence label. The `render` markdown carries no urgency,
recommendation, or interpretation prose (the queue's no-fabricated-urgency
rule); it is relayed to the human verbatim.
