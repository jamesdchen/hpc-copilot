# ops/clusters/

## What + why

`ops/clusters/` exposes read-only introspection of the cluster catalog
declared in `clusters.yaml`. Two primitives: `clusters-list` returns
every configured cluster's `{name, host, scheduler}`; `clusters-describe`
returns one cluster's resolved `ClusterConfig` (and, under `--strict`,
flags `clusters.yaml` keys the schema doesn't recognize). The agent
uses these before submit to discover what's available and to surface
config-drift errors with actionable messages.

## Invariant

`ops/clusters/` promises: cluster name (or none) in → projected /
resolved config dict out, no SSH, no scheduler probes, no writes. Every
read goes through `infra.clusters.load_clusters_config` so the
config-discovery contract is shared.

## Public vs internal

- `list.py` — `clusters-list` primitive (public).
- `describe.py` — `clusters-describe` primitive (public).
- No internal-only files.
