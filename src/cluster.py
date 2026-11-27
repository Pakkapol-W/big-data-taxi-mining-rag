"""Brief §05 Mine - cluster the 20 forecast zones by their April 24-hour pickup profile.

Profile  : for each zone, mean pickups per hour of day over April, divided by its sum -> 24 shares
           that sum to 1 (shape only; volume is removed on purpose).
Scaling  : none beyond that. The features are already shares on one scale; standardising each hour
           would inflate the near-empty 03-05h columns, so night noise would dominate distances.
Model    : scikit-learn KMeans, k in config (2..5), n_init and seed from config.
Choose k : highest silhouette among k whose smallest cluster has >= min_cluster_size zones
           (singletons cannot be interpreted as a group); ties -> smaller k.
Stability: May profiles of the same zones, KMeans refit with the same k; adjusted Rand index vs
           April labels. Reported only - never used to choose k. June is never read here.
Cluster ids are relabelled by (size desc, smallest zone_id) so they are identical on every machine.

Outputs : outputs/clusters.csv (zone_id, cluster_id), outputs/cluster_selection.csv, outputs/cluster_memberships.csv (all k),
          outputs/cluster_profiles.csv (zone and centroid profiles, long format)
Run     : python -m src.cluster --mode smoke|full
"""
from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd
import sklearn
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score, silhouette_score

from src.aggregate import hardware
from src.common import load_config, log, write_csv_atomic
from src.modelling_io import (data_hash, load_zone_hour, month_of, rel, replace_experiment_rows,
                              zone_names)

HOURS = list(range(24))


def profiles(zh: pd.DataFrame, month: str, strict: bool) -> pd.DataFrame:
    """zone x 24 matrix of hour-of-day shares for one month. NULL (unknown) hours are skipped."""
    m = zh[month_of(zh["hour"]) == month].copy()
    if m.empty:
        raise ValueError(f"no zone_hour rows for {month}")
    m["hod"] = m["hour"].dt.hour
    mean = (m.groupby(["zone_id", "hod"])["pickup_count"].mean().astype(float)
             .unstack("hod").reindex(columns=HOURS))
    if mean.isna().any().any():
        if strict:
            raise ValueError(f"{month}: some zone/hour-of-day has no observed value; profiles undefined")
        mean = mean.fillna(0.0)  # smoke only: sparse 20k sample, no accuracy claim
    total = mean.sum(axis=1)
    if (total <= 0).any():
        raise ValueError(f"{month}: zone(s) with zero pickups {list(total[total <= 0].index)}")
    prof = mean.div(total, axis=0)
    assert np.allclose(prof.sum(axis=1), 1.0), "profiles must sum to 1"
    return prof.sort_index()


def canonical_labels(labels: np.ndarray, zone_ids: np.ndarray) -> np.ndarray:
    """Relabel so cluster 0 is the largest; ties -> cluster containing the smallest zone_id."""
    keys = sorted(set(labels), key=lambda c: (-(labels == c).sum(), zone_ids[labels == c].min()))
    remap = {old: new for new, old in enumerate(keys)}
    return np.array([remap[c] for c in labels])


def fit_k(X: np.ndarray, k: int, cfg: dict) -> KMeans:
    return KMeans(n_clusters=k, n_init=cfg["cluster"]["n_init"], random_state=cfg["seed"]).fit(X)


def describe(prof_row: pd.Series) -> dict:
    """Plain numbers for interpreting a centroid profile (shares of the day's pickups)."""
    return {"peak_hour": int(prof_row.idxmax()),
            "share_07_09": round(float(prof_row[[7, 8, 9]].sum()), 4),
            "share_16_19": round(float(prof_row[[16, 17, 18, 19]].sum()), 4),
            "share_22_02": round(float(prof_row[[22, 23, 0, 1, 2]].sum()), 4),
            "share_03_05": round(float(prof_row[[3, 4, 5]].sum()), 4)}


