"""Shared I/O for the Modelling / LLM / Presentation stages (brief §05-§07).

Every downstream stage reads files written by earlier stages (never in-memory state), so each
stage can be rerun alone:  python -m src.<stage> --mode smoke|full
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from src.common import ROOT, sha256_file, write_csv_atomic

LOG_COLS = ["experiment_id", "stage", "data_hash", "config", "seed", "hardware", "repeat",
            "seconds", "intermediate_rows", "metric", "value", "output_path"]


def rel(path: Path) -> str:
    """Repo-relative POSIX path for logs/evidence (identical on Windows and macOS)."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def load_zone_hour(cfg: dict) -> pd.DataFrame:
    """outputs/zone_hour.csv -> typed frame. pickup_count stays nullable: NULL = unknown coverage."""
    path = cfg["paths"]["outputs"] / "zone_hour.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing - run the data/compute stages first")
    zh = pd.read_csv(path, dtype={"zone_id": "int64", "pickup_count": "Int64"})
    zh["hour"] = pd.to_datetime(zh["hour"], format="%Y-%m-%d %H:%M:%S")
    zh["coverage_ok"] = zh["coverage_ok"].astype(str).str.lower().eq("true")
    zh = zh.sort_values(["zone_id", "hour"], kind="mergesort").reset_index(drop=True)
    assert not zh.duplicated(["zone_id", "hour"]).any(), "duplicate (zone_id, hour) keys in zone_hour.csv"
    return zh


def zone_names(cfg: dict) -> pd.DataFrame:
    """zone_id -> Borough, Zone from the dimension table (names never stored on trips)."""
    dim = pd.read_parquet(cfg["paths"]["dim"] / "zones.parquet")
    return dim.rename(columns={"LocationID": "zone_id"})[["zone_id", "Borough", "Zone"]]


def data_hash(path: Path) -> str:
    return f"{Path(path).stem}:{sha256_file(path)[:16]}"


def month_of(ts: pd.Series) -> pd.Series:
    return ts.dt.strftime("%Y-%m")


def write_json_atomic(obj, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def replace_experiment_rows(cfg: dict, prefix: str, rows: list[dict]) -> None:
    """Drop this stage's old rows (experiment_id starts with `prefix`) and append the new ones,
    so reruns never duplicate entries and other stages' rows are kept."""
    path = cfg["paths"]["evidence"] / "experiment_log.csv"
    old = pd.read_csv(path, dtype=str, keep_default_na=False) if path.exists() else pd.DataFrame(columns=LOG_COLS)
    old = old[~old["experiment_id"].str.startswith(prefix)]
    new = pd.DataFrame(rows, columns=LOG_COLS).astype(str).replace({"nan": "", "None": ""})
    write_csv_atomic(pd.concat([old, new], ignore_index=True)[LOG_COLS], path)
