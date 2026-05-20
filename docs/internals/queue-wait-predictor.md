# Queue-wait predictor

The queue-wait predictor surfaces an estimated time-to-start for a
hypothetical submit. It backs the `predict-queue-wait` primitive and
the `eta_sec_via_des` field on `score-submit-plan` reports.

## Backends

Two layered backends, selected by the `backend` parameter (default
`auto`):

* **`diurnal_ma`** — the v1 baseline. Reads the runtime-prior pool for
  `(profile, cluster)`, buckets observed `queue_wait_sec` samples by
  hour-of-week (168 buckets), and returns the bucket's
  exponentially-weighted mean. Cold-start fallback when the DES inputs
  aren't available. Lives in
  `hpc_agent.forecast.queue_wait_baseline._predict_diurnal_ma`.
* **`des`** — Phase 4 discrete-event simulator. Loads the most recent
  persisted `ClusterSnapshot`, samples future arrivals per-user
  (non-homogeneous Poisson over `submit_hour_of_week_distribution`),
  samples residual lifetimes per-user (Triangular over the
  actual-over-ask ratio), and runs FIFO + EASY-backfill forward over
  a 7-day horizon. Returns the candidate's wait p10/p50/p90. Lives in
  `hpc_agent.forecast.queue_simulator` +
  `hpc_agent.forecast.queue_simulator_inputs`.

## Auto-fallback rule

`backend='auto'` chooses DES when **both** prerequisites hold:

1. At least one persisted snapshot under
   `<exp>/.hpc/cluster_history/<cluster>/<unix_ts>.json`.
2. User profiles cover at least 80% of currently-running jobs' users
   (read from `<exp>/.hpc/user_profiles/<cluster>.json`).

Otherwise it falls back to `diurnal_ma`. The decision logic is in
`hpc_agent.forecast.queue_wait_baseline._des_eligible`.

## Replay-mode validation

`scripts/validate_des_predictor.py` walks the runtime-prior pool, finds
the cluster_history snapshot just before each observed submit, and
runs DES forward to compare prediction to observation. Outputs
n_samples, MAE, MAPE, and the residual quantile ladder.

## Deferred

The following enhancements are intentionally out of scope for Phase 4
and tracked as follow-ups. Each lands when calibration data shows it
matters.

* **MULTIFACTOR priority.** The DES uses FIFO. SLURM clusters often
  run MULTIFACTOR (job age × QoS × fair-share × partition weight).
  When the residual-loop validation shows systematic favoritism for a
  subset of users, layer MULTIFACTOR in.
* **Weighted GPU-type matching.** The DES strict-matches `gpu_type`.
  Real schedulers handle "any GPU" requests and prefer the GPU with
  the most available capacity. Add when we observe DES predicting
  "queued forever" for jobs that actually started promptly because
  the user accepted any GPU.
* **Per-job dependency chains.** SLURM `--dependency=afterok:...`
  chains aren't surfaced in `ClusterSnapshot` today and the DES
  doesn't model them. Add a `co_tenants[i].dependency` field upstream
  and a deps-edge in `extract_running_jobs`.
* **Time-of-day-dependent walltime ratios.** The per-user
  `actual_over_ask` ratio is a single Triangular today. Some users
  overshoot more on Mondays / before deadlines. Add a 168-bucket
  ratio when residual analysis shows day-of-week structure.
* **Calibration loop.** A follow-up that tunes the simulator's noise
  distributions (the Triangular residual sampler, the Poisson rate
  multipliers) against the observed residual produced by
  `validate_des_predictor.py`. The data collection lives now; the
  tuning loop is deferred.
* **Slurm-simulator integration.** BSC's full SLURM fork as an
  alternative high-fidelity backend, if the thin DES proves
  insufficient. ~50K LOC of C with Munge/MariaDB transitive deps —
  worth the integration cost only after the thin DES has been shown
  inadequate on real residuals.
