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
