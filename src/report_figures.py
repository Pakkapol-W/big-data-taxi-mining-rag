"""Report figures for the Data + Compute sections, drawn from the pipeline's own outputs.

Run:  python -m src.report_figures      (after a full pipeline run)
Writes report/figures/fig1_storage.png, fig2_quality.png, fig3_compute.png
"""
from __future__ import annotations

import json

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

from src.common import ROOT  # noqa: E402

OUT = ROOT / "report" / "figures"
EVID = ROOT / "evidence"
GREY, DARK, ACCENT, ACCENT2 = "#9aa3ab", "#2b2f33", "#1f6feb", "#d1495b"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.edgecolor": "#888888"})


def fig_storage(summary: dict) -> None:
    """Data flow and storage layers: raw -> clean partitions / quarantine / zone dim -> zone_hour."""
    fig, ax = plt.subplots(figsize=(9.0, 4.0))
    ax.set_xlim(0, 100), ax.set_ylim(-0.5, 43.5), ax.axis("off")

    def box(x, y, w, h, title, lines, accent=False):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.3,rounding_size=1.2",
                                    fc="#e8f0fe" if accent else "white", ec=ACCENT if accent else "#7a8288",
                                    lw=1.6 if accent else 1.0))
        ax.text(x + w / 2, y + h - 2.6, title, ha="center", va="top", fontsize=9.5, weight="bold", color=DARK)
        for i, line in enumerate(lines):
            ax.text(x + w / 2, y + h - 6.6 - 3.3 * i, line, ha="center", va="top", fontsize=8, color="#444b52")

    def arrow(x1, y1, x2, y2, label=None, dy=1.2):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=11,
                                     color="#7a8288", lw=1.0))
        if label:
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + dy, label, ha="center", fontsize=7.5, color="#5b636a")

    n_in, n_ret, n_q = summary["input_rows"], summary["retained_rows"], summary["quarantined_rows"]
    zh = summary["zone_hour"]
    box(0.5, 9, 19, 26, "Raw (read-only)", ["3 monthly Parquet files", "+ zone lookup CSV",
                                             f"{n_in:,} trips", "SHA-256 in manifest"])
    box(27, 29, 24, 13, "Clean partitions", ["one folder per pickup month", f"{n_ret:,} trips + flags"],
        accent=True)
    box(27, 14.5, 24, 12, "Quarantine", [f"{n_q:,} trips", "+ quarantine_reason"])
    box(27, 1, 24, 11, "Zone dimension", ["zones.parquet, 265 rows"])
    box(58.5, 21, 18, 21, "Aggregate", ["A: global GROUP BY", "B: combine per split,", "then reduce",
                                        "A == B (exact)"])
    box(81, 21, 18.5, 21, "zone_hour", [f"{zh['zone_hour_all_rows']:,} rows", "263 zones x 2,184 h",
                                         f"top 20: {zh['zone_hour_rows']:,} rows"], accent=True)
    arrow(19.5, 28, 27, 35)
    arrow(19.5, 22, 27, 20.5)
    arrow(19.5, 14, 27, 6.5)
    arrow(51, 35.5, 58.5, 33)
    arrow(76.5, 31.5, 81, 31.5)
    arrow(51, 6.5, 90, 21)
    ax.text(74, 9.5, "zone ids and names", ha="center", fontsize=7.5, color="#5b636a")
    fig.savefig(OUT / "fig1_storage.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def fig_quality(report: pd.DataFrame) -> None:
    """Rows affected per rule: quarantine (removed from counts) vs flag (kept)."""
    r = report[report.rule_id != "TOTAL"].copy()
    r["n"] = r.quarantined_rows + r.flagged_rows
    r["kind"] = r.quarantined_rows.gt(0) | r.rule_id.isin(["R1", "R2", "R3", "R4", "R4b"])
    names = {"R1": "R1 timestamp NULL", "R2": "R2 outside window", "R3": "R3 invalid zone",
             "R4": "R4 exact duplicate", "R4b": "R4b reversal row", "R5": "R5 time order",
             "R6a": "R6a distance anomaly", "R6b": "R6b fare anomaly", "R7": "R7 missing values"}
    r = r.iloc[::-1]
    fig, ax = plt.subplots(figsize=(7.6, 3.4))
    colors = [ACCENT2 if k else GREY for k in r.kind]
    bars = ax.barh([names[i] for i in r.rule_id], r.n, color=colors, height=0.62)
    for b, v in zip(bars, r.n):
        ax.text(b.get_width() + 12000, b.get_y() + b.get_height() / 2, f"{v:,}", va="center", fontsize=8.5,
                color=DARK)
    tot = report.iloc[-1]
    ax.set_title(f"{int(tot.quarantined_rows):,} trips quarantined, {int(tot.flagged_rows):,} flagged and kept "
                 f"(of {int(tot.input_rows):,})", loc="left", fontsize=10.5, color=DARK)
    ax.set_xlabel("trips affected")
    ax.set_xlim(0, r.n.max() * 1.18)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1e6:.1f}M" if v else "0"))
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=ACCENT2, label="quarantine: removed from pickup counts"),
                       Patch(color=GREY, label="flag: kept, excluded only from fare/distance analyses")],
              loc="upper right", fontsize=8, frameon=False)
    fig.savefig(OUT / "fig2_quality.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def fig_compute(log: pd.DataFrame, summary: dict) -> None:
    """Left: records entering the shuffle (log scale). Right: per-run seconds and medians."""
    agg = summary["aggregation"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8.6, 3.1), gridspec_kw={"width_ratios": [1.15, 1]})
    labels = ["A: raw rows into\nglobal GROUP BY", "B: partial counts\nafter combiner", "final keys"]
    vals = [agg["intermediate_rows"]["A"], agg["intermediate_rows"]["B"], agg["output_keys"]]
    bars = a1.bar(labels, vals, color=[GREY, ACCENT, "#cfd6dc"], width=0.6)
    a1.set_yscale("log"), a1.set_ylim(1e4, 4e7)
    for b, v in zip(bars, vals):
        a1.text(b.get_x() + b.get_width() / 2, v * 1.25, f"{v:,}", ha="center", fontsize=8.5, color=DARK)
    a1.set_title(f"Combiner shrinks shuffle input {vals[0] / vals[1]:.0f}x", loc="left", fontsize=10.5, color=DARK)
    a1.set_ylabel("records (log scale)")
    a1.tick_params(axis="x", labelsize=8)

    runs = log[log.metric == "seconds"].copy()
    runs["method"] = runs.experiment_id.str.split("-").str[1]
    for i, m in enumerate("AB"):
        s = runs[runs.method == m].seconds.astype(float)
        a2.scatter([i] * len(s), s, color=GREY if m == "A" else ACCENT, zorder=3, s=22)
        med = agg["median_seconds"][m]
        a2.hlines(med, i - 0.22, i + 0.22, color=DARK, lw=1.4)
        a2.text(i + 0.27, med, f"median {med:.3f} s", va="center", fontsize=8.5, color=DARK)
    a2.set_xticks([0, 1], ["A (DuckDB,\n8 threads)", "B (pandas,\n1 process)"], fontsize=8)
    a2.set_xlim(-0.5, 1.95), a2.set_ylim(0, 0.6)
    a2.set_ylabel("seconds per run")
    a2.set_title("3 timed runs each, after 1 warm-up", loc="left", fontsize=10.5, color=DARK)
    fig.tight_layout(w_pad=2.5)
    fig.savefig(OUT / "fig3_compute.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    summary = json.loads((EVID / "pipeline_summary.json").read_text())
    if summary["mode"] != "full":
        raise SystemExit("report figures need a full run (evidence/pipeline_summary.json mode=full)")
    fig_storage(summary)
    fig_quality(pd.read_csv(EVID / "quality_report.csv"))
    fig_compute(pd.read_csv(EVID / "experiment_log.csv"), summary)
    print(f"wrote {sorted(p.name for p in OUT.glob('*.png'))}")


if __name__ == "__main__":
    main()
