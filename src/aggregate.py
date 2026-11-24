"""Brief §04 item 5 — Compute: two equivalent zone-hour aggregations + repeated benchmark.

Method A (global):  one DuckDB GROUP BY (zone_id, hour) over all clean partitions.
Method B (local, then combine): Pandas, file by file and batch by batch.
    MAP      each trip -> key (zone_id, floor(pickup, hour))
    COMBINER per batch: groupby(key).size()   -> partial counts (one map task = one batch)
    SHUFFLE  concat all partials (same keys from different batches/files meet)
    REDUCE   groupby(key).sum()               -> final counts
A single-machine experiment, not a cluster benchmark.

Run:  python -m src.aggregate --mode smoke|full
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.common import connect, load_config, log, write_csv_atomic

LOG_COLS = ["experiment_id", "stage", "data_hash", "config", "seed", "hardware", "repeat",
            "seconds", "intermediate_rows", "metric", "value", "output_path"]


def clean_files(cfg: dict) -> list[Path]:
    files = sorted(cfg["paths"]["clean"].glob("pickup_month=*/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no clean partitions under {cfg['paths']['clean']} — run clean first")
    return files


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Same column types/order for both methods (fix #4), sorted by key."""
    out = pd.DataFrame({
        "zone_id": df["zone_id"].astype("int32"),
        "hour": pd.to_datetime(df["hour"]).astype("datetime64[us]"),
        "pickup_count": df["pickup_count"].astype("int64"),
    })
    return out.sort_values(["zone_id", "hour"], kind="mergesort").reset_index(drop=True)


def method_a(con, files: list[Path]) -> tuple[pd.DataFrame, int, int]:
    """Global aggregation. Intermediate records = every input row flows into the one GROUP BY."""
    flist = [str(f) for f in files]
    df = con.execute(f"""
        SELECT PULocationID AS zone_id, date_trunc('hour', tpep_pickup_datetime) AS hour,
               count(*) AS pickup_count
        FROM read_parquet({flist})
        GROUP BY ALL""").df()
    df = normalize(df)
    return df, int(df["pickup_count"].sum()), 1


def method_b(files: list[Path], batch_rows: int) -> tuple[pd.DataFrame, int, int]:
    """Local count per batch (combiner), then shuffle + reduce. Returns (result, intermediate, tasks).

    Each file is cut into fixed `batch_rows` splits by row offset (like HDFS blocks), so the number
    of map tasks and intermediate records does not depend on how the Parquet writer chose row groups.
    Only two columns are read, one file at a time.
    """
    partials = []
    for f in files:
        table = pq.read_table(f, columns=["PULocationID", "tpep_pickup_datetime"])
        for off in range(0, table.num_rows, batch_rows):
            b = table.slice(off, batch_rows).to_pandas()
            key_hour = b["tpep_pickup_datetime"].dt.floor("h")                      # MAP
            partials.append(b.groupby([b["PULocationID"], key_hour]).size())        # COMBINER
    intermediate = sum(len(p) for p in partials)
    shuffled = pd.concat(partials)                                                  # SHUFFLE
    reduced = shuffled.groupby(level=[0, 1]).sum()                                  # REDUCE
    df = reduced.rename("pickup_count").reset_index()
    df.columns = ["zone_id", "hour", "pickup_count"]
    return normalize(df), intermediate, len(partials)


def assert_equivalent(a: pd.DataFrame, b: pd.DataFrame, retained_rows: int | None) -> None:
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    assert not a.duplicated(["zone_id", "hour"]).any(), "duplicate (zone_id, hour) keys"
    if retained_rows is not None:
        assert int(a["pickup_count"].sum()) == retained_rows, \
            f"sum of counts {a['pickup_count'].sum()} != retained rows {retained_rows}"


# ---------------------------------------------------------------- benchmark metadata
def hardware() -> str:
    try:
        ram = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()) / 2**30
        cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
    except Exception:  # non-macOS
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
        cpu = platform.processor() or platform.machine()
    return f"{cpu}; {os.cpu_count()} cores; {ram:.0f} GB RAM; {platform.system()} {platform.release()}"


def data_hash(cfg: dict) -> str:
    """Hash of the input identity: raw manifest hashes (full) or sample file (smoke)."""
    if cfg["mode"] == "smoke":
        from src.common import sha256_file
        return "sample:" + sha256_file(cfg["paths"]["sample"])[:16]
    man = pd.read_csv(cfg["paths"]["manifest"], dtype=str).sort_values("file")
    return "raw:" + hashlib.sha256("".join(man["sha256"]).encode()).hexdigest()[:16]


