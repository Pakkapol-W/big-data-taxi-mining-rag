"""Run pytest and write evidence/test_report.md from the real results (T1-T8 automated; T9-T10 manual).

Run:  python -m src.test_report
Each test records its observed values via pytest's record_property("actual", ...).
"""
from __future__ import annotations

import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from src.common import ROOT

CASES = [  # (test id, test function, input, expected)
    ("T1", "test_T1_missing_month_fails", "manifest without 2024-06; June file removed",
     "CoverageError naming 2024-06; zone_hour refuses zero-fill"),
    ("T1", "test_T1_missing_required_field_fails", "April schema without PULocationID",
     "SchemaError naming PULocationID"),
    ("T2", "test_T2_counts_conserved", "19-row fixture (tests/conftest.py)",
     "input 19 = retained 11 + quarantined 8 (R1 1, R2 3, R3 2, R4 1, R4b 1), report = files on disk"),
    ("T2", "test_T2_overlaps_not_double_subtracted",
     "row out-of-range AND unknown zone; row with distance AND fare flags",
     "first row counted once as R2; flag sum 4 but 3 flagged rows; two-flag row retained once"),
    ("T3", "test_T3_rerun_identical", "clean + aggregate + zone_hour + manifest run twice",
     "identical SHA-256 for every output file; manifest stays 4 rows"),
    ("T3", "test_T3_zero_fill_only_with_coverage", "fixture with sparse hours; smoke mode",
     "0 only where coverage_ok; NULL elsewhere; smoke never zero-fills"),
    ("T4", "test_T4_join_does_not_inflate", "19 trips incl. 2 in zone 999, 1 in zone 264",
     "rows before = after = 19; unmatched 2, ids [999]; 264/265 rows 1"),
    ("T4", "test_T4_duplicate_zone_key_rejected", "lookup with LocationID 1 twice", "SchemaError"),
    ("T5", "test_T5_both_methods_match_hand_counts", "fixture clean partitions; B with 2-row splits",
     "A == B == 7 hand-counted keys (sum 11); unique keys; B partials > keys"),
    ("T5", "test_T5_benchmark_logs_runs_and_medians", "benchmark on fixture",
     "experiment_log has per-run seconds, 2 medians, equivalence row"),
    ("T6", "test_T6_disjoint_splits", "synthetic grid 6 zones x 2,184 h (tests/test_model.py)",
     "train=Apr, valid=May, test=Jun; no shared keys; 168 h-history rows dropped and counted (train only)"),
    ("T6", "test_T6_features_past_only", "zone 20, target 2024-06-10 12:00; then y(>=h) set to 1e6",
     "lags/mean24 equal hand values; features of row h unchanged by the perturbation"),
    ("T6", "test_T6_train_only_selection", "June counts of zone 10 set to 1e6; profile_month=June config",
     "top zones from April only; clusters + May model selection unchanged; June profile config refused"),
    ("T7", "test_T7_metrics_hand_checked", "y=[0,2,4], p=[1,2,1]; clip([-0.5,0,1.25])",
     "MAE 4/3, RMSE sqrt(10/3); negatives -> 0, no rounding"),
    ("T7", "test_T7_same_rows_for_model_and_baseline", "forecast.run on synthetic grid",
     "every June row has y_true, y_pred, baseline; metrics recomputed from predictions.csv match; baseline=y(h-168)"),
    ("T8", "test_T8_corpus_ids_valid", "evidence/evidence_corpus.jsonl",
     ">=10 chunks; unique EV-* ids; source_file exists; metric_definition and timestamp present; no trip rows"),
    ("T8", "test_T8_top3_sorted", "toy vectors with a score tie; evidence/retrieval_log.json",
     "top-3 in descending score, ties by id; every saved retrieval has 3 sorted hits"),
    ("T8", "test_T8_unsupported_questions_recorded", "rag_questions.json; 4 hand-made outputs",
     "2 unanswerable questions recorded with expected INSUFFICIENT EVIDENCE; invented id/number fail the checker"),
]
OTHER = [("T9", "Offline HTML, both filters, reset and empty states (manual)", "Presentation"),
         ("T10", "Trace three dashboard values; verify all 12 LLM answers (manual)", "Presentation / LLM")]


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        xml_path = Path(d) / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", "-q", "tests", f"--junitxml={xml_path}", "-o", "junit_family=xunit1"]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        tree = ET.parse(xml_path)
    results = {}
    for tc in tree.iter("testcase"):
        props = {p.get("name"): p.get("value") for p in tc.iter("property")}
        failed = tc.find("failure") is not None or tc.find("error") is not None
        msg = (tc.find("failure") if tc.find("failure") is not None else tc.find("error"))
        results[tc.get("name")] = ("FAIL", (msg.get("message") or "")[:200]) if failed \
            else ("PASS", props.get("actual", "as expected"))
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "no output"

    lines = [
        "# Test report / 测试报告",
        f"Environment / 环境：{platform.platform()}; Python {platform.python_version()}; "
        f"duckdb {duckdb.__version__}; pandas {pd.__version__}",
        "Command / 命令：`python -m pytest -q tests` (report generated by `python -m src.test_report`)",
        f"Date / 日期：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Result / 结果：{summary}",
        "",
        "Fixture: 19 hand-made trips in 3 monthly Parquet files + 7-zone lookup; every expected value "
        "was counted by hand (see the table in `tests/conftest.py`). T6/T7 use a synthetic complete zone-hour "
        "grid (6 zones x 2,184 h) built in `tests/test_model.py`.",
        "",
        "|ID|Input 输入|Expected 预期|Actual 实际|Status 状态|Evidence 证据|",
        "|---|---|---|---|---|---|",
    ]
    for tid, fn, inp, exp in CASES:
        status, actual = results.get(fn, ("NOT RUN", "test not found"))
        mod = {"T1": "test_acquire", "T2": "test_clean", "T4": "test_clean",
               "T3": "test_idempotence", "T5": "test_aggregate", "T6": "test_model", "T7": "test_model", "T8": "test_rag"}[tid]
        lines.append(f"|{tid}|{inp}|{exp}|{actual.replace('|', '/')}|{status}|`tests/{mod}.py::{fn}`|")
    for tid, what, owner in OTHER:
        lines.append(f"|{tid}|{what}|—|pending ({owner} owner)|—|—|")
    out = ROOT / "evidence" / "test_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{summary}\nwrote {out}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
