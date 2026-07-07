# The harness contract

The normative specification a **conforming harness** implements so hpc-agent's
authorship, relay, and backgrounding guarantees hold. Claude Code is ONE
implementation (hooks over `~/.claude/settings.json`); the scheduled v1.5
jupytext render is intended to be a SECOND. This page is the vendor-lock-in
defense: the audit loop is defined against these three capabilities, not
against Claude Code, and implementations compete under the contract
(`docs/design/notebook-audit.md`, "THE HARNESS CONTRACT").

This page is **normative** for the write API in §2 (the frozen record schema,
the no-scaffold precondition, the provenance contract, fail-open) — the
PLUGIN wave and any second harness implement against that bullet list. The
rest is descriptive: wherever a line is mechanizable, the source of truth is
the cited code seam and the test that pins it (see the enforcement pointers),
not this prose.

The load-bearing principle throughout: **a guard the LLM itself satisfies is
not a guard** (`engineering-principles.md`). Every capability below exists to
move a trust anchor OUT of the model's reach; where a harness cannot provide
it, the machinery degrades HONESTLY to a weaker, named tier rather than
pretending the guarantee still holds.

## The three capabilities

A conforming harness MUST provide all three. Each names what it provides, the
trust property it earns, and what degrades — to which exact seam — when it is
absent.

### Capability 1 — the out-of-band human-utterance log

**Provides.** An append-only log of the text a human verifiably TYPED, written
by the harness at the moment of input, through the write API in §2. The model
never mediates the write: the harness is the writer, the log is the reader's
trust anchor.

**Trust property earned.** The FULL-STRENGTH authorship tier. With the log
present, the human-authorship gate
(`ops/decision/journal.py::_assert_human_authorship`) and the scope-unlock gate
(`::_assert_unlock_authorship`) and the notebook sign-off gate
(`::_assert_signoff_authorship`) require every REQUIRED_CALLER value token — and
every unlock rationale, and every sign-off — to DERIVE from this log. Journal
`response` fields, which the driving agent itself authors, carry no authorship
weight in this mode. This is the lock the verb-only v1 could only stage: the
same model that fabricates a value can fabricate a quote, but it cannot forge
a log the harness wrote before the model saw the prompt.

**Degrades when absent.** The tier machinery falls back to the
JOURNAL-RESPONSE FRICTION TIER at the exact seam
`ops/decision/journal.py::_harness_human_texts` **returning `None`** (no log /
older session / hook not installed). In that tier the evidence source is the
journal `response` fields — agent-authored — so the gate mechanically kills the
OBSERVED rationalization class (hand-injected fields, bare-`y` laundering) but
a determined agent could still fabricate a human quote. The fallback is
deliberate back-compat: refusing it outright would break every pre-hook
install. The honesty is that the two tiers are NAMED and different, not a
uniform claim.

### Capability 2 — the relay/verbatim enforcement point

**Provides.** A seam at which the harness can inspect the FINAL agent-visible
message and force the agent to continue (re-answer) instead of ending the turn.

**Trust property earned.** Conduct rule 10 becomes a seatbelt, not a
suggestion: numbers and state the durable journal does not support cannot reach
the human unchallenged. `verify-relay` mechanized the audit as a pure verb, but
nothing made a driving agent RUN it; this capability is what runs it at the one
sound moment — the outgoing message is final and the transcript is on disk.

**Degrades when absent.** The relay audit reverts to the VERB-ONLY posture: an
agent (or a human) may still invoke `hpc-agent verify-relay`, but an unaudited
relay reaches the human (the proving-run-#3 failure: "running" relayed while
the journal said "failed"). No exception, no wedge — just the weaker guarantee.

### Capability 3 — backgrounding / wake

**Provides.** The ability to detach a long-running block into a worker that
survives the turn, and to wake / re-invoke the driving agent when the worker
reaches a terminal or an anomaly.

**Trust property earned.** The detached-worker machinery (S2/S3/S4 detach,
campaign reconcile self-chaining, the driver watchdog) can run cluster waits —
staging, canary polls, harvest — outside the synchronous chat turn without the
human idle-blocking, while the journal remains the durable rendezvous the woken
agent reads to resume.

