# STATE/CONCURRENCY PREMORTEM — WS-DAEMON v2

Verified against source at main @ 57b31f9c. Findings ranked by corruption severity; each carries evidence (file:line) and a concrete mitigation. Findings 1, 2, 3 are the ones I'd block the build on.

---

## F1 — HIGH (journal-home + attribution corruption): daemon-spawned detached workers inherit the DAEMON's environment, not the call's

**Evidence**: `_spawn_detached` builds the worker env as `env={**os.environ, "HPC_DETACHED_RUN_ID": ..., ...}` (`src/hpc_agent/_kernel/lifecycle/detached.py:502-506`). The design threads `hpc_env` per-call via ContextVar for **in-process reads** (§4) and via `env=` for **Lane B**, but block-drive ticks are Lane-A-eligible (§3.1) and the supervisor "calls the same `launch_submit_block_detached`" (§3.3) — i.e., detached spawns happen FROM Lane A, inside the daemon, where `os.environ` is the daemon's boot env.

**Failure scenario**: client exports `HPC_JOURNAL_DIR` (test isolation, or the suite itself), or `HPC_ACTOR=alice`, or `HPC_AGENT_ALWAYS_CANARY=1`, and dispatches a submit via the daemon. The detached S2 worker spawns with the daemon's env: it writes leases/journal/breaker state to the **real** journal home (`current_homedir()` — `ssh_slots.py:19-21`, `ssh_circuit.py:29`), stamps `env_actor=None` (or the daemon-boot actor) on every journal record, and drops the canary flag. Multi-actor attribution (`_session_actor` → `attestor_id`, `ops/decision/journal/__init__.py:222-224`) is silently corrupted; a test run pollutes production state. Nothing refuses, nothing discloses.

**Mitigation**: `_spawn_detached` (and every `subprocess` site reachable from a daemon-dispatched verb) gains an explicit `base_env:` built from the `call_env()` seam, never bare `os.environ`. Mechanize: extend the §4 reader census to `subprocess.*(..., env=` construction sites — any `**os.environ` spread inside daemon-reachable code is a lint hit. Fire test: daemon-dispatched `submit-s2` with `HPC_ACTOR` + `HPC_JOURNAL_DIR` in `hpc_env` → worker's lease and journal record land in the overridden home with the right attestor.

---

## F2 — HIGH (duplicate mutating writes): the §5 "decision dedup" claim is FALSE, and client-deadline-falls-inline manufactures concurrent double execution

**Evidence**: `append_decision` (`ops/decision/journal/__init__.py:185-243`) appends unconditionally — there is no request-identity dedup anywhere in the path (the journal-status dedup at `state/journal.py` is run-submission dedup, a different thing). The codebase's own history proves duplicates land: "two `s1` greenlight records 32s apart" (`__init__.py:253-255`). §5 says a no-reply client must "re-drive through the verb — the verbs' existing idempotency (recorded-terminal replay, **decision dedup**) resolves it." For `append-decision`, that idempotency does not exist.

