"""Brief §04 items 2–3 — Quality rules, quarantine, flags, count conservation.

Run:  python -m src.clean --mode smoke|full
"""
from __future__ import annotations

import argparse

import pandas as pd

from src import store
from src.common import connect, ensure_dirs, load_config, log, write_csv_atomic

# Single source of truth for rule text: the report is generated from this list.
# (rule_id, brief's check, condition, action, justification)
RULES = [
    ("R1", "timestamp parsing",
     "pickup or dropoff timestamp is NULL (Parquet stores typed timestamps, so 'unparseable' = NULL)",
     "quarantine", "No valid pickup hour, so the trip cannot be counted"),
    ("R2", "pickup-date range", "pickup outside [2024-04-01 00:00, 2024-07-01 00:00) New York local time",
     "quarantine", "Out of scope (e.g. 2002/2009/2026 dates). Scope is set by pickup time, not by source file"),
    ("R3", "zone matching",
     "PULocationID not in zone lookup, or 264 (Unknown) / 265 (Outside of NYC)",
     "quarantine", "Pickup cannot be assigned to a real NYC zone"),
    ("R4", "exact duplicates", "all 19 source columns identical to an earlier row; first copy kept",
     "quarantine extra copies",
     "Same vendor, second-level timestamps, zones, distance and every fare component: a double "
     "submission, not two real trips"),
    ("R4b", "exact duplicates (reversal pairs)",
     "total_amount < 0 and an unmatched row with the same vendor, pickup, dropoff, PU and DO has "
     "total_amount = -this total (matched one-to-one)",
     "quarantine the negative reversal; keep the original",
     "TLC records a void/dispute as the original plus a negative reversal with identical timestamps: "
     "one pickup, two accounting rows. Counting both double-counts the pickup"),
    ("R5", "pickup/dropoff order", "dropoff <= pickup, or duration > 24 h",
     "flag f_order (retained)", "Pickup hour is still valid; only the duration is unreliable"),
    ("R6a", "distance anomaly", "trip_distance <= 0 or > 100 mi",
     "flag f_distance (retained)", "Kept in pickup counts; excluded only from distance analyses"),
    ("R6b", "fare anomaly", "fare_amount < 0 or > 1000, or total_amount < 0",
     "flag f_fare (retained)",
     "Kept in pickup counts; excluded only from fare analyses (negatives are refunds/voids)"),
    ("R7", "missing values",
     "NULL passenger_count, RatecodeID, store_and_fwd_flag, congestion_surcharge or Airport_fee",
     "flag f_missing (retained, not filled)", "Not filled with 0; kept NULL because the pickup itself is valid"),
]
QUARANTINE_IDS = ["R1", "R2", "R3", "R4", "R4b"]
FLAG_COL = {"R5": "f_order", "R6a": "f_distance", "R6b": "f_fare", "R7": "f_missing"}
REPORT_COLS = ["rule_id", "condition", "action", "reason",
               "input_rows", "retained_rows", "quarantined_rows", "flagged_rows"]


def source_sql(cfg: dict) -> str:
    """Full mode: the 3 raw files. Smoke mode: the 20k sample (already has source_file)."""
    if cfg["mode"] == "smoke":
        if not cfg["paths"]["sample"].exists():
            raise FileNotFoundError(f"smoke needs {cfg['paths']['sample']} — run acquire first")
        return f"SELECT * FROM read_parquet('{cfg['paths']['sample']}')"
    files = [str(cfg["paths"]["raw"] / cfg["file_pattern"].format(month=m)) for m in cfg["months"]]
    return (f"SELECT * EXCLUDE (filename), parse_filename(filename) AS source_file "
            f"FROM read_parquet({files}, filename=true)")


