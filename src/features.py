"""Brief §05 Forecast features - one row per (zone, target hour h); predicted at the end of hour t = h-1.

Every feature uses only counts at hours <= t (already observed when the forecast is made):
  hour_of_day, weekday  calendar fields of h            known in advance
  zone_id        categorical                     fixed set chosen from April only
  lag1           y(t) = y(h-1)                   last observed hour
  lag24          y(h-24)                         same hour yesterday
  mean24         mean y(h-24 .. h-1)             trailing 24 h, ends at t
  lag168         y(h-168)                        same hour last week  (= the baseline)
Rows whose history is incomplete (first 168 h of the window per zone) are dropped and counted.
Split is by the month of the target hour h; lags may reach into earlier months (already observed).

Full mode requires a complete grid (no NULL counts): missing coverage stops official forecasting.
Smoke mode fills NULL with 0 only so the code path runs; it says nothing about accuracy.
"""
from __future__ import annotations

import pandas as pd

from src.modelling_io import month_of

FEATURE_DOC = [  # feature, definition, latest hour used relative to target h, available at end of t?
    ("hour_of_day", "hour of day of target hour h (0-23)", "h (calendar only)", True),
    ("weekday", "day of week of h (0=Mon)", "h (calendar only)", True),
    ("zone_id", "pickup zone, categorical; zone set fixed from April", "none", True),
    ("lag1", "y(h-1): count in the last observed hour t", "h-1", True),
    ("lag24", "y(h-24): same hour on the previous day", "h-24", True),
    ("mean24", "mean of y(h-24) .. y(h-1): trailing 24 observed hours", "h-1", True),
    ("lag168", "y(h-168): same hour one week earlier (also the baseline)", "h-168", True),
]
LAG_COLS = ["lag1", "lag24", "mean24", "lag168"]


def feature_availability() -> pd.DataFrame:
    return pd.DataFrame(FEATURE_DOC, columns=["feature", "definition", "latest_hour_used",
                                              "available_at_end_of_t"])


def build(zh: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """zone_hour -> feature table with columns zone_id, hour, split, y_true, features..., baseline."""
    zh = zh.sort_values(["zone_id", "hour"], kind="mergesort").reset_index(drop=True)
    gaps = zh.groupby("zone_id")["hour"].diff().dropna()
    if not (gaps == pd.Timedelta(hours=1)).all():
        raise ValueError("zone_hour is not a contiguous hourly grid; row shifts would not equal hour lags")
    y = zh["pickup_count"].astype("Float64")
    n_null = int(y.isna().sum())
    if n_null:
        if cfg["mode"] == "full":
            raise ValueError(f"{n_null} zone-hours have unknown coverage; official forecasting stops (brief §04)")
        y = y.fillna(0)  # smoke only
    y = y.astype(float)

    g = y.groupby(zh["zone_id"])
    f = pd.DataFrame({"zone_id": zh["zone_id"].astype(int), "hour": zh["hour"], "y_true": y})
    f["hour_of_day"] = zh["hour"].dt.hour
    f["weekday"] = zh["hour"].dt.day_of_week
    f["lag1"] = g.shift(1)
    f["lag24"] = g.shift(24)
    f["mean24"] = g.transform(lambda s: s.shift(1).rolling(24, min_periods=24).mean())
    f["lag168"] = g.shift(cfg["model"]["history_hours"])
    f["baseline"] = f["lag168"]
    s = cfg["splits"]
    f["split"] = month_of(f["hour"]).map({s["train"]: "train", s["valid"]: "valid", s["test"]: "test"})

    lacking = f[LAG_COLS].isna().any(axis=1)
    info = {"rows_total": len(f), "rows_dropped_no_history": int(lacking.sum()),
            "dropped_by_split": f[lacking]["split"].value_counts().to_dict(),
            "smoke_null_filled": n_null if cfg["mode"] == "smoke" else 0}
    f = f[~lacking & f["split"].notna()].reset_index(drop=True)
    info["rows_by_split"] = f["split"].value_counts().reindex(["train", "valid", "test"]).fillna(0).astype(int).to_dict()
    return f, info


FEATURES = ["zone_id", "hour_of_day", "weekday", "lag1", "lag24", "mean24", "lag168"]
