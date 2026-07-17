# Stage-swap torn-window — seam map + design options (unit U4 stop-report)

2026-07-17 · investigation unit on AUDIT.md offender #3 (`_tar_ssh_push`
stage-swap). The unit was scoped "push path only"; the investigation
concluded STOP-AND-REPORT — the consumers are cross-plane and the correct
fix is a small design unit, not an in-file mechanical drop. This page is
the durable record of that investigation (source: the 2026-07-17 agent
report; verified against `transport/__init__.py` at the time of writing).

## Today's exact sequence (the `delete=True` full-copy fallback only)

Fires only when `rsync` is absent AND the remote content-hash manifest is
unavailable (first deploy, or `_DELTA_ENV_KILL=1`). The warm re-push is
the delta path, which never swaps. Four separate bounded ssh legs
(`_tar_fallback...stages_then_swaps` pins them):

| Leg | Remote command | Timeout |
|---|---|---|
| 1 stage-drop | `rm -rf <r>.hpc_stage` | 300s |
| 2 extract | `mkdir -p <r>.hpc_stage && tar x -C <r>.hpc_stage` | 1800s |
| 3 preclean | delete all unprotected live files (find/xargs rm, prune-guarded) | 300s |
| 4 swap | `cp -a <r>.hpc_stage/. <r>/ && rm -rf <r>.hpc_stage` | 300s |

**The torn window is legs 3+4**, and it is worse than one non-atomic
`cp`: leg 3 guts the live tree, leg 4 re-creates it per-file
(open/truncate/write — the #F20 hazard `_rsync_deploy` avoids via
temp+rename), and the two legs are DISTINCT ssh invocations with a
client-side gap between them.

## Consumers of the live tree (why one seam does not exist)

| Consumer | Plane | Torn-tree behavior today |
|---|---|---|
| compute-node array tasks + canary (`execution/mapreduce/dispatch.py`) | cluster-side, NO control-plane seam | blindly imports torn code → wrong science; the genuine hazard |
| `preflight_executor_exists` (`_remote_base.py:154`) | command | `test -f` existence-only — a half-written file passes |
| `detect_entry_point`, `inspect_deployment` | command | reads torn tree as valid |
| next push's delta manifest (`_delta.py`) | transfer | already self-heals (hashes actual bytes) — no bug |

No marker/sentinel exists anywhere in src (grep verified).

## Why neither in-scope shape was landed

- **(a) atomic rename swap** is blocked by contract: the swap must MERGE
  (preclean deliberately preserves `results/`, `_combiner/`, `logs/`,
  `.hpc/templates/` — staging holds only fresh code). A top-level rename
  pair cannot reconstruct protected content without staging the whole
  live tree (multi-GB `results/`), which the `_stage_swap_cmd` docstring
  explicitly rejects.
- **(b) marker protocol** requires cross-plane edits outside the unit:
  a `.hpc/.swap_in_progress` marker is NOT in `PROTECTED_RUNTIME_FILES`
  (`_excludes.py:111`), so the preclean would delete it and
  `_prune_manifest_known_extras` would prune it; making it survive means
  touching `_excludes.py`, `_prune.py`, and the `_delta.py` remote
  snippet — and the reader checks belong at each consumer seam
  (critically `preflight_executor_exists` and the `hpc_preamble.sh`
  compute-side preamble). A write-side-only marker would be zero safety
  with false comfort.

## Recommendation for the follow-up build unit

**Primary — (a′) remote-side atomic-per-file swap:** replace
`cp -a stage/. live/` with remote
`rsync -a --delete --exclude=<protected> stage/ live/` (login nodes have
rsync even when the Windows client doesn't): temp+atomic-rename per file
closes the torn-FILE hazard with no consumer changes and no marker;
residual A-before-B ordering is benign and identical to the delta path's
`tar x`. Needs a `cp -a` fallback when remote rsync is absent.

**Alternative — (b) marker-guarded two-phase commit** (+0 round-trips:
`touch` rides the preclean leg, `rm -f` rides the swap leg; marker
persists ⟺ severed mid-window): choose this if a remote-rsync dependency
is unwanted. Requires the `_excludes.py`/`_prune.py`/`_delta.py` trio
plus refuse-on-marker at `preflight_executor_exists` and the preamble
(F3: torn reads UNKNOWN, never valid).

Existing pins to extend at build time:
`test_rsync_push_fallback_delete_true_stages_then_swaps`,
`test_tar_fallback_transfer_death_leaves_live_tree_untouched`.

## Drift log

- 2026-07-17: created from the U4 investigation stop-report; no code
  changed by that unit.
- 2026-07-17: **PRIMARY (a′) BUILT.** Replaced the leg-4
  `cp -a <stage>/. <live>/` swap with the remote atomic-per-file
  `rsync -a --delete --exclude=<protected> <stage>/ <live>/`
  (`_stage_swap_rsync_cmd` in `transport/__init__.py`), selected by a
  zero-round-trip probe (`command -v rsync`) that rides the existing
  stage-drop leg (`_stage_drop_probe_cmd`, token `__HPC_REMOTE_RSYNC__`).
  **Leg-3 (pre-clean) FOLDS into the rsync path:** rsync's `--delete`
  removes every unprotected live file absent from the staged tree and its
  `--exclude` shields the protected set — byte-for-byte the pre-clean's
  "delete unprotected, preserve protected" semantics — so the atomic path
  is ONE swap leg (net one fewer ssh leg, smaller destructive window)
  instead of pre-clean + `cp -a`. The excludes are anchored to match the
  `find` pre-clean exactly (`_rsync_swap_exclude_flag`: internal-slash
  patterns root-anchored with a leading `/` mirroring `find -path
  <root>/<pat>`; bare names match-any-depth mirroring `find -name`). The
  torn-FILE hazard closes with **no consumer change and no marker** (temp +
  atomic-rename per file, the #F20 discipline). A **`cp -a` fallback stays**
  for rsync-absent login nodes (unchanged two-leg pre-clean + merge); a
  severed/truncated probe read leaves the token absent and falls back
  conservatively — never a false "rsync present". Residual A-before-B file
  ordering during the swap is accepted as benign (matches the delta path's
  `tar x`). Tests: extended `test_rsync_push_fallback_delete_true_stages_
  then_swaps` (now pins the rsync-absent 4-leg fallback + the probe riding
  the drop leg) and `test_tar_fallback_transfer_death_leaves_live_tree_
  untouched` (asserts the probe rode the drop leg, no swap of either shape
  fired); added `test_stage_swap_rsync_cmd_is_atomic_per_file_delete`
  (command shape), `test_rsync_push_fallback_delete_true_atomic_swap_when_
  remote_has_rsync` (3-leg routing), `test_rsync_push_fallback_delete_true_
  cp_a_when_remote_lacks_rsync` (4-leg fallback routing), and
  `test_stage_swap_rsync_merges_and_deletes_on_disk` (real-rsync merge
  contract, skips when rsync/POSIX-sh absent). Red-then-green verified
  (3 fail on the pre-U4 tree, all green after). NOTE: the rank-3 drill
  `tests/faultinject/test_stage_swap_atomicity.py` targets the `cp -a`
  builder `_stage_swap_cmd` directly, so it correctly STAYS `xfail` — the
  fallback retains the accepted residual window; the atomic primary is
  covered by the new pins above. Whoever owns the fault harness may want to
  re-point that drill at the rsync primary now that (a′) has landed.
