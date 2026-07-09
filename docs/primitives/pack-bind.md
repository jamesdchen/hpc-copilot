---
name: pack-bind
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/packs/<pack>.decisions.jsonl
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent pack-bind --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.pack.bind_op.pack_bind
---
# pack-bind

Bind a **domain pack** into an experiment **as data**: the explicit, journalable
moment a caller pins a set of domain standards — a pack manifest and every file
it lists, by raw-bytes SHA-256 — to an experiment (`docs/design/domain-packs.md`,
"The bind event"). A `mutate` verb; core never imports, executes, or interprets a
line of pack logic (DP2/DP3).

Given a caller-referenced manifest relpath (resolved against the experiment dir
exactly as `_AuditedSource.source` resolves — never a blessed directory, never a
search path), the verb:

1. reads the manifest **on disk** and parses it (`state/pack.py`), refusing loudly
   on a missing/unreadable/non-JSON/bad-shape manifest;
2. optionally cross-checks the caller-supplied `pack` name against the manifest's
   own `name` (the manifest stays authoritative; a mismatch is refused — a guard
   against binding the wrong manifest);
3. recomputes **every listed file's** raw-bytes SHA-256 against disk and refuses
   on any drift (`verify_manifest_integrity`), naming the path and both shas;
4. binds the manifest sha through the **one** attestation kernel
   (`state/attestation.py::bind`) against the freshly-recomputed hash — no sha is
   caller-suppliable;
5. appends the bind record (block `pack-bind`, `response="bound"`,
   `attestor="code"`, `subject_kind="pack"`, `subject_id=<pack name>`,
   `content_sha=<manifest_sha>`) to the pack's decision journal
   (`.hpc/packs/<name>.decisions.jsonl`);
6. echoes `{pack, version, manifest_sha, files, seams}` back.

## The recompute is the lock

No sha is caller-suppliable. The verb recomputes the manifest sha and every file
sha **server-side** and binds against the fresh hash — a bind can no more assert a
sha into existence than a human sign-off can (D5 lock 2). `version` is an opaque
string core echoes and never compares; ordering between versions is the sha's job,
via bind order.

## Re-bind is drift

A second `pack-bind` at a new manifest sha is just a newer record; the reduction
kernel (`attestation.reduce`, via `state/pack_receipts.py::current_bind`) makes
the old bind **stale**. Editing pack content without re-binding is equally revoked:
a gate recomputes file shas from disk against the current bind's recorded shas, so
changed-on-disk content reads as drift even before any re-bind. Either way: hashes
move → everything signed under the old standards reads stale → re-check, re-receipt,
re-sign. No drift state machine.

## Loud on a dangling reference

Reaching `pack-bind` at all means the caller intends to bind, so a missing or
unreadable manifest, or any listed file whose on-disk sha no longer matches, is a
broken opted-in setup — a loud `spec_invalid`, never a silent pass. Silent absence
(the D7 posture) belongs to the interview opt-in read, not to this verb.

## Boundary

Core reads bytes and hashes them — nothing more. It never imports, executes, or
interprets a manifest-named file; the seam declarations a pack ships are validated
for **shape only** (`state/pack.py`), never for meaning. An experiment that never
binds a pack behaves byte-identically to today.