def label_trips(con, cfg: dict) -> int:
    """Create TEMP TABLE labeled: every input row + quarantine_reason (first failing rule) + flags.
    Returns the input row count."""
    r, w = cfg["rules"], cfg["window"]
    cols = ", ".join(cfg["required_columns"])
    invalid = ",".join(str(int(z)) for z in cfg["invalid_zones"])
    trip_key = "VendorID, tpep_pickup_datetime, tpep_dropoff_datetime, PULocationID, DOLocationID"
    reversal = "TRUE" if r.get("reversal_pairs", True) else "FALSE"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE labeled AS
        WITH src AS ({source_sql(cfg)}),
        d AS (
          SELECT s.*, z.LocationID IS NOT NULL AS zone_matched,
                 row_number() OVER (PARTITION BY {cols} ORDER BY s.source_file) AS dup_rank
          FROM src s LEFT JOIN zones z ON s.PULocationID = z.LocationID
        ),
        -- R4b one-to-one matching: the k-th negative row of a (trip, |total|) group is a reversal
        -- only if the group has at least k distinct positive originals (exact copies excluded).
        rv AS (
          SELECT d.*,
                 count(*) FILTER (WHERE total_amount > 0 AND dup_rank = 1)
                   OVER (PARTITION BY {trip_key}, abs(total_amount)) AS n_pos,
                 row_number() OVER (PARTITION BY {trip_key}, abs(total_amount), (total_amount < 0), dup_rank = 1
                                    ORDER BY source_file) AS neg_rank
          FROM d
        )
        SELECT * EXCLUDE (zone_matched, dup_rank, n_pos, neg_rank),
          CASE WHEN tpep_pickup_datetime IS NULL OR tpep_dropoff_datetime IS NULL THEN 'R1'
               WHEN tpep_pickup_datetime <  TIMESTAMP '{w['start']}'
                 OR tpep_pickup_datetime >= TIMESTAMP '{w['end']}'          THEN 'R2'
               WHEN NOT zone_matched OR PULocationID IN ({invalid})        THEN 'R3'
               WHEN dup_rank > 1                                           THEN 'R4'
               WHEN {reversal} AND total_amount < 0 AND neg_rank <= n_pos  THEN 'R4b'
          END AS quarantine_reason,
          coalesce(tpep_dropoff_datetime <= tpep_pickup_datetime
                   OR tpep_dropoff_datetime - tpep_pickup_datetime > INTERVAL {int(r['max_duration_h'])} HOUR,
                   false) AS f_order,
          coalesce(trip_distance <= 0 OR trip_distance > {float(r['max_distance_mi'])}, false) AS f_distance,
          coalesce(fare_amount < 0 OR fare_amount > {float(r['max_fare'])} OR total_amount < 0, false) AS f_fare,
          (passenger_count IS NULL OR RatecodeID IS NULL OR store_and_fwd_flag IS NULL
           OR congestion_surcharge IS NULL OR Airport_fee IS NULL) AS f_missing
        FROM rv
    """)
    n_in = con.execute(f"SELECT count(*) FROM ({source_sql(cfg)})").fetchall()[0][0]
    n_lab = con.execute("SELECT count(*) FROM labeled").fetchall()[0][0]
    assert n_in == n_lab, f"zone join changed row count {n_in} -> {n_lab}"
    return n_in


def quality_report(con, cfg: dict, raw_rows: int) -> pd.DataFrame:
    """R1–R4 sequential (input of Rn = retained of Rn-1); R5–R7 flags on the final retained set."""
    q = dict(con.execute("SELECT quarantine_reason, count(*) FROM labeled "
                         "WHERE quarantine_reason IS NOT NULL GROUP BY 1").fetchall())
    flags = con.execute("""
        SELECT count(*), sum(f_order::INT), sum(f_distance::INT), sum(f_fare::INT), sum(f_missing::INT),
               count(*) FILTER (WHERE f_order OR f_distance OR f_fare OR f_missing)
        FROM labeled WHERE quarantine_reason IS NULL""").fetchall()[0]
    retained_final, any_flag = flags[0], flags[5]
    f_counts = dict(zip(FLAG_COL.values(), flags[1:5]))

    rows, inp = [], raw_rows
    for rid, _check, cond, action, reason in RULES:
        if rid in QUARANTINE_IDS:
            n = q.get(rid, 0)
            rows.append([rid, cond, action, reason, inp, inp - n, n, 0])
            inp -= n
        else:
            rows.append([rid, cond, action, reason, retained_final, retained_final, 0,
                         int(f_counts[FLAG_COL[rid]] or 0)])
    quarantined = sum(q.values())
    rows.append(["TOTAL", "all rules; a row is quarantined by its first failing rule only",
                 "retained + quarantined = input",
                 "flags never remove rows; flagged_rows counts rows with at least one flag",
                 raw_rows, retained_final, quarantined, any_flag])

    # Count conservation (T2): stop the pipeline if it fails.
    assert inp == retained_final, f"sequential retained {inp} != table retained {retained_final}"
    assert raw_rows == retained_final + quarantined, "input != retained + quarantined"
    df = pd.DataFrame(rows, columns=REPORT_COLS)
    write_csv_atomic(df, cfg["paths"]["evidence"] / "quality_report.csv")
    return df


def run(cfg: dict, con=None) -> dict:
    ensure_dirs(cfg)
    con = con or connect(cfg)
    store.write_zone_dim(con, cfg)          # registers view `zones` (+ dim parquet in full mode)
    raw_rows = label_trips(con, cfg)
    report = quality_report(con, cfg, raw_rows)
    store.write_clean(con, cfg)
    tot = report.iloc[-1]
    log("clean", f"input={raw_rows:,} retained={tot.retained_rows:,} quarantined={tot.quarantined_rows:,} "
                 f"flagged={tot.flagged_rows:,}  (input = retained + quarantined ✓)")
    return {"input_rows": int(raw_rows), "retained_rows": int(tot.retained_rows),
            "quarantined_rows": int(tot.quarantined_rows), "report": report}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
