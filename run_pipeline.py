"""Entry point.

    python run_pipeline.py --config config.json --mode smoke   # 20k sample, seconds; no zero-fill
    python run_pipeline.py --config config.json --mode full    # all 3 months, official outputs

Stages: acquire (full only) -> clean -> aggregate (A vs B benchmark) -> zone_hour grid.
Any failed check raises and exits with a non-zero code.
Teammates append their stages (clustering, model, RAG) after `zone_hour`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from src import acquire, aggregate, clean, store
from src.common import connect, ensure_dirs, load_config, log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], required=True)
    args = ap.parse_args(argv)
    cfg = load_config(args.config, args.mode)
    ensure_dirs(cfg)
    t0 = time.perf_counter()
    log("pipeline", f"mode={cfg['mode']}  outputs -> {cfg['paths']['outputs']}")

    if cfg["mode"] == "full":
        acquire.run(cfg)
    elif not cfg["paths"]["sample"].exists():
        raise FileNotFoundError(f"smoke mode needs {cfg['paths']['sample']} (shipped in the ZIP)")

    con = connect(cfg)
    c = clean.run(cfg, con)
    counts, agg = aggregate.run(cfg, retained_rows=c["retained_rows"])
    zh = store.build_zone_hour(con, counts, cfg)
    top = store.select_top_zones(zh, cfg)
    out = store.write_outputs(con, zh, top, cfg)

    report = c["report"]
    summary = {
        "mode": cfg["mode"],
        "input_rows": c["input_rows"], "retained_rows": c["retained_rows"],
        "quarantined_rows": c["quarantined_rows"],
        "quarantined_by_rule": {r.rule_id: int(r.quarantined_rows) for r in report.itertuples()
                                if r.rule_id in clean.QUARANTINE_IDS},
        "flagged_by_rule": {r.rule_id: int(r.flagged_rows) for r in report.itertuples()
                            if r.rule_id in clean.FLAG_COL},
        "rows_with_any_flag": int(report.iloc[-1].flagged_rows),
        "aggregation": agg,
        "zone_hour": {k: v for k, v in out.items() if k != "top_zones"},
        "top_zones": out["top_zones"],
        "zero_fill": "only where coverage_ok (full mode, all months, hour has >=1 city-wide trip); "
                     "0 = no recorded pickup, not proof of zero demand",
        "seconds_total": round(time.perf_counter() - t0, 1),
    }
    (cfg["paths"]["evidence"] / "pipeline_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    if cfg["mode"] == "smoke":
        log("pipeline", "SMOKE checks passed (cleaning/aggregation only; says nothing about model accuracy)")
    log("pipeline", f"done in {summary['seconds_total']}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # explicit failure, non-zero exit
        print(f"[pipeline] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
