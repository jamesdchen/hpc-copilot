# Proving run #16 — runsheet (the lane-fleet live-fire, SGE-side)

**Status: READY TO RUN** (wheel `0.11.3+g1ba0223d` fingerprint-verified on all
four envs 2026-07-20, incl. Hoffman2). DOCS-ONLY plan. Base = `main`/`1ba0223d`
(23 ahead of origin — push before or after, the run keys on the wheel not the
remote). Companion: `proving-run-15-runsheet.md` (submit-once, PROVEN on Slurm
2026-07-18; its §1 deploy discipline and §2.3 evidence-table style carry over).

**Theme:** run 15 proved the submit-once contract on **discovery/Slurm only**.
Since then the entire lane fleet (A–I) + the notebook-preview wiring merged with
hermetic proof only. Run 16 live-fires them where they bite: **Hoffman2/SGE**,
plus the two run-15 gate findings that Lane I was built to close.

**Posture note:** the user has a REAL experiment to run. Run 16 is designed to
piggyback — §2 and §3 ride any genuine submit; only §4 (drill legs) needs
dedicated throwaway runs. Do the real experiment first; harvest run-16 evidence
from its artifacts; run §4 drills opportunistically after.

## What this run first-exercises live

| Capability | Landed as | First live fire? |
|---|---|---|
| Lane C SGE fixes (qmaster bootstrap, dump/strace visibility) | `8a921b4c`+`6499ac29` | YES — run 15 never touched SGE |
| SGE submit-once leg (rung-1b token via `qstat -j` `-ac HPC_TOKEN=`) | run-15 §4 watch-list, untested | YES — the marker-only vs token-read disambiguation on SGE |
| Lane A/B1/B3 graduated breaker (cooldown ladder, demand probe, self-storm attribution) | `72f6cabe` + B-wave | YES under real load (run 15 saw only the OLD breaker open) |
| Lane B2 cross-process establishment pacing | B-wave | YES |
| Lane D remote-throttle rc==255 gate | `bef19c29` | YES |
| Lane E reconcile strict-all-complete settle | `6baf07a5` | YES — watch the pi-drill-59cac0a3 class (stuck in_flight) stay closed |
| Lane F/G/H ssh_options leg-aware gate, stray_sweep rc normalization, named-pipe retry | `21a2444a`/`3c53cc93`/`b0d35aaf` | YES (H fires only if a named-pipe master dies mid-window — observational) |
| **Lane I dev-mode cross-repo authorship** (grant journal, home-log evidence reader, revocation) | `629c1f82` | YES — this is run-15 gate-finding (1)'s fix |
| **Notebook preview sampled basis + disclosure block** (R1–R3) | `4b5867a5` | YES — first live sampled dry-run receipt greening an assertions leg as `sampled` |
| B4 streamed pull | validated live in run-15 addendum | regression-watch only |

## 0. Pre-flight (mostly DONE 2026-07-20 — verify, don't redo)

1. **Wheel:** all four envs (local CLI / demo venv / WSL / Hoffman2 hpc-pi) must
   report `hpc-agent --version` = `0.11.3+g1ba0223d` exactly. Hoffman2 needed a
   manual `psutil>=5.9` install (0.11.3 added the dep; `--no-deps` skips it) —
   already done; if any env is refreshed again, re-check psutil.
2. **Discovery/CARC:** NOT yet on the re-cut wheel (run 15 left it at
   `0.11.2+g1ac2e46a`). If run 16 touches discovery, refresh it first
   (runsheet-15 §1.3 commands; conda run works there). SGE-only run 16 can skip.
3. **Creds:** demo `clusters.yaml` (gitignored, real creds) via
   `HPC_CLUSTERS_CONFIG`; repo copy stays placeholders; creds stash@{1} untouched.
4. **Flag posture:** `HPC_SUBMIT_ONCE` per-window ON for §4 drills (the default
   flip is STILL the user's un-made call — run-15 §5 bar was met; run 16 SGE
   evidence strengthens but does not gate it). Real-experiment windows: user's
   choice; flag-ON is safe per run 15.
5. Demo session relaunch: PATH-prepend (`demo-hpc\.venv\Scripts` +
   `C:\Windows\System32\OpenSSH`), `HPC_CLUSTERS_CONFIG` set.

## 1. The real experiment IS the vehicle

Drive the user's experiment normally (`/submit-hpc` → S1..S4, or campaign).
Evidence to harvest passively from its artifacts — none of these add steps:

| Check | Where | Pass |
|---|---|---|
| env_lock + hw_facts stamp | run sidecar | `captured` ×2 (regression vs run 15) |
| Lane E settle | journal after terminal reconcile | run settles `complete` only when ALL tasks complete; no stuck `in_flight` |
| Breaker never falsely opens | ssh circuit log | zero opens under normal load; if one opens, the GRADUATED ladder (not flat 300s) shows in the log |
| Throttle classification | remote log on any retry | rc==255-gated: no content-failure misread as connect-throttle |
| One-array invariant | `qstat`/sacct vs journal | every run: exactly one array per attempt token |

## 2. Notebook-preview live leg (rides the experiment's notebook audit)

First live R1–R3 exercise, during the user's normal notebook-audit prelude:

