---
status: shipped
---
# The challenge attestation — structured dissent as a first-class record

**Status: IMPLEMENTED (2026-07-09).** Waves A–C landed (T1–T10); the drift log
at the foot records every deviation. Two C-disclose seats named in T6 are
DEFERRED with recorded reasons (run-story timeline events; the evidence
period-digest timeline) — see the drift log. The durable hand-off
(the `docs/design/notebook-audit.md` pattern): settled decisions with
recorded rationale, file-disjoint task waves for parallel Opus dispatch,
enforcement rows, and boundary-drift flags. Cite `path::symbol`, never line
numbers. Record implementation drift in the drift log at the foot of this
document. Sibling plans written concurrently: `docs/design/live-conformance.md`
and `docs/design/multi-human.md` — referenced below, never edited here.

## Product intent — the problem (user-ruled 2026-07-07)

**The substrate has rich machinery for approving and none for disagreeing
after commitment.** Greenlights, sign-offs, registrations, conclusions,
auto-clears — every one is an attestation FOR something
(`state/attestation.py` module docstring: "every trusted thing in the system
is one of these" — paraphrasing its actual wording, "the one primitive
every trusted record rides … instances of ONE object"). But science runs
on falsification, and the archive
currently remembers what you believed, not what survived attack. Concretely,
nothing today can record:

1. **"This conclusion is wrong"** — a later replication contradicts a
   recorded finding, and the only remedies are supersession by the original
   author's own re-conclusion or silence. A second party's structured
   disagreement has no object.
2. **"This sign-off should not have passed"** — a reviewer discovers,
   post-graduation, that an audited section's assertion was vacuous. The
   sign-off reads `signed_current` forever unless the source drifts.
3. **"This receipt's emitter was buggy"** — challenging CODE's work is
   legitimate dissent too: a render receipt journaled `error=False` by a
   broken emitter (the v1.5 truthfulness caveat,
   `docs/design/notebook-audit.md`) is trusted everywhere it is cited, and
   no record can stand against it.
4. **"This registration rests on refuted evidence"** — a failed
   reproduction lands as a FINDING (`ops/verify_reproduction.py` — exit-0,
   `needs_decision`) and then... nothing durable marks the registration it
   undermines. The finding scrolls away; the registration reads `current`.
   (Pre-implementation verification 2026-07-07: the ADDRESSABLE object for
   this case is the fingerprint-ledger SAMPLE the comparison appends —
   fingerprint D-consume appends every `verify-reproduction` comparison as
   a sample with a `content_sha`, resolvable via the `fingerprint`
   citation kind. The reproduction RECEIPT itself, in
   `_aggregated/<run_id>/reproduction_receipts.jsonl`, has NO
   `CITATION_KINDS` resolver — see C2/C3.)

The **challenge** is the missing object: a human-authored, evidence-bound,
sha-targeted attestation of DISSENT against a committed record — standing
(never consumed), disclosed wherever the challenged record is cited, never
blocking, resolved only by an equally effortful human verdict, append-only
forever. The notebook stops being a diary of beliefs and becomes a record of
what was attacked and what held.

## C1 — the spine: the challenge IS the nudge, generalized across the commitment boundary

This is the design's organizing recognition (user, 2026-07-07). The system
already has a first-class dissent speech-act: the **nudge** — the non-`y`
response at a decision rendezvous (`state/decision_journal.py` module
docstring: "the human answers with `y` (greenlight) or a natural-language
nudge"). A nudge is dissent DURING a decision: synchronous, targeting a
PENDING proposal, consumed by the redraft loop (the LLM re-drafts, the hash
moves, the next view supersedes it). The challenge is the SAME speech-act
lifted across the commitment boundary into the archive: asynchronous,
targeting a COMMITTED attestation, sha-bound to its target, holding STANDING
STATE instead of being consumed.

**One line: a nudge is a challenge against a proposal; a challenge is a
nudge against the archive.**

| | Nudge | Challenge |
|---|---|---|
| Timing | during the decision (synchronous) | after commitment (asynchronous — days, months later) |
| Target | the pending proposal in front of the human | a committed attestation, addressed by `{subject_kind, subject_id, content_sha}` |
| Lifecycle | consumed — the redraft loop absorbs it and the next view supersedes it | standing — `open` until a verdict record resolves it; never deleted |
| Evidence | the brief already on screen | MUST cite evidence shas (the record stands alone in the archive; the screen is gone) |
| Authorship | typed free text; a bare `y` is by definition not a nudge | identical bar, raised: typed, effortful, sha-naming |

The load-bearing consequence: **the challenge INHERITS the nudge's
authorship treatment wholesale.** The pipeline doctrine's asymmetry —
approval is cheap (`y`), dissent is where typing goes — already lives in
`ops/decision/journal.py` (`_is_bare_ack` refuses bare acks wherever a
record relaxes or asserts; the harness-utterance tier,
`_harness_human_texts`; token-exact naming, the #26 precedent; the 8+-hex
sha-prefix bar, registration-kernel R6). The challenge gate REUSES that
machinery — the same `_is_bare_ack`, the same tiered evidence source, the
same sha-prefix idiom — and never builds a parallel authorship stack
(enforcement row). A challenge is journaled through `append-decision` like
every other exchange; it is one more instance of the ONE attestation kernel,
not a new trust system.

## The settled design center (user-confirmed 2026-07-07 — DECIDED)

Everything in C1–C5 is settled; this document plans the consequences.
Departures during implementation are drift to be logged, not re-litigated.

### C2 — challengeable targets: any committed attestation, full-address bound

A challenge may target **any committed attestation the CITATION_KINDS
resolvers can address**: a conclusion, a registration, a notebook sign-off,
a scope unlock, a greenlight — and CODE's work too: an auto-clear record, a
render receipt, a fingerprint sample. Challenging a code attestation is
legitimate ("this receipt's emitter was buggy" is a claim about the world,
and the world contains bugs); what is NOT legitimate is code AUTHORING a
challenge (C3).

**The addressability boundary, honestly (pre-implementation verification
2026-07-07).** "Any committed attestation" is bounded by the closed
resolver vocabulary (C3): journal-riding records resolve via the
`attestation` kind; dossiers via `dossier`; fingerprint samples via
`fingerprint`. A **reproduction receipt** lives in an experiment-local
ledger (`_aggregated/<run_id>/reproduction_receipts.jsonl`) that NO
`CITATION_KINDS` member resolves — it is not addressable as a target (or
citable as evidence) until a receipt-ledger kind is added, which is a
reviewed vocabulary change to the closed set (the E6 form), deliberately
NOT made here. Near-term the failed-reproduction case rides the
fingerprint SAMPLE the same comparison appends (D-consume), which carries
the same evidentiary content and IS resolvable. Targeting a superseded
DOSSIER sha is likewise bounded: the `dossier` resolver recomputes the
LIVE signature only, so an old dossier sha is targetable via the
`attestation` kind over the registration record that carries it, or not
at all — a target the machine cannot resolve is refused at filing, which
is the R3 rejection working, not a gap to paper over.

**Target binding = the registration-kernel R3 full-address pattern** (a
bare slug was rejected there because a slug cannot be mechanically checked;
the same rejection applies here — a challenge against "the conclusion about
edge-x" is a challenge against nothing checkable). The target names
`{subject_kind, subject_id, content_sha}` of the challenged record, plus a
resolver dispatch kind, so the append gate can verify the target EXISTS as a
committed record at exactly that sha (shape in C-shape below). The
`content_sha` binding is what makes the challenge survive supersession
honestly: a challenge against sha X stays a truthful dated attack on X even
after a re-registration mints sha Y — and the reduction reads it
`superseded` mechanically (C4).

### C3 — evidence-bound; code never files dissent (SETTLED, with rationale)

A challenge MUST cite evidence shas — the challenger names what they rest
on: a failed replication's fingerprint sample (the resolvable form of "the
receipt" — see C2's addressability boundary), a contradicting run's
dossier, a fingerprint ledger sample. Citations reuse the evidence-memory citation
machinery verbatim (`state/evidence.py::CITATION_KINDS` + its per-kind
resolver dispatch — `docs/design/evidence-memory.md` E-shape): one closed
vocabulary, one set of resolvers, never a parallel copy. At append every
citation resolves against LIVE stores and a mismatch REFUSES (the E-shape
append posture; trusting a handed-in manifest is the receipt-laundering
hole). At read, re-resolution DISCLOSES (`cited (verified)` /
`cited (unresolvable here)`), never refuses — evidence legitimately moves.

**The promotion seam — SETTLED: the human promotes; code never files dissent
autonomously.** A failed reproduction IS a mechanical challenge in spirit —
but `verify-reproduction`'s mismatch STAYS a FINDING
(`docs/design/determinism-fingerprint.md`: "discovered nondeterminism is the
feature working" — exit-0, `needs_decision`, byte-unchanged), and the
challenge is the HUMAN act of promoting that finding into standing dissent:
an `append-decision` challenge record citing the receipt's sha. Recorded
rationale, three legs:

1. **The fabrication channel.** An LLM-driven agent that can file challenges
   autonomously is an actor that can mint dissent at zero cost — the exact
   inverse of the pipeline doctrine (dissent is where typing goes). Worse,
   the agent whose work is under audit would hold the pen that files
   disputes about the audit: a guard the LLM itself satisfies is not a
   guard, and a dissent ledger the LLM itself populates is not a dissent
   ledger (`docs/internals/engineering-principles.md`).
2. **Record noise.** Every flaky canary, every thin-envelope
   `needs_verdict`, every transient mismatch would become a standing
   contested flag on some record — the disclosure surfaces (C4) would train
   readers to ignore `contested`, which is rubber-stamp fatigue for
   dissent. Rarity buys seriousness (the D-attention rationale, applied to
   disagreement).
3. **Nothing is lost.** The mechanical record ALREADY exists — the receipt,
   the finding, the `reproduction-needs-verdict` attention item. The
   challenge adds precisely the judgment layer machines don't have: "this
   finding REFUTES that record" is a claim about meaning, and meaning is
   the human's column. The finding routes to the human (attention queue,
   VERDICT class); promoting it costs one typed `append-decision`.

Consequence: block `"challenge"` (and `"challenge-verdict"`) appear in NO
code-writer path — the same pin registration R6 carries ("the attestor is
ALWAYS human"), enforced identically.

### C4 — standing state: `contested` is DISCLOSED everywhere, blocks nothing

An open challenge makes its target read **`contested`** wherever the target
is cited — evidence-memory digests, `verify-registration` legs, the run
story, the attention queue — DISCLOSED, never revoked, never blocking.
Consumers decide: a registration MAY declare "no standing challenges" as a
prerequisite demand (the `evidence_meets` declarative pattern — C-registration
below), but core never auto-blocks on contest. This is the evidence-memory
T-NB posture applied to dissent: ten open challenges greenlight
byte-identically to none; the never-blocking pin is load-bearing and gets
its own task and enforcement row.

Resolution = a human verdict via `append-decision` (block
`"challenge-verdict"`): **upheld** (the refutation becomes a dated record;
the target's contested projection shows `challenge-upheld` wherever cited),
**dismissed** (with typed reasoning — dismissal is ALSO effortful; waving
dissent away with a bare `y` is exactly the asymmetry violation the nudge
machinery exists to prevent), or **superseded** (mechanical, no record
needed — the target's `content_sha` is no longer its subject's newest, so
the challenge reads superseded by construction; re-registration /
re-conclusion is the remedy for every upheld attack). Append-only
throughout; challenges are never deleted; a challenger may withdraw
(`"challenge-withdraw"`, mandatory typed reason — the R7 revoke form).

### C5 — attention routing: an open challenge is a verdict-class item

An open challenge is a `verdict`-class attention-queue item (the human's
judgment is what's pending — `docs/design/attention-queue.md` D2). Fan-out
follows the leverage-primary rule: the count of pending downstream subjects
the CONTESTED record blocks, walked over encoded edges only — a contested
registration prerequisite counts the registrations whose chains name it (the
R8 edge, reused); a contested record nothing names counts 0 and falls
through to class order. A contested registration is high-leverage by
construction — it sits on the capital boundary.

## Decisions settled in THIS document

### C-shape — the record, exactly

**Scope kind: a NEW kind `"challenge"`** in
`state/decision_journal.py::SCOPE_KINDS`, path branch →
`.hpc/challenges/<challenge_id>.decisions.jsonl` (+ the
`_wire/actions/decision_journal.py::ScopeKind` literal, schema regen).
Weighed against riding the TARGET's journal, and settled AGAINST it, with
reasons — this deliberately spends the no-new-store posture's one currency
(a path branch) rather than violating it (a new store):

- **Cross-scope targets.** A challenge may target a conclusion, a
  registration, a notebook sign-off, or a run-scoped greenlight — riding the
  target's journal scatters ONE record family across four+ path branches,
  and every target family's reduction (`state/notebook_audit.py`,
  `state/registration.py`, the conclusion reduction) would forever filter
  cross-family noise (the R9 rejection, verbatim).
- **Some targets have NO journal to ride.** A fingerprint sample lives in
  `_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl` — an experiment-local
  ledger, deliberately not a decision journal
  (`docs/design/determinism-fingerprint.md` D-store). A challenge against a
  ledger-resident record would have nowhere to land under the
  ride-the-target rule; and writing dissent INTO a measurement ledger would
  corrupt the measurements-vs-control-decisions store split that D-store
  settled.
- **The challenge thread is its own object.** Filing, verdict, withdrawal
  form one conversation under one `challenge_id` — the per-id journal IS the
  thread (the conclusion/registration precedent). The D3/T7 rule — one path
  branch per attestation FAMILY — lands on the same answer.

`challenge_id` is a caller-authored filesystem-safe slug (the `RunIdStrict`
class — it becomes a path segment), never core-invented.

**Block names:** `"challenge"` (filing), `"challenge-verdict"`
(resolution), `"challenge-withdraw"` (challenger withdrawal). Each refused
for any `scope_kind` other than `"challenge"` and vice versa (the
`scope-unlock` / R6 block-convention mirror, both directions).

**`resolved` fields at filing (all validated server-side at append):**

```json
{"challenge_id": "<caller-authored slug — RunIdStrict class, path segment>",
 "target": {"kind": "<CITATION_KINDS member — the resolver dispatch>",
            "subject_kind": "<the challenged record's subject_kind>",
            "subject_id": "<its subject_id — opaque>",
            "content_sha": "<the exact sha being challenged>",
            "scope": {"scope_kind": "...", "scope_id": "..."}},
 "citations": [{"kind": "<CITATION_KINDS member>",
                "ref": "<opaque id>",
                "sha": "<full sha the evidence carries>"}, "..."],
 "grounds": "<the human's free-text dissent — opaque, echoed, never parsed>"}
```

- **`target` is citation-shaped plus the R3 full address.** The `kind`
  dispatches target resolution through the SAME `CITATION_KINDS` resolver
  table evidence-memory owns (one definition — `dossier` / `run` /
  `fingerprint` / `attestation`; the `attestation` kind's `scope` names
  which journal to reduce). The gate verifies at append that a committed
  record with this `{subject_kind, subject_id}` exists at exactly this
  `content_sha` — **you cannot contest what the machine cannot find.** The
  record challenged need not be the NEWEST for its subject (challenging a
  superseded record is permitted and immediately reads `superseded` —
  harmless, honest, dated). Pre-implementation verification (2026-07-07):
  this means the gate's EXISTENCE check for the `attestation` kind scans
  the named journal's committed records for the asserted sha — it cannot
  route through the newest-wins `reduce` alone, which by construction never
  surfaces a non-newest record; `reduce` serves the SUPERSEDED computation
  (C-reduce), the existence scan serves filing. The per-kind resolvability
  limits (dossier, reproduction receipts) are recorded in C2.
- **`citations` MUST be non-empty** — the evidence-bound rule (C3). A
  challenge with no evidence is an opinion, and opinions ride chat, not the
  archive.
- **`grounds` is opaque caller prose**: stored, rendered verbatim, never
  interpreted (the `finding` field's discipline,
  `docs/design/evidence-memory.md` E-shape).

**Verdict record (`block="challenge-verdict"`) `resolved`:**
`{challenge_id, verdict: "upheld" | "dismissed", reasoning: "<free text,
mandatory non-empty>"}`. **Withdrawal:** `{challenge_id, reason: "<free
text, mandatory>"}`. Neither binds a new sha (they withdraw or judge; the
R7 revoke precedent) — but both face the full authorship bar (C-gate).

**The attestation projection (filing):** `attestor="human"`,
`subject_kind="challenge"`, `subject_id=<challenge_id>`,
`content_sha=<canonical-JSON sha over {verified target address, verified
citations list}>` (the harness sha canonicalization,
`docs/internals/harness-contract.md`), `view_sha` OPTIONAL (present when the
human signed after reading a rendered `challenge-status` or
`verify-registration` brief — binds what-they-saw). Bound via
`state/attestation.py::bind` with `recompute` = the server-side
re-canonicalization of the target+citations the gate just resolved — the
challenge is hash-locked to what it attacks and what it rests on; neither
can be asserted into existence.

### C-status — `contested` is an ORTHOGONAL dimension, not a fifth status (SETTLED)

The registration status vocabulary is
`current | stale | revoked | superseded | absent`
(`docs/design/registration-kernel.md` R7); the kernel reducer's is
`current | stale | absent`. **`contested` joins NEITHER — it is a parallel
flag computed by a separate reduction, and a record can be `current` AND
`contested`.** Recorded rationale, three legs:

1. **Different questions, different attestors.** The status vocabulary
   answers a MECHANICAL question — "does the recompute still hold?" —
   decidable by code from shas. `contested` answers a JUDGMENT question —
   "does a human stand against this?" — decidable only by reading the
   challenge journal. Folding them collapses "the evidence moved" and "a
   human disagrees" into one word, and every consumer loses the ability to
   distinguish drift (re-run the pipeline) from dispute (read the grounds,
   judge).
2. **A fifth status would grant revocation power core must not grant.**
   Every existing consumer of `status != "current"` (the caller-side deploy
   refusal, R8; prerequisite currency checks, R3) would silently start
   BLOCKING on contest the day `contested` became a status — a challenge
   would revoke by vocabulary. That violates C4's never-blocking rule at
   the type level: disclosure means the consumer sees BOTH dimensions and
   decides; a merged vocabulary decides for them.
3. **The upheld case proves the split.** Even an UPHELD challenge does not
   flip the target's status — the target family's own append-only remedy
   does (a `registration-revoke` record, a superseding re-registration, a
   superseding conclusion), filed by the human the verdict convinced. The
   challenge machinery never mutates the target's journal; it stands beside
   it. Status moves only through the target's own vocabulary.

**The projection shape** every disclosure seat carries:
`contested: {open: n, upheld: n, dismissed: n, withdrawn: n, superseded: n,
challenge_ids: [...]}` — counts and identities, never a severity score (the
attention-queue D1 no-urgency rule). A target with all-zero counts omits the
block (the `reproduces` emitted-only-when-present precedent).

### C-reduce — one reduction, one collector

`state/challenges.py` (new) owns:

- **The per-challenge reduction**: records for a `challenge_id` reduce to
  `open | upheld | dismissed | withdrawn | superseded`, routing through
  `state/attestation.py::reduce` for the base drift posture plus
  winner-selection over the verdict/withdraw blocks (the
  `state/registration.py` reduction form). `superseded` is COMPUTED, not
  recorded: the reduction re-resolves the target's subject through the
  target's `kind` resolver and, when the subject's newest `content_sha` no
  longer equals the challenged sha, the challenge reads `superseded`
  regardless of verdict records (drift-for-free, the R7 "no revocation
  state machine" property — mirrored for dissent). An explicit verdict on a
  since-superseded challenge remains in the record; the projection reports
  both (`superseded` wins the headline, the verdict is disclosed).
- **`standing_challenges(experiment_dir, *, content_sha=None, subject_kind=None,
  subject_id=None)`** — the ONE collector every disclosure seat routes
  through (the attention-queue D5 discipline; `inspect.getsource` pins per
  consumer): a non-creating glob over `.hpc/challenges/*.decisions.jsonl`,
  tolerant-read, reduced per id, filtered by target address. Matching keys
  on the target's `content_sha` (exact — the full address's discriminator);
  `subject_kind`/`subject_id` narrow it. Fleet mode rides
  `ops/attention_queue.py::discover_fleet_experiments` unchanged.

### C-gate — the authorship gate, the nudge machinery reused

`ops/decision/journal.py::_assert_challenge_authorship` — the
`_assert_registration_authorship` / `_assert_conclusion_authorship` sibling
(same three-lock structure; C1's inheritance made mechanical):

1. **Lock 1 — no affordance:** append-decision under block `"challenge"` is
   the ONLY write path. No primitive named challenge / contest / dispute /
   refute in the mutate registry; no chain, no next_block, no skill
   affordance (the no-unlock-verb doctrine,
   `docs/design/rigor-primitives.md`). **The verb question is SETTLED:
   challenges land via `append-decision` exactly like sign-offs,
   registrations, and conclusions** — the no-affordance lock-1 posture. An
   LLM-drivable challenge verb would be C3's fabrication channel with a
   schema; the missing affordance is the lock. `challenge-status` (below)
   is `verb="query"`, read-only.
2. **Lock 2 — recompute, un-fakeable:** the target resolved server-side
   through its `kind`'s resolver and confirmed committed at the asserted
   `content_sha`; every citation resolved and sha-compared; `content_sha`
   bound via `attestation.bind` against the re-canonicalized verified set;
   `challenge_id` slug-validated; `grounds` non-empty. Any mismatch refuses
   with the recorded-vs-recomputed pair.
3. **Lock 3 — authorship, the raised bar:** bare acks refused
   (`ops/decision/journal.py::_is_bare_ack` — imported, never a second
   list). The response must name the `challenge_id` token-exact (the #26
   floor), AND the TARGET's `content_sha` by an 8+-hex prefix (you must
   name what you attack), AND at least one cited evidence sha by an 8+-hex
   prefix (you must name what you rest on) — the R6 sha-prefix rationale
   verbatim: an 8-hex prefix exists nowhere in prior vocabulary and can
   only derive from the presented evidence. Under harness capability 1
   (`docs/internals/harness-contract.md`) the tokens derive from the
   out-of-band utterance log (full-strength tier); absent it, the
   journal-response friction tier applies, honestly named weaker. There is
   NO auto-cleared tier and NO code path: the attestor is ALWAYS human.

`_assert_challenge_verdict_authorship` (may be one function with the filing
gate — implementation's call, recorded as drift if merged): the verdict and
withdrawal face the SAME floor — non-bare, `challenge_id` token-exact,
mandatory free-text `reasoning`/`reason`, and for a DISMISSAL the response
must additionally name at least one of the challenge's cited shas by prefix
(dismissing evidence requires engaging it — dismissal is effortful by
construction, C4). An upheld verdict needs no extra sha (upholding agrees
with evidence already bound into the record it resolves).

**Resolver identity — the RULING is the multi-human sibling's; the GATE
CODE is ours.** Core today has one human; the gate cannot distinguish
challenger from resolver, and this plan deliberately does NOT invent an
identity field for it. The multi-human plan owns the ruling and the actor
plumbing (`docs/design/multi-human.md` MH7 consuming MH1–MH4); the check
itself lands in THIS plan's verdict gate — cross-doc verification
2026-07-07 resolved an ownership gap where each doc's task list pointed at
the other and neither implemented it. Recorded assignment: **whichever
plan lands SECOND adds, as a follow-up extension of T5 here, citing MH7:**
when >1 actor is declared, the verdict gate refuses
`resolver == challenger` (comparing the two records' `attestor_id`s) and
refuses an unattributed resolution; the WITHDRAWAL gate likewise refuses
`withdrawer != challenger` (a second actor silencing another's standing
dissent is the suppression channel, worse than self-adjudication);
zero/one actor declared → silent, byte-identical. This document pins today
that the verdict and withdrawal are SEPARATE records from the filing (so
the constraints are expressible later without a record-shape change).

### C-verb — `challenge-status`, the one read-only query

**`challenge-status`** — `verb="query"`, `side_effects=[]`,
`idempotent=True`, `requires_ssh=False`, agent-facing, MCP-exposed (the
`verify-registration` / `notebook-status` posture). Spec: a `challenge_id`
(the thread view) OR a target address `{content_sha}` /
`{subject_kind, subject_id}` (the "what stands against this record?" view);
optional `fleet`. Result: the reduced per-challenge statuses, the target's
re-resolution (`target: found-current | found-superseded | unresolvable`),
per-citation `cited (verified) | cited (unresolvable here)` (the E read
posture — disclose, never refuse), and a code-rendered markdown brief whose
canonical-JSON sha is the `view_sha` a subsequent verdict may carry
(`ops/relay_render.py` posture; deterministically renderable → recomputable,
the v1.6 precedent, so the verdict gate RECOMPUTES a carried `view_sha`).
Naming settled against the registry idiom: `notebook-status` /
`challenge-status` parallel; `verify-challenge` rejected (the `verify-*`
family recomputes prerequisite chains; this reduces a journal).
`_wire/queries/challenge_status.py` faces the `_FORBIDDEN_FIELD_NAMES`
schema walk. Registry +1 (148 expected after the slate + evidence-memory
per `docs/design/evidence-memory.md` → 149 — but the CONCURRENT siblings
also move the count: live-conformance +2, multi-human +1, in whatever
post-slate order lands; verify against `hpc-agent capabilities` at
implementation, never against a doc's frozen number).

### C-disclose — the disclosure seams (which readers surface `contested`)

Every seat routes through `state/challenges.py::standing_challenges` —
never a private re-collection (enforcement row). Additive fields only;
readers tolerate them; no chain/next_block change anywhere:

| Seat | What it gains |
|---|---|
| `verify-registration` (`ops/registration/verify_op.py`, registration T5) | the registration's own `contested` block + a `contested` block per prerequisite leg whose `content_sha` has standing challenges. Status stays whatever R7 computes — the flag rides beside it (C-status). The rendered brief prints one line per open challenge: `contested · <challenge_id> · filed <date> · cites <sha8>…` |
| evidence-memory digests (`state/evidence.py::collect_evidence` → `ops/evidence_render.py`) | conclusion lines gain `contested (1 open · c7a1f2e3)` when the conclusion's `content_sha` is challenged; the period digest's timeline includes challenge filings and verdicts as dated one-liners |
| the run story (`ops/run_story.py`) | challenge/verdict records whose target address names the run's subjects appear as timeline events (pure ordering/identity — the story never judges) |
| the attention queue (`ops/attention_queue.py`) | the C5 item kind (C-queue below) |
| `challenge-status` | the primary thread/target view (C-verb) |

The registration prerequisite checker (`ops/registration/prereqs.py`, R3)
does NOT gain an implicit contested check — a contested prerequisite that
still recomputes CURRENT passes the chain (never-blocking). The DECLARATIVE
demand is the only gate seat:

**C-registration — the `uncontested` demand (a reviewed vocabulary
change, coordinated with the registration plan).** A template prerequisite
entry MAY declare `requires: {"uncontested": true}` — checked by COUNTING
(`standing_challenges(content_sha=<entry sha>)` open-count == 0), the
`evidence_meets` declarative pattern: the caller opts in, core counts, core
never decides. `uncontested` becomes the one cross-kind `requires` key
(every `PREREQUISITE_KINDS` member accepts it, including the otherwise
requires-free generic `attestation` kind — recorded as a deliberate
amendment to R3's "accepts NO requires" line, because uncontested is a
mechanism property core CAN check, which was that line's whole test).
Unknown-key loud-refusal is unchanged for everything else. A registration
may likewise demand its OWN record be uncontested only caller-side (the
consumer reads `verify-registration`'s contested block — the deploy-refusal
seat, R8).

### C-queue — the attention item

New `ops/attention_queue.py` kind **`challenge-open`**, class `verdict` (a
human judgment is pending — the queue's namesake class). D5 route-through:
the collector calls `standing_challenges` (the one reduction), never
re-reads journals. `since` = the filing record's `ts` (the item AGES — old
unresolved dissent is the signal). Fan-out (`_apply_fanout`): the count of
pending registrations whose prerequisite chains name the contested
`content_sha` (a non-creating read of `.hpc/registrations/*.decisions.jsonl`
joined on chain-entry shas — the R8 edge pattern; a contested registration
prerequisite blocks capital, high-leverage by construction). No other
encoded edge exists yet → other targets count 0 and fall through to class
order. A DISMISSED or UPHELD challenge yields no item (resolved); an upheld
challenge whose target family has not yet moved (no revoke, no
re-registration) yields an `informational` item **`challenge-upheld-unremedied`**
— awareness that the archive contains a standing refutation nothing has
answered; fan-out 0; never blocking (the E-queue `campaign-unconcluded`
form: the loop-closing invitation, not a gate).

## Agnosticism (each an enforcement row)

1. **Opaque-by-construction.** `grounds`, `reasoning`, `subject_id`s,
   citation refs: identity-compared, counted, echoed — never read for
   meaning. Core owns only mechanism nouns: the block names, the reduction
   statuses, the reused `CITATION_KINDS`.
2. **No invented defaults.** No default `challenge_id`, no default target,
   no default verdict — the fabrication class.
3. **No severity, no merit, no reputation.** A challenge has no score, no
   "strength" field, no challenger track record — counts and dates only
   (the D1 no-urgency rule; a merit score is core grading dissent, which is
   core doing judgment).
4. **Toy-domain fixtures only.** The toy-widgets lineage challenges a
   widget conclusion / a widget-batch registration; never harxhar/quant
   vocabulary (the domain-packs fixture rule).
5. **Boundary-drift flags written before implementation** (below).

## Task waves (file-disjoint, for parallel Opus dispatch)

**Sequenced AFTER evidence-memory** (`docs/design/evidence-memory.md`, itself
after slate Phases 1–5 + proving run #10 per
`docs/design/slate-sequencing.md`): this plan REUSES `CITATION_KINDS` + the
citation resolvers (evidence T1), the registration reduction + prereq
checker (registration T1/T4/T5), the fingerprint ledger (fingerprint T3),
and the conclusion journals as challengeable targets. Standing slate rules
apply: regen commits strictly serial; enforcement-map edits append-only and
serialized; every wave ends regen → full suite → commit → push → CI green;
every task lands a fires+passes pair. Hot-file coordination:
`state/decision_journal.py`, `ops/decision/journal.py`, and
`ops/attention_queue.py` are the slate's named hot files — our Wave C
serializes behind evidence T7/T8/T10 (the last in-flight editors) AND
behind/around the concurrent siblings on the same files
(`docs/design/live-conformance.md` T7/T8 and `docs/design/multi-human.md`
MT7 also edit `ops/decision/journal.py` / `ops/attention_queue.py`; no
mutual post-slate order is recorded in `docs/design/slate-sequencing.md`
yet — treat these edits as strictly serial in whatever order executes,
and record the executed order in the drift logs).

**Wave A (parallel — new files only):**

- **T1** `state/challenges.py` (new) — the block-name constants + the
  filing/verdict/withdraw record validation + the target-address model +
  the per-challenge reduction (`open|upheld|dismissed|withdrawn|superseded`,
  routing through `state/attestation.py::reduce` + the computed-superseded
  re-resolution; route-through `inspect.getsource` assertion) +
  `standing_challenges` (non-creating glob, tolerant read, address filter)
  + the canonical target+citations sha helper. Citation/target resolution
  DISPATCHES to `state/evidence.py`'s resolver table — imported, never
  copied (route-through pin). Pre-implementation verification (2026-07-07):
  per evidence-memory's "dispatch placement" correction, the `dossier`
  resolver is an `ops` seam and the state substrate never imports `ops` —
  so `state/challenges.py` takes the dossier resolver as an injected
  callable exactly as `state/evidence.py` does, composed at the ops
  callers (T3's verb, T5's gate). Tests: crafted journals; every refusal fires
  (empty citations, unknown target kind, unresolvable target, verdict on
  unknown id); superseded-by-re-resolution; withdraw-wins; a current AND
  contested target reduces to both truthfully.
- **T2** `_wire/queries/challenge_status.py` (new) — spec + result
  (C-verb). Tests: at least one of id/address required; the
  `_FORBIDDEN_FIELD_NAMES` walk.

**Wave B (after A, parallel):**

- **T3** `ops/challenge_status_op.py` (new) — the `challenge-status`
  `@primitive` + the code-rendered brief + its canonical-JSON `view_sha`
  (deterministic projection → gate-recomputable, the v1.6 rule). Fleet via
  `discover_fleet_experiments`, `skipped` accounting, non-creating (test:
  no directory created under a fresh journal home). Registry +1; ALL SIX
  regen scripts after the wave (the dev_regen_list lesson); primitive doc
  page; `_SPEC_VERBS` inventory tails.

**Wave C (sequential — hot files, one at a time, after in-flight waves):**

- **T4** `state/decision_journal.py` — the `"challenge"` scope kind + path
  branch (`.hpc/challenges/`) + the `ScopeKind` wire literal + schema regen
  + contract tests in lockstep (the notebook-T7 / registration-T6 /
  conclusion-T7 precedent, serialized behind all of them).
- **T5** `ops/decision/journal.py` — `_assert_challenge_authorship` (three
  locks) + the verdict/withdraw floor, wired beside
  `_assert_conclusion_authorship`. Fire tests per lock: fabricated target
  sha, unresolvable target, fabricated citation sha, empty grounds, bare
  ack, response missing the target-sha prefix, response missing the
  evidence-sha prefix, a bare-ack dismissal, a dismissal not naming a cited
  sha, a `"challenge"` block under a non-challenge scope_kind (and vice
  versa), an agent-tier response under a present utterance log.
- **T6** the disclosure seams — `ops/registration/verify_op.py` (the
  contested blocks + brief lines) and `state/evidence.py` /
  `ops/evidence_render.py` (the conclusion-line flag + period-timeline
  entries) and `ops/run_story.py` (timeline events). Three files, three
  commits if concurrently hot; each seat's test pins the
  `standing_challenges` route-through and pins that status/ordering
  elsewhere in the seat is byte-unchanged when no challenge exists.
- **T7** `ops/attention_queue.py` — `challenge-open` (verdict class) +
  `challenge-upheld-unremedied` (informational): `KIND_CLASS` entries,
  collectors routing through `standing_challenges`, the registration-chain
  fan-out edge, D5-table rows, route-through pins. Serialized behind
  evidence T10.
- **T8** `state/registration.py` + `ops/registration/prereqs.py` — the
  `uncontested` cross-kind `requires` key (C-registration): counting via
  `standing_challenges`, loud refusal preserved for every other unknown
  key, the R3 "attestation accepts NO requires" line amended with the
  recorded reason. Fire tests: a contested prerequisite under an
  `uncontested` demand fails the chain naming the challenge ids; the same
  chain WITHOUT the demand passes byte-identically (never-blocking).
- **T-NB — the NEVER-BLOCKING pin (its own task, deliberately — the
  evidence-memory T-NB form):**
  `tests/contracts/test_challenge_boundary.py::test_contest_never_blocks` —
  (a) mechanical: source scan over every disclosure seat asserting no
  raise/gate branch keyed on challenge presence outside the declarative
  `uncontested` checker; (b) behavioral: a fixture namespace with ten open
  challenges against a run's conclusion and its registration's
  prerequisites greenlights, submits, and verifies with a byte-identical
  decision surface to an unchallenged namespace (absent any `uncontested`
  demand). This is C4 mechanized against future contributors — the single
  most likely line to be crossed by someone "just making contested records
  fail safe".
- **T9** `tests/contracts/test_challenge_boundary.py` — the remaining
  enforcement rows + toy fixtures (widget-lineage challenges only).
- **T10** this doc — status flip + drift log.

## Enforcement rows (accrue to `docs/internals/engineering-principles.md` maps)

| Rule | Enforced by | Fires when |
|---|---|---|
| **NEVER-BLOCKING by default (load-bearing):** challenge presence never gates, refuses, or reshapes any core path; the ONLY demand seat is a caller's declarative `uncontested` prerequisite; ten open challenges decide byte-identically to none | T-NB (source scan + behavioral byte-equality) | any raise/gate keyed on contest lands outside the `uncontested` checker — C4 re-litigated in code |
| **No agent-authored dissent:** the challenge/verdict/withdraw attestor is ALWAYS human; no code path appends the `"challenge"`-family blocks; no mutate verb named challenge/contest/dispute/refute; mechanical findings stay findings until a human promotes them | T5 fire tests + the no-affordance registry pin + the blocks absent from every code-writer block set | a mechanical writer gains a challenge block, an auto-filing seam appears at the mismatch FINDING, or a challenge verb lands |
| **Disclosure routes through the ONE collector:** every seat that surfaces `contested` calls `state/challenges.py::standing_challenges`; no reader re-globs or re-reduces | route-through `inspect.getsource` pins per seat (T6/T7/T8 tests — the attention-queue D5 form) | a seat re-implements the reduction or the address match |
| Challenge attestations route through the ONE kernel — bind, reduce, winner-selection never re-inlined; target/citation resolution dispatches to the evidence-memory resolver table, never a copy | T1 route-through assertions (the accruing-member rule on the attestation row) | a challenge path bypasses `state/attestation.py::bind`/`reduce` or grows a second resolver table |
| Target and citations verify at append, server-side: the target must exist committed at the asserted sha; every citation resolves live; no resolved-field sha is trusted-then-recorded | T5 fire tests (fabricated target sha refused; unresolvable target refused; fabricated citation refused) | the gate trusts an asserted sha — the receipt-laundering hole, at the dissent boundary |
| **Dissent AND dismissal are effortful — the nudge bar inherited, never forked:** bare acks refused via the ONE `_is_bare_ack`; filing names target-sha + evidence-sha prefixes; dismissal names a cited sha; the tier machinery is the shared `_harness_human_texts` path | T5 fire tests + an `inspect.getsource` pin that the challenge gate calls the shared helpers (no second ack list, no second tier fork) | a parallel authorship stack appears, or the bar softens to a bare ack in either direction |
| `contested` is orthogonal: no member of any status vocabulary; a `current` target with an open challenge reads `current` AND contested everywhere | T1/T6 tests (status unchanged under contest; contested block present) | `contested` lands in a status enum, or a reducer starts demoting a contested `current` |
| Append-only, forever: no deletion, no in-place edit; withdraw and verdict are NEW records; the challenge journal has no rewrite path | the existing journal append-only discipline + a T9 write-probe over `state/challenges.py` (read-only module: no write beyond none) | a "remove challenge" affordance appears |
| Toy-domain fixtures only | T9 token denylist scan over the challenge tests/fixtures | a real domain word lands in a fixture |

## Boundary-drift flags (the Q1 watch list — written before implementation)

- **No challenge verb the LLM can drive, ever.** Pressure for a "file
  challenge" primitive or skill step is the fabrication channel asking for
  a schema — the answer stays `append-decision` under the gated block, with
  the human typing the grounds. Soften only via richer harness-captured
  utterances, never an affordance.
- **Code never promotes its own findings.** The verify-reproduction
  mismatch, the lint flag, the failed canary stay FINDINGS routed to human
  attention. The day an "auto-challenge on repeated mismatch" heuristic
  lands, C3's three rationale legs have all been re-broken at once.
- **Core never reads `grounds` or `reasoning` for meaning.** The moment a
  branch keys on dissent TEXT ("if grounds mentions leakage…"), the line is
  crossed — classification of dissent is pack/caller territory.
- **`contested` never becomes blocking, and never becomes a status.** Watch
  both pressures: a consumer seat "failing safe" on contest (T-NB's line),
  and a reducer folding the flag into its vocabulary (C-status's line).
  Demanding uncontested evidence is exclusively the caller's declarative
  `uncontested` prerequisite.
- **No merit scoring, no challenger reputation, no dissent analytics.** A
  challenge is identity + evidence + dates. Ranking challenges by
  "strength" is core grading arguments — the fabrication class wearing a
  judicial robe.
- **Resolver≠challenger stays reserved until the actor plumbing exists.**
  Do not invent an identity field here; the multi-human plan
  (`docs/design/multi-human.md` MH7) owns attributed authorship and the
  ruling, and the separation check (plus withdrawer==challenger) lands as
  the recorded T5 follow-up in THIS plan's gate once that plumbing has
  landed (C-gate). Until then the single-operator honesty is recorded, not
  papered over.
- **The upheld remedy stays in the target's vocabulary.** An upheld
  challenge INVITES a revoke / re-registration / superseding conclusion; it
  never performs one. A "cascade revocation" feature is the challenge
  machinery mutating journals it does not own.

## Related docs

- `docs/design/notebook-audit.md` — the attestation kernel this
  instantiates; the D-attention asymmetry (approval cheap, dissent typed)
  the challenge inherits; the hand-off pattern this document follows.
- `docs/design/registration-kernel.md` — R3 full addresses (the target
  binding), R6 sha-prefix bar (reused), R7 append-only dated evidence, R8
  consumer seats; the `uncontested` demand amends its `requires`
  vocabulary (C-registration).
- `docs/design/evidence-memory.md` — `CITATION_KINDS` + resolvers (reused
  for targets and citations), the append-refuses/read-discloses posture,
  the T-NB never-blocking pin form, the conclusion object an upheld
  refutation invites.
- `docs/design/determinism-fingerprint.md` — mismatch-is-a-FINDING (the
  promotion seam's source side); the receipts and samples that serve as
  challenge evidence and as challengeable code attestations.
- `docs/design/attention-queue.md` — item classes, leverage fan-out, the
  D5 route-through collector rule.
- `docs/design/slate-sequencing.md` — this plan slots after
  evidence-memory (which is after Phase 5 + proving run #10).
- `docs/design/multi-human.md` (sibling, concurrent) — resolver≠challenger
  and attributed authorship live there; this plan reserves the hook.
- `docs/design/live-conformance.md` (sibling, concurrent).
- `docs/internals/harness-contract.md` — the sha canonicalization; the
  capability-1 utterance tiers the authorship locks ride.
- `docs/internals/engineering-principles.md` — the Q1 boundary the flags
  patrol; the enforcement maps the rows accrue to.

## Implementation drift log

Each deviation with its recorded reason (the `docs/design/notebook-audit.md`
form). Executed hot-file order (the task-wave section asked for it): Wave A/B
(T1 `state/challenges.py`, T2 `_wire/queries/challenge_status.py`, T3
`ops/challenge_status_op.py`) landed on their own branches and merged into
`br-ch-c`; Wave C then landed strictly serially on `br-ch-c` — **T4 → T5 → T6 →
T7 → T8 → T9 → T10** — with no other editor concurrently on `state/decision_journal.py`
/ `ops/decision/journal.py` / `ops/attention_queue.py` during the run (the
concurrent siblings `live-conformance` / `multi-human` had not landed on this
branch, so their hot-file coordination was moot here — recorded for the eventual
merge).

**Wave A (T1/T2) — recorded deviations:**

- **Injected-superseded reduction.** `reduce_challenge` is PURE over the record
  list and takes `superseded` as an INJECTED bool; the collector
  (`standing_challenges`) computes it via `resolve_target_current` and passes it
  in. Reason: `state` never imports `ops`, so the dossier resolver must be
  injected at the ops caller — the reduction cannot itself re-resolve the target,
  so the supersession input is computed one layer out and injected (the
  evidence-memory dispatch-placement rule, mirrored). The headline still reads
  `superseded` regardless of verdicts (C-reduce preserved).
- **Two-function target-resolver split.** The plan named one target resolution;
  the implementation split it into `resolve_target_existence` (the FILING check —
  the `attestation` kind SCANS the named journal for the asserted sha so a
  NON-newest record is findable, C2) and `resolve_target_current` (the newest-wins
  re-resolution the reduction uses to compute `superseded`). Reason: the two ask
  different questions (C-shape pins the existence scan cannot route through the
  newest-wins `reduce`); one function conflating them would have made a
  superseded-target challenge unfileable.
- **In-module render + `computed_at`.** The `challenge-status` brief is rendered
  in-module (T3 op) rather than via a shared `ops/relay_render.py` helper, and the
  result carries a `computed_at` timestamp field. Reason: no existing shared
  render seam matched the challenge brief shape (the `notebook-status` /
  `verify-registration` briefs are likewise op-local); `computed_at` dates the
  whole projection (the evidence-digest precedent) so a carried `view_sha` is
  recomputable against a known instant.

**Wave C (T4–T10) — recorded deviations:**

- **T5 gate is THREE functions, not one.** C-gate allowed "may be one function
  with the filing gate". The implementation has `_assert_challenge_filing_full`,
  `_assert_challenge_verdict_authorship`, and `_assert_challenge_authorship`
  (the dispatch) — SEPARATE records for filing vs verdict/withdrawal (as C-gate
  requires so the resolver≠challenger constraint stays expressible later without a
  record-shape change). No merge occurred; recorded for completeness.
- **T6 — evidence conclusion-line seam uses a LAZY import.** `state/evidence.py`
  imports `standing_challenges` INSIDE `_conclusion_contested` (function-local),
  not at module top. Reason: `state/challenges.py` imports `state/evidence.py`
  (the `CITATION_KINDS` resolver table), so a module-level back-import would be a
  cycle. The route-through pin (`inspect.getsource`) still holds.
- **T6 — two named C-disclose seats DEFERRED (with reasons).** The verify-registration
  seat (the capital boundary) and the evidence-memory CONCLUSION-LINE flag landed.
  Two others in the C-disclose table are deferred:
  - **The run-story timeline events** — deferred because `ops/run_story.py`'s
    stream vocabulary is a CLOSED set pinned EQUAL to the dossier's source stores
    (`tests/contracts/test_run_story_boundary.py::_EXPECTED_STREAMS`). Adding a
    `challenges` stream is a reviewed dossier-source vocabulary change touching the
    dossier definition, NOT the additive "readers tolerate a new field" seam
    C-disclose intends. Left as a scoped follow-up rather than silently expanding a
    cross-cutting vocabulary during a salvage.
  - **The evidence PERIOD-digest timeline entries** — deferred because surfacing
    challenge filings/verdicts as period-timeline one-liners needs a NEW field on
    the central `EvidenceCollection` + `render_period` threading, materially
    expanding the hot collector's contract. The conclusion-line flag already
    delivers `contested` disclosure in the primary evidence digest; the period
    timeline is a follow-up.
- **T6 — a real-fixture case was ADDED** to the dead agent's stub-only
  verify-registration test (`test_real_challenge_journal_surfaces_contested`) so
  the seat is exercised end-to-end through the LIVE collector, per the plan's
  "monkeypatch/stub + a real-fixture case".
- **T7 — the contested `content_sha` rides `evidence`.** `AttentionItem` has no
  `content_sha` field; the `challenge-open` fan-out edge reads the sha from
  `item.evidence["content_sha"]` (set by the collector) rather than a new item
  field — additive, no wire-shape change.
- **T8 — `_apply_uncontested_demand` downgrades to `stale`.** An unmet
  `uncontested` demand reads the existing `stale` verdict (naming the challenge
  ids), not a new status — keeping `contested` orthogonal (C-status) while the
  caller-declared gate blocks through the ordinary currency vocabulary.
- **Schema regen debt (inherited, NOT challenge scope).** The Wave-A/B branch
  merges left sibling schema files uncommitted (`evidence_brief`,
  `evidence_period`, `pack_*`, and a `resolve_submit_inputs.output.json` evidence
  drift); `challenge_status.{input,output}.json` were committed here (mine). The
  broad `test_schema_models_roundtrip[evidence_brief.input.json]` fails on that
  inherited evidence-memory schema (a missing `_CROSS_FIELD_OVERRIDES` entry the
  evidence author owns) — surfaced by regen, not caused by challenge work, and not
  in this plan's targeted suites. Left for the evidence-memory/pack merges' regen.
