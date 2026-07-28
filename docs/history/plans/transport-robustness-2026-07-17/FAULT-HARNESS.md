# Fault-Injection Harness — step 3 of the transport-robustness sequence

**Status:** harness skeleton + first test wave LANDED. New code only, under
`tests/faultinject/` — no product source or existing test touched.
**Date:** 2026-07-17.
**Feeds:** step 1 [`AUDIT.md`](AUDIT.md) (the fault-injection test-point
inventory, §7) → **step 3 THIS** → the step-2 unit list (AUDIT §9, U1–U9).

The doctrine under test (AUDIT §4): the *channels* are almost universally safe —
every remote reader is positive-evidence ack-gated, so a severed read
RAISES/UNKNOWN, never mis-settles. This harness proves that mechanically and
locks it in as regression armor **before** U1–U9 refactor the transport, plus it
pins the two protection layers (breaker, slot limiter) actually firing and the
one still-open write-window (rank-3 stage swap) as a strict-xfail signal.

---

## 1. Fixture vocabulary (`tests/faultinject/conftest.py`)

| Fixture | Injects | Built on | Use for |
|---|---|---|---|
| `sever_at(target, *, exc=ConnectionError, message, after_n_calls=0, passthrough=None)` | seam RAISES a transport exception mid-op | `unittest.mock.patch(side_effect=)` | a dropped channel; `after_n_calls` = drop after N good batches |
| `hang_at(target, *, seconds)` | seam BLOCKS for a SHORT test-tuned time | `patch(side_effect=sleep)` | seams whose caller enforces a **Python-level** deadline (pump-thread join, `future.result` backstop) — never a real 300 s/1800 s wait |
| `garble_at(target, *, return_value \| side_effect)` | seam RETURNS a truncated/garbage value | `patch(return_value=/side_effect=)` | rc-0-but-no-ack reads; an unparseable `ssh -V` |
| `fake_clock` → `FakeClock` | injectable wall clock; `.advance()` / `.sleep()` | — | drive breaker cooldown / slot-wait deadlines to expiry with **zero** real time |
| `proc(rc, stdout, stderr)` (helper) | a `CompletedProcess` — the `ssh_run` return shape | — | build the read a seam sees |
| `sleeper_argv(seconds)` (helper) | argv for a real short-lived sleeper child | — | the honest hang for `run_capture_bounded`, whose deadline is **kernel/OS-pipe** enforced (not a Python callable, so `hang_at` cannot drive it) |

Plus the **ssh-probe cache warm** autouse fixture (rationale copied from
`tests/infra/conftest.py`): a cold `ssh -V` probe firing inside an injection
`patch(...subprocess.run/Popen)` window is a known CI killer (the 661a6ca7
double-red). Every test starts with both cached probes warm.

**Doctrine assertions only.** Tests assert the *outcome* (raise / UNKNOWN /
`present:0` refuse / rc≠0 / breaker-fast-fail / deadline-fires), never an
implementation detail past the injected seam. The one command-shape assertion
(the rank-3 xfail) is itself the doctrine (no torn-live-tree window).

---

## 2. Coverage vs the AUDIT §7 inventory

22 injection points in AUDIT §7. This first wave covers the ones testable **today
without a new product seam**; the rest are the needed-seams list (§3).

