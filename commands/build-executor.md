Scan the experiment repo's source directory and generate a self-contained executor Python file for HPC jobs.

## Setup

Read `hpc.yaml` in the current working directory. Identify the source directory by checking (in order):
1. `src/` directory at repo root
2. Directory referenced in existing profile `run` commands
3. Ask the user

If `$ARGUMENTS` specifies a profile name, use it. Otherwise list profiles from `hpc.yaml` and ask which one to build an executor for.

## Arguments

$ARGUMENTS formats:

1. **For existing profile**: `<profile_name>` -- generate executor for this profile
2. **From template**: `<profile_name> --from <existing_executor>` -- copy and modify an existing executor (e.g., `ml_elasticnet --from ml_ridge`)
3. **With description**: `<profile_name> <model_description>` -- generate from scratch (e.g., `ml_elasticnet "ElasticNet with L1+L2 regularization"`)

## Step 1: Scan Source Directory

Read every `.py` file in the source directory. Classify each file:

| Category | Detection | Examples |
|----------|-----------|---------|
| **Shared utility** | No `if __name__` block, no argparse, only function/class defs | `loading.py`, `transforms.py`, `evaluation.py` |
| **Executor** | Has argparse + `if __name__ == "__main__"` + imports from shared utilities | `ml_ridge.py`, `dl_patchts.py` |

For shared utilities, catalog public functions/classes with their signatures.

For executors, extract:
- Imports (stdlib, third-party, and from `src/`)
- CLI arguments (from argparse definitions)
- Pipeline steps (the numbered comments in `main()`)
- Model type: ML (sklearn/xgboost/lightgbm) vs DL (torch)
- Whether it uses RollingRobustScaler (linear models) or not (tree models)
- Feature engineering approach (HAR lags, PCA, calendar features, univariate)

Present the inventory:

```
Source directory: src/

Shared utilities:
  loading.py      — load_raw_data(data_path) -> DataFrame
  transforms.py   — robust_transform(df, col, is_target) -> (array, baseline)
  evaluation.py   — calculate_metrics(pred, true), apply_duan_smearing(...)

Executors:
  ml_ridge.py     — Ridge, HAR features, RollingRobustScaler, refit=1
  ml_xgboost.py   — XGBRegressor, HAR+calendar, no scaling, refit=5
  dl_patchts.py   — PatchTST, univariate, multi-GPU, torch.mp
  ...
```

## Step 2: Determine Executor Type

If `--from` was provided, use that executor as the template — the new executor inherits its type.

Otherwise, classify from the profile's resources:

| Signal | Type | Base template |
|--------|------|---------------|
| Profile has `gpus` in resources | DL (GPU) | Closest existing DL executor |
| Profile has `env_group: dl` | DL (GPU) | Closest existing DL executor |
| No GPU resources | ML (CPU) | Closest existing ML executor |
| User mentions torch/transformer/LSTM/CNN | DL (GPU) | Auto-detect |
| User mentions sklearn/ridge/xgboost/tree | ML (CPU) | Auto-detect |

For ML executors, further determine:
- **Linear model** (Ridge, ElasticNet, Lasso) → needs RollingRobustScaler, refit_frequency=1
- **Tree model** (XGBoost, LightGBM) → no scaling needed, refit_frequency=5
- **Baseline** → no model fitting

## Step 3: Select Building Blocks

Determine which blocks the new executor needs. Every executor uses these:

| Block | Source | Always/Conditional |
|-------|--------|--------------------|
| Data loading | `from src.loading import load_raw_data` | Always |
| Robust transform | `from src.transforms import robust_transform` | Always |
| Horizon shift | `apply_horizon_shift()` | Always (inline) |
| Chunk split | Standard chunk logic | Always (inline) |
| Duan smearing | `apply_duan_smearing()` | Always (inline) |
| Result serialization | DataFrame + `to_csv` | Always (inline) |

Conditional blocks:

| Block | When needed | Source |
|-------|-------------|--------|
| HAR lag features | Linear/tree ML models | `generate_har_features()` inline |
| Calendar features | Tree models (XGB, LGBM) | `add_calendar_features()` inline |
| PCA lag features | PCR model | `generate_raw_lag_features()` inline |
| RollingRobustScaler + numba kernels | Linear models (Ridge, ElasticNet, PCR) | Inline classes |
| RollingBuffer | ML models with scaling | Inline class |
| Walk-forward backtest | All ML models | `run_backtest()` inline |
| GPU worker + torch.mp | DL models | Inline functions |
| Model class definition | DL models | Inline class |

Copy the needed blocks from the template executor (the `--from` source or the closest match).

## Step 4: Build CLI Arguments

**Standard HPC args** (always included):
```python
parser.add_argument("--data-path", default="all30min")
parser.add_argument("--horizon", type=int, default=1)
parser.add_argument("--chunk-id", type=int, default=0)
parser.add_argument("--total-chunks", type=int, default=1)
parser.add_argument("--output-file", required=True)
```

**ML-specific** (add for CPU executors):
```python
parser.add_argument("--train-window", type=int, default=500, help="training window in days")
```

**DL-specific** (add for GPU executors):
```python
parser.add_argument("--gpu-count", type=int, default=1)
parser.add_argument("--epochs", type=int, default=None)
parser.add_argument("--batch-size", type=int, default=None)
parser.add_argument("--learning-rate", type=float, default=None)
```

**Grid-derived args**: If the profile has a `grid` section, each grid key becomes a CLI argument with the appropriate type.

**Model-specific args**: Add any hyperparameters specific to the model (e.g., `--alpha` for Ridge, `--n-estimators` for tree models).

## Step 5: Generate Executor File

