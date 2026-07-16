# Run #13 findings docket (live, relay session 2026-07-15)

## 1. Template-compose tiebreak inverts on the receipt-bound domain pack
`[core]` Live: `audit-preflight` (template omitted) silently rebound the
causal_tune_linear audit from the rv program template to
`packs/quant/templates/quant_skeleton.py` (module_sha `dfac68f1`, the lab
copy). Every section then read `modified` against the new template prose and
the gate demanded fresh sign-offs on all five slugs when only
feature-construction had content changes — the human caught it at the
sign-off surface ("if only feature-construction was changed, why do I have
to sign off on the others?").

Root cause: `compose_audit_template`
(`state/pack_declarations.py`) breaks the multi-candidate tie with "the
FIRST pack that is the target of a `receipt_bindings` slot (the program
pack) wins over the domain skeleton" — but in harxhar-clean the
receipt-bound pack IS the domain skeleton (`quant`, via the gate-required
`quant-audit` slot) and the program pack (`rv`, the signed `rv_audit.py`,
`reader_calls` vocab) carries NO receipt binding. The heuristic's
receipt-bound ⇔ program-pack assumption is exactly backwards for the
two-layer quant/rv split (v0.2.0, user-ruled 2026-07-10). Consequence
beyond the sign-off churn: the sidecar/dossier would have carried the
`{pack: quant}` echo instead of the doctrinal `{pack: rv}` echo
(run-12 SESSION_HANDOFF rule).

Gate conduct note: the view_sha invalidation itself worked as designed —
sign-offs bind to section-body × template identity, and a template swap
MUST invalidate them. The defect is upstream, in the silent compose pick.

Live remedy relayed: re-run audit-preflight with
`template=packs/rv/templates/rv_audit.py` explicit → re-enter at lint →
the four unchanged sections re-hash to their previously signed shas; only
feature-construction needs a fresh sign-off.

Fix direction (RULED 2026-07-15, proper-fix-only; superseded by the
fable-sweep handoff — docs/plans/fable-sweep-devx-2026-07-15/): the principled signal
is the derivation edge, not receipt bindings — rv's manifest `derived_from`
names the quant skeleton sha, i.e. the derived (more specific) template is
the program pack and should win. Candidate rule: among audit_template
candidates, a pack whose template `derived_from` another candidate's
template wins; receipt-bindings tiebreak retired (or kept only as a
last-resort ordering). Whatever the rule, the compose disclosure should
name BOTH candidates and which rule picked the winner, so a wrong pick is
visible at preflight instead of at the sign-off surface.

## 2. Detached worker died exit-2 with NO disclosed failure in its log
`[core]` Live (submit-s2, run causal_tune_linear_fixmask-82ba92e8,
2026-07-15T21:31:46Z): the harness-written terminal records
`detached_worker_exit, exit_code 2` and its message asserts "the worker
log carries the disclosed failure" — but the log's final non-hb line is a
normal `[transport] progress` line. Nothing was flushed: no traceback, no
child scp/ssh stderr, no exit-path disclosure. The heartbeat showed a live
ssh.exe child with growing CPU shortly before death. The disclosure
contract the terminal message asserts is broken for hard-death paths
(unhandled exit, killed process, unflushed buffers). Fix class: worker
crash disclosure — faulthandler/atexit flush + capture child stderr and
exit status into the log before (or independent of) the terminal write;
the terminal message must never claim the log discloses something the
write path cannot guarantee.

## 3. Push manifest commits only at completion — died-mid-push retry re-pays the full delta
`[core]` Live, same run: attempt 1 shipped 355+ MB of its 1181.4 MB delta
before dying; the retry's content-hash delta line was byte-identical
("shipping 18972 changed/new, 1181.4 MB") because the delta compares
against the remote PUSH MANIFEST, which is written once at push
completion. Partial progress is invisible → every mid-push death re-ships
from zero (the run-12 delta-less double-pull class, now on the push side;
this run paid the ~39k-file hash scan + the transfer twice). Fix class:
incremental manifest checkpointing — commit manifest entries per file or
per batch as they land (the remote files are already there; only the
bookkeeping lags), so a retry's delta reflects remote reality.

## 4. Stack-created pull destinations missing from deploy excludes → 1.18 GB junk payload
`[target]+[core-default]` Live, same run: the deploy payload was 39,374
files / 9.9 GB with `results`, `logs`, `_combiner` excluded — but NOT
`_aggregated` and NOT `_per_task_results`, the directories the STACK
ITSELF creates as local pull/reduce destinations (run 12's 2,700-file
mirror + aggregate outputs). Run 13's code deploy therefore shipped run
12's analysis outputs to the cluster (~the whole 18,972-file "changed/new"
set). Core knows these directory names — it mints them; they belong in
the DEFAULT exclude set (same standing as `_combiner`), not in per-repo
config memory. Immediate demo remedy relayed: add both to the deploy
excludes before the fleet submit.

