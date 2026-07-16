# WS-DAEMON v2 — Architect memo (final, premortems folded)

Status: **HANDOFF-READY, 2026-07-16.** Produced by the daemon-engineering Fable
sweep (1 architect + 4 premortem lenses); this memo was folded inline by the
coordinating session after the sweep's finalize agent hit the spend limit —
the fold is a synthesis of the five verbatim documents in this directory, which
remain authoritative for detail:

- `DESIGN.md` — the full architecture (transport, protocol, lanes, per-call
  context, journal authority, lifecycle, migration ladder, test strategy).
- `premortems/state-concurrency.md` — 8 findings, F1–F4 HIGH (block-the-build).
- `premortems/lifecycle-windows.md` — H1–H5 / M1–M6 / L1–L6.
- `premortems/doctrine.md` — GO-WITH-CHANGES, 12 findings, required rows.
- `premortems/swarm-throughput.md` — corrected unit decomposition (DW0–DW3),
  in-flight gates, per-unit batteries; `unit-specs.json` is its machine form.

Supersession: this package **supersedes latency-plan units 3.2/3.3**
(`docs/plans/latency-elimination-2026-07-16/unit-specs.json`) — drift note
recorded here; do not build those units from the latency package.

USER RULING (2026-07-16): "fully engineer this" — build approved behind
`HPC_CLI_DAEMON=1`; default flip is a separate ruling after a full proving run.
CLI one-shot fallback permanent (amended by Δ-RULING-1 below).

---

## 1. Settled design deltas (premortem finding → binding resolution)

Every HIGH/blocking finding is resolved here; the build units carry these as
`design_constraints` in `unit-specs.json`. MEDIUM/LOW findings not listed are
carried verbatim in the premortem files and bound to their unit's constraints.

