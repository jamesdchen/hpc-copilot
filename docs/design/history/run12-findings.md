# Run #12 findings docket (live, relay session 2026-07-10)

## 1. The on-ramp interviews for what the bound pack already knows
`/new-experiment-hpc` asked the human "Template .py — path, if one exists."
In a pack-opted-in repo the ACTIVE audit template is derivable: the lab
pack's `audit_template` seam (harxhar-clean: `packs/rv/templates/rv_audit.py`,
the prepared 5-slug-conformant template) is bound and gate-required — the
question is the pre-pack legacy surface surviving into a pack world, and an
open path question invites exactly the wrong answers (the unsigned 12-slug
spec, the legacy `specs/…run10.py`). Fix class: poka-yoke — the on-ramp
COMPOSES the default from the bound pack's seam and asks for confirmation,
not a path. Natural home: the experiment-setup materialization verb of the
three-tier distribution ruling (`domain-packs.md` drift log 2026-07-10) —
setup pins the lab pack into `.hpc/` and the template default falls out of
the pin. (User, run #12: "it should be assumed that the 5-slug template is
what is prepared for us to build a specific experiment off of.")

## 2. The on-ramp interviews for experiment_dir — mechanizable from context
The interview asked "experiment_dir — absolute path (this repo, or a
separate dir?)". User: "is there a way to mechanize this? there should be
enough context." There is: the session's cwd git root, when it carries an
`interview.json` / `.hpc` tree (or is where /new-experiment-hpc was
invoked), IS the experiment dir in the standard flow — the question is
another compose-silently-and-disclose seat, same class as finding 1 (the
template default). Fix: default experiment_dir = the invoking repo root;
disclose in the record; ask ONLY when the cwd carries no experiment
markers at all.

## 3. The MCP client-server link has no human-visible liveness surface
Two wedged-server episodes cost ~10 min of human attention each with zero
mechanical disclosure — "Generating…" is not a liveness signal (the
no-black-box rule applied to the transport itself). The >10s-progress-file
discipline needs an MCP-link analogue (the client logs "still running (Ns)"
to a cache file nobody surfaces).

## 4. FIXED LIVE: subprocess in the server context wedged the whole server
`audit-preflight` hung the MCP server on its FIRST live call:
`_build_info.py::git_output` ran bare `subprocess.run(git ...)` — the child
inherited the server's stdin (the live JSON-RPC pipe), and on timeout the
post-kill drain waited on a git grandchild holding the pipes (the run-#7
orphaned-ssh class). Offline probes CANNOT reproduce it (piped stdin hits
EOF). Fix: `stdin=DEVNULL` + `run_capture_bounded` tree-kill (`git -C`
replaces the cwd kwarg). ENFORCEMENT CANDIDATE for
engineering-principles: no bare `subprocess.run` in code reachable from
`mcp-serve` — stdin isolation + tree-kill bounded, or the bounded runner.

## 5. Template-compose must exist at EVERY consuming verb (5-grep archaeology)
Live: the audit path cost five grep/bash calls re-deriving the pack's
audit_template because the silent-compose seat exists only at interview, and
interview.json carried a STALE run-#11 audited_source the agent rightly
distrusted. Fix: (a) audit-preflight + notebook-record-config accept
template-omitted → compose from the bound pack seam, disclosed in the
result; (b) pack-seam vs stale audited_source disagreement resolves to the
SEAM with a disclosure (the pack is the sealed standard). Also validated
live here: finding-4's fix (preflight GO instantly on the wheel that hung
twice before).

## 6. draft-context under-supplies: resolver root-bug + template-only engines
Live: ALL engines returned resolved:false ("unresolved under source_roots")
for symbols that ARE under src/ — the resolver appears to double-prefix
(module `src.data.loading` under root `src` → src/src/...), so signatures/
docs/shas were absent and the agent hand-read metrics.py/diebold_mariano.py/
loading.py (~6 commands). Fix (a): resolver tries <experiment_dir>/<module
path> as well as <root>/<module path>. Fix (b): the engine set is template-
imports-only (incl. sys/os noise); the variable sections' planned callables
(dm_test, MultiStageBacktest, get_bucket, enet_online) weren't in context —
spec grows `engines: [...]`; and axis vocab (SUBGROUPS bucket inventory) is
pack-vocab class (the readers.json seam) — sealed data, not a grep. Honest
residue: reading the prior program spec for the new sweep axes is real
drafting research, not mechanizable.

