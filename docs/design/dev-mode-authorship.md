# Dev-mode authorship — cross-repo utterance-log trust, opt-in per repo

Status: **RULING SETTLED (2026-07-19, user) — design banked, legs (b)–(d)
not yet built.** Leg (a) — the strict per-repo default with the
namespace-naming refusal and the accept-side provenance stamp — landed in
`efb980d6`. Prereq reading: `state/utterances.py` module docstring (the
trust-anchor store + no-scaffold discipline),
`ops/decision/journal/human_authorship.py` (the gate),
`ops/decision/journal/_shared.py` (`_derivation_rule` + the range-gated
off-by-one leg), `docs/design/bound-capture.md` (the tiered-evidence
register this doc follows), `state/scopes.py` (the newest-wins scope state
machine the grant/revoke record reuses).

## Motivation: the multi-repo dev loop

The human-authorship gate anchors a human's utterance log to ONE repo
namespace: `~/.claude/hpc/<repo_hash>/utterances.jsonl`, where
`repo_hash(experiment_dir)` is the 12-hex digest of the resolved experiment
dir. A developer running the agent across MULTIPLE repos — the hpc-agent
repo itself plus a quant experiment repo is the daily case — states the
sweep in a session whose cwd is repo A ("20 seeds at 1M samples, chunks of
5000 bars"), then drives the submit from repo B. The utterance was
captured — in A's namespace. The gate in B consults only B's namespace and
refuses:

```
human-authorship gate (conduct rule 9): task_generator is human-authored:
'y' cannot commit a value that appears only in the agent's proposal — ask
the human for the sweep (or the human states it in a prompt (captured to
the utterance log)); value token(s) ['1000000', '20'] derive from no
logged human utterance for this repo (harness-captured) — evidence was
sought in repo namespace <B-hash> (experiment_dir .../B)
```

The namespace-naming tail (landed in `efb980d6`, docket #2) makes the
refusal *diagnosable* — an operator session in the wrong cwd can see why
their utterance was not found — but it does not make the commit
*possible*: the human's only remedy is to retype the sweep inside repo
B's session. Across a multi-repo dev loop that is friction with no
attestation value (the human verifiably typed the sweep an hour ago, one
namespace over), and it has a sharp edge case: the store's no-scaffold
rule (`append_utterance` lands only in an EXISTING namespace) silently
drops prompts typed in a fresh repo before its first hpc-agent state
write, so "just retype it in B" can fail twice.

## Ruling (2026-07-19, user)

**Dev-mode = a per-repo STRICT default with an explicit cross-repo
authorship opt-in.**

- **(a) Strict default, no leakage.** Each repo keeps its own authorship
  namespace by default; there is no cross-repo evidence flow without an
  explicit grant, and the refusal names the repo namespace consulted.
  *Landed in `efb980d6`.*
- **(b) The opt-in is a journaled, human-authored decision record in the
  SECOND repo** — never a silent config flag. The second repo declares:
  "trust the utterance log of named home repo H."
- **(c) The matcher then accepts utterances from the home log, and every
  acceptance stamps WHICH log satisfied it** — provenance under a
  code-owned key, beside the accept-side disclosure `efb980d6` added.
- **(d) Revocation is a journaled record; previously-accepted records are
  NOT retro-invalidated** — grandfathered, the same posture as
  notebook-audit sign-off grandfathering (a section signed at sha X stays
  signed at sha X; drift changes future evaluation, never rewrites the
  past record).

### Alternatives considered and rejected

- **Silent config flag** (`interview.json` key, env var, clusters.yaml
  entry). Rejected as invisible consent: a trust delegation with no
  journal record is exactly the unanswerable-commit class docket #1 part 2
  names ("which rule fired" must be answerable from the journal). It is
  also self-minted trust — every config surface is agent-writable, so the
  model could point the gate at a log of its own choosing. The grant must
  be a journaled decision precisely so it passes through the append-only,
  authorship-gated record path.
- **Single global namespace** (one utterance log for every repo on the
  machine). Rejected as cross-repo leakage by default: a number stated
  for repo A's run would silently authorize repo B's unrelated commit —
  the coincidence-adjacency class run-15 gate finding 2 fought at token
  scale, replayed at namespace scale. It also destroys the diagnostic
  property the strict default just bought (the refusal can name exactly
  where evidence was sought). Isolation must be the DEFAULT; widening
  must be the deliberate act.
- **Status quo, retype per repo.** Rejected as friction without
  attestation value (see Motivation); it trains the human to paste
  boilerplate and is flaky for fresh repos under the no-scaffold drop.

## Mechanism

### Where the opt-in record lives: `scope_kind="scope"`, scope_id `authorship-home`

The grant/revoke record is an ordinary decision-journal entry in the
SECOND repo: `scope_kind="scope"`, `scope_id="authorship-home"` (a
code-reserved slug, validated by the shared `validate_tag` class), one
code-owned block `_AUTHORSHIP_HOME_BLOCK = "authorship-home"` carrying
`resolved.action ∈ {"grant", "revoke"}`. A grant's `resolved` carries:

```json
{
  "action": "grant",
  "home_experiment_dir": "C:/Users/james/CC Allowed/hpc-agent",
  "home_repo_hash": "<12-hex repo_hash of that dir>"
}
```

The journal lives at `<second-repo>/.hpc/scopes/authorship-home.decisions.jsonl`
— repo-local, append-only. State resolution is the **scope-lock state
machine verbatim** (`state/scopes.py` precedent): the NEWEST
grant/revoke record decides current trust; revocation never erases the
grant history.

**Why `scope` and not `registration`.** Registration journals attest
artifacts — a sealed dossier promoted across the deployment boundary
(registration-kernel); each record binds a specific artifact sha. A
home-log grant attests no artifact: it is a standing, revocable piece of
repo-local trust state, which is exactly the shape the scope journal
already hosts (`_SCOPE_LOCK_BLOCK = "scope-lock"` with
`resolved.scope_action`, newest-wins). The scope doctrine "core attaches
NO vocabulary to a caller's tag" constrains caller tag *semantics*
(holdout / test / embargo), not code-owned blocks inside the journal —
`scope-lock` is the precedent for a code-owned state machine riding a
scope journal, and `authorship-home` is the same pattern. One scope id,
one home at a time (v1); a second grant supersedes the first by
newest-wins. (Multi-home is open question 2.)

### How the matcher consults it: read path, revocation ordering, no cache

One new shared reader in `ops/decision/journal/_shared.py` —
`_authorship_evidence_texts(experiment_dir, actor_ids)` — replaces the
gate's direct `_actor_scoped_human_texts` call and owns the whole
cross-repo read:

1. **Own namespace first** — `_actor_scoped_human_texts(experiment_dir,
   actor_ids)`, byte-identical to today (including the MH4 rule: under
   >1 declared actors, the session actor's suffixed log only; an
   unattributed session falls to the journal-response friction tier and
   cross-repo reading does not apply at all — see below).
2. **Resolve grant state** — read the `authorship-home` scope journal;
   newest record wins. `revoke` (or no record) → own-only, done.
3. **Revalidate the grant on every read** — recompute
   `repo_hash(Path(home_experiment_dir))` and compare to the recorded
   `home_repo_hash`. Mismatch (home moved/renamed) or a missing home
   namespace → the grant is DANGLING: own-only, disclosed (never
   trusted-blind, never an exception — the store's fail-open doctrine,
   with the disclosure the doctrine elsewhere pairs it with).
4. **Home read, same scoping** — read the home namespace's utterances
   NON-CREATING (`read_utterances` on the home experiment dir), with the
   SAME actor scoping as the own read: under >1 declared actors the
   session actor's suffixed log in the HOME namespace only. MH4 composes
   across namespaces — actor A's agent cannot commit a value only actor
   B ever typed, whichever namespace B typed it in.
5. **Union pool with per-log membership tracked** — the evidence pool is
   own ∪ home; every number/word pool keeps which log(s) each member
   came from, for the provenance stamp below.

**No caching.** The state is re-read on every gated append — the
`read_signoff_ledger` posture (the notebook reuse ledger scans every
notebook journal per call; a scope journal is one small file). This is
what makes revocation effective on the very next append with no
invalidation surface.

**Journal-response (friction) tier is unchanged and own-repo only.** The
cross-repo read exists only in the harness-captured tier: an
agent-authored journal `response` from repo B can never pull in repo A's
log. When no utterance log exists anywhere relevant, the gate behaves
exactly as pre-ruling.

### The provenance stamp: `human_authorship.source_log`

The accept-side disclosure (`efb980d6`: journaled under the code-owned
`provenance["human_authorship"]` key, caller-asserted values overwritten)
gains two additive members — no shape change to existing keys:

```json
"human_authorship": {
  "evidence_source": "harness_captured",
  "evidence_logs": ["<own-hash>", "<home-hash>"],
  "fields": {
    "task_generator": {
      "numbers": {"0": "zero", "19": "off_by_one", "20": "verbatim"},
      "strings": ["samples", "seeds"],
      "source_log": "home"
    }
  }
}
```

- `evidence_logs` — every namespace CONSULTED (own always; home when a
  valid grant exists). Answers "which logs were searched" from the
  journal alone.
- per-field `source_log ∈ {"own", "home", "own+home"}` — the set of logs
  that contributed at least one matched claim token for that field.
  `zero`-rule tokens derive from no log and contribute no source;
  `off_by_one` tokens contribute the log that stated the anchor count.

Both are computed by code at commit time and stamped over any
caller-supplied value — the `efb980d6` code-owned-key rule, extended.

### Composition with the range-gated off-by-one matcher (efb980d6)

**No change to derivation rules — only to WHICH log is searched.**
`_derivation_rule` (`verbatim` / `zero` / `off_by_one`), the
`range_eligible` set, and the contiguous-run/string-range forms are
untouched; they run against the union pool exactly as they ran against
the own-only pool. `off_by_one_eligible` is a property of the VALUE's
shape (a range endpoint / length / range literal), never of the pool —
so the run-15 tightening composes cleanly: a standalone
`n_samples=10000004` adjacent to a HOME log's stated `10000003` is still
refused, now stamped as consulted-across-logs when it fails. The home
log widens the human's STATEMENTS, never the derivation grammar.

### The bootstrap rule: the grant must be authored from the HOME log

A grant append carries no REQUIRED_CALLER field, so the value-derivation
gate does not fire on it — it gets its own authorship leg, in the
naming-gate family (`_names_target_sha_prefix` / scope-unlock precedent):

- The human's grant utterance — non-bare, naming the 12-hex
  `home_repo_hash` as a whole token — must exist in the HOME namespace's
  utterance log. The hash is the vocabulary-impossibility class (a
  12-hex digest exists nowhere in a human's prior vocabulary; the
  sha-prefix FILING gates rely on the same argument), so naming it proves
  engagement with the home's presented identity — and requiring it in
  the HOME log proves presence in the namespace being delegated. An
  utterance in the SECOND repo's log naming the hash does NOT grant: the
  bootstrap is "the human demonstrated presence in the home they are
  delegating to," which a second-repo utterance cannot show.
- **Harness tier only.** Journal `response` text carries NO weight for a
  grant — an agent-relayed "the human says trust <hash>" is self-minted
  trust. Same tightening as the overnight-consent bound ruling
  (2026-07-12): some acts are important enough that only the
  out-of-band channel may authorize them.
- **Structural checks refuse structurally**: the home path must resolve,
  `repo_hash` must recompute to the recorded value, and the home
  namespace must exist with the naming utterance present. A wrong
  path/hash is `SpecInvalid` WITHOUT the E2 `authorship_evidence:
  missing` marker.
- **The missing-naming-utterance refusal also carries NO E2 marker** —
  this is the one place this family deliberately breaks the marker
  convention, and the reason is load-bearing: the E2 marker drives the
  MCP elicitation retry, and a re-elicited utterance is captured into
  the CURRENT session's namespace (the second repo) — the wrong log. A
  popup retry would be a guaranteed-failing round-trip, exactly what
  `_refuse_missing_authorship`'s docstring reserves the marker against.
  The refusal message instead directs the human: "in a session whose cwd
  is the home repo, state: trust this repo's utterance log <hash>".
- The agent can compute the home hash and place it in `resolved` — that
  is fine. The agent cannot WRITE the home-log utterance (there is
  deliberately no utterance-writing verb; pinned by the contracts
  suite), so the bootstrap cannot be self-satisfied.

The utterance-capture hook needs no change: it already captures per-cwd
into whatever namespace exists. The grant mechanism is read-side only.

## Enforcement map

No tests exist yet (legs b–d unbuilt); these are the pins to write. All
live in `tests/ops/decision/test_dev_mode_authorship.py` unless noted.

| Mechanism | Enforcing test | Fires when |
|---|---|---|
| Strict default (leg a, LANDED `efb980d6`) | `tests/ops/decision/test_authorship_scalar_adjacency.py` (existing) + the namespace-naming refusal pin | regression: home tokens accepted without a grant; refusal loses the namespace tail |
| Grant enables home-log derivation | `test_grant_record_enables_home_log_derivation` | a second-repo commit whose tokens derive ONLY from the home log is refused despite a valid grant |
| No grant → byte-identical | `test_no_grant_home_only_tokens_refused` | any home-log consultation without a grant record (leak regression) |
| Bootstrap: home-log naming required | `test_grant_requires_home_log_naming_utterance` | a grant accepted on a second-repo utterance, a bare ack, or no utterance |
| Bootstrap: responses weightless for grants | `test_agent_relayed_grant_without_home_utterance_refused` | the journal-response tier leaking into the grant path |
| Grant refusals are structural (no E2 marker) | `test_grant_refusals_carry_no_authorship_marker` | a grant refusal acquiring `failure_features.authorship_evidence` (would arm a guaranteed-failing popup retry) |
| Hash revalidation (moved home → dangling) | `test_grant_hash_mismatch_is_dangling_not_trusted` | a grant trusted on its recorded hash without recomputation |
| Revocation newest-wins, forward-only | `test_revoke_mid_session_next_append_refuses_and_prior_commit_stands` | a revoked grant still consulted; OR a pre-revocation accepted record retro-invalidated (grandfathering regression — the prior record's stamp must read back intact) |
| Provenance stamp | `test_accept_stamp_records_source_log_and_overwrites_caller_keys` | missing/wrong `source_log` or `evidence_logs`; a caller-asserted stamp surviving the commit |
| Range-gating composes across logs | `test_standalone_scalar_adjacent_to_home_log_number_still_refused` | the union pool re-widening the off-by-one leg to bare scalars (run-15 finding 2, cross-repo form) |
| Actor scoping composes across namespaces (MH4) | `test_home_log_read_is_actor_scoped` | the home read consulting the anonymous union or another actor's suffixed log under >1 declared actors |
| Dangling grant degrades disclosed, never wedged | `test_missing_home_namespace_degrades_to_own_only_disclosed` | an exception escaping the read path, or a silent (undisclosed) own-only fallback |
| Route-through contract | `tests/contracts/test_utterance_route_through.py` — extend with the home-log reader | any utterance read in the decision subject NOT routed through the shared `_shared.py` readers |

## Refusal and drift behavior

- **Revoked mid-session.** Newest-wins: the next gated append re-reads
  the scope journal, consults own-only, and a home-only value now
  refuses. The refusal discloses the state change: "home-log trust
  revoked at <ts> (home namespace <hash>); evidence was sought in repo
  namespace <own-hash> only." Prior accepted commits STAND — their
  journaled `source_log: "home"` stamps remain the audit trail of why
  they were allowed. Grandfathered: revocation changes future
  evaluation, never rewrites a past record (the notebook-audit
  `signed_stale` posture; registration-kernel's "no permanence flag" —
  trust is re-derived from current journal state at every commit).
- **Agent-authored opt-in.** Fails the bootstrap on two independent
  legs: journal responses carry no weight for grants (harness-tier
  only), and the naming utterance must live in the HOME log — the one
  place the model has no write path. A grant whose `resolved` hash was
  agent-computed is still fine; only the *attestation* must be human and
  home-sited.
- **Home repo unavailable / moved.** At grant time: structural refusal
  (path must resolve, hash must recompute, namespace must exist — the
  home must already be an hpc-agent repo, mirroring the store's
  no-scaffold posture). At match time: the grant is DANGLING — own-only,
  disclosed in both the refusal text and (on accepts that still pass)
  the `evidence_logs` stamp. The grant record itself is never erased
  (append-only); re-pointing requires a new grant, which supersedes by
  newest-wins.
- **Fresh second repo under the no-scaffold drop.** Unchanged and
  orthogonal: prompts typed before the second repo's first state write
  are dropped from its namespace as today — the grant exists precisely
  to cover this window honestly.

## Non-goals

- **No utterance-log sync, merge, or copy.** The home log is READ across
  the namespace boundary under a grant; it never leaves its namespace.
  There is no merged store to keep coherent.
- **No cross-machine trust.** The journal home (`~/.claude/hpc/`) is
  per-machine by construction; a grant says nothing on any other
  machine, and no mechanism moves utterances between machines.
- **No auto-discovery.** The matcher never scans the journal home for
  sibling namespaces, never guesses a home from cwd history. The grant
  names exactly one home; that is the whole of the widening.
- **No change to derivation rules, tier structure, or the friction
  fallback.** The efb980d6 matcher is adopted as-is; the journal-response
  tier stays own-repo only.
- **No retro-invalidation machinery.** Revocation is forward-only by
  newest-wins; there is no re-audit of past commits.
- **No new verb.** Grant/revoke ride the existing `append-decision`
  surface (`scope_kind="scope"`); the refusal remedy text names the
  exact record shape to compose.

## Open questions (for the user)

1. **Grant anchor.** Should the home-log naming utterance be required to
   post-date the SECOND repo's `repo.json first_seen` (the B4-shaped
   rule: a grant over repo B can only attest a repo B that existed when
   typed), or does any historical naming of the hash suffice (the hash's
   vocabulary-impossibility carrying the whole bar)? The anchor costs
   one code read and kills the "old debugging utterance that happened to
   name the hash gets ridden" class; the doc's recommendation is to
   adopt it.
2. **Multi-home.** v1 is single-home (one `authorship-home` scope; a
   second grant supersedes). Is a real multi-repo setup (hpc-agent + two
   experiment repos trusting one home, or two distinct homes) better
   served by per-home scope ids (`authorship-home-<hash>`, union of all
   non-revoked grants), or is that YAGNI until it hurts?
3. **Human-readable home alias.** The grant record carries
   `home_experiment_dir` + `home_repo_hash` (both machine-checked).
   Should it also carry the human's free-text alias for the home (pure
   journal readability, never matched), or is path+hash enough?

## Drift log

- **2026-07-19 — ruling recorded from session (user).** Leg (a) shipped
  in `efb980d6` (strict per-repo default; refusal names the repo
  namespace; accept-side `human_authorship` provenance stamping as a
  code-owned key). Legs (b)–(d) are designed here and unbuilt; the
  enforcement map names the pins to write. When they land, journal the
  build record here and flip the status header.
