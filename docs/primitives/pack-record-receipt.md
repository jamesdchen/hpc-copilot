# pack-record-receipt

Journal a **CODE receipt** that a domain check reported `passed` for one
caller-authored **slot**, against a set of checked files, under a pack's
**current bind**. The verb recomputes ON DISK the sha of every checked file AND
the current bind's manifest sha, builds the composite `content_sha` server-side,
binds it through the one attestation kernel, and appends a `pack-receipt` record
(block `pack-receipt`, `response="checked"`, `attestor:"code"`) to the pack's
decision journal (`docs/design/domain-packs.md`, "Receipt naming + the gate
contract").

A pack receipt is the evidence a pack gate consumes: for every opted-in
`receipt_bindings` entry, the slot must reduce CURRENT **and** `passed=true`. The
receipt is honestly no more than "this code, at these shas, under this pack
version, reported passed" — it is evidence a check *ran*, never proof the check
is correct (that is the pack's own CI's problem, per Q4).

## Freshness by construction (the load-bearing constraint)

Every sha is recomputed here from the bytes on disk — the parse IS the recompute.
No wire field lets a caller assert a `content_sha` / `manifest_sha` / per-file sha
the verb then trusts (the enforcement-map "receipt shas are server-computed"
row). The composite is built from the ONE definition
(`state.pack_receipts.receipt_content_sha` over `{manifest_sha, checked: {relpath:
sha, …}}`) that the read side rebuilds from disk at gate time, so record-form and
read-form can never drift apart. A caller therefore cannot assert a receipt for
content not on disk, and the receipt reads **stale** the instant any checked file
(or the bind) moves — closing the receipt-laundering hole one layer up.

### What freshness does NOT close: the truthfulness boundary

`passed` and `evidence` are **caller-attested** — the verb recomputes freshness
(the sha bind), not the check's correctness; it does not run the domain check. The
guarantee is narrower and honest: a receipt vouches for the exact bytes on disk
under the exact bind and drifts stale when they move. The gate **weighs** the
caller-attested `passed`; it never re-derives it. The trust boundary is the
emitter (the pack's CI), not this verb's recompute.

## Inputs

A `PackRecordReceiptSpec` (`hpc_agent._wire.actions.pack_record_receipt`):

- `pack` (slug, required) — the pack whose current bind this receipt is recorded
  under. **No current bind → loud `spec_invalid`** (a dangling reference; a mutate
  verb is always an explicit opt-in, so the "absence = silence" leg never applies
  here).
- `slot` (slug, required) — the caller-authored slot slug this receipt fills (the
  caller's name for one obligation, DP4). Opaque to core; **never** invented or
  defaulted, and needs no membership check — `fills_slots` is advisory identity
  only, so any well-formed slug records against the current bind.
- `checked` (list of experiment-relative paths, optional) — the files the domain
  check covered. The verb recomputes each file's raw-bytes sha ON DISK; a
  missing/unreadable path is a loud `spec_invalid` (a receipt cannot claim content
  that is not there).
- `passed` (bool, required) — the mechanical outcome the check reported. `true` is
  required for a gate to accept the slot.
- `evidence` (opaque object / string / null, optional) — arbitrary caller payload
  recorded verbatim; **never** read by core for meaning.

There is deliberately **no** `content_sha` / `manifest_sha` / per-file sha field.

## Outputs

`data` is a `PackRecordReceiptResult` — every sha server-recomputed:

```
{
  "pack": "<slug>",
  "version": "<opaque echoed version>",
  "manifest_sha": "<64-hex>",
  "slot": "<slug>",
  "content_sha": "<64-hex canonical-JSON composite>",
  "passed": true
}
```

`content_sha` is the canonical-JSON sha the verb built server-side from
`{manifest_sha, checked: {relpath: sha, …}}` — the freshness key a gate recomputes
at read time.

## Errors

- `spec_invalid` — no current bind for the named pack (a dangling reference), or a
  missing/unreadable `checked` file (naming the path). Not retry-safe; bind the
  pack first, or fix the path.

## Idempotency

Deliberately **not idempotent** (like `append-decision` and the render receipt):
the journal is append-only, so each call adds a fresh receipt line. A re-record at
unchanged shas appends a new record — the newest valid receipt wins on read
(`state.pack_receipts.slot_status`), so retries are safe but not byte-idempotent.

## Usage

```
hpc-agent pack-record-receipt --spec spec.json --experiment-dir .
```

where `spec.json` is `{"pack": "<slug>", "slot": "<slug>", "checked": ["<relpath>",
…], "passed": true, "evidence": <opaque>}`. The domain check runs OUTSIDE core and
emits the `passed` outcome; this verb journals it, sha-bound to the current bind.
The pack gate then reads the fresh, passing receipts and clears the opted-in slots.