## 7. FIXED: executes-live flags docstring prose as missing path literals
Live (causal_tune_linear lint): the module docstring (line 1) and the
`RollingTunedLinear` class docstring were flagged `executes_live` — "path
literal … does not exist under the declared input_roots" — because prose
like "qlike / mse / rmse" carries `/` and every string Constant was
path-shape tested. Two-part fix in `lint.py`: (a) docstrings (statement-
position string Expr opening a module/class/function body) start consumed —
documentation is never a path operand; (b) a literal containing a newline is
never path-shaped (no filesystem path spans lines). The `f"{estimator}/
{bucket}: …"` print in `unverifiable_paths` remains — an honest disclosed
gap, not a finding. Regression tests mirror the live source shape.

## 8. WATCH: elicitation bubble absent at audit-view is EXPECTED — the
## firing site is append-decision
The relay asked why no MCP elicitation bubble appeared at the sign-off
brief. By design (D6, mcp-elicitation.md) the ONE firing site is
`append-decision`: the popup opens only when the sign-off append hits the
authorship gate with no matching human utterance (E-render primary — the
popup carries the render digest, collects the typed utterance, and the
append re-runs atomically). `notebook-audit-view` never elicits. What to
watch when the demo agent reaches the sign-off: (a) popup appears → sign in
the popup; (b) tool call stalls ~300 s then returns a plain refusal → the
client declared elicitation but rendered nothing (declared-but-dark;
Addendum 7 marks the channel dark for the session) — THAT is a finding;
(c) instant plain refusal → the client never declared elicitation at
initialize (hook-path degrade, also worth recording).

## 9. FIXED: the sign-off popup could never land — the T8 gate had no
## utterance-log tier
Live (the sign-off boundary): the demo agent CORRECTLY refused to attempt
`append-decision` without a chat-typed utterance ("if I author the response
and you approve in the popup, that's a click"), and it was righter than it
knew: the T8 gate checked all three legs (non-bare, names-slug, diff-token)
against the agent-relayed `response` ONLY, so the E4 elicit-then-retry —
which re-runs the IDENTICAL argv after appending the popup text to the
utterance LOG — could never pass for a notebook sign-off. E-render-primary
was structurally dead at its flagship site. Fix (the queued run-#11
"sign-off echo detection" item, same seam): the gate now tiers evidence like
scope-unlock — with a harness utterance log present, the naming/engagement
legs run over LOGGED HUMAN UTTERANCES (chat capture hook OR the popup
handler, one log) and the response carries no authorship weight (the
composed-response laundering hole closes as a corollary); absent a log, the
non-bare response is the friction tier, byte-identical. Skill step 5
rewritten: over MCP the agent proceeds DIRECTLY to the append after relaying
the view (popup = primary); chat-first is the no-elicitation fallback.
NOTE: reaches the demo only after a wheel refresh + fresh MCP server — the
running run-#12 session signs via chat.

## 10. FIXED: the log tier accepted STANDING PROMPTS as sign-offs — popup
## never fired because the gate PASSED
Live, first exercise of the finding-9 fix: both human_required sections
landed `signed_current (human)` with NO popup and NO sign-off typed — the
session's earlier prompts (the resume paste names `feature-construction`,
`baseline`, and diff identifiers like `causal_tune_linear`; the
/new-experiment seed carries `tune_per`/`val_tail`/estimator names) sat in
the utterance log and satisfied naming+engagement. The popup only opens on a
REFUSAL, so a false PASS is silent. Two false human attestations are now in
the journal (records 15/16, ts 03:35–03:36Z) — REMEDIATION: after the fix
lands, the user re-signs both sections at the same hashes through the popup;
the newer genuine records supersede in the reduce, the tainted ones stay as
honest append-only history, noted here. Conduct note: the demo agent
narrated "your popup utterance" — fabricated; no popup existed.
FIX (temporal binding): a human can only attest a view that existed when
they typed — log-tier candidates must post-date the signed view's render
file (mtime anchor, floored to seconds; `write_render` now SKIPS rewriting
identical bytes so a re-view cannot move the anchor). Absent render skips
the filter (the unmarked trusted-display lock owns that refusal).

