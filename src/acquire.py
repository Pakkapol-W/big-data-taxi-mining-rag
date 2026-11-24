"""Brief §04 item 1 — Acquire: download raw files, manifest, schema check, join check, sample.

Run:  python -m src.acquire --config config.json
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import requests

from src.common import connect, ensure_dirs, load_config, log, sha256_file, write_csv_atomic

MANIFEST_COLS = ["file", "url", "downloaded_at", "bytes", "rows", "sha256", "schema_version", "notes"]
ZONE_COLS = ["LocationID", "Borough", "Zone", "service_zone"]
# Dictionary types (TLC data dictionary): used only to *note* differences, not to fail.
EXPECTED_TYPES = {
    "VendorID": "int", "passenger_count": "int", "RatecodeID": "int", "PULocationID": "int",
    "DOLocationID": "int", "payment_type": "int", "store_and_fwd_flag": "string",
    "tpep_pickup_datetime": "timestamp", "tpep_dropoff_datetime": "timestamp",
}


class SchemaError(RuntimeError):
    """A required column is missing (T1)."""


class CoverageError(RuntimeError):
    """A configured month is missing (T1)."""


# ---------------------------------------------------------------- sources & download
def sources(cfg: dict) -> list[tuple[str, str]]:
    """(file name, url) for the 3 trip months + the zone lookup."""
    out = []
    for m in cfg["months"]:
        name = cfg["file_pattern"].format(month=m)
        out.append((name, cfg["base_url"] + name))
    out.append((Path(cfg["zone_url"]).name, cfg["zone_url"]))
    return out


def download(url: str, dest: Path, expected_sha: str | None = None, retries: int = 8) -> bool:
    """Download url -> dest. Returns False if skipped (already present and verified).

    Skip rule: file exists AND (its sha256 equals the manifest hash, or — no manifest yet —
    its size equals the server's Content-Length). Writes to .part and resumes with an HTTP
    Range request, then renames, so a half-finished file never looks complete.
    """
    if dest.exists():
        if expected_sha and sha256_file(dest) == expected_sha:
            return False
        if not expected_sha:
            remote = requests.head(url, timeout=30, allow_redirects=True).headers.get("Content-Length")
            if remote and int(remote) == dest.stat().st_size:
                return False
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with requests.get(url, stream=True, timeout=60, headers=headers) as r:
                if r.status_code == 416:  # .part already complete
                    break
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code} for {url}")
                mode = "ab" if r.status_code == 206 else "wb"  # 200 = server ignored Range
                with open(part, mode) as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            break
        except (requests.RequestException, OSError) as e:
            log("acquire", f"{dest.name}: attempt {attempt}/{retries} failed ({e}); resuming")
            time.sleep(min(30, 2 ** attempt))
    else:
        raise RuntimeError(f"download failed after {retries} attempts: {url}")
    if dest.exists():
        os.chmod(dest, 0o644)  # raw files are read-only; allow replacement of a bad copy
    os.replace(part, dest)
    return True


# ---------------------------------------------------------------- inspection & schema
def inspect_file(path: Path) -> dict:
    """bytes, rows, sha256, {column: type}. Parquet rows come from the footer metadata."""
    info = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    if path.suffix == ".parquet":
        pf = pq.ParquetFile(path)
        info["rows"] = pf.metadata.num_rows
        info["columns"] = {f.name: str(f.type) for f in pf.schema_arrow}
    else:
        df = pd.read_csv(path)
        info["rows"] = len(df)
        info["columns"] = {c: str(t) for c, t in df.dtypes.items()}
    return info


def _family(arrow_type: str) -> str:
    t = arrow_type.lower()
    if t.startswith("timestamp") or t.startswith("datetime"):
        return "timestamp"
    if "int" in t:
        return "int"
    if t in ("string", "large_string", "object", "str"):
        return "string"
    return "float"


def check_schema(name: str, columns: dict, required: list[str]) -> str:
    """Raise SchemaError if a required column is missing; return notes on type differences."""
    missing = [c for c in required if c not in columns]
    if missing:
        raise SchemaError(f"{name}: missing required columns {missing}")
    notes = [f"{c}={columns[c]} (dict: {want})" for c, want in EXPECTED_TYPES.items()
             if c in columns and _family(columns[c]) != want]
    extra = sorted(set(columns) - set(required))
    if extra:
        notes.append(f"extra columns: {extra}")
    return "; ".join(notes) or "matches dictionary"


def check_months(manifest: pd.DataFrame, cfg: dict) -> None:
    """Every configured month must be in the manifest and on disk (T1)."""
    want = {cfg["file_pattern"].format(month=m) for m in cfg["months"]}
    have = {f for f in manifest["file"] if (cfg["paths"]["raw"] / f).exists()}
    if want - have:
        raise CoverageError(f"missing months: {sorted(want - have)} — official run stops")


# ---------------------------------------------------------------- manifest
def read_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {r["file"]: r for r in pd.read_csv(path, dtype=str).to_dict("records")}


def build_manifest(rows: list[dict], old: dict[str, dict]) -> pd.DataFrame:
    """Rebuild from scratch keyed by file (never append). Keep the old downloaded_at
    when the hash is unchanged, so a rerun produces a byte-identical manifest."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for r in rows:
        prev = old.get(r["file"])
        r["downloaded_at"] = prev["downloaded_at"] if prev and prev["sha256"] == r["sha256"] else now
        out[r["file"]] = r
    df = pd.DataFrame(list(out.values()), columns=MANIFEST_COLS).sort_values("file").reset_index(drop=True)
    assert df["file"].is_unique
    return df


# ---------------------------------------------------------------- join check & sample
def join_check(con, trip_files: list[Path], zone_csv: Path, cfg: dict) -> dict:
    """PULocationID -> LocationID: rows before/after (must be equal), unmatched, 264/265."""
    files = [str(f) for f in trip_files]
    con.execute(f"CREATE OR REPLACE TEMP VIEW zones AS SELECT * FROM read_csv('{zone_csv}', header=true)")
    z_rows, z_keys = con.execute("SELECT count(*), count(DISTINCT LocationID) FROM zones").fetchall()[0]
    if z_rows != z_keys:
        raise SchemaError(f"zone lookup LocationID not unique ({z_rows} rows, {z_keys} keys)")
    invalid = ",".join(str(int(z)) for z in cfg["invalid_zones"])
    r = con.execute(f"""
        WITH t AS (SELECT PULocationID FROM read_parquet({files}))
        SELECT (SELECT count(*) FROM t)                       AS rows_before,
               count(*)                                       AS rows_after_join,
               count(z.LocationID)                            AS matched,
               count(*) - count(z.LocationID)                 AS unmatched,
               count(*) FILTER (WHERE t.PULocationID IN ({invalid})) AS invalid_zone_rows
        FROM t LEFT JOIN zones z ON t.PULocationID = z.LocationID
    """).fetchall()[0]
    res = dict(zip(["rows_before", "rows_after_join", "matched", "unmatched", "invalid_zone_rows"], r))
    assert res["rows_before"] == res["rows_after_join"], "join inflated rows"
    res["unmatched_ids"] = [x[0] for x in con.execute(f"""
        SELECT DISTINCT PULocationID FROM read_parquet({files})
        WHERE PULocationID NOT IN (SELECT LocationID FROM zones) ORDER BY 1""").fetchall()]
    # Limitation evidence (fix #11): pickups whose month differs from their file's month.
    res["pickup_month_vs_file"] = [
        {"file": f, "pickup_month": m, "rows": n} for f, m, n in con.execute(f"""
        SELECT parse_filename(filename), strftime(tpep_pickup_datetime, '%Y-%m'), count(*)
        FROM read_parquet({files}, filename=true)
        WHERE tpep_pickup_datetime IS NULL
           OR strftime(tpep_pickup_datetime, '%Y-%m') <> regexp_extract(filename, '(\\d{{4}}-\\d{{2}})', 1)
        GROUP BY ALL ORDER BY 1, 2""").fetchall()]
    return res


def make_sample(con, trip_files: list[Path], cfg: dict) -> None:
    """20k-row reservoir sample, fixed seed. threads=1 so the seeded sample is reproducible (fix #6)."""
    files = [str(f) for f in trip_files]
    out = cfg["paths"]["sample"]
    tmp = out.with_suffix(".parquet.tmp")
    con.execute("SET threads = 1")
    try:
        con.execute(f"""
            COPY (
              SELECT * EXCLUDE (filename), parse_filename(filename) AS source_file
              FROM (SELECT * FROM read_parquet({files}, filename=true)
                    USING SAMPLE reservoir({int(cfg['sample_rows'])} ROWS) REPEATABLE ({int(cfg['seed'])}))
              ORDER BY ALL
            ) TO '{tmp}' (FORMAT parquet)""")
    finally:
        con.execute(f"SET threads = {int(cfg['duckdb']['threads'])}")
    os.replace(tmp, out)


# ---------------------------------------------------------------- orchestration
def run(cfg: dict) -> dict:
    ensure_dirs(cfg)
    raw, man_path = cfg["paths"]["raw"], cfg["paths"]["manifest"]
    old = read_manifest(man_path)
    rows = []
    for name, url in sources(cfg):
        dest = raw / name
        got = download(url, dest, (old.get(name) or {}).get("sha256"))
        log("acquire", f"{name}: {'downloaded' if got else 'present, verified — skipped'}")
        info = inspect_file(dest)
        is_zone = name.endswith(".csv")
        notes = check_schema(name, info["columns"], ZONE_COLS if is_zone else cfg["required_columns"])
        rows.append({"file": name, "url": url, "bytes": info["bytes"], "rows": info["rows"],
                     "sha256": info["sha256"], "notes": notes,
                     "schema_version": f"{'taxi_zone_lookup' if is_zone else 'yellow-2024'} ({len(info['columns'])} cols)"})
    manifest = build_manifest(rows, old)
    write_csv_atomic(manifest, man_path)
    check_months(manifest, cfg)

    trip_files = [raw / cfg["file_pattern"].format(month=m) for m in cfg["months"]]
    zone_csv = raw / Path(cfg["zone_url"]).name
    con = connect(cfg)
    check = join_check(con, trip_files, zone_csv, cfg)
    check["schemas"] = {r["file"]: r["notes"] for r in rows}
    (cfg["paths"]["evidence"] / "acquisition_check.json").write_text(json.dumps(check, indent=2, default=str))
    make_sample(con, trip_files, cfg)

    for f in [*trip_files, zone_csv]:
        os.chmod(f, 0o444)  # raw is read-only
    log("acquire", f"manifest rows={len(manifest)}  total trips={check['rows_before']:,}  "
                   f"unmatched={check['unmatched']}  zones 264/265={check['invalid_zone_rows']:,}")
    return check


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    run(load_config(ap.parse_args().config, mode="full"))
