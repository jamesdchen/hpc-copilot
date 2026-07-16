---
slug: cli-fast-path
order: 11
title: "The CLI single-verb fast path: byte-identical to the full walk, or it must not run"
scope: "Fast-path opt-in is an enumerated set; discovery verbs answer off the content-keyed bake or take the full walk — never a partial registry."
---

# The CLI single-verb fast path: byte-identical to the full walk, or it must not run

`hpc-agent <verb>` overwhelmingly runs ONE known verb, which needs only the
module that defines it — not the ~100-module `register_primitives` walk (roughly
half the process's cold-start cost). The single-verb fast path
(`cli.dispatch._try_fast_dispatch`) imports just that module and dispatches. The
load-bearing invariant is **behavioural equivalence**: every fast-path answer is
byte-identical to the full walk, and any case that cannot guarantee that defers
(`return None`) to the full path. Speed is the only thing that ever differs.

Two hazards make this subtle, both instances of the program's dominant failure
mode (wrong-but-plausible output, not slowness):

**Handler primitives that read the WHOLE registry.** `capabilities` / `describe`
/ `find` project the operations catalog. On the fast path the live registry is
PARTIAL — one module imported — so a naive answer silently matches ~4 rows
(`find`) or confidently errors on any other verb (`describe`). So a handler is
fast-path-eligible only when it opts in EXPLICITLY (`CliShape.fast_path_safe`),
and the discovery verbs that opt in do so via **baked hydration**: their handlers
rebuild the whole-truth catalog from the shipped `operations.json` bake
(`_resolve_catalog` → `load_baked_catalog`), never the partial registry. The
opt-in set is small and enumerated so a later default-flip (make every handler
`fast_path_safe`) cannot slip in unreviewed. `capabilities` stays OUT — its
envelope also needs live backend/plugin/cluster state a bake cannot carry.

**A stale bake is worse than no bake.** The bake is trusted only when it is
guaranteed to match the running code — keyed on the BUILD FINGERPRINT
(`_build_info.BUILD_SHA`, stamped into a wheel's build tree and travelling WITH
the code), NEVER the version string. A source checkout carries no `BUILD_SHA`, so
its possibly-stale bake is never trusted and the discovery verbs pay the full
walk (devs pay ~1.3 s; wheels get the win). Two installs of the same *version*
whose source diverged are thereby distinguished — the exact failure a
version-string key would cause. Any hydration miss (unreadable/absent bake)
completes the full walk, byte-identical.

The parser is memoized on the registry generation
(`primitive.registry_generation`) so a warm in-process caller (the MCP in-proc
runner drives `cli.dispatch.main` per tool call) stops re-paying the argparse
tree build — and plugin `register_cli` runs exactly once per registry state.

## Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| The `fast_path_safe` opt-in set is ENUMERATED — equality-pinned to `{install-commands, describe, find}` (reviewed-edit pattern): a verb cannot join the fast path, and the default cannot flip to "every handler is safe", without a reviewer moving this literal. A registry-introspecting handler (`capabilities`) is deliberately absent | `tests/cli/test_fast_dispatch.py::test_fast_path_safe_opt_in_set_is_pinned` + `tests/cli/test_fast_path_cache.py::test_capabilities_stays_excluded_from_fast_path`, `::test_describe_find_are_fast_path_safe_via_baked_hydration` | a `CliShape.fast_path_safe` verb is added/removed without the pinned set moving, or a default-flip makes handlers fast-path-eligible en masse |
| Stale-bake fallback is CONTENT-KEYED on the build fingerprint, never the version string: `describe`/`find` trust `operations.json` only when `baked_catalog_usable()` (i.e. `_build_info.BUILD_SHA is not None`, or the explicit `HPC_AGENT_FORCE_BAKED_CATALOG=1` test seam) — a source checkout (no fingerprint) walks and a seeded stale bake is never served; the walk output is byte-identical | `tests/cli/test_fast_dispatch.py::test_baked_catalog_usable_is_content_keyed_on_build_fingerprint` + `::test_seeded_stale_bake_falls_back_to_walk_byte_identical` (@slow) + `::test_discovery_baked_hydration_is_byte_identical_to_full_walk` (@slow, cross-module) | staleness is keyed on `__version__` (a version-string match wrongly trusts a diverged bake), or a fast-path discovery answer drifts from the full walk |