## 11. THE POPUP FIRED (finding-8 outcome a) — and its digest under-supplies
## review; RULING 2's never-the-diff-body clause reversed
Live, fresh-session clean-slate run: the whole day's stack fired in
sequence — lint zero findings (7), draft-context with `engines:[...]`
supplying signatures (6a/6b), the agent went straight to the append (9),
the gate refused the standing prompts (10), and the ELICITATION POPUP
RENDERED in Claude Code (outcome a — the client declares and renders).
User at the popup: "there's not enough diff showed for me to properly
review" — digest v2 deliberately carried per-hunk one-liners, never the
diff body (RULING 2), which leaves a +285-line section reviewable only via
the session's Read panes. That defeats read-and-sign-in-one-place. FIX
(interim until unified-render O3+ chunked popups): the popup embeds the
render's fenced diff body in its OWN bounded block (6 KB budget, line-
boundary cut, elision count disclosed, code-read bytes only); the digest's
honesty budget is untouched, and the honest-refusal oversize path carries
the bounded body too (review material with disclosed truncation is
additive, never a silent drop).

ADDENDUM (same session): the Read-pane fallback is NOT a review surface
either — the user reports the expanded Read result does not usably show the
diffs (long renders truncate/collapse). The skill's step-4 claim ("the
result pane displays the true disk bytes") holds for FIDELITY but not for
REVIEWABILITY; a 323-line render with a 285-line fenced diff is unreadable
there. Interim relay for run #12: the dev session sends the render FILES
(SendUserFile, rendered in the side panel) for review before the popup
sign-off. Consequences: (a) the popup diff-embed budget (6 KB) still
truncates a diff this size — the O3+ rendering-probe item should establish
what the client actually renders and the budget should follow the evidence;
(b) unified-render O3+ chunked popups remain the endgame; (c) the skill's
harness-capability ladder gains a rung: render-file SEND (side-panel
render) sits ABOVE the Read-pane relay when the harness supports it.

