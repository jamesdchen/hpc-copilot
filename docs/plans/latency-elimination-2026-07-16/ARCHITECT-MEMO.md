# LATENCY-ELIMINATION BUILD PROGRAM — ARCHITECT MEMO (final)
2026-07-16 · baseline main @ 94c0c484 (now b7e03e9c) · inputs: 36-claim sweep docket + 35 Opus verdicts + three premortems (technical / swarm-throughput / doctrine), all folded here. Machine-readable twin: `unit-specs.json` in this directory.

Missing verdict: `rt.transfer-plane-bypasses-engine` — carried as PLAUSIBLE-UNVERIFIED, gated behind unit 2.4's verify-during-build first step (never assumed true).

---

## 0. SETTLED DESIGN CALLS (deltas from the draft, all three premortems folded)

1. **Old unit 1.2 (`fast_path_safe` on describe/find) is REMOVED from Wave 1 and fused into 2.1** (technical premortem A1: describe/find read the LIVE registry; the fast path leaves it ~4 entries — silently-wrong find matches, confidently-wrong describe errors, and `describe_cache.store()` of a partial row POISONS full-path callers for the version's lifetime). The fast path may only answer describe/find after hydrating the catalog from the bake or running full registration.
2. **New Wave-1 unit 1.2 = fast-path prerequisites**: (a) move `describe --schema` into `CliShape.args` + regen, and lint-forbid `add_argument` inside `_register_from_registry` outside `_add_standard_args`, so the bake input is the whole truth (A2); (b) the one-line `describe_cache.store()` guard: refuse when `_REGISTRATION_DONE` is false (A1 immediate mitigation).
3. **P1 is folded into P3** (technical C4): exactly ONE remote waiter per HOST (the census watcher), multiplexing all runs, running on the engine's persistent channel, declared preemptible to the slot limiter, and shipped as a WATCH_VERB **subprocess** — named on 2.3's KEEP-subprocess list so WS-INPROC cannot inline it. **inotify is FORBIDDEN** (C1: no cross-node fire on NFS/Lustre): the remote wait is a sh poll loop (sleep 1–2s + `ls`/opendir forcing readdir revalidation). The win is moving the poll remote-side, not kernel notification.
4. **A wake is a HINT, never a settle** (technical C2 + doctrine row 11): the settle read failing its ack re-enters the wait; announce-plane reads within one settle sequence are sticky to the host the wake came from (login pools × NFS attribute caching).
5. **WS-INPROC entry point = the existing MCP in-proc runner seam** (`_shield_real_stdin` + stdout capture), NEVER `cli.dispatch.main` (technical D1 — regression 17243a17). Spans carry an explicit context object (run_id, block, cwd, env-overlay); `_record_detached_failure_terminal` and heartbeats take parameters, not `os.environ` (D2). In-process eligibility is a DECLARED, ENUMERATED list with a planted-violation contract test.
6. **F2 pull-manifest cache is built to the run-13 finding-13 rows** (doctrine D2): keyed on `.hpc_cmd_sha` where present — stat only ever decides *unchanged*; a cmd_sha move evicts even when mtime+size match; failures/severed reads are never cached; mtime(ns)+size with a skew window (young entries count dirty); cache file temp+rename, per-run keyed, excluded from include-match; fire test mirrors `test_flow_stale_mirror.py`.
7. **F7 verify-during-build is re-aimed at agent B's REAL landing site** (swarm A1): the preamble seam lives in `infra/clusters.py`/`ops/submit_flow.py`/`host_retarget.py`/`monitor_flow.py`, not transport. First step diffs the raw-spawn inventory against B's landed files, aborts legs already fixed, and asserts **byte-level command-line equality (no preamble text) for transfer-plane ops** post-routing (technical E1). Engine-seam laws extend: `EngineUnavailable` → one-shot fallthrough, `capture=False`/streaming never consults the engine, the one-shot leg stays on `run_capture_bounded` and its route test is UPDATED, never deleted.
8. **Daemon (3.3) gains the durability posture** (doctrine D3): every daemon-dispatched journal append routes through the one `infra/io.append_jsonl_line` seam AND the RPC reply is written only after the seam's fsync returns — fire path: kill the daemon immediately post-reply, record must be durable. v1 serializes requests (single worker queue) with per-request {cwd, env-allowlist}; concurrency is explicitly out of this program (technical B2). Launch via `python -m` (never the console-script .exe — WinError-32 class), handshake on the FULL build fingerprint with refuse-and-fall-back + self-exit on mismatch, pipe name embeds user+fingerprint, argparse `SystemExit` caught per-RPC, dist-signature re-checked per RPC (~5ms, G6), `env_actor` resolves from the CALL's env.
9. **R1's consent minting faces the typed-authorship bar** (doctrine D1): a bare `y` mints greenlight-only, byte-identical to today; minting a standing consent requires a typed phrase naming the boundary set (e.g. `y through morning`). The R1 brief below carries this as a constraint of the grant.
10. **Bake staleness is content-keyed, never version-string** (technical A4, doctrine 2.1 rows): key on build fingerprint; DIRTY source checkout → always walk (devs pay 1.3s; wheels get the win); any hydration mismatch → walk + byte-identical output, never an error. Plugin-carrying envs (A3): the baked answer is gated on `cached_cli_reshaping_verdict` EXTENDED to "any plugin contributing `primitive_modules` OR `register_cli` → full walk". All baked-artifact reads pass `encoding="utf-8"` (A5).
11. **Hook fusion fails toward running** (technical F1): prefilter reads `sys.stdin.buffer`, decodes utf-8 `errors='replace'`; decode failure = fail-OPEN to all guards; per-guard isolation (A's exception still runs B/C and reports A); prefilter is stdlib-only syntactic necessary-conditions, never a re-implementation of a guard predicate (one-definition). `install_agent_assets` removes the three legacy Stop entries by exact match in the same write (F2 — the 539c1cdc regression zone); doctor gains a duplicate-Stop-hook check; the conformance adapter surface is in 1.7's declared set (swarm A7).
12. **Shared-fakes instrumentation lands ONCE, pre-wave** (swarm A3): new sequential Unit 1.0 puts exec/dial counters into `tests/_ssh_fakes.py` + `tests/ops/monitor/conftest.py`; all counting units consume read-only; missing counter shapes go to the integrator, never edited on a unit branch.
13. **ci.yml has ONE owner per wave** (swarm A2): 1.1 owns all Wave-1 ci.yml edits; 1.3's eager smoke is a default-tier contract test (`tests/contracts/test_eager_import_smoke.py`) — which also covers 3.10/3.11 + Windows legs; 1.2's new lint is wired into ci.yml by the integrator at wave close.
14. **2.5 exclusively owns `infra/cluster_status.py` + `tests/infra/test_cluster_status_reporter.py`** (swarm A4); 2.2 asserts reporter-call counts inside `tests/ops/aggregate/test_canary_verify.py` only. 2.5's real scope includes the deployed remote reporter (`execution/mapreduce/reduce/status.py`) at the **Python 3.8 floor** (A5).
15. **2.3 resolves the drive.py mirror twin** (swarm A6): `block_drive.py:337`'s capture path mirrors `drive._run_cli_step` under `lint_mirror_ledger.py`; `_kernel/lifecycle/drive.py` is in 2.3's declared write set (rebased on the landed b7e03e9c) — either de-mirror with a ledger update or make the twin change.
16. **Generated-artifact protocol** (swarm A10): units never commit regenerated artifacts; ONE `python scripts/regen_all.py --write` per wave at integration; **2.1 merges FIRST in Wave 2** (it modifies the generator); **1.3 merges LAST in Wave 1** (full-CI-matrix gate — laziness failures manifest anywhere); regen-forcing units: 1.2, 2.1, 3.1, 4a, 5.1, 5.3; new-verb/flag units author primitive doc templates or `check_no_pending_primitive_docs.py` reds. If integrating via PR, do local regen + push to a `claude/**` branch (the regen-pr auto-commit does not retrigger CI).
17. **Slow-tier + Windows-skip discipline** (swarm A8/A9): acceptance numbers living in `-m slow` files get explicit targeted `-m slow --timeout=0` invocations (named per unit in the battery); new timing assertions are written xdist-load-tolerant or marked slow; units whose acceptance tests skip on win32 (2.6 signal tests, 4b severance) gate on the GitHub Linux matrix, never local green. Local battery = targeted tests only; full suite = GitHub CI (standing directive).
18. **Wave-5 internal order: 5.3 AFTER 5.1** (swarm A12 — both flip pins in `test_block_gate_and_speculate.py` and alter gate semantics at the same sites). SKILL.md twins (`slash_commands` mirror) join every unit that edits `skills/hpc-submit/SKILL.md` (A11); cross-wave order 3.1 → 4a → 5.1 on that file.
19. **G-series pins adopted**: G1 (2.2: terminal report reuses the e79efd2c breaker deadline-wait + one retry; acceptance test breaker-open-at-terminal), G4 (1.3: `__getattr__` serves underscore attrs incl. `_PACKAGE_ROOT`; honest AttributeError/ImportError, never swallow), G5 (1.1: doctor probe "jsonschema importable" keeps a preflight-time surface), E2 (remote helpers POSIX sh only; degrade to per-file when the pinned-interpreter probe reports absent), E3 (sentinel-framed fused outputs; missing end-sentinel → per-item fallback, never parse-and-trust truncation).
20. **DECLINED (no build, no ruling unless reopened)**: `--spec -` stdin transport (contradicts run-14 anti-over-authoring, reopens finding-13/17, ~0 marginal gain); Windows-Defender exclusion guidance (gap did not reproduce); `percall.pylint-guard-mypy-per-edit` (out-of-repo; operator note: ~0.5–0.7s warm). No-action calibration claims: `cold.trampoline-and-pyc-nonissues`, `percall.journal-and-envelope-floor` (post-fix floor ≈ 5–10ms dispatch + 5–20ms/durable write).

**Run-14 mandate placement**: WS-SPEC's materialization leg (every boundary materializes the complete successor spec as a sidecar file; verbs take `run_id` and dereference — submit-speculate is the proof case) is USER-MANDATED and proceeds without ruling; only the consumption leg (thin-`y` acting under the code-composed spec) waits on R3. This outranks latency ordering: 3.1 dispatches as soon as Wave 2 integrates, regardless of telemetry.

---

## 1. WAVE PLAN + DEPENDENCY EDGES

**Wave 0 — preconditions (sequential, no swarm)**
- (a) **Claim source of truth = `git status`, re-run at dispatch time.** Snapshot at memo time (post-b7e03e9c): dirty = `infra/clusters.py`, `infra/ssh_circuit.py`, `ops/host_retarget.py`, `ops/monitor_flow.py`, `ops/submit_flow.py`, `_kernel/hooks/relay_audit_stop/_contradiction.py`, `ops/decision/journal/verify_relay.py` (+ their tests). `drive.py` has LANDED (b7e03e9c); `state/journal.py` and `infra/transport/*` were never touched. Every dirty file is claimed; units touching one (1.7 ← relay_audit_stop, 1.9 ← monitor_flow, 2.4/F7 ← clusters/submit_flow, 2.6 ← monitor_flow, 5.2 ← ssh_circuit) hard-gate on its land and REBASE-FIRST (re-read the seam, do not trust docket line numbers — G3).
- (b) This plan package + the RULING-NEEDED docket to the maintainer; R1–R7 answered in parallel with Waves 1–2.
- (c) Operator steps (no repo file): purge `rfc3987-syntax` from the dev venv; optional pylint/mypy-per-edit hook trim.
- (d) **Unit 1.0** — shared-fakes counting instrumentation (see §0.12). Merges before Wave-1 dispatch.

**Wave 1 — cheap measured wins (9 parallel units, file-disjoint)**
1.1 lazy-jsonschema + heavy-import lint + uv pin (owns ci.yml) · 1.2 fast-path prerequisites (`--schema`→CliShape, describe_cache store-guard, parser-truth lint) · 1.3 PEP-562 lazy `__init__` (merges LAST, full-matrix gate) · 1.4 slot wakeup · 1.5 F5 server-side log loop · 1.6 F6 canary double-pull fold · 1.7 stop_multiplex (gated on relay-audit in-flight land) · 1.8 F1 include-fold · 1.9 P4-t1 wave-list combine (gated on monitor_flow land).
Integration: merge order arbitrary except 1.3 last → one regen → push → CI matrix green.

**Wave 2 — the M-weight kills (6 parallel units)**
2.1 baked hydration + narrow fast-path opt-ins (merges FIRST) · 2.2 verify_canary liveness split · 2.3 WS-INPROC · 2.4 F2+F7 transport (verify-during-build) · 2.5 F3 rows_observed · 2.6 P3+F4 push/census (P1 folded in).
Edges: 2.1 needs 1.2 landed (bake = whole truth); 2.2 rebases on 1.6's flipped pins; 2.3 lists 2.6's census waiter on KEEP-subprocess; 2.4 rebases on agent B's landed seam; 2.6 consumes Unit 1.0 counters.

**Wave 3 — after rulings R3/R4 + Wave-2 telemetry**
3.1 WS-SPEC S1+S2 (materialization unconditional; consumption leg iff R3) · 3.2 P2 idle horizon (ONLY if post-P1 telemetry shows residual reconnect-per-poll) · 3.3 WS-DAEMON (iff R4; opt-in `HPC_CLI_DAEMON=1` first). Edge: 3.1 before 4a (both touch block_drive + SKILL.md).

**Wave 4 — ruling-gated loop (sequential sub-steps: 4a → 4b)**
4a fused commit+advance (`--approve` routes through the one `append_decision`; rendezvous guard demoted to backstop that provably still fires) · 4b worker-exit `_park` one-definition-two-seats + L3 code-fired canary speculation (iff R2). Edge: 4a consumes 3.1's materialized spec; 4b's L3 reuses the #249 TTL cache as its ONE speculation-state definition.

**Wave 5 — rulings R1/R5/R6 (5.1 → 5.3 sequential; 5.2, 5.4 parallel-safe)**
5.1 standing-consent collapse (iff R1, with the D1 minting bar) · 5.2 breaker class-dependent cooldown (R6 — defer-recommended; build only after a telemetry cycle) · 5.3 speculation-eligibility law (iff R5; AFTER 5.1) · 5.4 WS-AGENT design doc + digest schema (docs only; carries the two doctrine mandates: attestation routes through `state/attestation.py` or stops calling itself one; the digest is DATA — control-plane classify/settle still computes every verdict).

**Program-wide integration checklist (each wave close)**: merge (ordered) → `regen_all.py --write` once → universal trio (`ruff check --fix`, `ruff format`, `mypy src/hpc_agent`) → targeted batteries incl. named `-m slow` legs → push → GitHub CI matrix green (Linux gates the win32-skips) → **enforcement-map rows for the wave appended to `engineering-principles.md`** (run-13 precedent, bea86ebf) → wall-clock deltas recorded in this directory's `telemetry.md`.

---

## 2. EXPECTED KILL PER WAVE (verifier-corrected)

| Wave | Units | Wall-clock killed |
|---|---|---|
| 1 | 1.0 + 9 | per-turn hooks 2.4–4.5s → 0.3–0.5s (≈4–8 min / 200-turn session); dev jsonschema import −2.4–2.9s; slot wake 4–8s → <0.6s per contended acquire; per-task harvest 6→2 round-trips + 2 hash-walks removed; log triage F×J → 1 exec; canary sample pull 4→2 RTs; burst tick → 1 combine exec |
| 2 | 6 | full-path CLI 7.0–7.1s → ~1.3s; describe/find ~5.2s → <1.6s; mcp-serve 7.5–8.7s → ~2s; gated submit −25–70s + ~490s/worker breaker-livelock class deleted; submit pipeline −8–14s (serial spans); terminal-notice staleness 150–300s → 1–2s; fleet 10–20 dials/min → 1/host; re-aggregate hash-walk → stat-walk (tens of s at 2700 tasks) |
| 3 | 3 | spec authoring −1–3 min/run + overnight auto-advance un-wedged (correctness); daemon (opt-in): every surface 1.3–7s → <100ms ≈ 30–120s/run; residual reconnects −~48/4h quiet watch (conditional) |
| 4 | 2 | approve 2–4 calls → 1/boundary + bounced-turn class; −3 agent round-trips/run; forgotten canary speculation −2–10 min/run (R2) |
| 5 | 4 | −3 human round-trips per healthy submit; UNBOUNDED AFK waits → bounded (R1, the largest single item); breaker livelock cycles ≥2 shortened (R6, deferred); push/pull inside review windows (R5) |

---

## 3. RULING-NEEDED DOCKET (maintainer answers R1–R7; nothing here is silently decided)

**R1 — standing-consent collapse (5.1). AMENDED with the D1 minting bar.** Substrate fully built (expiry+caps+cmd_sha+ledger; `overnight.py:248-290`, `block_gate.py:182-183`), wired at 1 of 4 gate sites. Proposal: extend `assert_greenlit_or_consented` to all four; the S1 brief OFFERS a run-scoped consent. **Constraint of the grant (D1): a bare `y` mints greenlight-only, byte-identical to today; minting the consent requires a typed phrase naming the boundary set — bare-ack laundering row applies; enforcement row 23 mandatory.** S4/aggregate-run = read-mostly (easy grant); S2/S3 authorize spend → the brief must code-render the full spend envelope the consent covers. Recommend: grant S4+aggregate-run unconditionally; S2/S3 contingent on the spend-envelope disclosure line. Largest wall-clock item in the docket.

**R2 — code-fired speculative canary at S1 resolved-park (4b/L3).** Scheduler submit with no agent action pre-greenlight. Doctrine aligns (determinism boundary + design §3's pre-greenlight cluster-touch policy already authorizes exactly this shape; #249 TTL cache = budget-of-1 + nudge invalidation, zero new machinery). Needs the explicit blessing that "no agent action" ≠ "no consent change". Recommend: grant; skip when required ambiguities exist; `stop_after_canary` pinned unstrippable.

**R3 — spec-consumption leg (3.1/S2).** Materialization ships regardless (run-14 #4 user mandate). Ruling: may a plain `{next_block, response:'y'}` cause the driver to run under the code-composed spec (relaxes `block_chain.py:377-386`)? Constraint if granted: sidecar spec sha-stamped at park, recomputed at consumption, refuse on drift (byte-stability keeps the provenance diff binding); `REQUIRED_CALLER_FIELDS`/goal/task_generator stay human-authored — composer refuses, never fabricates. Denying still kills the authoring class (spec stays a copy source). Recommend: grant with the byte-stability pin; also un-wedges overnight auto-advance, which R1 needs.

**R4 — daemon default vs opt-in (3.3). AMENDED with D3.** Recommend: opt-in (`HPC_CLI_DAEMON=1`) one run cycle, then a default-flip ruling with live numbers — the asyncssh ladder (445ce69a precedent). Grant is contingent on the durability posture (§0.8): one append seam + reply-after-fsync, per-call cwd/actor, full-fingerprint handshake, dist-signature per RPC, dead-daemon → byte-identical inline (a dead daemon must never mean a silently skipped guard).

**R5 — speculation-eligibility law (5.3).** Registry flag + kind-taxonomy lint mechanize only the non-scheduler-mutating leg; `idempotent + content-keyed` are ASSERTED semantics → each eligible verb requires an ENUMERATED double-run/byte-identical fire test to join the flag set, plus the pin that speculative execution never appends a decision record nor a block terminal (journal byte-identical). Recommend: grant the law; wire S2-push first, S4-pull second; speculative work low-priority under the slot limiter.

**R6 — breaker class-dependent cooldown (5.2).** The cooldown IS the ban guard; classifier fires only at cycle ≥2 and its false-positive rate on real bans is unproven. Recommend: DEFER one run cycle for classifier telemetry, then grant narrowly (single bare probe, ban-class keeps the full ladder, both directions pinned, `reopen_cycles` never reset by connection success — row L523).

**R7 — pre-resolve rendezvous / remote_path auto-derivation. Recommend DENY/DEFER.** Verifier-corrected gain ≈ 2–4s agent-side only (the genuine human wait lives at the SECOND park, which survives); `remote_path` → CODE_DERIVED is a real consent-boundary change against the deliberate 627ae62e posture. Not worth the doctrine spend.

---

## 4. PER-UNIT PRE-PUSH BATTERY (swarm premortem's table, corrected for the resequencing)

Universal, every unit: `.venv/Scripts/python.exe -m pytest` targeted + backgrounded (never `uv run`; full suite = GitHub CI, never local) + `ruff check --fix` / `ruff format` / `mypy src/hpc_agent` + `python scripts/regen_all.py --check`.

| Unit | Targeted tests (default tier) | Extra `-m slow` targets | Lints beyond universal |
|---|---|---|---|
| 1.0 | `tests/test_ssh_fakes_example.py`, `tests/ops/monitor/`, `tests/ops/aggregate/` | — | — |
| 1.1 | `tests/meta/campaign/atoms/test_budget_accounting.py`, `test_budget_ack.py`, `test_campaign_atoms.py`, `tests/meta/campaign/test_validate_campaign_workflow.py`, new lint's fire test in `tests/scripts/`, `tests/contracts/` twin | — | `lint_library_knowledge.py`, `lint_pure_files.py`; fire-path `python -X importtime -c "import hpc_agent.meta.campaign.atoms.budget"` |
| 1.2 | `tests/cli/test_setup*.py`, `tests/cli/test_fast_path_cache.py`, `tests/scripts/test_regen_all.py`, `tests/contracts/test_generated_merge_driver.py`, new parser-truth lint fire test | — | new lint fires on a synthetic in-`_register_from_registry` `add_argument` |
| 1.3 | `tests/contracts/` (incl. new eager-import smoke), `tests/cli/test_fast_dispatch.py`, `tests/test_mcp_server.py`, `tests/integration/test_dispatch_smoke.py`; **merge gate = full CI matrix, merges last** | `tests/test_mcp_server.py -m slow` | `lint_mirror_ledger.py` (TYPE_CHECKING mirror), `lint_subject_init.py` |
| 1.4 | `tests/infra/test_ssh_slots.py`, `tests/infra/test_ssh_engine.py`, `tests/infra/test_ssh_throttle.py` if present | `tests/infra/test_ssh_engine.py -m slow` | timing asserts xdist-tolerant |
| 1.5 | `tests/ops/monitor/test_logs_atom.py`, `test_summary.py`, `tests/ops/recover/test_net_triage.py` | — | `lint_no_raw_ssh.py`, `lint_deploy_python_floor.py` (if a server-side script ships), `lint_remote_read_ack.py` |
| 1.6 | `tests/ops/aggregate/test_canary_verify.py`, `test_pull_tar_seam.py`, `test_flow_incremental_pull.py` | — | — |
| 1.7 | ALL of `tests/_kernel/hooks/` (incl. `test_relay_audit_stop.py`, `test_answer_capture.py`, `test_skill_return_autofetch.py`, `test_scheduler_write_fence.py`, `test_alert_count.py`, `test_utterance_capture.py`), `tests/cli/test_agent_assets_settings_hook.py`, new trigger-equivalence contract test | — | **conformance lane**: `pytest -o addopts= -p no:cacheprovider -q src/hpc_agent/conformance/ --harness-adapter hpc_agent.conformance.adapters.claude_code:build` |
| 1.8 | ENTIRE `tests/ops/aggregate/` (flow tests interlock) | — | — |
| 1.9 | `tests/ops/aggregate/test_combine_wave_idempotent.py`, `tests/cli/test_aggregate.py`, `tests/ops/monitor/test_flow_announce.py`, `tests/execution/mapreduce/test_combiner.py`, `test_combiner_failures.py`, `tests/ops/aggregate/test_cluster_side_reduce.py` | — | `lint_deploy_python_floor.py`, `lint_schema_versions.py` |
| 2.1 (merges first) | `tests/cli/test_fast_dispatch.py`, `test_fast_path_cache.py`, `test_fast_path_plugins.py`, `tests/integration/test_dispatch_smoke.py`, `tests/scripts/test_regen_all.py`, `tests/test_mcp_curated.py`, `tests/contracts/test_schema_roundtrip.py`, `tests/contracts/test_generated_merge_driver.py` | `tests/test_mcp_server.py`, `tests/cli/test_fast_dispatch.py` (wall-time acceptance) | `bake_operations_json.py --check`, `build_schemas.py --check` |
| 2.2 | `tests/ops/aggregate/test_canary_verify.py` (rebased on 1.6's pins), `tests/ops/monitor/test_flow_liveness_gate.py`, `test_flow_harvest.py`, `test_reconcile_canary_pairing.py`, `tests/ops/test_block_gate_and_speculate.py` | — | do NOT edit `tests/infra/test_cluster_status_reporter.py` (2.5's) |
| 2.3 | `tests/_kernel/lifecycle/` (ALL, incl. `test_driver_tick_stamp.py`), `tests/contracts/test_src_subprocess_timeout_discipline.py`, cross-consumer: `tests/ops/attention/`, `tests/ops/status/test_snapshot_attention.py`, `tests/ops/monitor/test_blocks.py`, `tests/ops/aggregate/test_blocks.py`, `tests/meta/campaign/test_blocks.py`, `tests/ops/test_block_gate_and_speculate.py`, preflight tests | — | `lint_subject_imports.py`, `lint_private_cross_package_imports.py`, `lint_mirror_ledger.py` (drive.py twin) |
| 2.4 | `tests/infra/test_transport_pull.py`, `test_remote_rsync_fallback.py`, `tests/ops/aggregate/test_cluster_side_reduce.py`, `test_pull_tar_seam.py`, `test_flow_incremental_pull.py`, `test_flow_stale_mirror.py`, `tests/execution/mapreduce/test_combiner.py` | `tests/infra/test_remote.py -m slow` | `lint_no_raw_ssh.py`, `lint_remote_read_ack.py`, `lint_deploy_python_floor.py`; F7 memo diffs against agent B's landed `clusters.py`/`submit_flow.py` |
| 2.5 | `tests/ops/aggregate/test_flow_gates.py`, `tests/infra/test_cluster_status_reporter.py` (exclusive owner), `tests/execution/mapreduce/test_status.py`, `test_status_rollup.py`, `tests/ops/monitor/test_batch_status.py` | — | `lint_deploy_python_floor.py` (3.8!), `lint_schema_versions.py`, `lint_telemetry_labels.py` |
| 2.6 | ENTIRE `tests/ops/monitor/` + `tests/ops/decision/test_overnight_self_heal.py`, `tests/execution/mapreduce/test_dispatch.py`, `tests/ops/monitor/test_watchdog_stamp_contract.py`, `tests/ops/status/` | `tests/execution/mapreduce/test_dispatch_signal.py -m slow` (**skips on win32 — Linux CI gates**) | `lint_deploy_python_floor.py`, `lint_schema_versions.py`, `lint_atomic_durable_writes.py`, `lint_telemetry_labels.py`, `lint_remote_read_ack.py` |
| 3.1 | `tests/contracts/test_spec_hint_completeness.py`, `tests/_kernel/lifecycle/test_block_drive_specs.py`, `tests/ops/test_block_gate_and_speculate.py`, `tests/_wire/` (all), `tests/contracts/test_schema_roundtrip.py`, cross-consumer block-drive battery (as 2.3) | `tests/_wire/test_schema_models_roundtrip.py -m slow` | `build_schemas.py --check`, `lint_wire_suffix.py`, `lint_schema_reachability.py`, `lint_skill_command_sync.py` + slash twin, `lint_atomic_durable_writes.py` (sidecar spec), primitive doc templates |
| 3.2 | `tests/infra/test_ssh_engine.py`, `tests/ops/monitor/test_flow_adaptive_poll.py`, `test_flow_poll_floor.py`, `test_flow_poll_tolerance.py` | `tests/infra/test_ssh_engine.py -m slow` | — |
| 3.3 | `tests/integration/test_dispatch_smoke.py`, `tests/test_mcp_curated.py`, `tests/cli/` fast-path trio, new daemon durability/cwd/actor tests | `tests/test_mcp_server.py -m slow` | conformance lane (both adapters) |
| 4a | `tests/_kernel/hooks/test_decision_rendezvous_hooks.py` (against the FUSED multiplex), `tests/ops/decision/test_overnight_wiring.py`, `test_multi_human_gate.py`, `tests/ops/test_decision_journal_primitives.py`, cross-consumer block-drive battery | — | `lint_decision_content.py`, `lint_skill_command_sync.py`, conformance lane, primitive doc + regen |
| 4b | `tests/ops/monitor/test_wait_detached.py`, `tests/_kernel/lifecycle/test_detached_*` (**severance skips on win32 — Linux CI gates**), `tests/ops/test_block_gate_and_speculate.py`, `_park` import-location contract test | — | — |
| 5.1 | `tests/ops/decision/test_overnight_wiring.py`, `test_overnight_consent.py`, `tests/ops/test_block_gate_and_speculate.py`, `tests/ops/aggregate/test_blocks.py`, `tests/ops/monitor/test_blocks.py`, cross-consumer block-drive battery | — | `lint_decision_content.py`, `build_schemas.py --check` |
| 5.2 | `tests/infra/` circuit tests, `tests/ops/monitor/test_harvest_guard.py` (rebase on in-flight `ssh_circuit.py`) | — | — |
| 5.3 (after 5.1) | `tests/ops/test_block_gate_and_speculate.py`, new eligibility-lint fire test, per-verb idempotency fire tests, `tests/contracts/` | — | new lint wired into ci.yml + `bake_operations_json.py --check` |

---

## 5. ENFORCEMENT-MAP ROWS OWED (doctrine premortem's consolidated 24, assigned per unit)

Each landing unit owes its row(s) in `engineering-principles.md`'s enforcement map at wave integration (fire path named). Assignment: rows 1,5 → 2.1 · 2 → 1.3 · 3 → 1.5 · 4 → 1.7 · 6 → 2.2 · 7 → 2.3 · 8,9 → 2.4 · 10 → 2.5 · 11,12,13 → 2.6 (row 11 restated for the WS-AGENT digest in 5.4) · 14,15,16 → 3.1 · 17,18 → 3.3 · 19,20 → 4a · 21,22 → 4b · 23 → 5.1 · 24 → 5.3. Full row texts are carried verbatim in `unit-specs.json` per unit (`enforcement_map_rows`). Every NEW lint lands with a synthetic-violation fire test (repo standard).

---

## 6. RESIDUAL RISK REGISTER (top 5, from the premortems)

1. **Wrong-but-plausible output from partial/stale caches** — the program's dominant failure mode is silent wrongness, not slowness: fast-path partial-registry describe_cache poisoning (A1, now designed out), stale bake on editable installs (A4), the F2 manifest cache re-arming the run-13 finding-13 class (D2). Every cache in the program is content-keyed, success-only, with a walk/recompute fallback pinned byte-identical.
2. **NFS semantics silently defeating WS-PUSH** — inotify never firing cross-node (C1) and login-pool attribute-cache skew (C2). Designed out via remote sh poll + sticky-host + wake-is-a-hint, but this is the unit (2.6) where a lab-green implementation can still fail only at cluster scale: its Wave-2 telemetry gate is mandatory before P2/WS-AGENT build on it.
3. **Daemon durability/staleness** (B1/B2/D3) — lost human greenlights and stale code served indefinitely. Contained by R4 opt-in + fsync-before-reply + fingerprint self-exit; concurrency deliberately out of scope.
4. **Hook fusion single point of failure** (F1/F2-technical) — one crash silencing the relay-audit integrity guard. Contained by fail-open decode + per-guard isolation + conformance lane + settings de-dup migration.
5. **In-flight collision churn** (swarm A1 — already proven: the claim set changed between the premortem and this memo). Contained by the mechanized Wave-0 rule: `git status` at dispatch, dirty = claimed, rebase-first steps in every gated unit.

## Drift log

- 2026-07-16 late: units 3.2/3.3 (daemon) SUPERSEDED by docs/plans/daemon-engineering-2026-07-16/ (the R4 "fully engineer this" ruling; full design + 4-lens premortem + DW0-DW3 decomposition live there).

## Drift log addendum — 2026-07-16 late rulings

- **R1 APPROVED with D1** (maintainer, typed): ONE standing consent per run, minted ONLY by a typed bounds-naming utterance via the offered-hint popup (bare y never mints), covers chain-forward advances while every check is green; any deviation parks. Unit 5.1 unblocked.
- **R2 APPROVED**: S1 resolve fires the speculative canary itself (code-composed, sidecar-dereferenced, disclosed, auto-killed on spec-changing nudge). Unit 4b unblocked.
- **R5 APPROVED**: speculation-eligibility is a mechanized enumerated law (idempotent + content-keyed + non-scheduler-mutating, per-verb idempotency fire tests); each verb adoption remains its own decision.
- Still pending: R3 (spec-consumption byte-stability leg — present at 3.1 dispatch); R6/R7 keep their in-plan DEFER/DENY recommendations.
