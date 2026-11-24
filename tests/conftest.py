"""Tiny hand-made fixture: 19 trips across 3 monthly files + a 7-zone lookup.

Row  file  what                                   expected
 1-3 Apr   zone 1, 2024-04-02 08:xx               retained  -> (1, 04-02 08:00) = 3
 4   Apr   pickup NULL                            R1
 5   Apr   pickup 2024-03-15                      R2
 6   Apr   zone 999 (not in lookup)               R3, unmatched
 7   Apr   zone 264                               R3
 8   Apr   zone 2, 04-03 10:10                    retained  -> (2, 04-03 10:00)
 9   Apr   exact copy of row 8                    R4
10   Apr   pickup 2009 AND zone 999               R2 only (first failing rule), unmatched
11   Apr   zone 2, 04-03 10:30, dropoff < pickup  retained + f_order -> (2, 04-03 10:00) = 2
12   Apr   zone 3, distance 0 AND fare -5         retained + f_distance + f_fare (two flags, one row)
13   Apr   zone 3, NULL passenger_count etc.      retained + f_missing -> (3, 04-05 12:00) = 2
14   Apr   pickup 2024-05-01 00:10 (May in Apr file) retained, partition 2024-05
15   May   zone 1, 05-10 09:00                    retained
16   Jun   zone 2, 06-20 18:00                    retained
17   Jun   pickup 2024-07-01 00:00 (end excluded) R2
18   Apr   zone 4, 04-06 09:00, total +25         retained (original)
19   Apr   same trip, total -25 (reversal)        R4b
Totals: input 19 = retained 11 + quarantined 8 (R1 1, R2 3, R3 2, R4 1, R4b 1); rows with any flag 3.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.common import ROOT

SCHEMA = pa.schema([
    ("VendorID", pa.int32()), ("tpep_pickup_datetime", pa.timestamp("us")),
    ("tpep_dropoff_datetime", pa.timestamp("us")), ("passenger_count", pa.int64()),
    ("trip_distance", pa.float64()), ("RatecodeID", pa.int64()), ("store_and_fwd_flag", pa.large_string()),
    ("PULocationID", pa.int32()), ("DOLocationID", pa.int32()), ("payment_type", pa.int64()),
    ("fare_amount", pa.float64()), ("extra", pa.float64()), ("mta_tax", pa.float64()),
    ("tip_amount", pa.float64()), ("tolls_amount", pa.float64()), ("improvement_surcharge", pa.float64()),
    ("total_amount", pa.float64()), ("congestion_surcharge", pa.float64()), ("Airport_fee", pa.float64()),
])

HAND_COUNTS = {  # (zone_id, hour) -> retained pickups, counted by hand from the table above
    (1, "2024-04-02 08:00:00"): 3, (2, "2024-04-03 10:00:00"): 2, (3, "2024-04-05 12:00:00"): 2,
    (4, "2024-04-06 09:00:00"): 1, (1, "2024-05-01 00:00:00"): 1, (1, "2024-05-10 09:00:00"): 1,
    (2, "2024-06-20 18:00:00"): 1,
}
EXPECTED = {"input": 19, "retained": 11, "quarantined": 8,
            "R1": 1, "R2": 3, "R3": 2, "R4": 1, "R4b": 1, "any_flag": 3, "flag_sum": 4}


def ts(s):
    return None if s is None else datetime.fromisoformat(s)


def trip(pickup, dropoff=None, zone=1, **kw):
    """Base valid trip; override any column with kw."""
    p = ts(pickup)
    row = {"VendorID": 2, "tpep_pickup_datetime": p,
           "tpep_dropoff_datetime": ts(dropoff) if dropoff else (p + pd.Timedelta(minutes=12) if p else ts("2024-04-02 09:00:00")),
           "passenger_count": 1, "trip_distance": 2.0, "RatecodeID": 1, "store_and_fwd_flag": "N",
           "PULocationID": zone, "DOLocationID": 2, "payment_type": 1, "fare_amount": 10.0, "extra": 0.0,
           "mta_tax": 0.5, "tip_amount": 2.0, "tolls_amount": 0.0, "improvement_surcharge": 1.0,
           "total_amount": 13.5, "congestion_surcharge": 2.5, "Airport_fee": 0.0}
    row.update(kw)
    return row


def fixture_files() -> dict[str, list[dict]]:
    dup = trip("2024-04-03 10:10:00", zone=2, trip_distance=5.0)
    rev = dict(pickup="2024-04-06 09:00:00", dropoff="2024-04-06 09:20:00", zone=4)
    april = [
        trip("2024-04-02 08:05:00"), trip("2024-04-02 08:15:00", trip_distance=3.0),
        trip("2024-04-02 08:59:59", trip_distance=4.0),                                        # 1-3
        trip(None),                                                                            # 4  R1
        trip("2024-03-15 10:00:00"),                                                           # 5  R2
        trip("2024-04-02 11:00:00", zone=999),                                                 # 6  R3
        trip("2024-04-02 12:00:00", zone=264),                                                 # 7  R3
        dup, dict(dup),                                                                        # 8, 9 R4
        trip("2009-01-01 10:00:00", zone=999),                                                 # 10 R2
        trip("2024-04-03 10:30:00", dropoff="2024-04-03 10:00:00", zone=2),                     # 11 f_order
        trip("2024-04-05 12:00:00", zone=3, trip_distance=0.0, fare_amount=-5.0, total_amount=-5.0),  # 12
        trip("2024-04-05 12:30:00", zone=3, passenger_count=None, RatecodeID=None,
             store_and_fwd_flag=None, congestion_surcharge=None, Airport_fee=None),            # 13 f_missing
        trip("2024-05-01 00:10:00"),                                                           # 14
        trip(rev["pickup"], rev["dropoff"], zone=4, fare_amount=20.0, total_amount=25.0),      # 18 original
        trip(rev["pickup"], rev["dropoff"], zone=4, fare_amount=-20.0, total_amount=-25.0,
             payment_type=4),                                                                  # 19 R4b
    ]
    may = [trip("2024-05-10 09:00:00")]                                                        # 15
    june = [trip("2024-06-20 18:00:00", zone=2), trip("2024-07-01 00:00:00")]                  # 16, 17
    return {"2024-04": april, "2024-05": may, "2024-06": june}


def make_cfg(tmp_path, mode="full") -> dict:
    cfg = json.loads((ROOT / "config.json").read_text())
    cfg["mode"] = mode
    names = {"raw": "raw", "clean": "clean", "quarantine": "quarantine", "dim": "dim", "outputs": "outputs",
             "evidence": "evidence", "manifest": "data_manifest.csv", "sample": "sample.parquet"}
    cfg["paths"] = {k: tmp_path / v for k, v in names.items()}
    cfg["duckdb"] = {"threads": 2, "memory_limit": "1GB", "temp_directory": str(tmp_path / "duck_tmp")}
    cfg["benchmark"] = {"repeats": 1, "warmup": 0, "batch_rows": 2}   # tiny splits: keys cross batches
    for k in ("raw", "clean", "quarantine", "dim", "outputs", "evidence"):
        cfg["paths"][k].mkdir(parents=True, exist_ok=True)
    return cfg


def write_fixture(cfg: dict, zone_ids=(1, 2, 3, 4, 5, 264, 265)) -> None:
    from src import acquire
    raw = cfg["paths"]["raw"]
    for month, rows in fixture_files().items():
        pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), raw / cfg["file_pattern"].format(month=month))
    pd.DataFrame({"LocationID": list(zone_ids), "Borough": "B", "Zone": [f"Z{z}" for z in zone_ids],
                  "service_zone": "S"}).to_csv(raw / "taxi_zone_lookup.csv", index=False)
    rows = []
    for name, url in acquire.sources(cfg):
        info = acquire.inspect_file(raw / name)
        rows.append({"file": name, "url": url, "bytes": info["bytes"], "rows": info["rows"],
                     "sha256": info["sha256"], "schema_version": "fixture", "notes": ""})
    from src.common import write_csv_atomic
    write_csv_atomic(acquire.build_manifest(rows, {}), cfg["paths"]["manifest"])


@pytest.fixture
def cfg(tmp_path):
    c = make_cfg(tmp_path)
    write_fixture(c)
    return c


@pytest.fixture
def cleaned(cfg):
    """Fixture after the clean stage: (cfg, connection, clean.run result)."""
    from src import clean
    from src.common import connect
    con = connect(cfg)
    res = clean.run(cfg, con)
    return cfg, con, res


@pytest.fixture
def clone():
    return copy.deepcopy
