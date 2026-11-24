"""Brief §04 item 4 — Store: zone dimension, clean partitions, quarantine, zone_hour grid."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pandas as pd

from src.common import log, write_csv_atomic

ZONE_HOUR_COLS = ["zone_id", "hour", "pickup_count", "coverage_ok"]


# ---------------------------------------------------------------- dimension + clean partitions
def write_zone_dim(con, cfg: dict) -> None:
    """Full: raw lookup CSV -> data/dim/zones.parquet. Smoke: read the shipped dim (fix #1).
    Either way, register view `zones`."""
    dim = cfg["paths"]["dim"] / "zones.parquet"
    if cfg["mode"] == "full":
        csv = cfg["paths"]["raw"] / Path(cfg["zone_url"]).name
        tmp = dim.with_suffix(".parquet.tmp")
        con.execute(f"""COPY (SELECT LocationID::INTEGER AS LocationID, Borough, Zone, service_zone
                              FROM read_csv('{csv}', header=true) ORDER BY LocationID)
                        TO '{tmp}' (FORMAT parquet)""")
        os.replace(tmp, dim)
    elif not dim.exists():
        raise FileNotFoundError(f"{dim} missing — it ships in the ZIP; or run a full acquire first")
    con.execute(f"CREATE OR REPLACE VIEW zones AS SELECT * FROM read_parquet('{dim}')")


def _tmp_dir(p: Path) -> Path:
    return p.with_name(p.name + ".tmp")


def _swap_dir(tmp: Path, final: Path) -> None:
    """Replace `final` with `tmp` so readers never see a half-written folder."""
    if final.exists():
        shutil.rmtree(final)
    os.replace(tmp, final)


def write_clean(con, cfg: dict) -> None:
    """Retained rows -> clean/pickup_month=YYYY-MM/ ; quarantined rows (+reason) -> quarantine/.
    Written to *.tmp folders first, then swapped in (idempotent reruns, no partial output)."""
    clean, quar = cfg["paths"]["clean"], cfg["paths"]["quarantine"]
    clean_tmp, quar_tmp = _tmp_dir(clean), _tmp_dir(quar)
    for t in (clean_tmp, quar_tmp):
        shutil.rmtree(t, ignore_errors=True)
    quar_tmp.mkdir(parents=True)
    # One ordered COPY per month into a Hive-style folder. (DuckDB's PARTITION_BY write does not
    # preserve ORDER BY, which made row order — and Method B's split boundaries — vary per run.)
    months = [m for (m,) in con.execute(
        "SELECT DISTINCT strftime(tpep_pickup_datetime, '%Y-%m') FROM labeled "
        "WHERE quarantine_reason IS NULL ORDER BY 1").fetchall()]
    for m in months:
        part = clean_tmp / f"pickup_month={m}"
        part.mkdir(parents=True)
        con.execute(f"""
            COPY (SELECT * EXCLUDE (quarantine_reason) FROM labeled
                  WHERE quarantine_reason IS NULL AND strftime(tpep_pickup_datetime, '%Y-%m') = '{m}'
                  ORDER BY tpep_pickup_datetime, PULocationID)
            TO '{part / "data_0.parquet"}' (FORMAT parquet)""")
    con.execute(f"""
        COPY (SELECT * FROM labeled WHERE quarantine_reason IS NOT NULL
              ORDER BY quarantine_reason, source_file, tpep_pickup_datetime)
        TO '{quar_tmp / "quarantined.parquet"}' (FORMAT parquet)""")
    _swap_dir(clean_tmp, clean)
    _swap_dir(quar_tmp, quar)
    log("store", f"clean partitions: {sorted(p.name for p in clean.iterdir())}")


# ---------------------------------------------------------------- zone_hour grid
def months_complete(cfg: dict) -> bool:
    """Complete acquisition = full mode AND every configured month in the manifest and on disk."""
    from src.acquire import CoverageError, check_months
    man = cfg["paths"]["manifest"]
    if cfg["mode"] != "full" or not man.exists():
        return False
    try:
        check_months(pd.read_csv(man, dtype=str), cfg)
        return True
    except CoverageError:
        return False


def n_hours(cfg: dict) -> int:
    w = cfg["window"]
    return int((pd.Timestamp(w["end"]) - pd.Timestamp(w["start"])) / pd.Timedelta(hours=1))


def build_zone_hour(con, counts: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Every valid zone x every hour in the window, LEFT JOIN observed counts.

    Zero-fill ONLY where coverage is confirmed: complete acquisition (all months, full mode)
    AND the hour has >= 1 retained trip city-wide (fix #7). Elsewhere a missing count stays NULL
    = unknown. 0 means 'no recorded pickup', not proof of zero demand.
    """
    w = cfg["window"]
    invalid = ",".join(str(int(z)) for z in cfg["invalid_zones"])
    full_cov = "TRUE" if months_complete(cfg) else "FALSE"
    con.register("counts_df", counts)
    zh = con.execute(f"""
        WITH hours AS (
            SELECT generate_series AS hour
            FROM generate_series(TIMESTAMP '{w['start']}', TIMESTAMP '{w['end']}' - INTERVAL 1 HOUR, INTERVAL 1 HOUR)),
        valid_zones AS (SELECT LocationID AS zone_id FROM zones WHERE LocationID NOT IN ({invalid})),
        hour_cov AS (SELECT hour, sum(pickup_count) AS city_trips FROM counts_df GROUP BY hour),
        grid AS (
            SELECT z.zone_id, h.hour, c.pickup_count,
                   ({full_cov} AND hc.city_trips IS NOT NULL) AS coverage_ok
            FROM valid_zones z CROSS JOIN hours h
            LEFT JOIN counts_df c ON c.zone_id = z.zone_id AND c.hour = h.hour
            LEFT JOIN hour_cov hc ON hc.hour = h.hour)
        SELECT zone_id::INTEGER AS zone_id, hour,
               CASE WHEN coverage_ok THEN coalesce(pickup_count, 0) ELSE pickup_count END::BIGINT AS pickup_count,
               coverage_ok
        FROM grid ORDER BY zone_id, hour
    """).df()
    con.unregister("counts_df")

    n_z = con.execute(f"SELECT count(*) FROM zones WHERE LocationID NOT IN ({invalid})").fetchall()[0][0]
    assert len(zh) == n_z * n_hours(cfg), f"grid size {len(zh)} != {n_z} zones x {n_hours(cfg)} hours"
    assert not zh.duplicated(["zone_id", "hour"]).any(), "duplicate (zone_id, hour) keys"
    assert zh["pickup_count"].sum() == counts["pickup_count"].sum(), "counts lost/added when joining to grid"
    return zh[ZONE_HOUR_COLS]


def select_top_zones(zh: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Top N zones by retained pickups in the selection month (April only); ties -> lower zone_id."""
    month = zh["hour"].dt.strftime("%Y-%m") == cfg["selection_month"]
    tot = (zh[month].groupby("zone_id")["pickup_count"].sum().rename("selection_pickups").reset_index())
    tot = tot.sort_values(["selection_pickups", "zone_id"], ascending=[False, True]).head(cfg["top_n_zones"])
    tot.insert(0, "rank", range(1, len(tot) + 1))
    return tot.reset_index(drop=True)


def write_outputs(con, zh: pd.DataFrame, top: pd.DataFrame, cfg: dict) -> dict:
    out = cfg["paths"]["outputs"]
    out.mkdir(parents=True, exist_ok=True)
    # all zones (EDA / dashboard heatmap)
    tmp = out / "zone_hour_all.parquet.tmp"
    zh.to_parquet(tmp, index=False)
    os.replace(tmp, out / "zone_hour_all.parquet")
    # 20-zone deliverable for modelling
    sel = zh[zh["zone_id"].isin(top["zone_id"])].sort_values(["zone_id", "hour"]).copy()
    expected = len(top) * n_hours(cfg)
    assert len(sel) == expected, f"zone_hour.csv has {len(sel)} rows, expected {expected}"
    sel["hour"] = sel["hour"].dt.strftime("%Y-%m-%d %H:%M:%S")
    sel["pickup_count"] = sel["pickup_count"].astype("Int64")
    write_csv_atomic(sel, out / "zone_hour.csv")
    # top-zone list with names (joined from the dimension table, not stored on trips)
    names = con.execute("SELECT LocationID AS zone_id, Borough, Zone FROM zones").df()
    top_named = top.merge(names, on="zone_id", how="left")[["rank", "zone_id", "Borough", "Zone", "selection_pickups"]]
    write_csv_atomic(top_named, out / "top_zones.csv")
    cov_hours = int(zh.groupby("hour")["coverage_ok"].first().sum())
    log("store", f"zone_hour_all={len(zh):,} rows  zone_hour.csv={len(sel):,} rows  "
                 f"covered hours={cov_hours}/{n_hours(cfg)}")
    return {"zone_hour_all_rows": len(zh), "zone_hour_rows": len(sel), "covered_hours": cov_hours,
            "top_zones": top_named.to_dict("records")}
