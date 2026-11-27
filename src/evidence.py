"""Brief §06 Evidence corpus - chunks built ONLY from dictionaries, rules and aggregates (never trips).

Each chunk: evidence_id, source_file, metric_definition, timestamp (source file modification time, UTC),
            title, text. Numbers in `text` are rounded so a rerun on other hardware gives the same text.

Sources: evidence/data_dictionary.csv, evidence/quality_report.csv, config.json, evidence/experiment_log.csv,
         outputs/eda_summary.json, outputs/cluster_selection.csv, outputs/clusters.csv (+ eda/cluster
         summaries), outputs/metrics.json
Output : evidence/evidence_corpus.jsonl (one JSON object per line, sorted by evidence_id)
Run    : python -m src.evidence --mode smoke|full
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.common import ROOT, load_config, log
from src.modelling_io import rel, zone_names

ID_RE = r"EV-[A-Z0-9]+(?:-[A-Z0-9]+)*"


def _ts(path: Path) -> str:
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chunk(eid: str, path: Path, definition: str, title: str, text: str) -> dict:
    return {"evidence_id": eid, "source_file": rel(path), "metric_definition": definition,
            "timestamp": _ts(path), "title": title, "text": " ".join(text.split())}


def dictionary_chunks(ev: Path) -> list[dict]:
    path = ev / "data_dictionary.csv"
    d = pd.read_csv(path)
    groups = {"TIME": ("time", "Time fields and time zone"), "LOCATION": ("location", "Location fields and zones"),
              "DISTANCE": ("distance", "Distance field and unit"), "MONEY": ("money", "Fare and payment amounts (US dollars)"),
              "TRIP": ("trip|payment", "Trip attributes and codes"), "DERIVED": ("derived", "Derived fields: zone-hour target")}
    out = []
    for key, (grp, title) in groups.items():
        rows = d[d["group"].str.fullmatch(grp)]
        text = " ".join(f"{r.field} ({r.type}; unit: {r.unit}): {r.definition}." for r in rows.itertuples())
        out.append(chunk(f"EV-DICT-{key}", path, "TLC data dictionary fields and project-derived fields; "
                         "units as stated per field", f"Data dictionary - {title}", text))
    return out


def quality_chunks(ev: Path) -> list[dict]:
    path = ev / "quality_report.csv"
    qr = pd.read_csv(path)
    out = []
    for r in qr.itertuples():
        eid = "EV-QR-TOTAL" if r.rule_id == "TOTAL" else f"EV-QR-{r.rule_id.upper()}"
        text = (f"Rule {r.rule_id}. Condition: {r.condition}. Action: {r.action}. Reason: {r.reason}. "
                f"Counts: input rows {r.input_rows:,}; retained rows {r.retained_rows:,}; "
                f"quarantined rows {r.quarantined_rows:,}; flagged rows {r.flagged_rows:,}.")
        out.append(chunk(eid, path, "Trip-row counts before/after one cleaning rule over all 3 monthly files "
                         "(April-June 2024); quarantined rows are removed from pickup counts, flagged rows are kept",
                         f"Quality rule {r.rule_id}", text))
    return out


def scope_chunk(cfg: dict) -> dict:
    path = ROOT / "config.json"
    s, w = cfg["splits"], cfg["window"]
    text = (f"Data: NYC TLC yellow taxi trips for months {', '.join(cfg['months'])}, pickup window "
            f"[{w['start']}, {w['end']}) New York local time. Target: pickup_count per zone and hour (recorded "
            f"pickups, not demand). Forecast zones: top {cfg['top_n_zones']} zones by retained pickups in "
            f"{cfg['selection_month']}, ties to lower zone_id, fixed for the project. Temporal split: train "
            f"{s['train']}, validation {s['valid']}, test {s['test']}. Random seed {cfg['seed']}. "
            f"Zones 264 (Unknown) and 265 (Outside of NYC) excluded.")
    return chunk("EV-SCOPE", path, "Project configuration: scope, target, zone selection and split",
                 "Scope, target and split", text)


def benchmark_chunk(ev: Path) -> dict | None:
    path = ev / "experiment_log.csv"
    log_ = pd.read_csv(path, dtype=str, keep_default_na=False)
    med = log_[log_["experiment_id"].isin(["AGG-A-median", "AGG-B-median"])].set_index("experiment_id")
    eq = log_[log_["experiment_id"] == "AGG-equivalence"]
    if med.empty or eq.empty:
        return None
    a, b = med.loc["AGG-A-median"], med.loc["AGG-B-median"]
    text = (f"Method A (one global GROUP BY over all clean trips, DuckDB): median {float(a['seconds']):.2f} s over "
            f"3 runs, {int(float(a['intermediate_rows'])):,} intermediate records. Method B (per-batch local counts = map + "
            f"combiner, concatenated = shuffle, summed = reduce; pandas): median {float(b['seconds']):.2f} s, "
            f"{int(float(b['intermediate_rows'])):,} intermediate records. Both give identical results on "
            f"{int(float(eq.iloc[0]['value'])):,} zone-hour keys. Hardware: {a['hardware']}. A single-machine experiment, "
            f"not a cluster benchmark.")
    return chunk("EV-AGG-BENCH", path, "Median wall-clock seconds of 3 timed runs per aggregation method; "
                 "intermediate records passed to the final reduce", "Aggregation methods A vs B", text)


def eda_chunks(out_dir: Path) -> list[dict]:
    path = out_dir / "eda_summary.json"
    e = json.loads(path.read_text(encoding="utf-8"))
    t, z, q = e["temporal"], e["zones"], e["quality"]
    wk = ", ".join(f"{d} {v:,.0f}" for d, v in t["mean_daily_by_weekday"].items())
    temporal = (f"All valid zones, April-June 2024. Weekday pickups peak at {t['weekday_peak_hour']}:00 and are "
                f"lowest at {t['weekday_trough_hour']:02d}:00; the peak hour averages {t['weekday_peak_to_trough']} "
                f"times the trough hour. Weekend peak hour: {t['weekend_peak_hour']}:00. Mean pickups per day by "
                f"weekday: {wk}. Busiest day {t['busiest_day']['date']} ({t['busiest_day']['pickups']:,}); quietest "
                f"day {t['quietest_day']['date']} ({t['quietest_day']['pickups']:,}). Monthly totals: "
                + ", ".join(f"{m} {v:,}" for m, v in t["by_month"].items()) + ". Associations only.")
    per = pd.DataFrame(z["per_zone"])
    rows = "; ".join(f"{r.Zone} (zone {r.zone_id}): peak {r.peak_hour}:00, weekend/weekday {r.weekend_to_weekday_ratio:.2f}, "
                     f"mean {r.mean_per_hour:.0f}/h" for r in per.itertuples())
    zones = (f"The 20 forecast zones hold {z['top20_share_of_all_pickups'] * 100:.1f}% of all retained pickups "
             f"({z['top20_total_pickups']:,}) across {z['n_valid_zones']} valid zones. Per zone (April-June): {rows}.")
    qual = (f"Input {q['input_rows']:,} trip rows; retained {q['retained_rows']:,}; quarantined {q['quarantined_rows']:,} "
            f"({q['quarantined_share'] * 100:.2f}%); rows with at least one flag {q['rows_with_any_flag']:,} "
            f"({q['flagged_share'] * 100:.1f}%), kept in pickup counts.")
    if q.get("top20_quarantined_share") is not None:
        hz = q["highest_quarantine_zone"]
        qual += (f" In the 20 forecast zones {q['top20_quarantined_share'] * 100:.2f}% of input rows were quarantined; "
                 f"highest share in zone {hz['zone_id']} ({hz['share'] * 100:.2f}%).")
    return [
        chunk("EV-EDA-TEMPORAL", path, "City-wide pickups per hour/day (sum over all valid zones), New York local time",
              "Temporal pickup patterns", temporal),
        chunk("EV-EDA-ZONES", path, "Per-zone hourly pickup statistics for the 20 forecast zones, April-June 2024",
              "Differences between forecast zones", zones),
        chunk("EV-EDA-QUALITY", path, "Share of trip rows quarantined or flagged by the quality rules",
              "Effect of data quality on counts", qual),
    ]


def cluster_chunks(cfg: dict, out_dir: Path) -> list[dict]:
    sel_path, cl_path, prof_path = out_dir / "cluster_selection.csv", out_dir / "clusters.csv", out_dir / "cluster_profiles.csv"
    sel = pd.read_csv(sel_path)
    names = zone_names(cfg).set_index("zone_id")
    chosen = int(sel[sel["chosen"]]["k"].iloc[0])
    table = "; ".join(f"k={r.k}: silhouette {r.silhouette:.3f}, sizes {r.sizes}, May stability ARI {r.may_ari:.2f}"
                      for r in sel.itertuples())
    out = [chunk("EV-CLU-SELECTION", sel_path,
                 "K-means on April 24-hour pickup-share profiles of the 20 forecast zones (each profile sums to 1)",
                 "Cluster count selection",
                 f"{table}. Chosen k={chosen}: highest silhouette with every cluster >= "
                 f"{cfg['cluster']['min_cluster_size']} zones. June was not used.")]
    cl = pd.read_csv(cl_path)
    prof = pd.read_csv(prof_path)
    cent = prof[prof["kind"] == "centroid"]
    for cid, g in cent.groupby("cluster_id"):
        s = g.set_index("hour")["share"]
        members = cl[cl["cluster_id"] == cid]["zone_id"].tolist()
        text = (f"Cluster {cid} of k={chosen}: {len(members)} zones - "
                + ", ".join(f"{names.loc[m, 'Zone']} ({m})" for m in members)
                + f". Average profile peaks at {int(s.idxmax())}:00. Share of daily pickups: 07:00-09:59 "
                f"{s[[7, 8, 9]].sum() * 100:.1f}%; 16:00-19:59 {s[[16, 17, 18, 19]].sum() * 100:.1f}%; "
                f"22:00-02:59 {s[[22, 23, 0, 1, 2]].sum() * 100:.1f}%; 03:00-05:59 {s[[3, 4, 5]].sum() * 100:.1f}%.")
        out.append(chunk(f"EV-CLU-{cid}", prof_path, "Mean April hour-of-day share profile of the cluster's member zones",
                         f"Cluster {cid} profile", text))
    return out


def model_chunks(cfg: dict, out_dir: Path) -> list[dict]:
    path = out_dir / "metrics.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    t, sel = m["test"], m["selection"]
    settings = "; ".join(f"{s['setting']} May MAE {s['valid_mae']:.2f}" for s in sel["settings"])
    spec = (f"Target: pickups in hour h=t+1 per zone, predicted at the end of hour t. Features: "
            f"{', '.join(m['features'])} (lag1=y(t), lag24=y(h-24), mean24=mean of the last 24 observed hours, "
            f"lag168=y(h-168)); all use hours up to t only. Baseline: same zone and hour 7 days earlier. Model: "
            f"HistGradientBoostingRegressor; settings compared on May: {settings}; May baseline MAE "
            f"{sel['baseline_on_valid']['valid_mae']:.2f}. Chosen: {sel['chosen']} (lowest May MAE), refit once on "
            f"April+May, tested on June without refitting. Rows lacking 168 h history dropped: "
            f"{m['rows']['rows_dropped_no_history']:,}.")
    overall = (f"June 2024 test, {t['rows']:,} zone-hours, 20 zones; model and baseline on identical rows. "
               f"Model MAE {t['mae_model']:.2f}, RMSE {t['rmse_model']:.2f}. Baseline MAE {t['mae_baseline']:.2f}, "
               f"RMSE {t['rmse_baseline']:.2f}. MAE is {t['mae_improvement_vs_baseline'] * 100:.1f}% lower than the "
               f"baseline. Mean pickups per zone-hour {t['mean_y']:.1f}. Model beats the baseline in "
               f"{m['zones_model_beats_baseline']} of 20 zones.")
    zones = "; ".join(f"{r['Zone']} (zone {r['zone_id']}): model {r['mae_model']:.2f}, baseline {r['mae_baseline']:.2f}"
                      for r in m["per_zone"])
    w = m["worst_zone"]
    worst = (f"Largest model MAE: {w['zone']} (zone {w['zone_id']}), MAE {w['mae_model']:.2f} vs baseline "
             f"{w['mae_baseline']:.2f}, mean {w['mean_y']:.1f} pickups/hour. Worst hours of day: "
             + ", ".join(f"{h['hour_of_day']}:00 MAE {h['mae_model']:.1f}" for h in w["worst_hours_of_day"])
             + ". Largest single errors: "
             + "; ".join(f"{e['hour']} actual {e['y_true']:.0f}, predicted {e['y_pred']:.0f}, baseline {e['baseline']:.0f}"
                         for e in w["largest_errors"][:3]) + ".")
    pk, off = m["peak"], m["off_peak"]
    peak = (f"Peak period = {pk['definition']}. Peak ({pk['rows']:,} rows): model MAE {pk['mae_model']:.2f}, "
            f"baseline {pk['mae_baseline']:.2f}. Off-peak ({off['rows']:,} rows): model MAE {off['mae_model']:.2f}, "
            f"baseline {off['mae_baseline']:.2f}.")
    if m.get("by_cluster"):
        peak += " By cluster: " + "; ".join(
            f"cluster {c['cluster_id']} MAE model {c['mae_model']:.2f} / baseline {c['mae_baseline']:.2f}"
            + (f", peak model {c['peak_mae_model']:.2f} / baseline {c['peak_mae_baseline']:.2f}" if "peak_mae_model" in c else "")
            for c in m["by_cluster"]) + "."
    d = "June 2024 one-step-ahead errors in pickups per zone-hour (MAE, RMSE); negative predictions clipped to 0"
    return [chunk("EV-MOD-SPEC", path, "Forecast specification frozen before the June test", "Forecast setup", spec),
            chunk("EV-MOD-OVERALL", path, d, "Forecast accuracy: model vs baseline", overall),
            chunk("EV-MOD-ZONES", path, d + "; per zone", "Per-zone forecast MAE", zones),
            chunk("EV-MOD-WORST", path, d + "; worst zone", "Worst-zone error analysis", worst),
            chunk("EV-MOD-PEAK", path, d + "; peak vs off-peak and by cluster", "Peak-period errors", peak)]


def build(cfg: dict) -> list[dict]:
    ev, out = cfg["paths"]["evidence"], cfg["paths"]["outputs"]
    # the dictionary is hand-written and lives in evidence/ for both modes
    chunks = dictionary_chunks(ROOT / "evidence") + quality_chunks(ev) + [scope_chunk(cfg)]
    b = benchmark_chunk(ev)
    chunks += ([b] if b else []) + eda_chunks(out) + cluster_chunks(cfg, out) + model_chunks(cfg, out)
    chunks.sort(key=lambda c: c["evidence_id"])
    ids = [c["evidence_id"] for c in chunks]
    assert len(ids) == len(set(ids)), "duplicate evidence_id"
    assert len(chunks) >= 10, "brief requires at least 10 chunks"
    return chunks


def run(cfg: dict) -> list[dict]:
    chunks = build(cfg)
    path = cfg["paths"]["evidence"] / "evidence_corpus.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    log("evidence", f"{len(chunks)} chunks -> {rel(path)}")
    return chunks


def load(cfg: dict) -> list[dict]:
    path = cfg["paths"]["evidence"] / "evidence_corpus.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="full")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
