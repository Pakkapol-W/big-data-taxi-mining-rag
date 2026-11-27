"""Brief §07/§09 T10 (automated part) - dashboard values agree with the outputs; page is offline.

Checks
  offline   every <script src>/<link href> in index.html is a relative local file that exists; app.js makes
            no network calls (fetch / XMLHttpRequest / http URLs)
  trace     values the dashboard shows by default, recomputed from assets/data.js exactly as app.js does,
            equal the values in the output files (traced: source file -> data.js -> chart/tile)
  narrative every number in the findings / recommendations / limitations appears in its cited evidence
Output: evidence/integration_check.json (fails the pipeline on any mismatch)
Run   : python -m src.check_dashboard --mode smoke|full
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime

import numpy as np
import pandas as pd

from src import evidence
from src.common import load_config, log, sha256_file
from src.modelling_io import rel, write_json_atomic
from src.rag_verify import numbers, supported


def load_data(dash) -> dict:
    txt = (dash / "assets" / "data.js").read_text(encoding="utf-8")
    return json.loads(txt[txt.index("window.DASH = ") + len("window.DASH = "):].rstrip().rstrip(";"))


def offline_checks(dash) -> list[dict]:
    html = (dash / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', html)
    app = (dash / "assets" / "app.js").read_text(encoding="utf-8")
    remote = [r for r in refs if re.match(r"^(https?:)?//", r)]
    missing = [r for r in refs if not remote.count(r) and not (dash / r).exists()]
    net = [k for k in ("fetch(", "XMLHttpRequest", "http://", "https://", "import(") if k in app]
    return [
        {"check": "all script/style references are local files", "refs": refs, "remote": remote, "missing": missing,
         "status": "PASS" if refs and not remote and not missing else "FAIL"},
        {"check": "app.js makes no network calls", "found": net, "status": "PASS" if not net else "FAIL"},
        {"check": "vendored chart library hash recorded", "file": "dashboard/assets/echarts.min.js",
         "sha256": sha256_file(dash / "assets" / "echarts.min.js"), "status": "PASS"},
    ]


def trace(name, shown, source, recomputed, tol=0.0) -> dict:
    ok = (abs(float(shown) - float(recomputed)) <= tol) if isinstance(shown, (int, float)) else shown == recomputed
    return {"value": name, "dashboard": shown, "source_file": source, "source_value": recomputed,
            "tolerance": tol, "status": "PASS" if ok else "FAIL"}


def trace_checks(cfg: dict, d: dict) -> list[dict]:
    out, ev = cfg["paths"]["outputs"], cfg["paths"]["evidence"]
    rows = []
    # 1. default selection pickups (selection summary + chart 2a) = zone_hour.csv
    zh = pd.read_csv(out / "zone_hour.csv")
    shown = sum(v for a in d["counts"].values() for v in a if v is not None)
    rows.append(trace("Recorded pickups, default selection (filter bar, Chart 2a)", shown, rel(out / "zone_hour.csv"),
                      int(zh["pickup_count"].sum())))
    # 2. quality totals (Chart 1, KPI tiles) = quality_report.csv
    qr = pd.read_csv(ev / "quality_report.csv").set_index("rule_id")
    q = d["quality"]["total"]
    for k, col in (("input", "input_rows"), ("retained", "retained_rows"), ("quarantined", "quarantined_rows")):
        rows.append(trace(f"Quality total {k} (Chart 1, KPI)", q[k], rel(ev / "quality_report.csv"), int(qr.loc["TOTAL", col])))
    rows.append(trace("input = retained + quarantined (Chart 1)", q["input"], "derived", q["retained"] + q["quarantined"]))
    # 3. forecast MAE recomputed from the arrays charts 5/6 use = metrics.json (y_pred stored at 4 dp)
    met = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    if d["forecast"]["zones"]:
        y = np.concatenate([np.array(v["y"], float) for v in d["forecast"]["zones"].values()])
        p = np.concatenate([np.array(v["p"], float) for v in d["forecast"]["zones"].values()])
        b = np.concatenate([np.array(v["b"], float) for v in d["forecast"]["zones"].values()])
        rows.append(trace("June model MAE, all zones (Chart 6 selection tile)", round(float(np.mean(np.abs(y - p))), 4),
                          rel(out / "metrics.json"), met["test"]["mae_model"], tol=5e-4))
        rows.append(trace("June baseline MAE, all zones (Chart 6 selection tile)", round(float(np.mean(np.abs(y - b))), 4),
                          rel(out / "metrics.json"), met["test"]["mae_baseline"], tol=1e-4))
        pz = {r["zone_id"]: r["mae_model"] for r in met["per_zone"]}
        worst = max(d["forecast"]["zones"], key=lambda z: np.mean(np.abs(np.array(d["forecast"]["zones"][z]["y"], float)
                                                                         - np.array(d["forecast"]["zones"][z]["p"], float))))
        rows.append(trace("Worst zone in Chart 6 (top bar)", int(worst), rel(out / "metrics.json"), met["worst_zone"]["zone_id"]))
        w = d["forecast"]["zones"][worst]
        rows.append(trace(f"Chart 6 MAE of zone {worst}", round(float(np.mean(np.abs(np.array(w["y"], float) - np.array(w["p"], float)))), 4),
                          rel(out / "metrics.json"), pz[int(worst)], tol=5e-4))
    # 4. cluster sizes (Chart 4) = clusters.csv
    cl = pd.read_csv(out / "clusters.csv")["cluster_id"].value_counts().sort_index().tolist()
    rows.append(trace("Cluster sizes (Chart 4)", [c["size"] for c in d["clusters"]["clusters"]], rel(out / "clusters.csv"), cl))
    # 5. top-20 share tile = eda_summary.json
    eda = json.loads((out / "eda_summary.json").read_text(encoding="utf-8"))
    rows.append(trace("Top-20 share of pickups (KPI)", d["eda"]["top20_share"], rel(out / "eda_summary.json"),
                      eda["zones"]["top20_share_of_all_pickups"]))
    return rows


def narrative_checks(cfg: dict, d: dict) -> list[dict]:
    corpus = {c["evidence_id"]: c for c in evidence.load(cfg)}
    res = []
    for part in ("findings", "recommendations", "limitations"):
        for i, item in enumerate(d["narrative"][part], 1):
            pool = [n for e in item["evidence"] if e in corpus for n in numbers(corpus[e]["text"] + " " + corpus[e]["metric_definition"])]
            bad_ids = [e for e in item["evidence"] if e not in corpus]
            nums = [n for n in numbers(item["text"]) if n not in (2024,)]
            unsup = sorted({n for n in nums if not supported(n, pool)})
            res.append({"item": f"{part} {i}", "evidence": item["evidence"], "unknown_ids": bad_ids,
                        "unsupported_numbers": unsup, "status": "PASS" if not bad_ids and not unsup else "FAIL"})
    return res


def run(cfg: dict) -> dict:
    dash = cfg["paths"]["dashboard"]
    d = load_data(dash)
    result = {"generated_at": datetime.now().isoformat(timespec="seconds"), "mode": cfg["mode"],
              "page": rel(dash / "index.html"), "offline": offline_checks(dash), "traced_values": trace_checks(cfg, d),
              "narrative": narrative_checks(cfg, d),
              "manual_T9": ["Open dashboard/index.html from disk with Wi-Fi off: all charts render",
                            "Zone filter: pick 'Cluster 1' -> charts 2a, 2b, 3, 5, 6 and the selection summary update",
                            "Date filter: 'Apr' -> charts 5/6 show the June-only empty state; 'Jun' restores them",
                            "'None' zones -> every filtered chart shows the empty-state message; 'Reset' restores all",
                            "Fixed charts 1 and 4 are labelled and do not change"]}
    allc = result["offline"] + result["traced_values"] + result["narrative"]
    fails = [c for c in allc if c["status"] != "PASS"]
    result["summary"] = {"passed": len(allc) - len(fails), "total": len(allc)}
    write_json_atomic(result, cfg["paths"]["evidence"] / "integration_check.json")
    log("check_dashboard", f"{result['summary']['passed']}/{result['summary']['total']} checks pass -> "
                           f"{rel(cfg['paths']['evidence'] / 'integration_check.json')}")
    if fails:
        raise AssertionError("dashboard check failed: " + "; ".join(c.get("value") or c.get("check") or c.get("item") for c in fails))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="full")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
