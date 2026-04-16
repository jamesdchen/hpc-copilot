"""<Model name> <type> backtest executor.

Self-contained <description>. No imports from core/ or projects/.
"""

import argparse
import os

import numpy as np
import pandas as pd

from src.loading import load_raw_data
from src.transforms import robust_transform

# ── Constants ─────────────────────────────────────────────────────────────
PERIODS_PER_DAY = 48

# ── Feature engineering ───────────────────────────────────────────────────
# --- EDIT: choose feature engineering approach ---
# HAR lags:     generate_har_features(df, target_col="adj_RV")
# PCA lags:     generate_raw_lag_features(df, target_col="adj_RV")
# Calendar:     add_calendar_features(df)
# Univariate:   no feature engineering (DL models)


# ── Horizon shift ─────────────────────────────────────────────────────────
# def apply_horizon_shift(...): ...
# --- EDIT: copy from src/transforms.py or import ---


# ── [Scaling classes if linear model] ─────────────────────────────────────
# --- EDIT: include RollingRobustScaler + RollingBuffer + numba kernels
#     for linear models (Ridge, ElasticNet, PCR).
#     Omit for tree models (XGBoost, LightGBM). ---


# ── [Model definition if DL] ─────────────────────────────────────────────
# --- EDIT: torch.nn.Module subclass for DL models ---


# ── Duan smearing ─────────────────────────────────────────────────────────
# def apply_duan_smearing(...): ...
# --- EDIT: copy from src/evaluation.py or import ---


# ── Walk-forward backtest ─────────────────────────────────────────────────
# def run_backtest(...): ...
# --- EDIT: copy from src/scaling.py or import ---
# OR for DL:
# ── GPU backtest ──────────────────────────────────────────────────────────
# def _gpu_worker(...): ...


# ── CLI ───────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="...")

    # Standard HPC args (always included)
    parser.add_argument("--data-path", default="all30min")
    parser.add_argument("--horizon", type=int, default=1)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=-1)
    parser.add_argument("--output-file", required=True)

    # ML-specific
    parser.add_argument("--train-window", type=int, default=500, help="training window in days")
    parser.add_argument("--params-file", default=None, help="JSON file with tuned hyperparams")

    # DL-specific (uncomment if GPU executor)
    # parser.add_argument("--gpu-count", type=int, default=1)
    # parser.add_argument("--epochs", type=int, default=None)
    # parser.add_argument("--batch-size", type=int, default=None)
    # parser.add_argument("--learning-rate", type=float, default=None)

    # --- EDIT: add model-specific args ---

    args = parser.parse_args()

    # 1. Load data
    df = load_raw_data(args.data_path, allow_missing=True)

    # 2. Robust transform on RV
    adj_rv, baseline = robust_transform(df, "RV", is_target=True, use_diurnal=True, winsor_window=240)
    df["adj_RV"] = adj_rv
    df["baseline"] = baseline

    # 3. Feature engineering
    # --- EDIT: model-specific ---
    # df, har_names = generate_har_features(df, target_col="adj_RV")
    # cal_names = add_calendar_features(df)
    # feature_names = har_names + cal_names

    # 4. Drop initial NaN rows
    # max_lag = resolve_har_lags()[-1]
    # df = df.iloc[max_lag:].reset_index(drop=True)

    # 5. Extract numpy arrays
    # X = df[feature_names].values.astype(np.float64)
    # y = df["adj_RV"].values.astype(np.float64)
    # dates = df["t"]
    # baselines = df["baseline"].values.astype(np.float64)

    # 6. Horizon shift
    # X, y, dates, baselines = apply_horizon_shift(X, y, dates, baselines, args.horizon)

    # 7. Data slice
    # start = args.start
    # end = len(X) if args.end == -1 else args.end
    # X_chunk, y_chunk = X[start:end], y[start:end]
    # dates_chunk = dates.iloc[start:end].reset_index(drop=True)
    # baselines_chunk = baselines[start:end]

    # 8. Run model
    # --- EDIT: walk-forward backtest or GPU backtest ---

    # 9. Duan smearing + save
    # pred_raw, true_raw = apply_duan_smearing(preds, y_oos, baselines_oos)
    # results = pd.DataFrame({
    #     "date": dates_oos,
    #     "horizon": args.horizon,
    #     "true_adj": y_oos,
    #     "pred_adj": preds,
    #     "true_raw": true_raw,
    #     "pred_raw": pred_raw,
    # })
    # os.makedirs(os.path.dirname(args.output_file) or ".", exist_ok=True)
    # results.to_csv(args.output_file, index=False)
    # print(f"Saved {len(results)} rows -> {args.output_file}")
    pass


if __name__ == "__main__":
    main()
