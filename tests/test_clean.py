"""T2 — input = retained + quarantined; overlapping problems/flags never double-subtract.
T4 — the zone join does not inflate rows; unknown zones are logged."""
import duckdb
import pandas as pd
import pytest

from src import acquire
from tests.conftest import EXPECTED, make_cfg, write_fixture


def test_T2_counts_conserved(cleaned, record_property):
    cfg, con, res = cleaned
    rep = res["report"].set_index("rule_id")
    assert res["input_rows"] == EXPECTED["input"]
    assert res["retained_rows"] == EXPECTED["retained"]
    assert res["quarantined_rows"] == EXPECTED["quarantined"]
    assert res["input_rows"] == res["retained_rows"] + res["quarantined_rows"]
    for rid in ("R1", "R2", "R3", "R4", "R4b"):
        assert rep.loc[rid, "quarantined_rows"] == EXPECTED[rid], rid
    # sequential: input of each quarantine rule = retained of the previous one
    ids = ["R1", "R2", "R3", "R4", "R4b"]
    for prev, nxt in zip(ids, ids[1:]):
        assert rep.loc[nxt, "input_rows"] == rep.loc[prev, "retained_rows"]
    # files on disk agree with the report
    n_clean = duckdb.sql(f"SELECT count(*) FROM read_parquet('{cfg['paths']['clean']}/*/*.parquet')").fetchall()[0][0]
    n_quar = duckdb.sql(f"SELECT count(*) FROM read_parquet('{cfg['paths']['quarantine']}/*.parquet')").fetchall()[0][0]
    assert (n_clean, n_quar) == (EXPECTED["retained"], EXPECTED["quarantined"])
    record_property("actual", f"input {res['input_rows']} = retained {res['retained_rows']} + "
                              f"quarantined {res['quarantined_rows']}; on disk {n_clean}+{n_quar}")


def test_T2_overlaps_not_double_subtracted(cleaned, record_property):
    cfg, con, res = cleaned
    rep = res["report"].set_index("rule_id")
    # row 10 is out of range AND in zone 999: counted once, under R2 only
    reasons = dict(con.execute("SELECT quarantine_reason, count(*) FROM labeled "
                               "WHERE PULocationID = 999 GROUP BY 1").fetchall())
    assert reasons == {"R2": 1, "R3": 1}
    # row 12 has two flags but is one retained row; TOTAL counts rows, not flags
    flag_sum = int(rep.loc[["R5", "R6a", "R6b", "R7"], "flagged_rows"].sum())
    assert flag_sum == EXPECTED["flag_sum"]
    assert rep.loc["TOTAL", "flagged_rows"] == EXPECTED["any_flag"]
    two = con.execute("SELECT count(*) FROM labeled WHERE quarantine_reason IS NULL "
                      "AND f_distance AND f_fare").fetchall()[0][0]
    assert two == 1
    # the negative row of a non-reversal (row 12, total -5, no +5 partner) stays retained
    assert con.execute("SELECT quarantine_reason FROM labeled WHERE total_amount = -5").fetchall()[0][0] is None
    record_property("actual", f"double-problem row -> R2 only; flag sum {flag_sum} vs rows with any flag "
                              f"{rep.loc['TOTAL', 'flagged_rows']}; two-flag row retained once")


def test_T2_reversal_rule_can_be_disabled(cfg):
    from src import clean
    from src.common import connect
    cfg["rules"]["reversal_pairs"] = False
    res = clean.run(cfg, connect(cfg))
    assert res["retained_rows"] == EXPECTED["retained"] + 1
    assert res["input_rows"] == res["retained_rows"] + res["quarantined_rows"]


def test_T4_join_does_not_inflate(cfg, record_property):
    from src.common import connect
    raw = cfg["paths"]["raw"]
    files = [raw / cfg["file_pattern"].format(month=m) for m in cfg["months"]]
    res = acquire.join_check(connect(cfg), files, raw / "taxi_zone_lookup.csv", cfg)
    assert res["rows_before"] == res["rows_after_join"] == EXPECTED["input"]
    assert res["unmatched"] == 2 and res["unmatched_ids"] == [999]
    assert res["invalid_zone_rows"] == 1
    record_property("actual", f"rows before {res['rows_before']} = after {res['rows_after_join']}; "
                              f"unmatched {res['unmatched']} ids {res['unmatched_ids']}; 264/265 rows "
                              f"{res['invalid_zone_rows']}")


def test_T4_duplicate_zone_key_rejected(tmp_path):
    from src.common import connect
    cfg = make_cfg(tmp_path)
    write_fixture(cfg, zone_ids=(1, 1, 2, 3, 4, 5, 264, 265))           # LocationID 1 twice
    raw = cfg["paths"]["raw"]
    files = [raw / cfg["file_pattern"].format(month=m) for m in cfg["months"]]
    with pytest.raises(acquire.SchemaError):
        acquire.join_check(connect(cfg), files, raw / "taxi_zone_lookup.csv", cfg)
