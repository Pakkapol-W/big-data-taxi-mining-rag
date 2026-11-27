"""Brief §07 Dashboard data - build dashboard/assets/data.js from computed outputs only.

The page (dashboard/index.html + assets/app.js + assets/style.css + assets/echarts.min.js) is static and
opens offline from disk: data is a plain <script> (window.DASH = {...}), so no fetch, server or CDN.
Every number shown is read from these files or computed in the browser from them - never typed in:
  outputs/zone_hour.csv, outputs/top_zones.csv, evidence/quality_report.csv, outputs/eda_summary.json,
  outputs/clusters.csv, outputs/cluster_profiles.csv, outputs/cluster_selection.csv,
  outputs/predictions.csv, outputs/metrics.json, evidence/rag_questions.json, evidence/rag_evaluation.json,
  evidence/evidence_corpus.jsonl
Narrative text (findings / recommendations / limitations) is templated below with numbers filled from
those files; edit the wording here, not in the page.

Run: python -m src.dashboard --mode smoke|full   (smoke writes to outputs/smoke/dashboard)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime

import pandas as pd

from src import evidence
from src.common import ROOT, load_config, log
from src.modelling_io import load_zone_hour, rel, zone_names

STATIC = ["index.html", "assets/app.js", "assets/style.css", "assets/echarts.min.js", "assets/ECHARTS_LICENSE.txt"]


def _f(x, nd=4):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def narrative(eda: dict, clu: dict, met: dict) -> dict:
    t, z = eda["temporal"], eda["zones"]
    c_night = max(clu["clusters"], key=lambda c: c["share_22_02"])
    c_day = min(clu["clusters"], key=lambda c: c["share_22_02"])
    tm, w = met["test"], met["worst_zone"]
    return {
        "findings": [
            {"text": f"City-wide weekday pickups peak at {t['weekday_peak_hour']}:00 and bottom out at "
                     f"{t['weekday_trough_hour']:02d}:00; the peak hour averages {t['weekday_peak_to_trough']}x the trough.",
             "evidence": ["EV-EDA-TEMPORAL"]},
            {"text": f"A {c_night['size']}-zone night-life cluster ({', '.join(c_night['zones'])}) takes "
                     f"{c_night['share_22_02'] * 100:.1f}% of its pickups between 22:00 and 02:59, versus "
                     f"{c_day['share_22_02'] * 100:.1f}% for the other {c_day['size']} zones.",
             "evidence": [f"EV-CLU-{c_night['cluster_id']}", f"EV-CLU-{c_day['cluster_id']}"]},
            {"text": f"On the June test month the model's MAE is {tm['mae_model']:.2f} pickups per zone-hour versus "
                     f"{tm['mae_baseline']:.2f} for the same-hour-last-week baseline "
                     f"({tm['mae_improvement_vs_baseline'] * 100:.1f}% lower), better in "
                     f"{met['zones_model_beats_baseline']} of 20 zones; the largest error is at {w['zone']} "
                     f"(MAE {w['mae_model']:.1f}).",
             "evidence": ["EV-MOD-OVERALL", "EV-MOD-WORST"]},
        ],
        "recommendations": [
            {"text": f"If taxis are positioned to match recorded pickups, then schedule commuter-zone supply for the "
                     f"weekday 16:00-19:59 window and night-life-zone supply for 22:00-02:59, mostly at weekends - "
                     f"provided recorded pickups are an acceptable proxy, since unmet demand is not observed.",
             "evidence": ["EV-EDA-TEMPORAL", f"EV-CLU-{c_night['cluster_id']}"]},
            {"text": f"If a next-hour forecast is used for planning, then prefer the model to the weekly rule "
                     f"(MAE {tm['mae_model']:.1f} vs {tm['mae_baseline']:.1f}), but add a manual check for airport "
                     f"zones and public holidays, where the largest errors occur.",
             "evidence": ["EV-MOD-OVERALL", "EV-MOD-WORST"]},
        ],
        "limitations": [
            {"text": "The target is recorded yellow-taxi pickups: it excludes green taxis and app-based rides and "
                     "says nothing about people who could not get a taxi, so it is not demand.",
             "evidence": ["EV-DICT-DERIVED", "EV-SCOPE"]},
            {"text": f"One season (April-June 2024) with a single test month; trips near the window edges filed in "
                     f"the March or July files are missing, and holidays (e.g. the quietest day, "
                     f"{t['quietest_day']['date']}) are not modelled as features.",
             "evidence": ["EV-SCOPE", "EV-EDA-TEMPORAL"]},
        ],
    }


def build(cfg: dict) -> dict:
    out, ev = cfg["paths"]["outputs"], cfg["paths"]["evidence"]
    zh = load_zone_hour(cfg)
    top = pd.read_csv(out / "top_zones.csv")
    clusters = pd.read_csv(out / "clusters.csv")
    names = zone_names(cfg).set_index("zone_id")
    hours = sorted(zh["hour"].unique())
    start = pd.Timestamp(hours[0])
    assert len(hours) == int((pd.Timestamp(cfg["window"]["end"]) - start) / pd.Timedelta(hours=1)), "hour grid gap"

    zones = []
    for r in top.sort_values("rank").itertuples():
        cid = clusters.loc[clusters["zone_id"] == r.zone_id, "cluster_id"]
        zones.append({"id": int(r.zone_id), "name": r.Zone, "borough": r.Borough, "rank": int(r.rank),
                      "april_pickups": int(r.selection_pickups), "cluster": int(cid.iloc[0]) if len(cid) else None})
    counts = {}
    for zid, g in zh.groupby("zone_id"):
        counts[str(zid)] = [None if pd.isna(v) else int(v) for v in g.sort_values("hour")["pickup_count"]]

    qr = pd.read_csv(ev / "quality_report.csv")
    tot = qr[qr["rule_id"] == "TOTAL"].iloc[0]
    quality = {"rules": [{"id": r.rule_id, "condition": r.condition, "action": r.action,
                          "quarantined": int(r.quarantined_rows), "flagged": int(r.flagged_rows)}
                         for r in qr[qr["rule_id"] != "TOTAL"].itertuples()],
               "total": {"input": int(tot.input_rows), "retained": int(tot.retained_rows),
                         "quarantined": int(tot.quarantined_rows), "any_flag": int(tot.flagged_rows)}}

    prof = pd.read_csv(out / "cluster_profiles.csv")
    sel = pd.read_csv(out / "cluster_selection.csv")
    cl_summary = {"clusters": []}
    cent = prof[prof["kind"] == "centroid"]
    for cid, g in cent.groupby("cluster_id"):
        s = g.set_index("hour")["share"]
        mem = clusters[clusters["cluster_id"] == cid]["zone_id"].tolist()
        cl_summary["clusters"].append({"cluster_id": int(cid), "size": len(mem), "zones": [names.loc[m, "Zone"] for m in mem],
                                       "zone_ids": mem, "peak_hour": int(s.idxmax()),
                                       "share_22_02": float(s[[22, 23, 0, 1, 2]].sum()),
                                       "share_16_19": float(s[[16, 17, 18, 19]].sum()),
                                       "share_07_09": float(s[[7, 8, 9]].sum()), "profile": [_f(v, 6) for v in s.sort_index()]})
    member_prof = {str(int(z)): [_f(v, 6) for v in g.sort_values("hour")["share"]]
                   for z, g in prof[prof["kind"] == "zone"].groupby("zone_id")}
    clu = {"chosen_k": int(sel[sel["chosen"]]["k"].iloc[0]),
           "selection": sel.drop(columns=["rule"]).to_dict("records"), "rule": sel["rule"].iloc[0],
           **cl_summary, "member_profiles": member_prof}

    met = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    pred = pd.read_csv(out / "predictions.csv")
    pred["hour"] = pd.to_datetime(pred["hour"], format="%Y-%m-%d %H:%M:%S")
    t0 = pred["hour"].min()
    n_test = int((pred["hour"].max() - t0) / pd.Timedelta(hours=1)) + 1
    fz = {}
    for zid, g in pred.groupby("zone_id"):
        g = g.sort_values("hour")
        assert len(g) == n_test and (g["hour"].diff().dropna() == pd.Timedelta(hours=1)).all(), f"zone {zid} test gap"
        fz[str(zid)] = {"y": g["y_true"].astype(int).tolist(), "b": g["baseline"].astype(int).tolist(),
                        "p": [round(float(v), 4) for v in g["y_pred"]]}
    forecast = {"test_start": t0.strftime("%Y-%m-%d %H:%M:%S"),
                "offset": int((t0 - start) / pd.Timedelta(hours=1)), "n_hours": n_test, "zones": fz,
                "metrics": {k: met[k] for k in ("test", "peak", "off_peak", "per_zone", "worst_zone",
                                                "zones_model_beats_baseline", "by_cluster", "rows", "features")},
                "selection": met["selection"], "peak_def": cfg["model"]["peak"]}
    # one formatted label, used everywhere (avoids JS vs Python rounding of a .x5 value)
    forecast["metrics"]["test"] = {**met["test"],
                                   "improvement_label": f"{met['test']['mae_improvement_vs_baseline'] * 100:.1f}%"}

    eda = json.loads((out / "eda_summary.json").read_text(encoding="utf-8"))
    corpus = {c["evidence_id"]: c for c in evidence.load(cfg)}
    llm = None
    rev_path = ev / "rag_evaluation.json"
    if rev_path.exists():
        rev = json.loads(rev_path.read_text(encoding="utf-8"))
        qs = json.loads((ROOT / "evidence" / "rag_questions.json").read_text(encoding="utf-8"))
        runs = {r["run_id"]: r for r in rev["runs"]}
        items = []
        for q in qs["questions"]:
            ret = next(r for r in rev["retrieval"] if r["question_id"] == q["id"])
            item = {**{k: q[k] for k in ("id", "type", "question", "answerable", "reference_facts", "expected_evidence")},
                    "top_k": ret["top_k"], "runs": {}}
            for cond in ("with_evidence", "without_evidence"):
                r = runs.get(f"{q['id']}_{cond}")
                if r:
                    item["runs"][cond] = {"output": r["raw_output"], "cited": r["auto"]["cited_ids"],
                                          "auto_pass": r["auto"]["auto_pass"], "auto_failed": r["auto"]["auto_failed"],
                                          "unsupported_numbers": r["auto"]["unsupported_numbers"],
                                          "human_verdict": (r.get("human") or {}).get("verdict"),
                                          "human_errors": (r.get("human") or {}).get("errors", []),
                                          "corrected": (r.get("human") or {}).get("corrected_answer", ""),
                                          "model": r["model"], "date": r["date"], "file": r["output_file"]}
            items.append(item)
        first = next(iter(runs.values()), {})
        llm = {"summary": rev["summary"], "embedding_model": rev["embedding_model"],
               "model": first.get("model"), "model_digest": (first.get("model_digest") or "")[:12],
               "questions_written_at": qs["written_at"], "items": items}

    return {
        "meta": {"generated_at": datetime.now().isoformat(timespec="seconds"), "mode": cfg["mode"],
                 "window": cfg["window"], "hours_start": start.strftime("%Y-%m-%d %H:%M:%S"), "n_hours": len(hours),
                 "time_note": "New York local time; hour = pickup time rounded down",
                 "splits": cfg["splits"],
                 "sources": {"zone_hour": rel(out / "zone_hour.csv"), "top_zones": rel(out / "top_zones.csv"),
                             "quality": rel(ev / "quality_report.csv"), "eda": rel(out / "eda_summary.json"),
                             "clusters": rel(out / "clusters.csv"), "cluster_profiles": rel(out / "cluster_profiles.csv"),
                             "cluster_selection": rel(out / "cluster_selection.csv"),
                             "predictions": rel(out / "predictions.csv"), "metrics": rel(out / "metrics.json"),
                             "rag": rel(rev_path), "corpus": rel(ev / "evidence_corpus.jsonl")}},
        "zones": zones, "counts": counts, "quality": quality, "eda": {
            "temporal": {k: eda["temporal"][k] for k in ("weekday_peak_hour", "weekday_trough_hour", "weekday_peak_to_trough",
                                                          "busiest_day", "quietest_day", "by_month")},
            "top20_share": eda["zones"]["top20_share_of_all_pickups"], "n_valid_zones": eda["zones"]["n_valid_zones"],
            "quality_per_zone": eda["quality"].get("per_zone")},
        "clusters": clu, "forecast": forecast, "llm": llm,
        "evidence": {k: {"title": v["title"], "source": v["source_file"], "text": v["text"]} for k, v in corpus.items()},
        "narrative": narrative(eda, cl_summary, met),
    }


def run(cfg: dict) -> dict:
    data = build(cfg)
    dash = cfg["paths"]["dashboard"]
    (dash / "assets").mkdir(parents=True, exist_ok=True)
    if cfg["mode"] == "smoke":  # copy the static page next to the smoke data so it can be opened too
        for f in STATIC:
            shutil.copyfile(ROOT / "dashboard" / f, dash / f)
    path = dash / "assets" / "data.js"
    tmp = path.with_suffix(".js.tmp")
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    tmp.write_text("/* generated by src/dashboard.py - do not edit */\nwindow.DASH = " + body + ";\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    log("dashboard", f"{len(body) / 1e6:.2f} MB of data -> {rel(path)}  (open {rel(dash / 'index.html')})")
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="full")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