| # | Finding (lens) | Binding resolution |
|---|---|---|
| Δ1 | Lane A "warm ssh reads" contradicts "no ssh" and builds a crash-loop under slow hosts (state F3) | **v1 Lane A is strictly no-ssh; the asyncssh pool ships DISABLED in the daemon.** The hooks/reads win needs no pool. Pool enablement = its own post-proving flag with a dedicated ssh dispatch thread + drain-never-shoot watchdog policy (state F3's option i). D-POOL is rescoped to "transport-env snapshot + mismatch refusal + pool scaffolding, disabled". |
| Δ2 | `append-decision` has NO request-identity dedup; client-deadline-falls-inline manufactures duplicate greenlights with possibly different `resolved.next_block` (state F2) | Three layers, all in D-FSYNC/D-CORE: (a) daemon drops queued-not-started RPCs whose pipe is closed at dequeue; (b) mutating RPCs carry a client-minted `request_id` stamped into the record; same-id re-append = replay no-op (also fixes the pre-existing run-#2 duplicate class); (c) until (b) lands, re-drive-after-UNKNOWN for `append-decision` is human-surfaced, never automatic. Fire tests per layer. |
| Δ3 | `os._exit` mid-append tears the journal's final line; the next writer appends onto the torn tail = merged unparseable line (state F4) | `append_jsonl_line` gains seam-level self-heal (inside the lock: if last byte ≠ `\n`, restore line boundary before writing) + a critical-section flag the watchdog defers on + lock-wait time exempted from the WEDGED clock. One definition, closes the class for every killer. Fire test: planted torn tail → both subsequent records parse; torn record reported, never merged. |
| Δ4 | A DETACHED_PROCESS daemon has no console; `_invoke_cli`'s telemetry `sys.stderr.write` would fail EVERY call *after* the journal committed → UNKNOWN → re-drive storm (state F5; lifecycle H4) | Boot contract in D-CORE: spawn with stdout/stderr bound to the rolling log (the `_spawn_detached` log-handle pattern, stated explicitly) AND verify/rebind `sys.stdout/stderr` at boot. All non-dispatch threads write only through the boot-captured `_DAEMON_LOG`; install a daemon `threading.excepthook` routing there (state F6). Fire test: real detached daemon, verb with heartbeat, envelope purity asserted. |
| Δ5 | Daemon-spawned detached workers inherit the DAEMON's env → journal-home/actor/canary corruption (state F1) | `_spawn_detached` and every daemon-reachable `subprocess` site build `env=` from the `call_env()` seam, never bare `os.environ`. Reader census extends to `subprocess(..., env=` construction sites; `**os.environ` spread in daemon-reachable code = lint hit. Fire test: daemon-dispatched submit with `HPC_ACTOR`+`HPC_JOURNAL_DIR` overlays → worker lease + record land in the overridden home with the right attestor. |
| Δ6 | "stdlib-only daemon_client" would fork `_build_info` + `env_flags` (doctrine F1); psutil-backed `pid_alive` tension (doctrine F11) | Client imports `_build_info` (the one definition); "stdlib-only" is replaced by an **enforced import-weight budget** (`-X importtime` contract test: no pydantic, no `_kernel.registry`, no `infra.clusters`). Hello does NOT pre-partition transport env — client sends all `HPC_*`; the daemon (which owns `env_flags`) partitions. `pid_alive`: accept the psutil import in the client and let the budget test price it; if it busts the budget, a bounded client-side probe is a NAMED second definition with a mirror-ledger entry — decided at D-CLIENT build, loudly. |
| Δ7 | Rung-1 hooks are in-process package code, not CLI verbs; forwarding them forks guard logic (doctrine F2) | Per hook, NAME the verb that IS the hook body (`relay_audit_stop`→`verify-relay` is the model); register missing ones `agent_facing=False`; the hook module becomes a thin client of its verb; hook-path/verb-path output parity pinned. This is D-HOOKS' core deliverable, not a head-insertion. |
| Δ8 | ContextVar doesn't propagate to verb-spawned threads (doctrine F3) | Lane-A eligibility gains a mechanized no-`threading.Thread`/`ThreadPoolExecutor` leg (allowlist w/ justification), or the lane snapshots `hpc_env` into spawned-thread contexts. Fire test: planted thread-spawning Lane-A verb → red. |
| Δ9 | Editable-install "key files" staleness stat is an undeclared heuristic (doctrine F5) | **v1: editable/dirty installs are daemon-INELIGIBLE** — client detects and falls inline (memo §0.10 precedent; devs pay the 1.3s). Enumerated, fire-tested. |
| Δ10 | Client 250ms connect deadline not implementable with `multiprocessing.connection.Client` as written (lifecycle H1) | D-CLIENT owns the engineered connect: non-blocking/timeout-bounded dial (pre-probe the pipe with a bounded open / overlapped connect on win32; bounded socket connect on POSIX) so a wedged daemon costs ≤ the declared client deadline, never 20s. The acceptance test measures it. |
| Δ11 | Discovery pid-reuse; boot-crash loop tax; spawn-lock races; sleep/resume clock; pipe-name squatting; user-derivation split-brain (lifecycle H2, H3, M4, M5, H5, L1, L2) | D-CORE/D-CLIENT constraints: discovery file carries `started_at` and liveness = pid+started_at match (the repo's existing lesson); activation keeps a negative-cache breaker (N boot failures → stop respawning for T, disclose); spawn lock is per-fingerprint; instance flock is the daemon's FIRST statement pre-import; watchdog uses suspend-aware deltas; `Listener` uses first-instance semantics and boot fails loudly into the negative cache; ONE casefolded `getpass.getuser()` seam feeds pipe name + discovery filename + dir, hashed to the same 12-hex discipline. |
| Δ12 | Env-mismatch set under-inclusive: engine tunables (`HPC_SSH_CONNECT_TIMEOUT`, keepalive, idle-close, max-connections) read from daemon env while disclosure reads call env → disclosure lies (state F8) | The transport-policy snapshot/mismatch set = ALL engine-read env vars, enumerated from `ssh_engine.py`/`ssh_slots.py` reads (census-derived, not hand-listed). Moot for v1 cluster work (Δ1 pool disabled) but binding for the pool flag. |
| Δ13 | One-definition/route-through/elicitation/never-unpickle need PINS not prose (doctrine F6, F7, F8, F9, F10, F12) | D-CORE/D-LANES ship: AST lint pinning `recv_bytes`/`send_bytes` only + pickle-payload refusal test + wrong-authkey test + POSIX ACL pin; `inspect.getsource` route-through assertion (x-hpc/cli → `_in_process_cli_runner` / `run_capture_bounded`); import/grep pin that the daemon package never imports elicitation machinery + locality fire test; the daemon-hosted marker for state-guards is a lane-set contextvar (journal cwd-fallback refuses loudly under it); supervisor wake fan-out restates wake-is-a-hint (client settles from durable lease/terminal) and the subscription mechanism is declared outside Lane A; WEDGED-shoot prose cites the three real reasons (OS lock release, whole-line atomicity + backward-scan reader, UNKNOWN-re-drive), not "no locks". |
| Δ14 | Lane A dispatch through `cli.dispatch.main` arms worker machinery (detached heartbeat, faulthandler, env-triggered terminal recorder) (swarm finding 5) | Lane A enters BELOW `main()` (at `_dispatch_main`/`_invoke_parsed` altitude); `daemon_client` strips `HPC_DETACHED_*`; Lane-A admission refuses `hpc_env` containing them; faulthandler armed once at boot to the daemon log. Fire tests each. |
| Δ15 | Memoized parser freezes boot-cwd argparse defaults (swarm) | Client ALWAYS materializes `--experiment-dir`; daemon refuses/flags an experiment-dir-accepting argv lacking it (D-CONTEXT battery). |
| Δ16 | Windows CI daemon gate does not exist to inherit (swarm finding 1) | D-SOAK BUILDS it: new required `test-windows-daemon` job (~7–10 min budget, ≤15 daemon spawns, session-scoped daemon for read-only tests, 60s+ fixture boot deadlines decoupled from the 15s product budget, `HPC_DAEMON_DIR` conftest-autouse guard). |
| Δ17 | `hpc_agent/__init__` eagerness sinks the hook-head win (swarm finding 2) | Latency unit 1.3 is a HARD prerequisite of D-CLIENT's acceptance numbers (code may merge earlier; the <200ms claim may not). |
| Δ18 | HPC_*-reader census is 70 sites / append seam has 16 consumers (swarm findings 3, 4) | Census allowlist starts with enumerated exempt buckets (`deploy|worker|hook|lane-b-only`, one-line justification each); untagged reader = red. D-FSYNC's deliverable includes the 16-consumer classification table; its battery covers every consumer's test dir. |

## 2a. RULED 2026-07-16 (maintainer): MEASURE-THEN-DECIDE — the daemon gates on the stateless floor

The maintainer's framing: "if stateful warm processes are convenient but
break constantly, perhaps the best thing is to get the stateless CLI path
latency down so much that the warm start doesn't matter." Amendment to R4,
ruled same night:

1. Latency waves 1–2 (the stateless program) land FIRST — already sequenced.
2. Then MEASURE the real post-wave per-call floor per surface (hook, CLI
   verb, block-drive span) on the primary Windows box, including the
   irreducible spawn+Defender tax the census left unmeasured.
3. **DW1+ builds ONLY if the residual gap still justifies the program**
   (daemon warm call ≈15–20ms vs projected stateless ≈300–700ms; the
   marginal win must earn 13 units of lifecycle machinery). Otherwise the
   package stands as design-of-record, shelved. This INVERTS the prior
   cut-line (which shipped DW0–DW2 unconditionally).
4. **D-FSYNC extracts and builds NOW, standalone** — the append-seam
   durability fix (fsync-before-ack on source-of-truth ledgers, torn-line
   self-heal, request_id replay dedup) is a correctness win for the
   stateless path too and fixes the duplicate-greenlight class observed
   live in run-14. Consequence, disclosed: Δ-RULING-1 is thereby resolved
   AFFIRMATIVE — the one-shot CLI also starts surfacing source-of-truth
   fsync failures as errors ("byte-identical modulo the shared durability
   fix, ruled 2026-07-16"); row 313's language amends in the same wave.

## 2. Rulings needed from the maintainer (carry to the popup, offered-hint style)

- **Δ-RULING-1 (doctrine F4):** the `fsync_required` split changes the SHARED
  append seam — a suppressed-fsync OSError will surface as ok:false on the
  one-shot CLI too. This amends "fallback byte-identical" to "byte-identical
  modulo the shared durability fix". Recommended: APPROVE (it is the right
  correctness fix for both paths); the memo line + row 313 amend in the same
  wave. *(Blocking D-FSYNC.)*
- **Δ-RULING-2:** in-daemon watches (moving detached workers into daemon
  asyncio tasks) — DESIGN.md §3.3 records DECLINED-FOR-NOW; re-open only by
  ruling. No unit builds it.
- **Δ-RULING-3:** pool enablement flag (post-Δ1) — deferred until the daemon
  has proving-run telemetry; not part of this program's exit criteria.

## 3. Build program (waves; machine form in `unit-specs.json`)

`D-FSYNC → [D-CORE ∥ D-CLIENT ∥ D-CONTEXT ∥ D-VERBS] → regen+integrate →
[D-LANES ∥ D-POOL(rescoped) ∥ D-SUPERVISOR ∥ D-SOAK] → integrate + windows-CI
green → D-HOOKS (post-latency-1.7) → D-CLI (post-2.1) → D-INSTALL → telemetry
gate → D-SHIM → D-DRIVE (post-2.3/3.1/4a, block_drive queue LAST).`

Daemon ships as a package (`src/hpc_agent/_kernel/extension/daemon/`:
`transport.py`, `identity.py`, `lanes.py`, `supervisor.py`, `lifecycle.py`,
`__init__.py`) so DW1/DW2 units are file-disjoint. Units never commit
regenerated artifacts; one `regen_all.py --write` per wave at integration
(D-VERBS is the only regen-forcing unit). Integration checklist per wave =
ordered merge → regen → ruff/format/mypy → per-unit batteries → push → CI
matrix green → enforcement rows appended → telemetry deltas recorded.

**Cut line if latency waves slip:** DW0–DW2 (core, opt-in flag, zero
default-path deltas) ship and soak standalone; the three surface rungs wait
for their latency gates (1.7 / 2.1 / 2.3).

## 4. Residual-risk register (top 5)

1. `multiprocessing.connection` AF_PIPE under high client churn is the least-
   exercised stdlib corner here — rung-1 fallback-rate telemetry is the
   tripwire; client deadlines bound blast radius to "runs like today".
2. D-SHIM carries the elicitation machinery (HIGH merge risk) — scheduled
   after telemetry gate, alone; full elicitation quartet gates it.
3. D-DRIVE joins block_drive.py as position 5-of-5 across two programs —
   scheduled dead last, alone.
4. The reader-census allowlist rots if entries land untagged — the lint fails
   on untagged entries by construction; keep it that way.
5. Windows-runner AV flake vs daemon-spawn-heavy tests — capped spawn count +
   decoupled fixture deadlines; if the gate flakes anyway, the session-scoped
   daemon fixture absorbs more tests before we relax the gate.

## 5. Enforcement-map rows (added at wave integration)

From DESIGN.md §8 + doctrine premortem: rows 17 (durability ack), 18 (per-call
context), fingerprint-in-address, transport-env split, client-deadline-falls-
inline, detached-env refusal (Δ5/Δ14), never-unpickle framing, route-through
pin, elicitation-locality, request-id replay no-op (Δ2). Each with its named
fire-path test per the map conventions.