**Degrades when absent.** The blocks collapse to synchronous, in-turn execution:
a submit that would detach at S2 instead blocks the turn for the whole canary
poll. Correctness is unaffected (the journal is still the source of truth); only
the wall-clock ergonomics degrade. A harness that cannot background at all still
runs the full pipeline, just without the detach optimisation.

## The utterance-log WRITE API (normative)

The one capability a second harness MUST implement byte-for-byte. The reference
implementation is `state/utterances.py::append_utterance` (the SOLE writer); a
conforming harness writes records the reader
(`state/utterances.py::read_utterances`, consumed by
`ops/decision/journal.py::_harness_human_texts`) accepts. The obligations, as
the bullet list the PLUGIN wave implements against:

- **Storage locator.** `<journal home>/<repo_hash>/utterances.jsonl`, derived
  exactly as `state/utterances.py::utterances_path`:
  `_current_homedir() / repo_hash(experiment_dir) / "utterances.jsonl"`.
  `_current_homedir()` (`state/run_record.py`) resolves the journal home:
  `HPC_JOURNAL_DIR` env if set-and-non-empty, else the module `HPC_HOMEDIR`
  attribute, else `~/.claude/hpc`. `repo_hash(experiment_dir)`
  (`state/run_record.py`) is the path-form-invariant
  `sha256(canonicalized resolved dir)[:12]`. The locator MUST reuse these two
  derivations, never re-implement the hash — a divergent hash writes into a
  namespace the reader never looks up.

- **Frozen record schema.** One JSON object per line, sorted keys, append-only,
  oldest-first. Exactly three fields:
  - `ts` — ISO-8601 UTC timestamp of the write.
  - `sha256` — the SHA-256 hex digest of the FULL raw text, computed BEFORE any
    capping, so a capped entry still carries a verifiable fingerprint of the
    whole utterance.
  - `text` — the raw text, capped at `MAX_UTTERANCE_BYTES` (4096) UTF-8 bytes,
    truncated on a CODEPOINT boundary (never mid-codepoint —
    `raw[:max].decode("utf-8", errors="ignore")`).
  Serialize with `json.dumps(record, sort_keys=True)` + `"\n"`. No other fields;
  the reader tolerates unknown keys but the writer MUST NOT add them.

- **No-scaffold precondition.** Write ONLY when the namespace directory
  (`<journal home>/<repo_hash>/`) ALREADY EXISTS — i.e. some prior hpc-agent
  state write already claimed this cwd as an experiment repo. NEVER create the
  namespace. The capture writer is installed user-globally and fires in ANY repo
  the human works in; scaffolding a `<repo_hash>/` dir for an arbitrary cwd
  leaks namespace directories across the user's whole filesystem (proving-run-#3
  finding g). Both the writer and the reader are non-creating; `utterances_path`
  is deliberately NOT `journal_dir()` (which mkdirs + writes `repo.json`).

- **Provenance contract.** Only HUMAN-TYPED text may enter the log — this is the
  entire trust value; admitting anything else hands the authorship gate's anchor
  back to the model. Two obligations:
  - The writer MUST run OUT-OF-BAND relative to the LLM's tool/CLI surface — it
    fires from the harness input path, before the model sees the input, NOT as a
    verb the model can call. (In Claude Code: a `UserPromptSubmit` command hook,
    `_kernel/hooks/utterance_capture.py`; and a `PostToolUse` hook for typed
    AskUserQuestion answers, `_kernel/hooks/answer_capture.py`.)
  - The writer MUST FILTER harness-injected and agent-authored text. Reference
    filters:
    - `_kernel/hooks/utterance_capture.py::_HARNESS_INJECTION_RE` — a prompt that
      OPENS with a harness-injection tag (`<task-notification>`,
      `<system-reminder>`, `<local-command-caveat>`, `<command-name>`,
      `<command-message>`, `<local-command-stdout>`) is dropped: notification
      text is agent-influenced, so admitting it is a laundering channel into the
      trust anchor. (A human merely quoting a tag mid-text still lands.)
    - `_kernel/hooks/answer_capture.py::_is_clicked` (over `_typed_texts`) — a
      CLICK on an agent-authored option label is NOT captured (the agent wrote
      that text); only the free-text "Other" residue and typed annotation notes
      are. A multi-select composed entirely of offered labels is skipped; if any
      part was typed, the whole answer is captured.

