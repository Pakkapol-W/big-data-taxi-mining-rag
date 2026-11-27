"""Brief §05 Explore - numbers behind the exploratory charts (the dashboard draws them).

Three questions, each answered with numbers; associations only, never causes:
  Q1 temporal  - how do pickups vary by day, weekday and hour of day?
  Q2 zones     - how do the 20 forecast zones differ (volume, peak hour, weekend share, volatility)?
  Q3 quality   - how much do the quality rules change the counts, overall and in the 20 zones?

Inputs : outputs/zone_hour_all.parquet (all valid zones), outputs/top_zones.csv,
         evidence/quality_report.csv, data/clean + data/quarantine (rule effect per zone)
Output : outputs/eda_summary.json
Run    : python -m src.explore --mode smoke|full
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from src.common import connect, load_config, log
from src.modelling_io import rel, write_json_atomic, zone_names

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NOTE = ("Counts are recorded yellow-taxi pickups, not demand. Differences are associations; "
        "nothing here identifies a cause.")


def _r(x, nd: int = 4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def temporal(zh_all: pd.DataFrame) -> dict:
    """City-wide (all valid zones) daily and hourly patterns. NULL (unknown) hours are skipped."""
    city = zh_all.groupby("hour", as_index=False)["pickup_count"].sum(min_count=1)
    city = city.dropna(subset=["pickup_count"])
    city["date"] = city["hour"].dt.date
    city["hod"] = city["hour"].dt.hour
    city["dow"] = city["hour"].dt.day_of_week
    daily = city.groupby("date", as_index=False)["pickup_count"].sum()
    daily["dow"] = pd.to_datetime(daily["date"]).dt.day_of_week
    wkd = city[city["dow"] < 5].groupby("hod")["pickup_count"].mean().reindex(range(24))
    wke = city[city["dow"] >= 5].groupby("hod")["pickup_count"].mean().reindex(range(24))
    by_dow = daily.groupby("dow")["pickup_count"].mean().reindex(range(7))
    by_month = city.groupby(city["hour"].dt.strftime("%Y-%m"))["pickup_count"].sum()
    busiest, quietest = daily.loc[daily["pickup_count"].idxmax()], daily.loc[daily["pickup_count"].idxmin()]
    return {
        "question": "How do pickups vary by day, weekday and hour of day (all valid zones)?",
        "daily_total": [{"date": str(d), "pickups": int(c)} for d, c in zip(daily["date"], daily["pickup_count"])],
        "by_month": {m: int(c) for m, c in by_month.items()},
        "mean_daily_by_weekday": {WEEKDAYS[i]: _r(v, 1) for i, v in by_dow.items()},
        "mean_hourly_weekday": [_r(v, 1) for v in wkd],
        "mean_hourly_weekend": [_r(v, 1) for v in wke],
        "weekday_peak_hour": int(wkd.idxmax()), "weekday_trough_hour": int(wkd.idxmin()),
        "weekday_peak_to_trough": _r(wkd.max() / wkd.min(), 2) if wkd.min() else None,
        "weekend_peak_hour": int(wke.idxmax()),
        "busiest_day": {"date": str(busiest["date"]), "pickups": int(busiest["pickup_count"])},
        "quietest_day": {"date": str(quietest["date"]), "pickups": int(quietest["pickup_count"])},
        "unknown_hours_skipped": int(zh_all.groupby("hour")["pickup_count"].count().eq(0).sum()),
    }


def zones(zh_all: pd.DataFrame, top: pd.DataFrame, names: pd.DataFrame, cfg: dict) -> dict:
    """Per-zone profile of the 20 forecast zones; all 3 months, NULL = unknown is skipped."""
    total_all = zh_all["pickup_count"].sum()
    z = zh_all[zh_all["zone_id"].isin(top["zone_id"])].copy()
    z["hod"], z["dow"] = z["hour"].dt.hour, z["hour"].dt.day_of_week
    z["month"] = z["hour"].dt.strftime("%Y-%m")
    rows = []
    for zid, g in z.groupby("zone_id"):
        obs = g.dropna(subset=["pickup_count"])
        hourly = obs.groupby("hod")["pickup_count"].mean()
        wk = obs[obs["dow"] < 5]["pickup_count"].mean()
        we = obs[obs["dow"] >= 5]["pickup_count"].mean()
        y = obs["pickup_count"].astype(float)
        rows.append({
            "zone_id": int(zid),
            "total_pickups": int(y.sum()),
            "april_pickups": int(obs[obs["month"] == cfg["selection_month"]]["pickup_count"].sum()),
            "mean_per_hour": _r(y.mean(), 2),
            "peak_hour": int(hourly.idxmax()) if len(hourly) else None,
            "weekend_to_weekday_ratio": _r(we / wk, 3) if wk else None,
            "cv_hourly": _r(y.std() / y.mean(), 3) if y.mean() else None,
            "zero_hours": int((y == 0).sum()),
            "unknown_hours": int(g["pickup_count"].isna().sum()),
        })
    per = pd.DataFrame(rows).merge(names, on="zone_id", how="left")
    per = per.merge(top[["zone_id", "rank"]], on="zone_id").sort_values("rank")
    top20_total = per["total_pickups"].sum()
    return {
        "question": "How do the 20 forecast zones differ?",
        "n_valid_zones": int(zh_all["zone_id"].nunique()),
        "top20_share_of_all_pickups": _r(top20_total / total_all) if total_all else None,
        "top20_total_pickups": int(top20_total),
        "highest_cv_zone": int(per.loc[per["cv_hourly"].idxmax(), "zone_id"]),
        "highest_weekend_ratio_zone": int(per.loc[per["weekend_to_weekday_ratio"].idxmax(), "zone_id"]),
        "per_zone": per.to_dict("records"),
    }


def quality(con, cfg: dict, top: pd.DataFrame) -> dict:
    """Rule counts from the quality report + per-zone rule effect from clean/quarantine files."""
    qr = pd.read_csv(cfg["paths"]["evidence"] / "quality_report.csv")
    tot = qr[qr["rule_id"] == "TOTAL"].iloc[0]
    by_rule = [{"rule_id": r.rule_id, "action": r.action,
                "quarantined_rows": int(r.quarantined_rows), "flagged_rows": int(r.flagged_rows)}
               for r in qr[qr["rule_id"] != "TOTAL"].itertuples()]
    out = {
        "question": "How much do the quality rules change the counts?",
        "input_rows": int(tot.input_rows), "retained_rows": int(tot.retained_rows),
        "quarantined_rows": int(tot.quarantined_rows), "rows_with_any_flag": int(tot.flagged_rows),
        "quarantined_share": _r(tot.quarantined_rows / tot.input_rows, 6),
        "flagged_share": _r(tot.flagged_rows / tot.input_rows, 6),
        "by_rule": by_rule, "per_zone": None, "per_zone_note": None,
    }
    clean = sorted(cfg["paths"]["clean"].glob("pickup_month=*/*.parquet"))
    quar = sorted(cfg["paths"]["quarantine"].glob("*.parquet"))
    if not clean:
        out["per_zone_note"] = "clean partitions not on disk (run acquire/clean); per-zone effect not computed"
        log("explore", out["per_zone_note"])
        return out
    ids = ",".join(str(int(z)) for z in top["zone_id"])
    cl = con.execute(f"""
        SELECT PULocationID AS zone_id, count(*) AS retained,
               count(*) FILTER (WHERE f_fare) AS f_fare, count(*) FILTER (WHERE f_distance) AS f_distance,
               count(*) FILTER (WHERE f_order) AS f_order, count(*) FILTER (WHERE f_missing) AS f_missing
        FROM read_parquet({[str(f) for f in clean]}) WHERE PULocationID IN ({ids}) GROUP BY 1""").df()
    if quar:
        q = con.execute(f"""
            SELECT PULocationID AS zone_id, quarantine_reason AS rule_id, count(*) AS n
            FROM read_parquet({[str(f) for f in quar]}) WHERE PULocationID IN ({ids}) GROUP BY 1, 2""").df()
        qw = q.pivot_table(index="zone_id", columns="rule_id", values="n", aggfunc="sum", fill_value=0)
        qw.columns = [f"quarantined_{c}" for c in qw.columns]
        cl = cl.merge(qw.reset_index(), on="zone_id", how="left").fillna(0)
    qcols = [c for c in cl.columns if c.startswith("quarantined_")]
    cl["quarantined"] = cl[qcols].sum(axis=1) if qcols else 0
    cl["input"] = cl["retained"] + cl["quarantined"]
    cl["quarantined_share"] = (cl["quarantined"] / cl["input"]).round(5)
    cl["fare_flag_share"] = (cl["f_fare"] / cl["retained"]).round(5)
    cl = cl.astype({c: "int64" for c in cl.columns if c not in ("quarantined_share", "fare_flag_share")})
    out["per_zone"] = cl.sort_values("zone_id").to_dict("records")
    out["per_zone_note"] = ("Quarantined rows (e.g. R4b refund reversals) are removed from pickup counts; "
                            "flagged rows stay in counts and are excluded only from fare/distance analyses.")
    out["top20_quarantined_share"] = _r(cl["quarantined"].sum() / cl["input"].sum(), 5)
    worst = cl.loc[cl["quarantined_share"].idxmax()]
    out["highest_quarantine_zone"] = {"zone_id": int(worst["zone_id"]), "share": float(worst["quarantined_share"])}
    return out


def run(cfg: dict) -> dict:
    out_dir = cfg["paths"]["outputs"]
    zh_all = pd.read_parquet(out_dir / "zone_hour_all.parquet")
    zh_all["pickup_count"] = zh_all["pickup_count"].astype("Float64").astype(float)
    top = pd.read_csv(out_dir / "top_zones.csv")[["rank", "zone_id"]]
    names = zone_names(cfg)
    con = connect(cfg)
    summary = {
        "mode": cfg["mode"], "note": NOTE,
        "scope": {"window": cfg["window"], "zones_all": "all valid zones (excl. 264/265)",
                  "zones_forecast": "top 20 by retained April pickups", "time": "New York local",
                  "sources": [rel(out_dir / "zone_hour_all.parquet"), rel(out_dir / "top_zones.csv"),
                              rel(cfg["paths"]["evidence"] / "quality_report.csv")]},
        "temporal": temporal(zh_all),
        "zones": zones(zh_all, top, names, cfg),
        "quality": quality(con, cfg, top),
    }
    write_json_atomic(summary, out_dir / "eda_summary.json")
    t = summary["temporal"]
    log("explore", f"weekday peak {t['weekday_peak_hour']}:00, trough {t['weekday_trough_hour']}:00, "
                   f"top-20 share {summary['zones']['top20_share_of_all_pickups']} -> {rel(out_dir / 'eda_summary.json')}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="full")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