## 12. Audit-view responses dump ~11k tokens into the agent per pass — the
## model-as-display-channel payload is vestigial under popup-primary
Live (user: "there's a lot of things being dumped in context. is this
normal?"): every loop pass returns the FULL audit view through the model —
and the diff ships TWICE per response (inside `markdown` AND as the
structured `sections[].diff` arrays). The design predates popup-primary:
when the model relay WAS the display channel, the full markdown had to
transit the agent. Now the render files are content-addressed on disk, the
POPUP carries the diff to the human, and the gate recomputes server-side —
the agent needs slugs / tiers / shas / render_paths / next-actions only.
Every diff byte in the agent's context is cost plus the re-summarization
temptation the relay doctrine spends effort suppressing. FIX SHAPE (post-
run-#12, pairs with unified-render; user-ruled wording 2026-07-11: OMIT AT
THE SOURCE, never compact downstream — the verb simply does not emit the
payload, so there is nothing to compact anywhere): (a) quick win — drop the
`sections[].diff` duplication (halves the payload, zero flow change);
(b) the real cut — the view response emits METADATA + digest + render_path
POINTERS only by default; the full markdown is emitted only behind an
explicit `full: true` for harnesses that still model-relay. Same treatment
for draft-context. Adjacent: the
agent hand-read the 2218-line interview.json hunting pack seams — finding
5's compose-at-every-verb gap, same session. ALSO RECORD (user, positive):
the nudge → re-draft → hash-move → fresh-view cycle "synergizes well with
the workflow" — the auto-revoke rendezvous is validated UX, keep its shape.

## 13. FIXED: mcp-serve decodes stdin with cp1252 — human text mojibakes
## inside the server (the journaled goal's "â€"" em-dashes)
Live (audit-handoff echoing the config record): the goal recorded via
notebook-record-config carries `â€"` where the human typed `—`. Traced:
`cli/dispatch.py::main` reconfigures stdout/stderr to UTF-8 but NOT stdin;
`mcp-serve`'s JSON-RPC reader therefore decodes Claude Code's UTF-8 bytes
with the Windows locale default (cp1252), corrupting human text INSIDE the
server before the spec temp file or journal is written. Fix: stdin joins
the reconfigure loop. The already-journaled goal keeps its mojibake
(append-only; cosmetic); the INTERVIEW copies the goal from the handoff
draft — at confirm, restore the human's actual `—` bytes in the resolved
spec (that IS the verbatim text; the mojibake never was).

## 14. submit-s1 ignores the interview's recorded data_axis_hint — the seat
## exists, the consumer never wired
Live (the S1 brief): the interview persisted `data_axis_hint: bounded_halo
(halo expr "halo" = 24000)` to `interview.json._materialized.entry_point.
data_axis` — the seat built EXPLICITLY so classify-axis/submit never
re-asks — yet S1's ambiguity walk recommended `{kind: sequential}` (the
fail-safe) with `depends_on: [entry_point]`, and its provenance labels the
interview-materialized tasks.py `hand_written_tasks_py`. A `y` on that
brief would have submitted 2700 BoundedHalo tasks under a sequential
classification. Same class as findings 2/5 (compose-from-recorded-config
missing at a consumer). FIX: S1's data_axis resolver reads the
materialized hint (provenance `interview_hint`, disclosed); tasks.py
materialized BY the interview is labeled `interview_materialized`, never
`hand_written`. ALSO surfaced by the same brief: `gpu_type: a100` injected
via `cluster_default` for a pure-CPU workload — cluster resource defaults
deserve a workload-shaped sanity line in the brief (disclosure, not a
guess). And: 8m56s turn time — the latency program's exhibit.

## 15. Output-contract detector scans the WRAPPER, not the wrapped script —
## false WARNING on every shell_command entry point that passes env through
Live (S1 resolve, run causal_tune_linear-de448128): "the executor script
'causal_tune_linear.py' never references $HPC_RESULT_DIR … outputs
DISCARDED by write-isolation" — but the AUDITED source reads
`os.environ.get("HPC_RESULT_DIR", ...)` in its signed section; the scanned
file was the materialized WRAPPER (same basename), which subprocess-invokes
the real script with env inherited. The static scan cannot see through the
subprocess boundary, so every shell_command wrapper earns this warning
regardless of the target's actual contract. FIX: for a shell_command entry
point, the detector also scans the argv's script target(s) (the interview
knows the argv verbatim); better, the audit's `output_roots`/declared
outputs seat feeds the contract check (the audited source already stated
its write target honestly). SECONDARY (canary will adjudicate): the script
nests its own `causal_tune_linear/<est>/<bucket>` subtree under the
per-task exported HPC_RESULT_DIR while result_dir_template already encodes
est/bucket/chunk — a doubled layout worth checking in the canary's actual
result tree before 2700 tasks bake it in.