- **Fail-open semantics.** Any error — missing namespace, unwritable log,
  encoding error, malformed payload, empty text — degrades to a CLEAN NO-OP,
  never an exception. A broken capture channel MUST degrade to the friction tier
  (capability 1 absent), never wedge input submission or the harness. The
  reference writer catches `(OSError, ValueError, UnicodeError)` and returns
  `None`; the reader skips blank/corrupt lines so one bad line never strands the
  trail.

**The LLM must never gain a sanctioned write call.** This API is importable by
HARNESS-SIDE code only. There is NO CLI verb, NO MCP tool, NO primitive, NO
chain/next_block/skill affordance that writes an utterance — appending one is
the harness's exclusive act. A write verb would let the model author its own
authorship evidence, which is precisely the lock-1 posture this API exists to
deny. The contract test in `tests/contracts/` pins the absence of any such verb
in the operations registry.

## The relay-enforcement capability (detail)

The reference implementation is the Stop hook `_kernel/hooks/relay_audit_stop.py`.
On the turn-final event a conforming harness MUST be able to:

1. resolve the cwd repo's journal namespace WITHOUT creating it (no-scaffold) —
   no namespace → not an hpc repo → silent pass;
2. read the final agent-visible message text (the trailing run of assistant
   entries in the transcript);
3. find which journaled run ids that text actually NAMES (a claim is only
   attributable to a run the relay mentions);
4. run `verify-relay` per mentioned run;
5. on a CONTRADICTION mismatch (`number` / `state` / `run_id` — NOT the
   `unverifiable` kind, which a final message legitimately carries) BLOCK the
   stop ONCE with the itemized summary, forcing the agent to correct the relay
   to match the journal.

Loop-safety is mandatory: block a given stop AT MOST ONCE
(`stop_hook_active` → no-op), never hard-block a session. Fail-open everywhere
(missing transcript, no run mentions, per-run audit error, any exception →
silent pass). The harness need not use a "Stop hook" specifically — it needs the
CAPABILITY: inspect the final agent-visible message and force one continuation.

## The honest trust limit

From `ops/decision/journal.py`'s own contract: the utterance log is out-of-band
relative to the MODEL's tool/CLI surface — that is the whole guarantee and the
whole boundary. It defeats the model fabricating its own authorship evidence. It
does NOT defend against filesystem-level attacks (a process editing
`utterances.jsonl` directly) or harness-config-level attacks (disabling the
capture hook) — those are OUT OF SCOPE, and a harness that cannot keep its own
config honest cannot be made honest by this contract. The tier is stated, not
overclaimed: full-strength when the harness holds up its end, named friction
when it does not.

## The CLI is the invariant substrate

The `hpc-agent` CLI stays the invariant substrate under every harness (the
block-drive doctrine); MCP tools and skills are PROJECTIONS of it. A conforming
harness supplies the three capabilities AROUND that CLI — it does not replace
or fork the verb surface. This is what lets a second harness be additive: the
audit records, gates, and journal are identical; only the capability providers
differ.

## The second-conforming-harness sketch

The ceiling this contract earns. A jupytext notebook render (scheduled v1.5,
`docs/design/notebook-audit.md`) is a projection over SEALED records (source +
template + execution receipt), built in the plugin/tools lane (jupytext never
enters core). It plays two roles, in order:

1. **The portability artifact** — audits readable anywhere, with no harness.

2. **A SECOND CONFORMING HARNESS** — a human typing into a notebook sign-off
   cell IS out-of-band from the LLM. A render that writes that typed text
   through THIS write API (§2 — same locator, same frozen schema, same
   no-scaffold, same provenance filter, same fail-open) provides the
   full-strength authorship tier with NO Claude Code anywhere in the loop. The
   sign-off is then an ordinary `append-decision` under the `notebook` scope,
   its value tokens deriving from a log the notebook harness wrote — the gate
   cannot tell, and need not tell, which conforming harness produced the
   utterance. The execution-receipt emitter that feeds the render remains ~30
   lines of caller-side convention.

This is the product claim the contract secures: "we also export notebooks," not
"we are one notebook tool" — every trusted thing (approval, sign-off, receipt)
is an attestation over a journal a conforming harness fed, and the harness is
swappable.