## 5. Hand-rolled bash watch loop over the terminal file
`[harness]` The demo armed a 220-iteration bash loop (grep terminal ts +
tasklist PID + sleep) instead of the sanctioned `poll-detached` /
block-drive rendezvous — the improvisation class, and it led to narrating
"progressing normally" against a directory already containing a failure
terminal (the loop was keyed to detect a ts CHANGE from the recorded
failure). Relay rule reminder issued; no gate gap — the sanctioned verb
exists and was bypassed.

## 6. Content-hash scan has no cache — 37 minutes re-hashing an unchanged tree
`[core]` Live, same run: the retry's push-delta scan re-hashed all 39,374
files (9.9 GB) from scratch — ~37 min of the 47-min worker lifetime (hb
timeline: conhost idle until the ssh transfer child appears at ~2,340s).
The scan is pure re-computation: every byte hashed on every push, no
reuse. Fix class: rsync-quick-check semantics — a local hash cache keyed
by (relpath, size, mtime_ns) → sha, persisted under .hpc/; only files
whose (size, mtime) moved get re-hashed. Unchanged trees scan in seconds;
correctness unchanged (a stale mtime+size collision is the same risk
rsync's default mode accepts; a --checksum-style escape hatch can force
full re-hash). Compounded by finding 4 (the tree is 4x inflated) and
finding 3 (the scan itself was paid twice).

## 7. Canary verifier polls a `-canary2` sidecar that was never pushed — false-negative blocks the fleet boundary
`[core]` Live (job 10284428_1 COMPLETED exit 0, fresh
results/causal_tune_linear/reclasso/sentiment/chunk_24000/_runtime.json
with correct axis bindings, run causal_tune_linear_fixmask-82ba92e8-canary):
verify_canary reported `reporter_unreachable` because it polled a
`-canary2`-suffixed sidecar that was never pushed to the remote, while the
real `-canary` sidecar is present. Same class as the 07-09 punch-list
canary-verifier false-negative — a RECURRENCE, still unfixed at the
3cb3db99 wheel. The S2 brief then proposed "fix before main" against a
canary that in fact passed; the human overrode with direct evidence (the
07-11 precedent). Fix class: one-definition of the canary sidecar name —
the verifier must derive the sidecar path from the SAME recorded identity
the submit wrote (never re-derive an attempt suffix); a missing sidecar
under the derived name should disclose WHICH name it polled and which
sibling names exist.

