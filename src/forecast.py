"""Brief §05 Forecast - next-hour pickups per zone: baseline vs HistGradientBoosting.

Protocol (no leakage):
  1. Features from src.features (past-only). Split by month of the target hour: Apr train, May valid, Jun test.
  2. Preprocessing (zone_id ordinal encoding) is fitted inside the pipeline on the training rows only.
  3. Each setting in config.model.param_grid: fit on April, score MAE on May. Pick the lowest May MAE
     (ties -> earlier in the grid). June is not touched in this step.
  4. Freeze the specification (written to evidence/model_spec_frozen.json BEFORE June is predicted).
  5. Refit once on April+May with the frozen setting (config.model.refit_on_train_valid).
  6. June: rolling one-step prediction. Each row's features are already-observed counts up to t, so
     no refitting and no future hours are used. Negative predictions clipped to 0, never rounded.
  7. Baseline (same hour 7 days earlier) and model are scored on the identical June rows.

Outputs: outputs/predictions.csv (zone_id, hour, y_true, y_pred, baseline), outputs/metrics.json,
         evidence/feature_availability.csv, evidence/model_spec_frozen.json, MOD-* rows in experiment_log.csv
Run    : python -m src.forecast --mode smoke|full
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime

import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OrdinalEncoder

from src import features as feat
from src.aggregate import hardware
from src.common import load_config, log, write_csv_atomic
from src.modelling_io import (data_hash, load_zone_hour, rel, replace_experiment_rows,
                              write_json_atomic, zone_names)

NUMERIC = ["hour_of_day", "weekday", "lag1", "lag24", "mean24", "lag168"]


# ---------------------------------------------------------------- metrics (hand-checked in T7)
def mae(y, p) -> float:
    return float(np.mean(np.abs(np.asarray(y, float) - np.asarray(p, float))))


def rmse(y, p) -> float:
    return float(np.sqrt(np.mean((np.asarray(y, float) - np.asarray(p, float)) ** 2)))


def clip(p) -> np.ndarray:
    """Negative counts are impossible: clip to 0. No rounding (brief §05)."""
    return np.clip(np.asarray(p, float), 0.0, None)


# ---------------------------------------------------------------- model
def make_model(setting: dict, seed: int) -> Pipeline:
    params = {k: v for k, v in setting.items() if k != "name"}
    pre = ColumnTransformer([("zone", OrdinalEncoder(), ["zone_id"]), ("num", "passthrough", NUMERIC)])
    hgb = HistGradientBoostingRegressor(**params, categorical_features=[0], early_stopping=False,
                                        random_state=seed)
    return Pipeline([("pre", pre), ("hgb", hgb)])


def fit_predict(setting: dict, seed: int, train: pd.DataFrame, score: pd.DataFrame) -> tuple[Pipeline, np.ndarray]:
    m = make_model(setting, seed).fit(train[feat.FEATURES], train["y_true"])
    return m, clip(m.predict(score[feat.FEATURES]))


# ---------------------------------------------------------------- error analysis
def is_peak(df: pd.DataFrame, cfg: dict) -> pd.Series:
    pk = cfg["model"]["peak"]
    return df["weekday"].isin(pk["weekdays"]) & df["hour_of_day"].isin(pk["hours"])


def group_mae(df: pd.DataFrame, by: str) -> pd.DataFrame:
    g = df.groupby(by)
    return pd.DataFrame({"rows": g.size(), "mean_y": g["y_true"].mean(),
                         "mae_model": g.apply(lambda x: mae(x["y_true"], x["y_pred"]), include_groups=False),
                         "mae_baseline": g.apply(lambda x: mae(x["y_true"], x["baseline"]), include_groups=False)})


def _round(d: dict, nd: int = 4) -> dict:
    return {k: (round(v, nd) if isinstance(v, float) else v) for k, v in d.items()}


def error_analysis(test: pd.DataFrame, cfg: dict) -> dict:
    out = cfg["paths"]["outputs"]
    names = zone_names(cfg).set_index("zone_id")
    pz = group_mae(test, "zone_id")
    pz["relative_mae_model"] = pz["mae_model"] / pz["mean_y"]
    pz["model_beats_baseline"] = pz["mae_model"] < pz["mae_baseline"]
    pz = pz.join(names)
    worst = int(pz["mae_model"].idxmax())
    w = test[test["zone_id"] == worst].copy()
    w["abs_err"] = (w["y_true"] - w["y_pred"]).abs()
    by_hod = group_mae(w, "hour_of_day").sort_values("mae_model", ascending=False).head(3)
    top_err = w.nlargest(5, "abs_err")[["hour", "y_true", "y_pred", "baseline", "abs_err"]]
    peak = test[is_peak(test, cfg)]
    off = test[~is_peak(test, cfg)]
    clusters_path = out / "clusters.csv"
    by_cluster = None
    if clusters_path.exists():
        cl = pd.read_csv(clusters_path)
        tc = test.merge(cl, on="zone_id", how="left")
        bc = group_mae(tc, "cluster_id")
        by_cluster = [_round({"cluster_id": int(i), **r}) for i, r in bc.to_dict("index").items()]
        pc = group_mae(tc[is_peak(tc, cfg)], "cluster_id")
        for row in by_cluster:
            if row["cluster_id"] in pc.index:
                row["peak_mae_model"] = round(float(pc.loc[row["cluster_id"], "mae_model"]), 4)
                row["peak_mae_baseline"] = round(float(pc.loc[row["cluster_id"], "mae_baseline"]), 4)
    return {
        "per_zone": [_round({"zone_id": int(z), **{k: (v.item() if hasattr(v, "item") else v) for k, v in r.items()}})
                     for z, r in pz.sort_values("mae_model", ascending=False).to_dict("index").items()],
        "zones_model_beats_baseline": int(pz["model_beats_baseline"].sum()),
        "worst_zone": {
            "zone_id": worst, "zone": names.loc[worst, "Zone"],
            "mae_model": round(float(pz.loc[worst, "mae_model"]), 4),
            "mae_baseline": round(float(pz.loc[worst, "mae_baseline"]), 4),
            "mean_y": round(float(pz.loc[worst, "mean_y"]), 4),
            "worst_hours_of_day": [_round({"hour_of_day": int(h), **r}) for h, r in by_hod.to_dict("index").items()],
            "largest_errors": [_round({**r, "hour": r["hour"].strftime("%Y-%m-%d %H:%M:%S")}) for r in top_err.to_dict("records")],
        },
        "peak": {"definition": cfg["model"]["peak"]["definition"], "rows": len(peak),
                 "mae_model": round(mae(peak["y_true"], peak["y_pred"]), 4),
                 "mae_baseline": round(mae(peak["y_true"], peak["baseline"]), 4),
                 "rmse_model": round(rmse(peak["y_true"], peak["y_pred"]), 4),
                 "rmse_baseline": round(rmse(peak["y_true"], peak["baseline"]), 4),
                 "mean_y": round(float(peak["y_true"].mean()), 4)},
        "off_peak": {"rows": len(off), "mae_model": round(mae(off["y_true"], off["y_pred"]), 4),
                     "mae_baseline": round(mae(off["y_true"], off["baseline"]), 4)},
        "by_cluster": by_cluster,
    }


# ---------------------------------------------------------------- run
def run(cfg: dict) -> dict:
    t0 = time.perf_counter()
    mc, seed = cfg["model"], cfg["seed"]
    if mc["features"] != feat.FEATURES:
        raise ValueError(f"config model.features {mc['features']} != implemented {feat.FEATURES}")
    out, ev = cfg["paths"]["outputs"], cfg["paths"]["evidence"]
    zh = load_zone_hour(cfg)
    f, info = feat.build(zh, cfg)
    write_csv_atomic(feat.feature_availability(), ev / "feature_availability.csv")
    train, valid = f[f["split"] == "train"], f[f["split"] == "valid"]
    assert set(train["zone_id"]) == set(f["zone_id"]), "zone set must come from the training month"

    # --- tune on May (June untouched)
    sel_rows = []
    for s in mc["param_grid"]:
        ts = time.perf_counter()
        _, p = fit_predict(s, seed, train, valid)
        sel_rows.append({"setting": s["name"], "params": {k: v for k, v in s.items() if k != "name"},
                         "train_rows": len(train), "valid_rows": len(valid),
                         "valid_mae": round(mae(valid["y_true"], p), 4), "valid_rmse": round(rmse(valid["y_true"], p), 4),
                         "seconds": round(time.perf_counter() - ts, 3)})
        log("forecast", f"{s['name']}: May MAE={sel_rows[-1]['valid_mae']} RMSE={sel_rows[-1]['valid_rmse']}")
    valid_base = {"valid_mae": round(mae(valid["y_true"], valid["baseline"]), 4),
                  "valid_rmse": round(rmse(valid["y_true"], valid["baseline"]), 4)}
    best_i = min(range(len(sel_rows)), key=lambda i: (sel_rows[i]["valid_mae"], i))
    best = mc["param_grid"][best_i]

    # --- freeze before June is predicted
    fit_rows = pd.concat([train, valid]) if mc["refit_on_train_valid"] else train
    spec = {"frozen_at": datetime.now().isoformat(timespec="seconds"), "model": "HistGradientBoostingRegressor",
            "sklearn": sklearn.__version__, "setting": best, "features": feat.FEATURES, "seed": seed,
            "preprocessing": "OrdinalEncoder(zone_id) fitted on the fit rows; numeric passthrough",
            "selected_by": f"lowest {mc['selection_metric']} on {cfg['splits']['valid']}",
            "refit_on": sorted(fit_rows["hour"].dt.strftime("%Y-%m").unique().tolist()),
            "test_month": cfg["splits"]["test"], "baseline": "y(h-168)"}
    write_json_atomic(spec, ev / "model_spec_frozen.json")

    # --- final test on June
    test = f[f["split"] == "test"].copy()
    assert fit_rows["hour"].max() < test["hour"].min(), "fit rows must precede every test row"
    _, test["y_pred"] = fit_predict(best, seed, fit_rows, test)
    assert test["baseline"].notna().all() and test["y_pred"].notna().all(), "model and baseline need identical rows"
    pred = test[["zone_id", "hour", "y_true", "y_pred", "baseline"]].sort_values(["zone_id", "hour"]).copy()
    pred["hour"] = pred["hour"].dt.strftime("%Y-%m-%d %H:%M:%S")
    for c in ("y_true", "baseline"):
        pred[c] = pred[c].astype("int64")
    write_csv_atomic(pred, out / "predictions.csv")

    overall = {"rows": len(test), "zones": int(test["zone_id"].nunique()),
               "mae_model": round(mae(test["y_true"], test["y_pred"]), 4),
               "rmse_model": round(rmse(test["y_true"], test["y_pred"]), 4),
               "mae_baseline": round(mae(test["y_true"], test["baseline"]), 4),
               "rmse_baseline": round(rmse(test["y_true"], test["baseline"]), 4),
               "mean_y": round(float(test["y_true"].mean()), 4),
               "negative_predictions_clipped": "clipped to 0, not rounded"}
    overall["mae_improvement_vs_baseline"] = round(1 - overall["mae_model"] / overall["mae_baseline"], 4)
    metrics = {
        "mode": cfg["mode"], "note": "Target = recorded pickups (not demand). Mode smoke says nothing about accuracy.",
        "target": mc["target"], "splits": cfg["splits"], "features": feat.FEATURES, "rows": info,
        "selection": {"metric": mc["selection_metric"], "settings": sel_rows, "baseline_on_valid": valid_base,
                      "chosen": best["name"]},
        "frozen_spec": rel(ev / "model_spec_frozen.json"),
        "test": overall, **error_analysis(test, cfg),
    }
    write_json_atomic(metrics, out / "metrics.json")

    common = {"stage": "forecast", "data_hash": data_hash(out / "zone_hour.csv"), "seed": seed,
              "hardware": hardware(), "repeat": "", "intermediate_rows": "", "output_path": rel(out / "metrics.json")}
    conf = f"sklearn {sklearn.__version__} HistGradientBoostingRegressor; features {'/'.join(feat.FEATURES)}"
    rows = []
    for r in sel_rows:
        rows.append({**common, "experiment_id": f"MOD-{r['setting']}-valid_mae", "config": f"{conf}; {r['params']}",
                     "seconds": r["seconds"], "intermediate_rows": r["train_rows"], "metric": "valid_mae", "value": r["valid_mae"]})
    rows.append({**common, "experiment_id": "MOD-baseline-valid_mae", "config": "lag168 baseline", "seconds": "",
                 "metric": "valid_mae", "value": valid_base["valid_mae"]})
    for who in ("model", "baseline"):
        for m in ("mae", "rmse"):
            rows.append({**common, "experiment_id": f"MOD-test-{who}-{m}",
                         "config": f"{conf}; chosen {best['name']}; refit {spec['refit_on']}" if who == "model" else "lag168 baseline",
                         "seconds": round(time.perf_counter() - t0, 3) if who == "model" and m == "mae" else "",
                         "metric": f"test_{m}", "value": overall[f"{m}_{who}"]})
    replace_experiment_rows(cfg, "MOD-", rows)
    log("forecast", f"chosen {best['name']}; June MAE model={overall['mae_model']} baseline={overall['mae_baseline']} "
                    f"RMSE model={overall['rmse_model']} baseline={overall['rmse_baseline']} -> {rel(out / 'predictions.csv')}")
    return metrics


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="full")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
