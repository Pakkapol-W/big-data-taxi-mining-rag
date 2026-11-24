"""Shared helpers: config loading, paths, hashing, DuckDB connection, atomic writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
# Smoke never overwrites full results. clean/quarantine go to data/smoke/ (siblings, not children,
# so a full run's "replace data/clean" can never touch smoke output and vice versa).
SMOKE_REDIRECT = {"clean": "data/smoke/clean", "quarantine": "data/smoke/quarantine",
                  "outputs": "outputs/smoke", "evidence": "evidence/smoke"}


def load_config(path: str | Path = "config.json", mode: str = "full") -> dict:
    if mode not in ("smoke", "full"):
        raise ValueError(f"mode must be 'smoke' or 'full', got {mode!r}")
    cfg_path = Path(path) if Path(path).is_absolute() else ROOT / path
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["mode"] = mode
    paths = {k: ROOT / v for k, v in cfg["paths"].items()}
    if mode == "smoke":
        for k, v in SMOKE_REDIRECT.items():
            paths[k] = ROOT / v
    cfg["paths"] = paths
    cfg["duckdb"]["temp_directory"] = str(ROOT / cfg["duckdb"]["temp_directory"])
    return cfg


def ensure_dirs(cfg: dict) -> None:
    for k in ("raw", "clean", "quarantine", "dim", "outputs", "evidence"):
        cfg["paths"][k].mkdir(parents=True, exist_ok=True)


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def connect(cfg: dict) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB; temp_directory lets the duplicate window spill to disk."""
    db = cfg["duckdb"]
    Path(db["temp_directory"]).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads = {int(db['threads'])}")
    con.execute(f"SET memory_limit = '{db['memory_limit']}'")
    con.execute(f"SET temp_directory = '{db['temp_directory']}'")
    return con


def write_csv_atomic(df: pd.DataFrame, path: str | Path) -> None:
    """Write to path.tmp then rename, so a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False, lineterminator="\n")
    os.replace(tmp, path)


def log(stage: str, msg: str) -> None:
    print(f"[{stage}] {msg}", flush=True)
