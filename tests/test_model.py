"""T6 (no leakage) and T7 (fair metrics) for the Modelling stages.

Fixture: synthetic complete zone_hour grid, 6 zones x 2,184 hours (2024-04-01 .. 2024-06-30 23:00),
count = daily sine shape + zone offset + seeded noise. Small enough to run in seconds.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from src import cluster, features, forecast, store
from tests.conftest import make_cfg

ZONES = [10, 20, 30, 40, 50, 60]


def synthetic_zone_hour(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    hours = pd.date_range("2024-04-01 00:00:00", "2024-06-30 23:00:00", freq="h")
    rows = []
    for i, z in enumerate(ZONES):
        shift = 0 if i < 3 else 8  # two shapes, so clustering has structure
        for h in hours:
            y = 20 + 15 * math.sin(2 * math.pi * (h.hour - 6 - shift) / 24) + 4 * i + int(rng.integers(0, 5))
            rows.append({"zone_id": z, "hour": h, "pickup_count": max(0, int(y)), "coverage_ok": True})
    return pd.DataFrame(rows)


def write_zone_hour(cfg: dict, zh: pd.DataFrame) -> None:
    out = zh.copy()
    out["hour"] = out["hour"].dt.strftime("%Y-%m-%d %H:%M:%S")
    out.to_csv(cfg["paths"]["outputs"] / "zone_hour.csv", index=False)


@pytest.fixture
def mcfg(tmp_path):
    cfg = make_cfg(tmp_path)
    pd.DataFrame({"LocationID": ZONES, "Borough": "B", "Zone": [f"Z{z}" for z in ZONES],
                  "service_zone": "S"}).to_parquet(cfg["paths"]["dim"] / "zones.parquet", index=False)
    cfg["cluster"] = {**cfg["cluster"], "k_values": [2, 3], "min_cluster_size": 1}
    cfg["model"]["param_grid"] = [
        {"name": "small_a", "loss": "squared_error", "learning_rate": 0.1, "max_iter": 30, "max_leaf_nodes": 15},
        {"name": "small_b", "loss": "poisson", "learning_rate": 0.1, "max_iter": 30, "max_leaf_nodes": 15},
    ]
    write_zone_hour(cfg, synthetic_zone_hour())
    return cfg


def _y(zh, zone, hour):
    return float(zh[(zh["zone_id"] == zone) & (zh["hour"] == pd.Timestamp(hour))]["pickup_count"].iloc[0])


# ---------------------------------------------------------------- T6
def test_T6_disjoint_splits(mcfg, record_property):
    from src.modelling_io import load_zone_hour
    f, info = features.build(load_zone_hour(mcfg), mcfg)
    months = {s: set(f[f["split"] == s]["hour"].dt.strftime("%Y-%m")) for s in ("train", "valid", "test")}
    assert months == {"train": {"2024-04"}, "valid": {"2024-05"}, "test": {"2024-06"}}
    keys = {s: set(zip(f[f["split"] == s]["zone_id"], f[f["split"] == s]["hour"])) for s in months}
    assert not (keys["train"] & keys["valid"]) and not (keys["valid"] & keys["test"]) and not (keys["train"] & keys["test"])
    assert info["rows_dropped_no_history"] == 168 * len(ZONES)
    assert info["dropped_by_split"] == {"train": 168 * len(ZONES)}
    assert f.groupby("split")["hour"].max()["train"] < f.groupby("split")["hour"].min()["valid"]
    record_property("actual", f"months {months}; key overlap 0; dropped {info['rows_dropped_no_history']} "
                              f"rows lacking 168 h history, all in train")


def test_T6_features_past_only(mcfg, record_property):
    from src.modelling_io import load_zone_hour
    zh = load_zone_hour(mcfg)
    h, z = pd.Timestamp("2024-06-10 12:00:00"), 20
    f, _ = features.build(zh, mcfg)
    row = f[(f["zone_id"] == z) & (f["hour"] == h)].iloc[0]
    # hand-computed from the grid
    assert row["lag1"] == _y(zh, z, h - pd.Timedelta(hours=1))
    assert row["lag24"] == _y(zh, z, h - pd.Timedelta(hours=24))
    assert row["lag168"] == row["baseline"] == _y(zh, z, h - pd.Timedelta(hours=168))
    assert row["mean24"] == pytest.approx(np.mean([_y(zh, z, h - pd.Timedelta(hours=k)) for k in range(1, 25)]))
    assert (row["hour_of_day"], row["weekday"]) == (12, 0)
    # perturb the target hour and everything after it: features of row h must not change
    zh2 = zh.copy()
    zh2.loc[(zh2["zone_id"] == z) & (zh2["hour"] >= h), "pickup_count"] = 10**6
    f2, _ = features.build(zh2, mcfg)
    row2 = f2[(f2["zone_id"] == z) & (f2["hour"] == h)].iloc[0]
    cols = features.FEATURES + ["baseline"]
    assert row[cols].equals(row2[cols])
    assert row2["y_true"] == 10**6
    record_property("actual", "lag1/lag24/lag168/mean24 equal hand values; setting y(>=h)=1e6 leaves row-h "
                              "features unchanged (only y_true changes)")


def test_T6_train_only_selection(mcfg, record_property):
    from src.modelling_io import load_zone_hour
    # (a) top-zone selection reads April only: a zone that explodes in June is not selected
    zh = synthetic_zone_hour()
    zh.loc[(zh["zone_id"] == 10) & (zh["hour"].dt.month == 6), "pickup_count"] = 10**6
    top = store.select_top_zones(zh, {**mcfg, "top_n_zones": 2})
    april = zh[zh["hour"].dt.month == 4].groupby("zone_id")["pickup_count"].sum().nlargest(2)
    assert list(top["zone_id"]) == list(april.index) and 10 not in list(top["zone_id"])
    # (b) clustering and (c) model selection are unchanged when June is perturbed
    clu1 = cluster.run(mcfg)
    m1 = forecast.run(mcfg)
    write_zone_hour(mcfg, zh)  # June of zone 10 now 1e6
    clu2 = cluster.run(mcfg)
    m2 = forecast.run(mcfg)
    assert clu1 == clu2
    strip = lambda m: [{k: v for k, v in s.items() if k != "seconds"} for s in m["selection"]["settings"]]
    assert strip(m1) == strip(m2) and m1["selection"]["chosen"] == m2["selection"]["chosen"]
    assert m1["test"]["mae_model"] != m2["test"]["mae_model"]  # June did change; only the test score moved
    # (d) config that would cluster on the test month is refused
    bad = {**mcfg, "cluster": {**mcfg["cluster"], "profile_month": "2024-06"}}
    with pytest.raises(ValueError):
        cluster.run(bad)
    record_property("actual", f"top-2 = {list(top['zone_id'])} (June spike in zone 10 ignored); clusters and "
                              f"May selection identical after changing June; profile_month=June refused")


# ---------------------------------------------------------------- T7
def test_T7_metrics_hand_checked(record_property):
    y, p = [0, 2, 4], [1, 2, 1]
    assert forecast.mae(y, p) == pytest.approx(4 / 3)          # (1 + 0 + 3) / 3
    assert forecast.rmse(y, p) == pytest.approx(math.sqrt(10 / 3))  # sqrt((1 + 0 + 9) / 3)
    assert list(forecast.clip([-0.5, 0.0, 1.25])) == [0.0, 0.0, 1.25]  # clipped, not rounded
    record_property("actual", "MAE([0,2,4],[1,2,1]) = 1.3333; RMSE = 1.8257; clip([-0.5,0,1.25]) = [0,0,1.25]")


def test_T7_same_rows_for_model_and_baseline(mcfg, record_property):
    m = forecast.run(mcfg)
    pred = pd.read_csv(mcfg["paths"]["outputs"] / "predictions.csv")
    assert list(pred.columns) == ["zone_id", "hour", "y_true", "y_pred", "baseline"]
    assert len(pred) == len(ZONES) * 30 * 24 == m["test"]["rows"]
    assert pred[["y_true", "y_pred", "baseline"]].notna().all().all() and (pred["y_pred"] >= 0).all()
    assert not pred.duplicated(["zone_id", "hour"]).any()
    assert m["test"]["mae_model"] == round(forecast.mae(pred["y_true"], pred["y_pred"]), 4)
    assert m["test"]["mae_baseline"] == round(forecast.mae(pred["y_true"], pred["baseline"]), 4)
    assert m["test"]["rmse_baseline"] == round(forecast.rmse(pred["y_true"], pred["baseline"]), 4)
    zh = synthetic_zone_hour()
    r = pred.iloc[100]
    assert r["baseline"] == _y(zh, r["zone_id"], pd.Timestamp(r["hour"]) - pd.Timedelta(hours=168))
    spec = json.loads((mcfg["paths"]["evidence"] / "model_spec_frozen.json").read_text(encoding="utf-8"))
    assert spec["refit_on"] == ["2024-04", "2024-05"] and spec["test_month"] == "2024-06"
    record_property("actual", f"{len(pred)} June rows, each with y_true/y_pred/baseline; metrics.json MAE/RMSE "
                              f"recomputed from predictions.csv match; baseline = y(h-168) by hand")