| # | AUDIT §7 injection point | Seam | Status | Test |
|---|---|---|---|---|
| 5 | rc-0 read but drop the ack line | `ssh_status_report` | ✅ COVERED | `test_channel_refusal::test_status_report_rc0_no_ack_raises` |
| 6 | drop the scheduler-query ack | `ssh_batch_scheduler_states` | ✅ COVERED | `..::test_scheduler_states_rc0_no_ack_is_unreachable` |
| 7 | truncate fused combine (no `BATCH_END`) | `_combiner._parse_batch_output` | ✅ COVERED | `test_transfer_pipe::test_combine_batch_truncation_falls_back` |
| 9 | kill ssh mid-pull → pump error | `_pull._pull_transfer` | ✅ COVERED | `..::test_pull_pump_sever_forces_nonzero_rc` |
| 9h | (variant) hung pull pump | `_pull._pull_transfer` | ✅ COVERED | `..::test_pull_pump_hang_is_bounded_and_reaps_ssh` |
| 10 | timeout mid stage-swap `cp -a` | `transport._stage_swap_cmd` / `_ssh_bounded` | ⚠️ **XFAIL (open gap, U4)** | `test_stage_swap_atomicity::test_stage_swap_has_no_torn_live_tree_window` |
| 12 | 3 consecutive connect failures → open + half-open | `ssh_circuit.guarded_call` | ✅ COVERED | `test_breaker_and_slots::test_breaker_opens_*`, `::test_breaker_half_open_probe_recovers` |
| 13 | slot exhaustion (N=2) under 3rd acquirer | `ssh_slots.acquire_slot` | ✅ COVERED | `..::test_slot_exhaustion_wait_deadline_fires` |
| 15 | remote self-destruct rc 124 (bounded-runner deadline) | `run_capture_bounded` | ✅ COVERED (local analogue) | `test_transfer_pipe::test_bounded_runner_deadline_fires_and_reaps` |
| 17 | session-death `mark_run`→`harvest_on_terminal` | `reconcile._harvest_if_owed` | ✅ COVERED (fix pinned) | `test_reconcile_backstop::*` |
| 4 | qsub dispatch→job-id window (the apex) | submit atom + `reconcile._recover_submitting` | ✅ COVERED (U3: seam built + drilled) | `test_submit_once::test_dispatch_id_window_sever_then_reconcile_adopts_no_reqsub` (+ the sever/prune/race/phantom-id fires) |
| — | census transport sever → not zero-rows (§3f) | `announce.read_announcements` | ✅ COVERED | `test_channel_refusal::test_census_transport_sever_raises_not_zero_rows` |
| — | census truncation → `present:0` refuse (§3f) | `announce.read_announcements` | ✅ COVERED | `..::test_census_truncated_read_refuses_present_zero` |
| — | done-set truncation → refuse to partition (§3f) | `announce.read_announced_task_ids` | ✅ COVERED | `..::test_announced_ids_no_ack_refuses_to_partition` |
| — | pre-reduce ack-gate (§3g) | `aggregate.runner.verify_per_task_outputs` | ✅ COVERED | `..::test_verify_per_task_outputs_rc0_no_ack_raises` |
| — | garbled `ssh -V` → no demotion | `ssh_options._local_openssh_major` | ✅ COVERED | `test_transfer_pipe::test_garbled_version_probe_does_not_demote` |
| — | hard mid-op sever propagates (F3) | `ssh_status_report` | ✅ COVERED | `test_channel_refusal::test_status_report_channel_sever_propagates_not_swallowed` |
| 21 | kill/hang raw TUI ssh pager | `tui._open_log` | ➖ **NO LONGER A GAP** — revised post-audit (Popen + kill-on-unwind + disclose); a *justified exemption* with an existing contract test (`tests/contracts/test_src_subprocess_timeout_discipline.py`) | — |

**18 tests pass, 1 xfails.** Two AUDIT-ranked *latent bugs* were found already
FIXED during/after the audit and are pinned as GREEN drills here:

- **rank-2** reconcile settle-arm (terminal-with-no-harvest): closed by
  `reconcile._harvest_if_owed`'s journal-evidence backstop (`harvest_receipt_exists`
  re-fires exactly once) — cites "audit U8 / rank 2" in-code.
- **rank-7** the bare `tui._open_log` pager: rewritten to spawn via `Popen`, kill
  on any abnormal unwind, and disclose failures — now a documented exemption.

The one still-open ranked gap is **rank-3** (stage-swap torn live tree), carried
as the strict-xfail below.

---

## 3. The xfail (a FINDING — confirmed open gap)

`test_stage_swap_atomicity::test_stage_swap_has_no_torn_live_tree_window`
— **`xfail(strict=True)`**.

`transport._stage_swap_cmd` returns
`cp -a <stage>/. <live>/ && rm -rf <stage>` — a purely additive **merge** into
the live root, not an atomic rename. A drop mid-`cp -a` leaves a partially-merged
live tree a concurrent array could import (AUDIT rank-3; every other transfer
step is atomic temp+rename or staged-and-swapped). The code uses `cp -a`
deliberately (`mv` cannot move a dir onto an existing non-empty one; the
pre-clean preserves protected paths), so closing it is real design work =
**step-2 U4** (atomic-rename discipline or a marker-guarded two-phase commit).

`strict=True` is the mechanism: when U4 lands and the swap becomes atomic, this
drill XPASSes → the strict marker turns that into a hard failure → the signal to
**un-xfail** the drill. An xfail flipping to xpass is the fix landing.

---

## 4. Needed seams — points NOT testable today (for step-2 to add)

These AUDIT §7 rows need a product-side test seam that does not exist. **None were
added** (this step is new-tests-only). Each is a candidate seam for the step-2
unit that owns the site.