Write to `src/<profile_name>.py`. Follow this exact structure:

```python
"""<Model name> <type> backtest executor.

Self-contained <description>. No imports from core/ or projects/.
"""

import argparse
import os
# ... other imports ...

from src.loading import load_raw_data
from src.transforms import robust_transform

# ── Constants ─────────────────────────────────────────────────────────────
PERIODS_PER_DAY = 48

# ── Feature engineering ───────────────────────────────────────────────────
# ... (HAR lags, PCA, calendar features as needed) ...

# ── Horizon shift ─────────────────────────────────────────────────────────
def apply_horizon_shift(...): ...

# ── [Scaling classes if linear model] ─────────────────────────────────────
# ... (RollingRobustScaler, RollingBuffer, numba kernels) ...

# ── [Model definition if DL] ─────────────────────────────────────────────
# ... (torch.nn.Module subclass) ...

# ── Duan smearing ─────────────────────────────────────────────────────────
def apply_duan_smearing(...): ...

# ── Walk-forward backtest ─────────────────────────────────────────────────
def run_backtest(...): ...
# OR for DL:
# ── GPU backtest ──────────────────────────────────────────────────────────
def _gpu_worker(...): ...

# ── CLI ───────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="...")
    # ... args ...
    args = parser.parse_args()

    # 1. Load data
    df = load_raw_data(args.data_path)

    # 2. Robust transform on RV
    adj_rv, baseline = robust_transform(df, "RV", is_target=True)
    df["adj_RV"] = adj_rv
    df["baseline"] = baseline

    # 3. Feature engineering
    # ... (model-specific) ...

    # 4. Drop initial NaN rows
    # ... (depends on feature lag depth) ...

    # 5. Extract numpy arrays
    X = df[feature_names].values.astype(np.float64)
    y = df["adj_RV"].values.astype(np.float64)
    dates = df["t"]
    baselines = df["baseline"].values.astype(np.float64)

    # 6. Horizon shift
    X, y, dates, baselines = apply_horizon_shift(X, y, dates, baselines, args.horizon)

    # 7. Chunk split
    n = len(X)
    chunk_size = n // args.total_chunks
    start = args.chunk_id * chunk_size
    end = n if args.chunk_id == args.total_chunks - 1 else start + chunk_size
    # ... slice X, y, dates, baselines ...

    # 8. Run model
    # ... (walk-forward backtest or GPU backtest) ...

    # 9. Duan smearing + save
    pred_raw, true_raw = apply_duan_smearing(preds, y_oos, baselines_oos)
    results = pd.DataFrame({
        "date": dates_oos,
        "horizon": args.horizon,
        "true_adj": y_oos,
        "pred_adj": preds,
        "true_raw": true_raw,
        "pred_raw": pred_raw,
    })
    os.makedirs(os.path.dirname(args.output_file) or ".", exist_ok=True)
    results.to_csv(args.output_file, index=False)
    print(f"Saved {len(results)} rows -> {args.output_file}")


if __name__ == "__main__":
    main()
```

**Critical rules:**
- Results DataFrame columns must be exactly: `date, horizon, true_adj, pred_adj, true_raw, pred_raw`
- Use `# ── Section Name ──...` comment headers with box-drawing characters
- First docstring line: model name. Third line: "No imports from core/ or projects/."
- File name matches profile name: profile `ml_elasticnet` → `src/ml_elasticnet.py`

## Step 6: Update hpc.yaml

Set the profile's `run` field:
```yaml
run: "python3 src/<profile_name>.py"
```

If the profile doesn't exist yet, add it using the nearest existing profile as a template. Set appropriate resources based on executor type (ML defaults: 1 cpu, 16G, 4h; DL defaults: 4 cpus, 16G, 6h, 2 GPUs).

Ensure `chunking` is configured:
```yaml
chunking:
  total: 100       # ML default
  chunk_arg: "--chunk-id"
  total_arg: "--total-chunks"
```
(DL default: total 10)

## Step 7: Validate

1. Run `python src/<profile_name>.py --help` to verify the CLI parses
2. Check that `from src.loading import load_raw_data` resolves (run from repo root)
3. Verify the output format matches `results.pattern` in the profile
4. If validation fails, fix and re-validate

## Common Patterns Reference

Existing executors and their characteristics — use this to pick the closest template:

| Executor | Model | Features | Scaling | Refit | GPU |
|----------|-------|----------|---------|-------|-----|
| `ml_ridge` | `Ridge(alpha=1.0)` | HAR lags | RollingRobustScaler (numba) | 1 | No |
| `ml_xgboost` | `XGBRegressor` | HAR + calendar | None (tree) | 5 | No |
| `ml_lightgbm` | `LGBMRegressor` | HAR + calendar | None (tree) | 5 | No |
| `ml_pcr` | PCA + Ridge | Log-spaced lags | RollingRobustScaler (numba) | 1 | No |
| `ml_baseline` | Naive (lag 125) | HAR lags | None | N/A | No |
| `dl_patchts` | PatchTST (HuggingFace) | Univariate adj_RV | Instance norm | Every window | Yes (multi-GPU, torch.mp) |
| `dl_ae_ridge` | Autoencoder + Ridge | Multi-lag features | Instance norm | Every window | Yes (multi-GPU, torch.mp) |

## Edge Cases

| Situation | Handling |
|-----------|----------|
| `src/` doesn't exist | Check for alternatives, ask user for source directory |
| No `hpc.yaml` | Error: "Run from a directory with hpc.yaml" |
| Profile `run` already points to existing file | Warn and ask whether to overwrite or create alongside |
| Model not in any existing executor | Ask for model import path and hyperparameters, use closest template |
| `--from` executor not found | List available executors, ask user to pick |