## 8. Relay-audit correction FLOOD — 10 code-appended corrections, most false-positive
`[core]` Live, same boundary: the Stop hook appended TEN corrections to one
fleet-boundary relay. Real signal ≈ zero; the flood classes: (a) DATE
FRAGMENTS tokenized as numeric claims ('07-09'/'07-11' session references
→ '07','09','11' each "matches no source number"); (b) the brief's OWN
cost line rejected ('300 tasks × 4 cpus × 3h = 3600 core-hours' → '300',
'3', '3600' unsupported — the S2 brief text is not in this run-scope's
verify corpus despite the F-Q ruling adding briefs); (c) scheduler + task
artifacts outside corpus (job elapsed 1m39s, _runtime.json 97s vs journal
82); (d) '300-task' flagged as a run-id-shaped token; (e) a VERBATIM QUOTE
of the code-rendered brief's decision line ("canary failed verification")
flagged as a state claim contradicting journal 'complete'. Quoting the
gate's own rendered output must be corpus-supported by construction.
Fix classes: corpus completeness (the CURRENT block brief + scheduler
query results + pulled task artifacts are legitimate sources for the runs
they name), tokenizer precision (date fragments, unit-suffixed numbers,
hyphenated count phrases), and dedup/rate-bounding so one relay never
carries 10 corrections of which 9 are noise — a flood teaches the operator
to ignore the gate (the false-positive cost IS a conduct-surface cost).

