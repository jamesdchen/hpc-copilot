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
