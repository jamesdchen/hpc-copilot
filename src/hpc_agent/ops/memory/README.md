# ops/memory/

## What + why

`ops/memory/` is the agent's persistent memory: `recall` reads prior
campaigns to seed planning; `interview` writes the campaign's design
dialogue so future recalls have context. Together they let the agent
build on past work instead of restarting from blank.

## Invariant

`ops/memory/` promises: typed query/spec in → ranked prior context out
(recall) OR durable interview record out (interview), with no remote
I/O — purely local journal reads/writes.

## Public vs internal

- `recall.py` — public read primitive.
- `interview.py` — public write primitive.
- No internal-only files.