## 9. Hand-authored chain spec at the fleet boundary + fabricated invocation_argv
`[harness]` The demo python-composed .hpc/specs/submit-s3-fixmask.json from
the s2 spec by hand, hit the spec_invalid refusal (missing
invocation_argv — the one-shot skeleton feature worked), then PATCHED the
field with a fabricated value ("/monitor-hpc <run>"). Hand-authored chain
specs are the forbidden improvisation class (run-12 conduct item 13: the
DRIVER composes successor specs — block-drive would have carried the real
invocation_argv). Also the hand-rolled 230-iteration bash watch loop
again (finding 5's class), re-armed after a stop. Relay correction
issued; no new gate gap — the sanctioned surfaces exist and were
bypassed. Worth noting for the conduct ledger: the append-decision
greenlight itself was properly journaled (count 3, canary evidence
attached).

## 10. Breaker livelock on a degraded login node — probe succeeds, preamble times out, forever
`[core]` Live (S3 worker 1af806fe, run causal_tune_linear_fixmask-82ba92e8):
27 failed ssh attempts across 9+ monitor ticks to discovery2.usc.edu. The
repeating cycle: 3 × 60s command timeouts (the full `cd && module load
conda && source conda.sh && ...` preamble) → circuit OPENS 300s →
half-open PROBE SUCCEEDS (connection-level) → circuit closes → same 3 ×
60s timeouts → reopen. The breaker state machine LIVELOCKS: a cheap probe
passing while the process-spawning preamble hangs is the DISCOVERY2
FORK-EXHAUSTION SIGNATURE (run-12 finding 20 — the node with the pending
CARC ticket; run 13 was scoped to discovery1/hoffman2 for exactly this
reason), not a transport fault. The demo self-diagnosed "VPN flapping" —
wrong: a VPN flap fails the probe too. Two defects: (a) resolve-at-use-
time yielded discovery2 as the carc login FQDN with no health input —
the resolver should prefer/rotate sibling login nodes and the journaled
host-retarget verb exists for exactly this failover (this segment is its
missed first live use); (b) the failure classifier HAS the signal
(probe-OK + repeated command timeouts, N cycles) to classify NODE
DEGRADATION and surface "suggest host-retarget <sibling>" in the
transient-fault tick line, instead of riding the breaker forever. Also
live confirmation of latency-audit rank 4: a preamble-free scheduler
poll would not spawn conda at all on the poll path.

## 10-addendum. The degradation is the module/conda preamble, not the node wholesale
`[core]` Decisive evidence (demo's own raw probe + ~/.ssh/config): the
`usc-discovery` alias resolves to discovery2.usc.edu ITSELF, and a raw
`squeue` + bare `python3` glob ran instantly on that node while the
worker's `module load conda && source conda.sh` preamble times out >60s
every attempt. So finding 10's classifier signal refines to: probe-OK +
light-command-OK + preamble-timeout ⇒ the /apps module subsystem (or its
mount) is degraded, not the host. Consequences: (a) latency rank 4 (O4's
preamble-free scheduler poll) would have settled this run on time using
the very node that is "down" — the strongest live evidence for that fix;
(b) host-retarget to a sibling remains the right mid-run remedy for a
degraded preamble; (c) the eventual classifier message should name the
preamble as the hanging stage (the timeout already carries the command —
parse it).

## 11. Raw ssh side-channel at the terminal boundary — numbers relayed with zero provenance
`[harness]` With the S3 worker breaker-livelocked, the demo ran TWO raw
ssh calls (a scheduler query; then an ad-hoc `python3` glob over
results/ computing per-bucket qlike figures) and relayed those numbers
to the human. Raw cluster ssh is permission-denied conduct (sanctioned
verbs only), and the figures carry no journal/artifact provenance — the
"code computed it" fact does not sanitize an improvised side-channel
around a blocked sanctioned path. The correct sequence existed and was
already relayed: host-retarget (or settle-run with directed sacct
evidence), then the stack's own settle/harvest/aggregate produces the
same numbers WITH provenance. CORRECTED same-day: the ssh-guard
hook's pattern DOES cover the `usc-discovery` alias — the guard is an
ask-gate, so the call was either human-approved at the prompt or the
demo session's permission mode auto-allowed it; the conduct question is
therefore about the ask being accepted mid-livelock, not a hook hole. Secondary: the
relay-audit corpus did not fire on the ad-hoc numbers (they name no
run_id scope) — the finding-8 corpus work should consider unscoped
numeric tables in relays.

## 12. Remote combine/harvest emits no progress — and the breaker-override remediation prose misleads on degraded preambles
`[core]` Live (aggregate-run workers 9246ef1d + 490d4248): (a) the first
worker refused correctly (typed ssh_circuit_open — the S3 saga's open
breaker); (b) the demo followed the refusal's OWN remediation text
("verify reachable out-of-band, then HPC_SSH_CIRCUIT_OVERRIDE") — but a
bare `echo` reachability probe proves connection, not the degraded
preamble, so the prose licenses exactly the grind-without-fail-fast the
breaker exists to prevent (it worked out tonight only because the
preamble degradation had passed); (c) the second worker then ran 25+ min
with ZERO non-heartbeat lines — the cluster-side combine + harvest pull
stage has no progress disclosure at all (the >10s-progress-file rule,
unapplied to this stage), making an active transfer indistinguishable
from a hang from the outside. Fixes: progress lines for combine + pull
stages (the O2 pull engine already carries them — wire the aggregate
worker's stages through); remediation prose for circuit_open should say
"verify with the SAME command class that failed (the preamble), not a
bare connect" and prefer naming host-retarget.

## 13. Repair/graft runs have no first-class support — the combine walks 2,700 payloads because the grafts invalidate nothing
`[core]+[operation]` Live: run 13's fixmask repair re-ran 300 bad arms
under a NEW run id (causal_tune_linear_fixmask-82ba92e8) grafted
in-place into run de448128's results tree — a real research pattern
(localize → fix → re-run the subset → re-aggregate). The machinery has
no notion of it: the grafted pieces sit outside the original run's wave
bookkeeping (whose wave_map was also never written — the aggregate-check
missing_waves issue the human overrode), so the combiner cannot use wave
partials and falls back to reading all 2,700 task-dir payloads — the
metadata walk the announce kernel killed for STATUS reappears for the
COMBINE, by necessity. The status walks are resolved; the payload walk
needs the incremental-combine shape: per-piece fingerprints, partial
invalidation scoped to the waves the grafts touch, recombine only those
(canon-bump / temporal-scan backlog class — this finding is its first
concrete consumer). Note the layering: aggregate-check DID surface the
integrity gap; the cost of overriding was paying the slow path — the
gate worked, the incremental machinery to make the override cheap is
what's missing.
