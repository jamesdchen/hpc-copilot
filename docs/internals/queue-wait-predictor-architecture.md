# Queue-wait forecast — architecture

## TL;DR

```
┌────────────────────────────────────────────────────────┐
│  predict_start_time(squeue, history, your_job_spec)    │
│                                                        │
│  ┌──────────────┐   ┌─────────────┐   ┌─────────────┐  │
│  │ FIFO drain   │   │ Backfill    │   │ LightGBM    │  │
│  │ simulator    │   │ drain       │   │ residual    │  │
│  │ (earliest    │   │ (even-      │   │ regression  │  │
│  │  start floor)│   │  earlier    │   │ (how much   │  │
│  │              │   │  floor)     │   │  later?)    │  │
│  └──────┬───────┘   └──────┬──────┘   └──────┬──────┘  │
│         │                  │                  │        │
│         └──────────┬───────┴──────────────────┘        │
│                    ▼                                   │
│              StartForecast                             │
│              (floors + predicted_iso + features)       │
└────────────────────────────────────────────────────────┘
```

**Floor + residual learning.** Two simulators produce
earliest-start floors. A LightGBM regression learns how much LATER
reality usually is (future arrivals, fair-share decay, scheduler
config drift). The combined prediction is `pessimistic_floor +
overhead_sec` — the FIFO-drain start time plus the empirical
extra-wait the regression expects. Both floors are also passed as
features so the regression can weight them empirically.

## Why two simulators

Both simulators compute an **earliest possible start time** under
different assumptions. Neither is a worst-case bound on wait — the
LGBM residual is the term that pushes the prediction *past* the
floor when reality is slower than the simulator's assumptions.

| Simulator | Mode | Captures |
|---|---|---|
| FIFO drain | `enable_backfill=False` | Earliest start assuming no future arrivals and no SLURM backfill scheduler. Conservative (slower) of the two floors, so we call it the "pessimistic floor" — but it's still an *earliest* start, not an upper bound on wait. |
| Backfill drain | `enable_backfill=True` (phantom-slot) | Even earlier start: any short pending job that fits in any running-job shadow runs immediately. Optimistic about parallelism and backfill aggressiveness. |

Both feed into the LGBM as features:

* `pessimistic_floor_sec` — strong predictor in busy windows
  (regression learns "expected wait is the FIFO drain time plus
  some learned overhead").
* `optimistic_floor_sec` — strong predictor in idle windows
  (regression learns "when backfill exists, predicted start drops
  toward the optimistic floor").
* `floor_gap_sec` — measure of cluster slack; the difference
  between the two floors. Wide gap → the regression can place the
  prediction anywhere in between based on other features.

## Data pipeline

```
┌───────────────┐                ┌─────────────────┐
│ scripts/      │  every 5 min   │ <experiment>/   │
│ snapshot_     │ ────cron────►  │ .hpc/squeue_    │
│ squeue.py     │                │ snapshots/      │
│               │                │ <YYYYMMDDTHHMMSS│
│  ssh_run(     │                │ .tsv.gz>        │
│    'squeue    │                └─────────────────┘
│     --user=*  │                        │
│     -O ...')  │                        │
└───────────────┘                        ▼
                                ┌─────────────────┐
                                │ scripts/train_  │
                                │ wait_           │
                                │ predictor.py    │
                                │                 │
                                │ • walks         │
                                │   sacct history │
                                │ • for each job, │
                                │   finds nearest │
                                │   snapshot      │
                                │ • runs 2 sims   │
                                │ • extracts      │
                                │   features      │
                                │ • fits LightGBM │
                                │ • writes        │
                                │   model.txt     │
                                └─────────────────┘
                                        │
                                        ▼
                                ┌─────────────────┐
                                │ <experiment>/   │
                                │ .hpc/wait_      │
                                │ predictor/      │
                                │ ├ model.txt     │
                                │ ├ training_     │
                                │ │ summary.json  │
                                │ └ training_     │
                                │   history.jsonl │
                                └─────────────────┘
                                        │
                                        ▼
                              loaded at inference time
```

## Feature set (29 features, 3 tiers)

### Tier S — required, ~80% of variance

* `hour_of_week` (0-167) — diurnal × day-of-week
* `queue_depth_pending` / `queue_depth_running`
* `pending_at_or_above_priority` — relative rank
* `your_priority` / `your_priority_percentile` (1.0 = best)
* `mean_priority_of_pendings_ahead`
* `competitor_count_external_account` / `_fs_top` / `_fs_high` /
  `_fs_mid` / `_fs_low` — bucketed by fairshare quintile

### Tier A — high value

* `is_weekend` / `is_business_hours_utc`
* `gpu_pool_count` — number of GPU pools matched by Features=
* `constraint_specified`
* `median_running_time_left_sec` / `max_running_time_left_sec`
* `recent_arrival_rate_per_hour` (from saved snapshots)
* `your_fairshare_value` — caller's own fairshare
* `partition_load_pct` — running ÷ capacity
* `pessimistic_floor_sec` / `optimistic_floor_sec` /
  `floor_gap_sec` — simulator outputs as features

### Tier C — academic clusters

* `min_days_to_deadline` — to nearest upcoming venue deadline
* `is_within_deadline_week` / `is_within_deadline_month`
* `deadline_density_30d` — count of deadlines within 30d (captures
  ML-conference clustering: ICML+ICLR+AAAI+CVPR all in Jan-Feb)

Project overrides via `.hpc/deadlines.yaml`.

## Quantile predictions (uncertainty)

The trainer fits three separate LightGBM models with
`objective=quantile, alpha={0.1, 0.5, 0.9}` rather than a single
regression. The predictor returns:

* `predicted_iso_p10` — optimistic 10th percentile
* `predicted_iso_p50` — median
* `predicted_iso_p90` — pessimistic 90th percentile

Lets the agent surface "expected wait 4h, worst-case 12h" rather
than a single point estimate.

## Training quality metrics

### MAE vs naive baselines

Reported in `training_summary.json`:

* `val_mae_sec` — model's MAE on holdout
* `baseline_mae_floor_sec` — MAE if we predicted the pessimistic
  floor for every row (no learning)
* `baseline_mae_recent_mean_sec` — MAE if we predicted the recent
  empirical mean overhead

The model is only useful if `val_mae_sec` < both baselines. If not,
either the features aren't informative or there isn't enough data
to fit a non-trivial model.

### Bracket invariant

`bracket_pct` — fraction of validation predictions that land
between `optimistic_floor_sec` and a generous upper bound
(`max(pessimistic_floor * 5, pessimistic_floor + 86400)`). A
healthy model lands >90% inside; <80% suggests features or labels
have a problem.

### Feature importance (gain)

Sorted `(feature_name, gain)` list. When 1-2 features dominate
(>50% of total gain), the rest can probably be pruned without
hurting accuracy.

### Drift detection

`forecast/drift_detector.py` compares the most recent run's
`val_mae_sec` against the median of prior runs (default last 5).
Flags `mae_regression` when recent > 1.5× median; `mae_improvement`
when recent < 0.66× median (worth investigating — could indicate
label leakage).

## Why this architecture

* **Sim does what sim is good at.** Earliest-start floors,
  structural knowledge about running jobs' end times.
* **Regression does what regression is good at.** Learning patterns
  the model can't predict deterministically (future arrivals,
  fair-share recompute, scheduler config drift).
* **Both** feed each other: floors are features the regression uses;
  the regression's predictions live on top of the floors.

The alternative ("simulate everything") would require modeling
SLURM's backfill scheduler config, fair-share decay, every
reservation event — and feeding that simulator a guess about future
arrivals, which is exactly what the regression learns from data.

The deliberate non-goal: do not try to be SLURM. Predict what
SLURM does empirically.

## Cold-start behavior

| State | Predictor returns |
|---|---|
| No `model.txt` (never trained) | `predicted_iso = floor_pessimistic_iso`, `method="floor_only"` |
| Model exists but a feature is missing at inference | `predicted_iso = floor_pessimistic_iso`, `method="floor_only_cold_start"` |
| Model + all features | `predicted_iso = floor + overhead`, `method="floor_plus_residual"` |

Snapshotting starts immediately; after ~7-14 days of snapshots +
sacct history the trainer has enough data for a first useful model.
Until then the floor-only predictions are still actionable.