1. Draft the module; run `notebook-dry-run` (sampled) BEFORE full execution.
2. **R1:** `notebook-audit-view` render shows the assertions leg greened with
   basis `sampled` — and tier stays `human_required` (sampled never auto-clears).
3. **R2:** the render carries the `### preview (sampled dry-run)` block between
   `### assertions` and `### lint flags` — relay VERBATIM; confirm the popup
   carries it (it is the point: as accessible as the review itself).
4. **R3:** capture `view_sha` before/after the sampled receipt lands —
   MUST be identical (presentation-only pin, live).
5. Then full execution → full receipt → basis flips to `full` → auto-clear/
   sign-off proceed normally. **Pass:** no consumer ever treated `sampled` as
   `full`; the human saw the preview block in the actual sign-off surface.
6. R4 (skill OFFERS the dry-run) is NOT live yet — the 4 SKILL.md edits are
   still in pending-approvals. If approved before the run, watch the offer fire
   after each nudge re-draft; else note "R4 pending" and move on.

## 3. Lane I live leg — the run-15 authorship findings, closed or not

Run-15 gate findings: (1) utterance capture is session-cwd-namespaced — a
delegated operator session couldn't satisfy the gate for a foreign
experiment_dir; (2) the gate accepted `n_samples 10000004` absent from the
demo-namespace log (the human HAD typed it in the dev-session namespace).
Lane I's grant + home-log evidence reader is the designed fix. Live checks:

1. **Grant bootstrap:** from the HOME (dev) session, utter the grant naming the
   demo repo's 12-hex `repo_hash` as a whole token; append the grant decision.
   Confirm the journal shows the grant with the hash, actor-scoped.
2. **Cross-repo accept:** drive a decision in the demo experiment whose
   authorship evidence lives in the HOME log. **Pass:** the gate accepts, and
   the decision record's provenance stamps `source_log: home` (or `own+home`)
   with `evidence_logs` listing both — CODE-computed, not caller-claimed.
3. **Finding-(2) re-test:** repeat the run-15 shape — a value typed only in the
   dev namespace, decision in the demo namespace, but now WITHOUT a grant.
   **Pass:** plain SpecInvalid refusal (the run-15 acceptance must NOT
   reproduce; if it does, the undocumented-matcher hole is still open → docket,
   HARD finding).
4. **Revocation:** revoke the grant, retry → own-only refusal WITH the
   degraded-to-own-only disclosure on the refusal path. Prior accepted
   decisions stay (grandfathered).

## 4. SGE drill legs (dedicated throwaway runs, Hoffman2, flag ON)

1. **Happy path on SGE** (run-15 §2.1 shape, hoffman2): mint→promote clean;
   marker + wave-0.id durable; `qstat -j <jid>` echoes `HPC_TOKEN=<run_id>#0`.
   **This answers the run-15 §4 open question: does SGE surface the `-ac`
   context?** If `qstat -j` does NOT echo it, rung-1b is blind on SGE —
   marker-only (rung-1a) is load-bearing; record which rung the adoption uses.
2. **Apex kill on SGE** (run-15 §2.2 apex construction — transport-delay
   wrapper at the `HPC_SSH_BINARY` seam, kill on token sighting; the natural
   window is sub-second, reactive kills lose): expect `submitting` orphan →
   reconcile adopts from the SGE marker → **one array by qstat/qacct, zero
   re-qsub**. Disclose the fault-injection construction as in run 15.
3. **Breaker under storm:** the kill storm should exercise the GRADUATED
   cooldown (B1/B3) — check the log for ladder steps + demand probe +
   self-storm attribution (run 15 saw the old flat 300s).
4. **Stray-sweep leg (G):** after the kill, the dead watcher's ps-probe on the
   cluster must classify rc correctly (no false stray from the rc-normalization
   class).

## 5. Evidence bar / what would flip defaults

- **SGE adoption proven** (§4.1–2) + Slurm proof from run 15 ⇒ submit-once
  contract proven on BOTH scheduler families — the strongest possible basis for
  the user's `HPC_SUBMIT_ONCE` default-flip call (one-line PR, evidence linked).
- **Lane I §3 all-pass** ⇒ run-15 gate findings (1)+(2) CLOSED on the docket;
  the authorship chain is trustworthy cross-repo. Any §3.3 acceptance = HARD
  finding, docket, do not trust the chain until read.
- **Preview §2 all-pass** ⇒ R1–R3 live-proven; R4 remains approval-gated.
- Failures anywhere: finding-first discipline — capture run_id, sidecar,
  logs, scheduler ledger verbatim; file to the docket; never patch mid-run.

## Drift log

- 2026-07-20: Created, post lane-fleet merge (A–I + preview, main `1ba0223d`,
  wheel re-cut + 4-env fingerprint-verified same day). Grounded in run-15
  drift-log evidence and its open docket: SGE never live-fired, `-ac` token
  echo unverified, authorship findings (1)/(2) open pending Lane I live proof,
  natural id-window sub-second (reactive kills lose — use the disclosed
  fault-injection construction). Discovery still on 0.11.2 — refresh before
  any Slurm leg. Piggyback posture: the user's real experiment is the vehicle
  for §1–§3; §4 is dedicated drills.