**Failure scenario** (the design creates this, it isn't exotic): hook sends `append-decision` with `deadline_ms=250`; Lane A is momentarily busy (a 300ms read, or flock contention on the same journal); client deadline expires → falls inline per doctrine → inline append lands at T+0.4s. The daemon dequeues the abandoned call and ALSO lands it at T+2s. Two greenlight records — and worse, `_default_next_block` derives from the parked pending decision (`__init__.py:246-269`), whose state the FIRST append may have consumed, so the two records can carry **different** `resolved.next_block` values. Block-drive's gate now has conflicting authorizations; downstream forensics ("who authorized this advance") is corrupted.

**Mitigation**, three layers: (a) daemon drops queued-not-yet-started RPCs whose client connection is closed — check pipe liveness at dequeue (closes the common case: an abandoning client closes the pipe); (b) make the §5 claim true before leaning on it: mutating RPCs carry a client-minted `request_id`, stamped into the record; the append path treats a same-`request_id` re-append as a replay no-op (this also fixes the pre-existing run-#2 duplicate class); (c) until (b) exists, classify `append-decision` re-drive-after-UNKNOWN as **human-surfaced**, not automatic — the fire test in §5 ("retry is a no-op/replay, no duplicate semantic decision") will fail today as specified, which is the tell the design didn't check this verb.

---

## F3 — HIGH (internal contradiction → wedge factory): §3.1 forbids ssh in Lane A; §3.4 gives Lane-A verbs the warm ssh channel — both cannot hold, and each resolution breaks something

**Evidence**: §3.1 eligibility criteria: "no ssh, no `os.chdir`, no unbounded wait, ceiling ≤ 20s." §3.4: "Lane-A verbs that do a bounded cluster read get the warm channel."

**Failure scenario if ssh verbs enter Lane A**: a warm cluster read against a slow host legitimately runs connect-timeout (15s, `ssh_engine.py:266-274`) + command time + `_RESULT_MARGIN` (10s) ≈ the 20s ceiling. Three per-turn hooks (10ms-class) queue behind it — the daemon's entire latency win evaporates exactly when the cluster is slow. Worse: ceiling ×1.5 = 30s → watchdog marks WEDGED → daemon self-exits → client respawns it → next cluster read wedges it again. A merely-slow login node becomes a daemon **crash-loop**, with every surface paying spawn+probe on every call — strictly worse than today. Also: a `_submit` blocked in `future.result(timeout=deadline)` (`ssh_engine.py:347-349`) cannot be interrupted by the watchdog; `os._exit` while a pooled connection has a command in flight violates the engine's own no-mid-command-sever rule (finding-24, `ssh_engine.py:135-136`) — the remote half keeps executing with no observer.

**Failure scenario if ssh verbs are excluded**: the §3.4 pool serves **nobody** in v1 — Lane B and detached workers dial their own engines by design. The daemon then holds warm per-host connections + their slots (`ssh_slots` whole-call holds) purely as dead weight that double-books against the workers doing real ssh.

**Mitigation**: resolve the contradiction explicitly in the doc. Recommended: v1 Lane A = strictly no-ssh (the hooks/reads win is the mission; it needs no pool); the pool ships DISABLED in the daemon until either (i) a dedicated single "Lane A-ssh" dispatch thread exists with its own ceiling (45s) and its own watchdog policy (drain, never shoot mid-command — wait for `inflight==0`), or (ii) the stream-router v2 lands. Do not let the pool's existence pressure ssh verbs into the serial lane.

---

## F4 — HIGH (torn journal line): "a wedged daemon … is safe to shoot" is false while Lane A is inside the append seam

**Evidence**: watchdog policy (§3.1): hard-exit "via `os._exit` after flushing the log." `append_jsonl_line` (`infra/io.py:283-287`): flock → `fh.write(line)` → `flush()` → best-effort fsync. `os._exit` fired from the watchdog thread while the dispatch thread is mid-`flush()` can land a **partial line without trailing newline** on disk (the kernel write may be partially complete). The flock releases on process death.

**Failure scenario**: the abandoning client (F2) has already fallen inline; its inline `append-decision` acquires the now-free lock and opens `"a"` — its record is appended **onto the torn tail**: one merged unparseable line = the daemon's record lost AND the inline record corrupted. Journal authority violated by the designed kill path, and the design's own doctrine made the second writer show up at exactly the wrong moment. Note also the ceiling interplay: `append-decision` is Lane-A-eligible but its flock wait is up to 120s (`io.py:283`) — a wedged one-shot holder makes the daemon's append exceed 30s → WEDGED → shoot → this scenario, repeatedly.

**Mitigation**: (a) seam-level self-heal, closes the whole class for every killer (power loss, kill -9, watchdog): inside the lock, before writing, check the file's last byte; if not `\n`, prepend one (the torn record is already lost; don't let it poison the next). One-definition: it lives in `append_jsonl_line`. (b) Watchdog cooperation: a critical-section flag set around the flock-hold in the seam; the shot defers until clear (bounded by lock-timeout + ms-scale write). (c) Exempt lock-wait time from the WEDGED clock, or cap the seam's flock timeout at the lane ceiling for daemon-dispatched calls. Fire test: plant a torn tail → daemon append + inline append → both new records parse, torn record reported not silently merged.

---

## F5 — HIGH (every call fails after side effects): a DETACHED_PROCESS daemon may have `sys.stderr`/`sys.stdout` = None or invalid handles, and `_invoke_cli` writes to them unconditionally

**Evidence**: §6 spawns the daemon "with the same platform detach flags `_spawn_detached` uses" — `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` (`detached.py:218-222`), i.e. **no console**. `_spawn_detached` itself always binds the child's stdout/stderr to a log handle (`detached.py:493-495`), but §6 names only the flags. In a no-console Python process with unbound streams, `sys.stderr` is None/invalid. `_invoke_cli` captures `real_err = sys.stderr` for the heartbeat thread (`mcp_server.py:1255-1264`) and writes per-call telemetry via `sys.stderr.write(...)` (`mcp_server.py:1272-1274`) **after** the runner returns.

**Failure scenario**: verb runs, journal append commits, fsync completes — then the telemetry write raises `AttributeError` inside `_invoke_cli`, the RPC errors, the client treats the outcome as UNKNOWN and re-drives → feeds F2's duplicate-append path on **every single call**. The heartbeat thread dies on its first write, silently. This is the stdin-false-EOF incident's sibling: CLI-hosted code assuming console streams inside a stream-less server.

**Mitigation**: daemon-serve boot contract (spec it in §6, don't inherit it by analogy): spawn with stdout/stderr bound to the rolling log (the `_spawn_detached` log-handle pattern, stated explicitly), AND at boot verify/rebind `sys.stdout`/`sys.stderr` to the log file if unset. Fire test: spawn the real detached daemon, run a verb long enough to heartbeat, assert completion + telemetry lines in the log.

---

## F6 — MEDIUM (corrupt envelopes → re-drives): non-dispatch threads bleed into the process-global redirect capture

**Evidence**: `redirect_stdout/stderr` rebind process-wide (the design's own §3.1 premise). The daemon has more non-dispatch threads than mcp-serve ever did: connection acceptor, per-connection readers, the watchdog ("logs the offending argv"), supervisor wake fan-out, ssh sweeper. Python's default `threading.excepthook` prints tracebacks to `sys.stderr` — which, during a Lane A call, is the call's captured StringIO.

**Failure scenario**: a client disconnects mid-handshake → its reader thread raises → traceback lands in the concurrent Lane A call's captured stderr; or the watchdog's WEDGED log line lands in the captured stdout **ahead of the single-line JSON envelope** → envelope parse fails → client error → re-drive (F2). Cross-call bleed without any concurrent dispatch.

**Mitigation**: daemon module rule (the `mcp_server.py:1255` lesson promoted): all non-dispatch threads write only through a boot-time-captured `_DAEMON_LOG` handle; install a daemon `threading.excepthook` routing to it. The §8 concurrency test should plant a deliberately chatty background thread + a disconnecting client and assert envelope purity.

---

## F7 — MEDIUM (cross-call env leakage): two ContextVar legs the design doesn't pin

**(a) Context copies into the pool's loop.** `asyncio.run_coroutine_threadsafe` copies the *submitting thread's* context into the loop callback — engine coroutines submitted from Lane A run under the CALL's `call_env()`. Any pool-side read routed through the seam (post-refactor `env_flag` is exactly such a route) makes shared connection setup depend on which client happened to trigger the connect; that connection is then reused by everyone. **Mitigation**: the reader census (§4.2) gets a third bucket — "pool/loop-side code: forbidden from `call_env`, reads the boot snapshot"; or `_submit` (`ssh_engine.py:336`) strips context via `contextvars.Context().run`.

**(b) Missing token-reset.** If Lane A sets the ContextVar without `token = var.set(...) / finally: var.reset(token)` and an exception path skips reset, the next call on that thread inherits the previous call's env — actor A's `HPC_ACTOR` stamped on actor B's decision. **Mitigation**: mandate the token/finally shape in the seam's contract + fire test: planted raising verb, then assert the next call resolves its own `env_actor`.

---

## F8 — MEDIUM (silent env-vs-behavior drift, manufactured by the daemon): engine tunables outside the 5-var mismatch set

**Evidence**: the §4 refusal covers only `HEALABLE_TRANSPORT_ENV_VARS` (`env_flags.py:44-52`). The pool also reads: `HPC_SSH_CONNECT_TIMEOUT` and `HPC_SSH_KEEPALIVE_INTERVAL` fresh from `os.environ` per connect (`ssh_engine.py:271, 301` — the **daemon's** env), and `HPC_SSH_IDLE_CLOSE_SEC` frozen at module import (`ssh_engine.py:142`), plus `HPC_SSH_MAX_CONNECTIONS` in slots.

**Failure scenario**: client exports `HPC_SSH_CONNECT_TIMEOUT=60` for a flaky far cluster; daemon serves it with 15s and no refusal (not in the mismatch set) — while `active_env_overrides()`, now reading **call env**, discloses `60` in every brief. The disclosure surface now lies about live behavior: precisely the env-vs-record drift class (run-12 finding 24) the split exists to close, re-created by the split's own under-inclusive allowlist.

**Mitigation**: the daemon-side snapshot/mismatch set is not the healable-5 — it is the **census output** of every `os.environ` read in pool/engine/slots/circuit code paths. `hello` compares all of them; the refusal message names the mismatched var. The census lint keeps the set from rotting.

---

## F9 — MEDIUM (half-dead daemon that staleness detection can never clear): no signal/KI/accept-loop-death engineering in §6

**Evidence**: §6 covers idle-exit, drain, reboot — nothing on SIGTERM/SIGINT/console-ctrl or on the accept loop dying. `KeyboardInterrupt` delivers to the main thread only; a developer running `hpc-agent daemon-serve` in a console (this WILL happen — it's how everyone debugs) Ctrl+C's it: the main-thread accept loop unwinds while Lane A, the engine loop, and the sweeper keep running.

**Failure scenario**: pid alive, discovery file live, listener gone → every client's staleness rung (b) passes (`pid_alive` true), rung (c) burns the full 250ms deadline, forever — the file is never cleaned because the pid never dies (Lane A thread is non-daemon or the engine loop holds it). Every call on every surface is now +250ms indefinitely: a permanent tax with no self-heal.

**Mitigation**: (a) main thread wraps the accept loop in try/finally → any exit (KI, SIGTERM handler setting a drain event, accept-loop crash) runs the same drain: close listener, finish in-flight reply (D3), `shutdown_all()`, **delete discovery file**, exit; (b) POSIX SIGTERM + win32 `SetConsoleCtrlHandler` route to that event; (c) client-side counter: N consecutive connect-timeouts against a live-pid discovery file → doctor verdict "live but unreachable daemon, stop with `hpc-agent daemon-stop --pid <pid>`" — the design's doctor check should include this verdict, not just fingerprint skew.

---

## F10 — MEDIUM (surface-divergent answers + week-scale pid reuse): supervisor table vs lease authority, and `pid_alive` on recycled pids

**Evidence**: §3.3 keeps disk authoritative but adds an in-memory watch table + launch dedup. Leases self-heal on dead pids (`detached.py` lease liveness); slots reclaim on `pid_alive` (`ssh_slots.py:40-50`, deliberately no TTL).

**Failure scenarios**: (a) the table answers "already running" from a scan snapshot while the lease's holder died post-scan — a daemon client is refused a relaunch that a one-shot CLI (fresh lease read, self-heal) would grant: same question, different answer by surface — the class the design elsewhere calls drift. (b) Over a week-long daemon, Windows recycles pids aggressively: a dead worker's lease/slot pid gets reused by an unrelated long-lived process → `pid_alive` true forever → phantom "live" watch entry + permanent relaunch refusal (leases) / permanent slot under-admission, until a human deletes files. Today's processes are too short-lived to hit this often; a week-long daemon plus week-long leases makes it a when, not if.

**Mitigation**: (a) the table is telemetry only — every "is it running / may I launch" answer delegates to the lease read at ask time (the launch already does; pin the *query* path too, contract test). (b) Lease and slot claims record `(pid, process_create_time)` (psutil already underpins `pid_alive`); liveness = pid alive AND create-time matches. This upgrades the single PID-liveness definition in `infra/proc` — one definition, both consumers inherit.

---

## F11 — LOW-MEDIUM (week-scale memory): the design has no growth budget

Concrete inventories, not vibes: (a) per-connection `McpServer` state (capability flags, dark-channel state `mcp_server.py:1069`) under hook churn — 3 hooks × every turn × a week ≈ 10⁴–10⁵ connections; any dict keyed by connection not freed on close grows without bound. Fire test: N connect/disconnect cycles → `daemon-status.connections` and object counts return to baseline. (b) Supervisor watch-table strays whose terminals never land (hard-killed worker) — evict on periodic `_detached/` rescan; disk is authoritative so eviction is always safe. (c) Lane A queue payloads (`hpc_env` snapshots) — bounded at 32, fine. (d) CPython arena fragmentation + long-lived asyncssh loop over 7×24h — the engineered answer is a **bounded-uptime recycle** (`HPC_DAEMON_MAX_UPTIME_SEC`, default ~24h) through the existing drain path: activation makes restart ~free, and it converts every unknown slow leak into a bounded one. Add `rss_mb` to `daemon-status` and assert a ceiling in the §8 soak (10k-call soak → RSS delta < budget).

---

## F12 — LOW (warm-cache staleness beyond the dist-signature check): audit item

The per-RPC `installed_dist_signature` check covers code (`_verb_aliases` lru_cache, `plugins.py` `@cache` — a plugin install changes the signature). It does NOT cover **mutable config files parsed into module-level caches**: `clusters.yaml` content (the path var `HPC_CLUSTERS_CONFIG` is daemon-side policy, but the file's *content* can be edited mid-week), and any TTL-less parse cache in `infra/clusters.py`. A one-shot CLI re-reads per invocation; the daemon would serve week-old cluster defs. **Mitigation**: the §4 audit gains a fourth grep — module-level caches over file content in daemon-reachable paths must be content-keyed (mtime/sha) or absent; `inspect/_common.py`'s TTLCache pattern (`:80`) is the acceptable shape. Not verified as a live defect (I did not confirm clusters.py caches); classify as a mandatory build-unit audit, same mechanization as the cwd greps.

---

## Summary table

| # | Severity | One-line | Blocks build? |
|---|---|---|---|
| F1 | HIGH | Detached spawns from Lane A inherit daemon env (`detached.py:502`) → journal-home + actor corruption | Yes |
| F2 | HIGH | `append-decision` has no dedup; §5's idempotency claim false; deadline-abandon = concurrent double append | Yes |
| F3 | HIGH | §3.1 vs §3.4 ssh-in-Lane-A contradiction → slow-cluster crash-loop OR a pool with zero consumers | Yes (design fix, cheap) |
| F4 | HIGH | Watchdog `os._exit` mid-append-seam tears a line the inline fallback then merges into | No, but §5 must adopt the newline self-heal |
| F5 | HIGH | Detached daemon's `sys.stderr` may be invalid → telemetry write fails every call post-commit (`mcp_server.py:1272`) | No — one boot-contract sentence + fire test |
| F6 | MED | Background threads bleed into process-global capture → corrupt envelopes | No |
| F7 | MED | ContextVar copies into the engine loop; missing token-reset leaks env across calls | No |
| F8 | MED | Engine tunables outside the mismatch-5 → disclosure lies about live behavior | No |
| F9 | MED | No signal/accept-loop-death handling → live-pid unreachable daemon, permanent +250ms tax | No |
| F10 | MED | Supervisor-table vs lease divergence; pid reuse defeats liveness at week uptimes | No |
| F11 | LOW-MED | No memory budget: per-connection state churn, watch strays; add bounded-uptime recycle + RSS soak assertion | No |
| F12 | LOW | Config-content caches invisible to the dist-signature check — mandatory audit | No |

Cross-cutting note for the Finalize agent: F2+F4+F5 compose — a failed reply for any reason (stream crash, watchdog shot, deadline) funnels the client into re-drive/fall-inline, and the journal's lack of request-identity is what converts every one of those availability events into a **semantic corruption** event. The single highest-leverage mitigation in this document is the client-minted `request_id` on mutating RPCs (F2b): it downgrades three HIGH findings' corruption legs to availability legs at once.