| AUDIT §7 row | Seam missing | What a test needs |
|---|---|---|
| 1–3 engine sever/connect/loop-hang | `ssh_engine._do_run` / `._open` / `._submit` | an injectable warm-conn + event-loop harness: patch points to sever a held `conn.run`, fail a connect, and wedge the loop so `future.result` times out. `hang_at` is ready for the loop-wedge once the seam exists. Owner: U7 (engine seam preservation). |
| ~~4 qsub dispatch→job-id window~~ | ~~`_remote_base._execute_command`~~ | **DONE (U3).** Moved to §2 covered: the submit-once contract built the cluster-durable jobmap marker + the reconcile recovery ladder, and `test_submit_once` drills a dispatch→id-window sever → reconcile adopts from the marker with a re-qsub spy asserting ZERO re-dispatch (plus severed-stays-submitting, never-GC'd, double-submit-race, Δ4 phantom-id-refused). The marker-append-under-`timeout -k` remote half (row 15) stays uncovered — not locally injectable — documented `xfail(strict=False)`. |
| 8 kill ssh mid-`tar\|ssh` **push** | `_tar_ssh_push` pump | the push pump is symmetric to the pull pump already covered, but its `_attempt` is not import-isolated the same way; a small seam (or the same Popen/`run_capture_bounded` patch surface) would let the push drill mirror `test_pull_pump_sever_*`. Owner U1. |
| 11 named-pipe `getsockname` failure | `run_with_named_pipe_retry` | inject the marker into a returned `CompletedProcess.stderr` and assert the sticky-verdict flip + single rebuild-retry. Doable with `garble_at` on the wrapped callable — a fast follow, just not in this wave. |
| 14 preamble hang classifier | `ssh_circuit.is_preamble_degraded` | craft the cheap-probe-OK + preamble-timeout history the classifier keys on; needs a breaker-state fixture builder. Owner U5. |
| 15 remote self-destruct rc 124 (remote half) | `build_remote_command` `timeout -k` | the *local* bounded-runner deadline is covered; the *remote* `timeout -k` firing (rc 124 classified transient) needs a real or faked remote. |
| 16 NAT idle-drop / keepalives | keepalive opts / engine keepalive | not locally injectable (a real idle socket); daemon-rung (step 4) territory. |
| 18 torn `cluster_reduce` user-`aggregate_cmd` write | `aggregate/cluster_reduce.py:307` | inject a non-atomic user output torn on the cluster; assert never pulled (rc-gate) + force re-run overwrites. Needs a remote-write fault seam. Owner U2. |
| 19 re-run `watcher_install` (duplicate watchers) | `watcher_install.py:394` | a qsub-submission spy to prove a second install submits a second self-resubmitting job (the non-idempotent duplicate). Owner: rank-8. |
| 20 sever mid scheduler-detection dial chain | `scheduler_resolve.probe_cluster` | inject a sever at dial k of the 6–12 chain and assert the partial "absent" verdict is surfaced, not silently trusted. Owner U9 (consolidate to 1–2 dials — the drill then guards the consolidation). |

---

## 5. How step-2 units extend this suite

1. **Reuse the vocabulary.** New drills should use `sever_at` / `hang_at` /
   `garble_at` / `fake_clock` and assert DOCTRINE outcomes only. Add a row to the
   §2 coverage table and cite the AUDIT §7 row in the test docstring.
2. **When a unit adds a product seam** (U3's submit-leg id-discovery injection
   point, U1's push-pump isolation, the engine harness), move its row from §4
   (needed seams) to §2 (covered) and land the drill in the same PR — the seam and
   its fault drill are one change.
3. **When a unit closes an open gap**, the corresponding strict-xfail flips to
   xpass. That is the acceptance signal: **remove the `xfail` marker** in the same
   PR (rank-3 → U4 is the live example — un-xfail
   `test_stage_swap_has_no_torn_live_tree_window` when the swap becomes atomic).
4. **Keep drills fast and hermetic.** No real ssh, no real long deadlines: mock at
   the seam, drive time with `FakeClock`, and use a real sleeper subprocess only
   for the kernel-enforced `run_capture_bounded` deadline. The suite runs in
   ~12 s under xdist today; keep it there.
5. **New refusal readers inherit for free.** Any new ack-gated reader added by U2
   (the sentinel-ack primitive spine) should get a two-line `garble_at` drill
   (rc-0-no-ack → raise/UNKNOWN) next to the existing `test_channel_refusal` ones —
   that is the primitive's correctness contract.