def run(cfg: dict) -> dict:
    t0 = time.perf_counter()
    c = cfg["cluster"]
    if c["profile_month"] != cfg["splits"]["train"] or cfg["splits"]["test"] in (c["profile_month"], c["stability_month"]):
        raise ValueError("clustering must use the train month only; test month may never be read")
    strict = cfg["mode"] == "full"
    zh = load_zone_hour(cfg)
    zh = zh[month_of(zh["hour"]).isin([c["profile_month"], c["stability_month"]])]  # June never loaded
    apr = profiles(zh, c["profile_month"], strict)
    may = profiles(zh, c["stability_month"], strict).reindex(apr.index)
    zone_ids = apr.index.to_numpy()
    X, X_may = apr.to_numpy(), may.to_numpy()

    rows, labels_by_k = [], {}
    for k in c["k_values"]:
        km = fit_k(X, k, cfg)
        lab = canonical_labels(km.labels_, zone_ids)
        lab_may = fit_k(X_may, k, cfg).labels_
        sizes = np.bincount(lab, minlength=k)
        labels_by_k[k] = lab
        rows.append({"k": k, "silhouette": round(float(silhouette_score(X, lab)), 4),
                     "sizes": "/".join(str(int(s)) for s in sizes), "min_size": int(sizes.min()),
                     "inertia": round(float(km.inertia_), 6),
                     "may_ari": round(float(adjusted_rand_score(lab, lab_may)), 4)})
    sel = pd.DataFrame(rows)
    ok = sel[sel["min_size"] >= c["min_cluster_size"]]
    if ok.empty:
        raise ValueError(f"no k has all clusters >= {c['min_cluster_size']} zones")
    best_k = int(ok.sort_values(["silhouette", "k"], ascending=[False, True]).iloc[0]["k"])
    sel["chosen"] = sel["k"] == best_k
    sel["rule"] = (f"max silhouette with min cluster size >= {c['min_cluster_size']}; tie -> smaller k; "
                   f"May ARI reported, not used; June unused")
    lab = labels_by_k[best_k]

    out = cfg["paths"]["outputs"]
    clusters = pd.DataFrame({"zone_id": zone_ids, "cluster_id": lab}).sort_values("zone_id")
    write_csv_atomic(clusters, out / "clusters.csv")
    write_csv_atomic(sel, out / "cluster_selection.csv")
    # sensitivity: membership for every k compared (e.g. k=3 splits the commuter cluster), not used downstream
    memb = pd.DataFrame([{"k": k, "zone_id": int(z), "cluster_id": int(l), "chosen": k == best_k}
                         for k, labs in labels_by_k.items() for z, l in zip(zone_ids, labs)])
    write_csv_atomic(memb.sort_values(["k", "zone_id"]), out / "cluster_memberships.csv")

    # long-format profiles: each zone + each centroid (mean of member shares) for the dashboard
    names = zone_names(cfg).set_index("zone_id")
    long = []
    for zid, prow in apr.iterrows():
        cid = int(clusters.set_index("zone_id").loc[zid, "cluster_id"])
        long += [{"kind": "zone", "zone_id": int(zid), "cluster_id": cid, "hour": h, "share": round(float(prow[h]), 6)}
                 for h in HOURS]
    cents = apr.groupby(lab).mean()
    for cid, prow in cents.iterrows():
        long += [{"kind": "centroid", "zone_id": "", "cluster_id": int(cid), "hour": h, "share": round(float(prow[h]), 6)}
                 for h in HOURS]
    write_csv_atomic(pd.DataFrame(long), out / "cluster_profiles.csv")

    summary = {"chosen_k": best_k, "selection": sel.drop(columns="rule").to_dict("records"), "clusters": []}
    for cid, prow in cents.iterrows():
        members = clusters[clusters["cluster_id"] == cid]["zone_id"].tolist()
        summary["clusters"].append({"cluster_id": int(cid), "size": len(members), "zone_ids": members,
                                    "zones": [names.loc[z, "Zone"] for z in members], **describe(prow)})

    secs = round(time.perf_counter() - t0, 3)
    common = {"stage": "cluster", "data_hash": data_hash(out / "zone_hour.csv"), "seed": cfg["seed"],
              "hardware": hardware(), "repeat": "", "seconds": "", "intermediate_rows": len(apr),
              "output_path": rel(out / "clusters.csv")}
    conf = f"sklearn {sklearn.__version__} KMeans n_init={c['n_init']}; April 24h share profiles; no extra scaling"
    log_rows = []
    for r in rows:
        for metric in ("silhouette", "may_ari", "min_size"):
            log_rows.append({**common, "experiment_id": f"CLU-k{r['k']}-{metric}", "config": f"{conf}; k={r['k']}",
                             "metric": metric, "value": r[metric]})
    log_rows.append({**common, "experiment_id": "CLU-chosen", "config": conf, "metric": "chosen_k",
                     "value": best_k, "seconds": secs})
    replace_experiment_rows(cfg, "CLU-", log_rows)
    log("cluster", " | ".join(f"k={r['k']} sil={r['silhouette']} sizes={r['sizes']} mayARI={r['may_ari']}" for r in rows))
    log("cluster", f"chosen k={best_k} -> {rel(out / 'clusters.csv')}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="full")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
