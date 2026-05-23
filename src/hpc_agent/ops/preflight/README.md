# ops/preflight

## What and why

`ops/preflight/` runs the environment check (rsync or the scp+tar fallback transport, ssh reachability, scheduler/cluster binary presence, parseable `clusters.yaml`, and optional TCP probe of a named cluster's port 22) before a campaign starts. The check is pure-read: it never mutates remote or local state. Running it up front lets the agent surface missing prerequisites with actionable messages rather than failing mid-submit with opaque transport errors.

## Invariant

`ops/preflight/` promises: cluster config in → categorized list of (ok | missing | misconfigured) checks out, no mutation of remote or local state.

## Public vs internal

- `check.py` is the public primitive module (exports `check_preflight`); no internal-only files in this subject.
