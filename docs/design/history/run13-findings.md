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
