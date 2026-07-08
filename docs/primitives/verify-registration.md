## Purpose

Read-only consumer seat of the **registration kernel** — the last-mile
deployment-boundary attestation (`docs/design/registration-kernel.md` R8).
Given a `registration_id` (or a `run_id` naming a registration), it REDUCES the
id's decision journal to a status, RECOMPUTES the prerequisite chain and the
live dossier signature **at read time**, and renders a deterministic markdown
brief. It is a **reporter**: every status — `absent` included — returns ok; it
never blocks and never raises on a stale, revoked, or missing subject. The
deployment refusal lives caller-side (core does not own the deploy boundary):
the consuming repo wires the ~10-line "status != `current` → don't deploy"
check against this verb's output.

A registration is the same object as every other trusted record in the system
— a human attestation over the sealed dossier, bound by its `bundle_sha256`.
This verb answers, mechanically, "is that clearance still valid?" — the
question failure class 4 (no revocation semantics) left unanswerable.

## Inputs

A `VerifyRegistrationSpec` JSON spec with **exactly one** of:

- `registration_id` (strict slug) — verify that registration directly; the
  newest record under the id wins (R7).
- `run_id` (strict slug) — find the registration(s) naming that run and report
  the resolved (newest matching) one.

Supplying both, or neither, is refused.

## Outputs

A `VerifyRegistrationResult`:

- `status` — the reduced registration status (R7): `current | stale | revoked |
  superseded | absent`. `current` requires the newest record to be a
  registration whose **live dossier signature AND every prerequisite slot**
  still hold; a drifted dossier store or a prerequisite that now reads
  non-current flips the answer to `stale`. Template drift does NOT flip the
  status (see below).
- `registration_id` / `registered_at` — the resolved id and its journal
  timestamp (both `None` when `absent`).
- `dossier` — `{recorded_sha, recomputed_sha, drifted_stores}`: the
  `bundle_sha256` the registration bound vs the live dry re-gather through the
  one signature seam. When the run moved or cannot be re-gathered,
  `recomputed_sha` is empty and the status reads `stale` (never a crash).
  `drifted_stores` names the source stores whose bytes moved when the record
  carries a per-store breakdown, else empty (a sha-only comparison, disclosed
  in the brief).
- `template` — `{status, recorded_sha, recomputed_sha}`: the template file's
  raw-bytes sha on disk vs the sha the registration recorded. Template drift is
  a **disclosed finding, never a silent revoke** (R5): a registration made
  under the standards in force at its timestamp stays a truthful dated record;
  a consumer requiring the new standard re-registers.
- `prerequisites` — one `{slot, kind, status, recorded_sha, recomputed_sha,
  evidence_note}` per chain entry, each re-checked through its kind's one
  existing currency definition (R3). `evidence_note` echoes what filled the
  slot verbatim (opaque, never parsed) — for the generic `attestation` kind,
  the satisfying record's `{block, attestor}`, so an agent-authored record that
  fills a slot is visible, never silent.
- `fields` — `{declared, present, missing}`: template-field completeness by
  COUNTING (R5). A declared field slug is present when it carries a non-empty
  value in the registration; slugs and values are opaque and never interpreted.
- `brief` — the code-rendered markdown a human reviews.
- `view_sha` — the canonical-JSON sha of the projection (including the brief).
  This is the **witness a subsequent registration sign-off must carry** (R6):
  because the brief is a pure function of the reduced status + legs, the sign-off
  gate recomputes the same sha and binds it rather than trusting it.

## Behaviour notes

- **Reporter, never a gate.** A non-`current` status is a successful run — the
  finding IS the feature. Pressure to make this verb refuse a deploy is the
  design working; that refusal stays caller-side.
- **All shas are server-computed.** The dossier sha comes from the one
  signature seam, the template sha from disk bytes, every chain sha from its
  kind's route-through checker, and the `view_sha` from the deterministic
  projection. No caller-asserted sha is trusted.
- **Opaque by construction.** Field slugs, field values, `subject_id`s, and
  evidence notes are counted, echoed, and diffed by identity — never read for
  meaning. The only vocabularies this verb owns are the status set and the
  closed `PREREQUISITE_KINDS`.
- Read-only, no SSH, no writes. Safe to re-run.
