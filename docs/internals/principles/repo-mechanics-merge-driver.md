---
slug: repo-mechanics-merge-driver
order: 8
title: "Repo mechanics: the generated-artifact merge driver keeps ours, never silently drops theirs"
scope: "A keep-ours merge driver for 100%-generated files only; a partially generated file must never carry it."
---

# Repo mechanics: the generated-artifact merge driver keeps ours, never silently drops theirs

The dominant swarm merge-conflict class is files that are a PURE FUNCTION of the
primitive registry (`operations.json`, `docs/generated/*`, `_verb_module_map.py`,
the emitted schemas). `scripts/merge_generated.py` supplies a keep-ours `generated`
merge driver (declared in the root `.gitattributes`, self-deployed per clone by its
`ensure` subcommand, which WS1's `regen_all` runs), so a both-sides-changed
regenerable file takes ours and a post-merge regen rebuilds it from the merged
source. Keep-ours is SAFE only when regen recreates the ENTIRE file, so the
attribute set is confined to 100%-generated files — a partially generated file
(`docs/primitives/README.md`, whose prose is hand-authored) must never carry it, or
the driver silently discards theirs-side prose regen cannot restore (the
silent-wrong-pick class). An undeployed clone degrades to an ordinary conflict
(loud), never a silent wrong pick.

## Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| The `merge=generated` attribute set equals the fully-generated manifest and NEVER includes a partially generated file: the root `.gitattributes` `merge=generated`/`!merge` lines equal `scripts/merge_generated.py::FULLY_GENERATED_PATTERNS`/`SCHEMA_MERGE_UNSET` exactly, the effective merge=generated schema set equals what `build_schemas.py` emits (a new hand-authored composite schema turns the pin RED, not silently keep-ours'd), no partially generated file (`PARTIALLY_GENERATED_EXCLUDED`) resolves to the driver, and only the ROOT attributes file carries it; the deployed driver keeps ours + exits 0 while the undeployed state conflicts loudly | `tests/contracts/test_generated_merge_driver.py` (manifest lockstep both directions + the build_schemas drift guard + the fires-AND-passes pair: undeployed conflict / deployed keep-ours in a space-bearing tmp repo) | a `merge=generated` line drifts from the manifest, a hand-authored/partially generated file enters the driver set, a second `.gitattributes` grows `merge=generated`, or the deployed driver stops keeping ours cleanly |