def benchmark(cfg: dict, retained_rows: int | None = None) -> tuple[pd.DataFrame, dict]:
    """Warm-up (logged, excluded from median), then `repeats` interleaved runs of A and B."""
    files = clean_files(cfg)
    bcfg = cfg["benchmark"]
    # Smoke files hold ~7k rows each, so 500k-row splits would give one map task per monthly file:
    # no key would cross tasks and the reduce step would merge nothing. Smaller splits exercise it.
    batch_rows = bcfg.get("smoke_batch_rows", bcfg["batch_rows"]) if cfg["mode"] == "smoke" else bcfg["batch_rows"]
    con = connect(cfg)
    threads = con.execute("SELECT current_setting('threads')").fetchall()[0][0]
    confs = {
        "A": f"duckdb {duckdb.__version__}; threads={threads}; one global GROUP BY over {len(files)} files",
        "B": f"pandas {pd.__version__} + pyarrow {pa.__version__}; single process; per-file batches of "
             f"{batch_rows:,} rows (map+combine), concat (shuffle), groupby-sum (reduce)",
    }
    run = {"A": lambda: method_a(con, files), "B": lambda: method_b(files, batch_rows)}
    common = {"stage": "aggregate", "data_hash": data_hash(cfg), "seed": cfg["seed"], "hardware": hardware()}
    out_rel = str(Path(cfg["paths"]["outputs"].name) / "zone_hour_all.parquet")

    rows, times, results, inter, tasks = [], {"A": [], "B": []}, {}, {}, {}
    schedule = [("warmup", m) for _ in range(bcfg["warmup"]) for m in "AB"] + \
               [(r, m) for r in range(1, bcfg["repeats"] + 1) for m in "AB"]
    for rep, m in schedule:
        t0 = time.perf_counter()
        df, n_inter, n_tasks = run[m]()
        sec = time.perf_counter() - t0
        results[m], inter[m], tasks[m] = df, n_inter, n_tasks
        if rep != "warmup":
            times[m].append(sec)
        rows.append({**common, "experiment_id": f"AGG-{m}-{rep}", "config": confs[m], "repeat": rep,
                     "seconds": round(sec, 4), "intermediate_rows": n_inter,
                     "metric": "warmup_seconds" if rep == "warmup" else "seconds",
                     "value": round(sec, 4), "output_path": out_rel})
        log("aggregate", f"{m} {rep}: {sec:.3f}s intermediate={n_inter:,}")

    assert_equivalent(results["A"], results["B"], retained_rows)
    merged = inter["B"] - len(results["A"])  # partial rows the reduce step had to merge
    if cfg["mode"] == "smoke":
        assert merged > 0, "smoke: no key crossed map tasks, so combine/reduce did no work; lower smoke_batch_rows"
    meds ={m: statistics.median(times[m]) for m in "AB"}
    for m in "AB":
        rows.append({**common, "experiment_id": f"AGG-{m}-median", "config": confs[m], "repeat": "median",
                     "seconds": round(meds[m], 4), "intermediate_rows": inter[m], "metric": "median_seconds",
                     "value": round(meds[m], 4), "output_path": out_rel})
    rows.append({**common, "experiment_id": "AGG-equivalence", "config": "assert_frame_equal(A, B), exact",
                 "repeat": "", "seconds": "", "intermediate_rows": "", "metric": "identical_output_keys",
                 "value": len(results["A"]), "output_path": out_rel})
    write_csv_atomic(pd.DataFrame(rows, columns=LOG_COLS), cfg["paths"]["evidence"] / "experiment_log.csv")

    summary = {"median_seconds": {m: round(meds[m], 4) for m in "AB"},
               "intermediate_rows": inter, "map_tasks": tasks, "output_keys": len(results["A"]),
               "b_batch_rows": batch_rows, "b_rows_merged_by_reduce": merged, "equivalent": True}
    log("aggregate", f"A == B ✓  keys={len(results['A']):,}  median A={meds['A']:.3f}s  B={meds['B']:.3f}s  "
                     f"B intermediate={inter['B']:,} from {tasks['B']} map tasks "
                     f"(reduce merged {merged:,})")
    return results["A"], summary


def run(cfg: dict, retained_rows: int | None = None) -> tuple[pd.DataFrame, dict]:
    return benchmark(cfg, retained_rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
