---
name: program-init
verb: mutate
side_effects:
- file_write: <experiment>/packs/<program>/**
- file_write: <experiment>/.hpc/packs/<pack>.decisions.jsonl
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent program-init --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.pack.init_op.program_init
---
# program-init

Materialize (or adopt) the **PROGRAM** layer of the three-tier pack architecture
(`docs/design/program-init.md`). A domain pack ships a reusable skeleton; at
program creation this verb CONSUMES that skeleton to generate a program template,
stamps the lineage (`derived_from {pack, seam, version, sha}`) **mechanically**,
seals the program manifest via the generic pack re-seal, and binds the packs. One
command = a working program layer; lineage is code-authored, never asked (P3).

Given an experiment dir and a spec, the verb runs in one of two modes.

## create — a fresh program layer

`mode: create` (default) generates `packs/<program>/`:

1. reads the DOMAIN manifest's `audit_template` seam file and records its
   raw-bytes SHA-256 as `derived_from.sha` (never caller-suppliable);
2. writes the program template = a code-authored provenance header + a **verbatim
   byte copy** of the domain skeleton (core never interprets template content —
   DP1; the pinned/variable-section markers + the pinned-verbatim check are
   build-order item 3, not this verb);
3. writes a `sweep.json` recipe carrying the lineage stamp (the recipe is the
   DURABLE stamp — the generic reseal copies it into the manifest, so a hand-edited
   manifest `derived_from` self-revokes on the next reseal);
4. seals `manifest.json` from the recipe and binds BOTH packs (journaling old→new).

Refuses (loud `spec_invalid`) if `packs/<program>/` already exists — use adopt.

## adopt — migrate an existing program pack onto its lineage

`mode: adopt` stamps lineage onto an EXISTING program pack **without byte-changing
any content file** (the signed-template migration path): it stamps the `sweep.json`
recipe (raw read-modify-write, preserving any unknown lab keys), reseals the
manifest, and rebinds **only the pack whose bytes moved** — the domain pack is a
lineage root, untouched (no no-op rebind). The signed template stays byte-identical,
so every section sign-off is preserved; the rebind stales the covered receipts,
which the caller re-earns (zero re-sign-offs — sign-offs bind section-body ×
template identity, both unchanged).

## The check leg is caller-resolvable only

`program-init` runs a check ONLY when a command resolves caller-side: an explicit
`spec.check`, run once in the experiment dir (`shlex`-split, `shell=False` — the
executor precedent), a failing check REPORTED (`check_ok: false`) not raised. With
no resolvable command, the domain pack's receipt slots are reported as
`slots_to_earn` (each with its caller-side check command when the opt-in recorded
one). Core never composes a check argv from pack content (DP2/DP4).

## Boundary

`program-init` never writes `interview.json` (the interview primitive is the one
writer — it ECHOES the exact opt-in block as `packs_optin` for the on-ramp to
persist). Until that block is persisted, `pack-status` stays empty about the
freshly-bound packs — confirm an init via `read-decisions` or this result, not
`pack-status`. All manifest/recipe changes are additive-optional: a legacy wheel
parses a stamped manifest, and this wheel parses legacy manifests. Pure local read
+ file write + journal append, no SSH.

