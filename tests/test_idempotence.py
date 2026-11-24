"""T3 — idempotent reruns: the same configuration gives identical results."""
import pandas as pd

from src import acquire, aggregate, clean, store
from src.common import connect, sha256_file, write_csv_atomic


def _run_once(cfg) -> dict[str, str]:
    con = connect(cfg)
    c = clean.run(cfg, con)
    counts, _ = aggregate.run(cfg, retained_rows=c["retained_rows"])
    zh = store.build_zone_hour(con, counts, cfg)
    store.write_outputs(con, zh, store.select_top_zones(zh, cfg), cfg)
    out, ev = cfg["paths"]["outputs"], cfg["paths"]["evidence"]
    files = [out / "zone_hour.csv", out / "zone_hour_all.parquet", out / "top_zones.csv",
             ev / "quality_report.csv", cfg["paths"]["manifest"]]
    files += sorted(cfg["paths"]["clean"].glob("*/*.parquet"))
    return {f.name if f.parent.name in ("outputs", "evidence") else str(f.relative_to(cfg["paths"]["raw"].parent)):
            sha256_file(f) for f in files}


def test_T3_rerun_identical(cfg, record_property):
    first = _run_once(cfg)
    # rebuild the manifest exactly as a rerun of acquire does (never append)
    old = acquire.read_manifest(cfg["paths"]["manifest"])
    rows = [{**r, "bytes": int(r["bytes"]), "rows": int(r["rows"])} for r in old.values()]
    write_csv_atomic(acquire.build_manifest(rows, old), cfg["paths"]["manifest"])
    second = _run_once(cfg)
    assert first == second
    assert len(pd.read_csv(cfg["paths"]["manifest"])) == 4
    record_property("actual", f"{len(first)} output files byte-identical across 2 runs; manifest still 4 rows")


def test_T3_zero_fill_only_with_coverage(cfg):
    con = connect(cfg)
    c = clean.run(cfg, con)
    counts, _ = aggregate.run(cfg, retained_rows=c["retained_rows"])
    zh = store.build_zone_hour(con, counts, cfg)
    covered = zh[zh["coverage_ok"]]
    uncovered = zh[~zh["coverage_ok"]]
    assert covered["pickup_count"].notna().all()            # zero-filled where coverage confirmed
    assert uncovered["pickup_count"].isna().all()           # unknown elsewhere (fixture has sparse hours)
    # smoke mode never zero-fills
    cfg_smoke = dict(cfg, mode="smoke")
    zh_s = store.build_zone_hour(con, counts, cfg_smoke)
    assert not zh_s["coverage_ok"].any()
    assert zh_s["pickup_count"].isna().sum() == len(zh_s) - len(counts)
