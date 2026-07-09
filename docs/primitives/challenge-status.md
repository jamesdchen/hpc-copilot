# challenge-status

The **read-only query over standing dissent**: given a `challenge_id` (the
**thread view** — the filing/verdict/withdraw conversation under that id) or a
target **address** (a `content_sha`, or a `subject_kind` + `subject_id` pair —
the **target view**, "what stands against this record?"), it projects every
standing challenge in scope, reduced to `open | upheld | dismissed | withdrawn |
superseded`. A deterministic markdown brief rides the result for the human to
read **verbatim**; its canonical-JSON `view_sha` is the hash a subsequent
`challenge-verdict` may bind.

A read-only `query` (`verb="query"`, `side_effects=[]`, `idempotent=True`,
`requires_ssh=False`; the `notebook-status` / `evidence-brief` posture). A
**challenge** is a human-authored, evidence-bound, sha-targeted attestation of
DISSENT against a committed record — "a nudge against the archive"
(`docs/design/challenge-attestation.md`, C1). This verb READS that state; it
never files, resolves, or withdraws — those land only via `append-decision`
under the gated `challenge`-family blocks (C-gate lock 1, the no-affordance
posture).

## Inputs

A `ChallengeStatusSpec` (`hpc_agent._wire.queries.challenge_status`) plus the
standard `--experiment-dir`. **Exactly one addressing** is required:

- `challenge_id` (a slug) — the **thread view**: one challenge conversation by
  its id.
- `content_sha` — the **target view** by sha: every challenge whose target names
  that exact sha.
- `subject_kind` **and** `subject_id` (both, or neither) — the **target view**
  by subject: every challenge whose target names that record. A bare half is
  refused (the R3 full-address rule, read side).
- `fleet` (bool, default `false`) — when `true`, the identical per-namespace
  walk over **every experiment this machine has journaled**; a torn/unreadable
  namespace is **skipped** and counted, never fatal.

An under- or over-specified address is refused by the spec validator — you
cannot read the dissent over a record the machine cannot name.

## Output

A `ChallengeStatusResult`:

- `view` — `thread` | `target`, plus the `addressed_*` echo of the key given.
- `target_resolution` — the addressed target re-resolved:
  `found-current | found-superseded | unresolvable`. **Disclosed, never
  refused** — a target legitimately moves (a superseding re-registration mints a
  new sha; a wiped store makes it unresolvable). Null when no challenge names the
  address.
- `entries` — the reduced per-challenge lines: `status`, filing date, the
  target address + its per-entry re-resolution, the `verdict` / `reasoning` when
  resolved, `grounds` (echoed verbatim, never parsed), and each cited evidence
  sha re-resolved at read (`verified` / unresolvable).
- `contested` — the C-status counts + ids (`open` / `upheld` / `dismissed` /
  `withdrawn` / `superseded` / `challenge_ids`). An **orthogonal** flag beside
  the target's own status — a `current` target reads `current` AND contested;
  challenge presence **never blocks** anything (C-status / C4).
- `skipped` — namespaces skipped during fleet collection (fail-open accounting).
- `render` — the deterministic markdown brief, relayed to the human verbatim.
- `view_sha` — the canonical-JSON sha of the projection.

## Boundary

The brief is **code-composed from the records' own fields** — dates, ids, sha
prefixes, reduced statuses — with **no urgency, recommendation, or
interpretation vocabulary** (the attention-queue no-urgency rule); `grounds` and
`reasoning` are echoed verbatim, never summarised. The `view_sha` is a **pure
function of the result data** — no wall-clock, no fleet accounting — so it is
byte-stable across calls and a `challenge-verdict` gate RECOMPUTES a carried
`view_sha` and it matches (the recomputable-render precedent). Every surface
routes through the ONE collector `state/challenges.py::standing_challenges`; this
verb moves **no state** and writes nothing.