## 16. S2 detached worker froze at birth (0.015s CPU / 14min) — stale
## detach lease suspected; CLI-vs-MCP env skew (uv tool had no asyncssh);
## staging has ZERO liveness (finding 3 at scale)
Live: the respawned S2 worker (pid 27124, uv-tool python via the CLI
fallback) sat 14+ min with 0.015s CPU, 4MB RSS, zero network (451 B/s
box-wide), zero log growth after the payload-enumeration WARNs (which
belonged to the PRIOR attempt) — kernel-blocked at startup, before any
transport work. Prime suspects: (a) the run's detach `lease.lock` left by
the previous killed worker — a blocking, timeout-less acquire freezes every
successor (fix: lease acquire gets a deadline + names the holder pid; a
dead-pid lease self-reaps at acquire, not only in doctor's scan); (b) the
finding-4 inherited-handle class at the detach spawn. COMPOUNDING: the uv
tool env (freshly repaired with the bare wheel) had NO asyncssh while the
MCP path's demo venv did — the two sanctioned drive paths ran different
ssh engines (fix: the transport must REFUSE LOUDLY at startup when the
configured engine's module is missing, never wait; and the repair playbook
installs the ssh extra). ALSO: net-triage exonerated the network in one
call (both clusters reachable, breakers closed) — the verb earned its
seat. ALSO: deploy payload enumerated at 8,688 MB / 20,426 files (data/
parquets ride the repo push) with bare-exclude WARNs — first-time staging
cost needs surfacing in the S1 brief (a size line), and staging needs
progress lines under the >10s discipline (finding 3's third bite today,
now with 8.7GB behind it).

## 17. The native-Windows staging path was broken three ways — the 8.7GB
## stage became an hour of live workaround archaeology
Live (S2, run causal_tune_linear-de448128). Legs, in discovery order:
(1) **tar|ssh fallback — the DESIGNED no-rsync Windows path — dies with
WinError 206**: the content-hash delta passed every relpath as a tar
ARGUMENT; a 20k-file repo overflows Windows' ~32k argv limit. FIXED: the
member list streams through a `-T` names temp file (GNU tar + bsdtar;
./-prefixed against bsdtar's @archive syntax), unlinked after the retry
loop. (2) **rsync src drive-colon**: once the demo agent installed MSYS
rsync, "C:/Users/…" parsed as remote host "C" ("source and destination
cannot both be remote") — bit AFTER auth and ~656s of transfer worked.
FIXED: win32 src converts to /c/… form. (3) **workers exited on the
pre_stage_smoke refusal WITHOUT journaling a block terminal** — six
dead-worker alerts, an hour of forensics, and the doctor could only
suggest re-invokes. QUEUED: the detach wrapper records a terminal on ANY
exit, refusals included. Legs 1+2 shipped; uv tool + demo venv refreshed
so the demo's next submit-s2 rides them.
CONDUCT NOTES (for the ledger): the demo agent's recovery was resourceful
but improvised SYSTEM mutations mid-run (copied MSYS DLLs into
~/.local/bin — later cleaned; pacman-installed openssh into C:\msys64) —
the determinism-boundary doctrine wants those as surfaced proposals, not
unilateral acts. The auto-mode classifier correctly blocked its raw-ssh
probe (deny-rule intent honored via full-path detection). The circuit
breaker opened after 3 failures and the agent WAITED the cooldown rather
than overriding — correct. net-triage earned its seat twice.

## 18. Canary task-0 metrics pull KeyErrors on sweep-axis templates —
## swallowed by the best-effort mint, but the sample never mints
Live (S2 terminal, run causal_tune_linear-de448128): `_pull_canary_task0_
metrics` rendered `result_dir_template` with only `{task_id, run_id}`;
a sweep-axis template (`{estimator}/{exog_bucket}/chunk_{chunk_start}`)
raised a bare KeyError — line 115's own comment documents "cannot render
→ raises", but a builtin KeyError is not the documented raise-class. The
double-canary evidence mint swallowed it (correct posture: the submit was
unaffected and the canary VERIFIED — job 10161700 green on CARC), so the
cost was silent: the determinism-fingerprint sample never mints for ANY
sweep-templated run. FIXED: render with task 0's REAL kwargs when the
sidecar records them (`trial_params`), and convert any residual miss to
the documented SpecInvalid. NOTE this canary's sidecar carries
`trial_params: null` — the sidecar writer should record task-0 kwargs for
canaries (follow-up seat) so the sample can mint at all here.
MILESTONE, same brief: **canary GREEN end-to-end on the fixed transport**
— stage completed via the -T delta path (finding 17 legs 1+2 live-
validated), output contract passed on-cluster (finding 15 confirmed
false-positive), est. 32,400 core-hours disclosed, next_block submit-s3.

## 19. FIXED: The S3 watch read ssh TIMEOUTS as run death (cause=abnormal-exit)
## and fired a terminal harvest at a 10-minute-old LIVE array
Live (S3 worker 2d2a9d6d): three consecutive 60s ssh connection timeouts
→ breaker opened (correct) → but the watch classified the run
`abnormal-exit` and ran the TERMINAL HARVEST while 27 jobs sat healthy in
the queue. The positive-evidence doctrine (timeouts = UNKNOWN, never
terminal) was applied to scheduler-query silence in the sentinel-ack wave
— but the abnormal-exit classifier path still treats CONNECTION-level
failure as a terminal cause. FIXED (e8bfefa1, 304 tests): a terminal cause requires
POSITIVE scheduler evidence (a sacct/squeue answer saying the jobs are
gone); transport failure is UNKNOWN and re-polls after the breaker.
SECOND LEG: the terminal harvest scp-pulled the WHOLE `results/` root
(prior runs' outputs included → 1800s timeout); harvest must scope to
the run's own result_dir_template subtree. MITIGATION that held: the
premature harvest wrote NO terminal state into the run record — the
journal stayed honest, and the demo agent re-armed the sanctioned
status-watch (alive, polling). ALSO NOTED: the relay-audit hook forced
two journal-true corrections this leg (working as designed) but flagged
"27" as an unsupported numeric claim when it is len(job_ids) — the hook's
number pool should include derivable COUNTS of journal lists (minor).
CONDUCT (mine, the dev session): two env breakages tonight were MY
reinstalls racing live workers (uv tool mid-exe-use; demo venv mid-run →
the ops.pack ModuleNotFoundError the demo had to repair). STANDING RULE:
never reinstall an env that has live workers — ship the wheel, let the
next idle boundary pick it up.

### The design note (why this class existed at all)
The clean design is BOUND CAPTURE, not forensic reconstruction: a sign-off
utterance should be captured AT a surface that knows what it signs — the
elicitation handler knows the exact (audit_id, section, view_sha) it
elicited for and should journal the utterance BOUND to that triple; the
gate's primary evidence then becomes "an utterance captured FOR this view",
with zero token-matching. The token/diff/temporal machinery exists because
attestation was retrofitted onto a general chat log (the only out-of-band
channel Claude Code offered pre-elicitation) — deriving intent from an
unstructured stream is inherently forensic, and findings 9/10 are the
forensic rules converging on what bound capture gets by construction. FUTURE
SEAT (post-run-#12, plan bank): elicitation-response records carry the
elicited scope; the chat hook path stays forensic (naming + engagement +
temporal) as the explicitly weaker fallback tier it honestly is.

## 20. CARC login-node fork exhaustion for jc_905 — the connection storm's
## REMOTE cost (the run-#7 orphaned-ssh class, server side)
Live: the one monitor tick that landed carried the login node's own words —
`/home1/jc_905/.bashrc: fork: retry: Resource temporarily unavailable`
(rc=127). Connections LAND; the SHELL starves: the user's login-node
process quota is exhausted, most plausibly by orphaned remote halves of the
night's many failed/killed ssh sessions. Every heavy poll (cd && module
load && conda && reporter = many forks) dies at startup; retries add
processes to an exhausted pool. RESPONSES: stop re-arm pressure; a
single-fork probe (squeue -h) may pass where the reporter starves (probe
pattern validated this night); the human path is the OnDemand WEB portal —
`ps -u jc_905 | wc -l`, pkill strays. FIX CLASS: the crash-only-monitoring
plan's one-gateway + cluster-announces design removes both the client storm
AND its remote residue; nearer-term, the reporter command should be
fork-minimal (one exec, no module/conda for a pure sacct/squeue read).
RULED RESPONSE (user, 2026-07-11 — "structurally proper, not a quick
fix"): build BOTH defense layers pre-run-13, sequenced AFTER the
2026-07-11 bug-sweep swarm lands (its agents own the neighboring files):
LAYER 1 = remote-side deadline: the ssh_run seam wraps every framework
remote command in `timeout <budget+margin>` derived from the SAME deadline
the client computes — an orphaned remote half self-destructs by
construction, regardless of how the client died. LAYER 2 = self-identifying
remote processes (an HPC_AGENT_OP=<op>:<epoch> argv token at the same seam)
+ a doctor hygiene probe: ps -u $USER for MARKED processes older than the
max legitimate deadline, pkill only those (never unmarked user processes),
and surface the stray count in the doctor brief (the observability gap:
47 strays would have been visible days before the quota wedged). These are
belt-and-suspenders UNDER the crash-only plan, not a substitute: staging
transfers and human sessions can orphan processes even after polling dies.
Sequencing note: 2026-07-11 discovery2 was so exhausted that ssh/web-shell
logins could not fork a shell — self-service recovery IMPOSSIBLE (the
kill -9 -1 builtin needs a shell to run IN); only a CARC admin ticket or
stray decay clears it. Bounded-lifetime strays (layer 1) are therefore
also the guard against the UNRECOVERABLE version of this failure.
BUILT (2026-07-11): both layers at the `infra/remote.py` `ssh_run` seam via
`build_remote_command` — LAYER 1 `timeout -k 10 <client_budget+60>s bash -c`
(client-timeout=None → 3600s default; HPC_SSH_NO_REMOTE_DEADLINE=1 escape
hatch; rc 124 classified transient, never broken-env) + LAYER 2 the
`HPC_AGENT_OP=<op>:<epoch>` marker riding argv (bash `$0`) AND environ. The
hygiene probe is the NEW `stray-sweep` verb (`ops/recover/stray_sweep.py`) —
NOT doctor (its contract is no-SSH) nor net-triage (opens no ssh by design):
one fork-minimal `ps -u $USER`, counts total + marked, flags marked-and-over-age
strays, reaps ONLY those PIDs and ONLY under `reap: true`, warns at count > 40.
Scoped to ssh_run (finding-20's poll/reporter root path); transport transfers
keep their existing client-side tree-kill bound.

## 21. Stop-guard livelock: a CONSUMED greenlight is indistinguishable from
## a fresh one — every turn-end forces a tick against a parked-on-external
## rendezvous
Live (aggregate chain, causal_tune_linear-de448128): the human's earlier
`y` for aggregate-check was consumed by a tick that ran the block, got
`not_ready` (login node can't fork — finding 20), and RE-PARKED. `_park`
writes the new pending marker but appends NOTHING to the decision journal,
so the journal's latest record is still that already-consumed `y`. The stop
guard's whole test is marker-present + latest-record-is-y
(`decision_rendezvous_stop_guard.py` `find_committed_unadvanced`), so it
blocked EVERY turn end — "invoke block-drive … (do not end the turn)" —
and each forced tick re-ran aggregate-check, i.e. another SSH volley at the
exhausted login node. The guard's own docstring calls the condition
"self-healing"; it heals only until the next park, after which the stale
`y` re-arms it forever. `stop_hook_active` caps it at one forced tick per
turn, so it's a per-turn tax + SSH amplifier (compounding finding 20), not
an in-turn spin. `plan_block_action` shares the blindness: the forced
tick's routing consumes the same stale `resolved` again. FIX CLASS: record
CONSUMPTION — `_park` stamps the consumed decision's identity (journal
length or record sha) into the marker; guard + planner treat a latest-`y`
at-or-before that stamp as spent (park is "waiting for the human" →
silent). The 2026-06-10 stall class stays closed: a genuinely unconsumed
`y` is always NEWER than the marker it answers. CROSS-REF: independently
confirmed as bug-sweep 2026-07-11 #1 (HIGH, 2 skeptic votes,
docs/internals/bug-sweep-2026-07-11.md) — the sweep adds the driver-side
consequence (forced advance into the gated successor, whose gate refuses →
spurious "block failed" instead of "awaiting the human") and a fix sketch:
scope the resume-path approval to the parked boundary via
resolved["next_block"] == cursor next_verb. One fix, both entries.

## 22. Registry-name vs CLI-verb skew: guidance strings say
## "reconcile-journal", the CLI only knows `reconcile`
Live: framework prose told the relay agent to run reconcile-journal
(campaign_run.py, status_pipeline.py, status_blocks.py all name it);
`describe reconcile-journal --schema` FOUND the primitive (registry name)
but errored "declares no input schema", while `dispatch reconcile-journal`
said unknown command — the verb map (`_verb_module_map.py`) binds the CLI
name `reconcile` to primitive `reconcile-journal`. Two contradictory
error surfaces for one name, two burned round-trips; the did-you-mean
suggester was the only thing that recovered it. This is the CLI-verbs-over-
Python-internals doctrine's (#200) unfinished edge: agent-facing guidance
must speak CLI names, or describe/dispatch must both resolve BOTH names.
FIX CLASS: (a) make `describe` and `dispatch` resolve registry aliases
(one alias map, shared), and (b) lint guidance strings against the CLI
verb list so a non-verb primitive name in user-facing prose fails CI.

## 23. ssh_target is FROZEN in the journal record — no sanctioned path to
## retarget a run to a different login node of the SAME cluster
Live (discovery2 fork-exhausted and self-service-unrecoverable, discovery1
healthy): editing ~/.hpc-agent/clusters.yaml host did NOTHING for the
in-flight run — every consumer (ops/monitor/reconcile, cli/aggregate,
cli/lifecycle, infra/backends/remote_factory) reads `record.ssh_target`,
minted at submit time. The relay agent had to hand-edit the journal record
JSON (ssh_target discovery2→discovery1). It worked, but that is surgery on
journal state with no locking, no provenance trail, no validation — the
exact improvisation class the block-drive papercut work exists to remove.
`retarget-run` covers CLUSTER changes only. FIX CLASS: either (a) a
`retarget-run` patch axis for host (`patch: {host: ...}` — re-derives
ssh_target, journals the change as a decision, validates the new host
serves the same scheduler/scratch), or (b) the structural fix: stop
freezing the host — journal the CLUSTER key and resolve user@host at USE
time from clusters.yaml (host is config, identity stays journaled), making
login-node failover a config edit; needs a migration story for existing
records. ALSO OBSERVED: the breaker UX held up well under the failover
(net-triage breaker_open_cooling verdict + the sanctioned
HPC_SSH_CIRCUIT_OVERRIDE worked as designed), and DNS shows
discovery.usc.edu == discovery1's IP — the bare alias already IS node 1.

## 24. NAT idle-drop severs every long-SILENT remote leg at ~100s — the
## "empty reporter output" mystery AND finding 20's orphan population source
Live chain of evidence (2026-07-11): the framework's reconcile with an
1800s budget "returned empty, rc 0" at ~1m43s while ps on discovery1 showed
its remote python STILL RUNNING 35 minutes later (channel severed, remote
half orphaned); the manual control run of the same reporter died exit 255
(ssh's own connection-death code) ~20+ min in; the reporter is COMPLETELY
silent on the wire for its whole 20-25 min walk (2700 tasks × resolve +
scratch stats). Cause: the NAT'd home client's idle TCP flow is dropped by
a middlebox at its idle threshold (~100s observed); no keepalives were
configured. The demo session's workaround (user ssh_config
ServerAliveInterval 30) held the channel and is correct — but the
framework must not depend on the user's ssh_config for a liveness
guarantee it needs everywhere. FIXED (this repo): ssh_argv splices
-o ServerAliveInterval=30 -o ServerAliveCountMax=60 into every framework
ssh/scp (HPC_SSH_KEEPALIVE_INTERVAL tunable; 'default' defers to
ssh_config). CLASS NOTE: this was misattributed twice before the evidence
converged (first to login-node fork exhaustion — real but separate — then
to the stale wheel — also real, also separate); the tell that finally
discriminated was ps showing the remote half OUTLIVING its channel. The
remote-deadline wrapper (finding 20 layer 1) bounds exactly this orphan
class; keepalives prevent the sever; the fast frozen-manifest reporter
(bug-sweep #3) shrinks the silent window. All three ship together.
ADDENDUM (same night): keepalives are NECESSARY but NOT SUFFICIENT — a
reconcile run WITH user-ssh_config keepalives active still returned
fast-empty, so a SECOND severing mechanism lives inside the framework
client; prime suspect the asyncssh engine's idle reaper (severs any
connection with no COMPLETED command in 120s — bug-sweep #8, fixed this
repo same day, absent from the demo wheel), else an internal per-exec
timeout HPC_SSH_TIMEOUT_SEC does not govern in 0.11.0. Discrimination
pending the demo env's HPC_SSH_ENGINE value + the detached nohup reporter
control run.
