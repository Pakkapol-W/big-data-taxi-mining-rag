"""T1 — a missing month or a missing required field fails explicitly."""
import pandas as pd
import pyarrow.parquet as pq
import pytest

from src import acquire, store


def test_T1_missing_month_fails(cfg, record_property):
    man = pd.read_csv(cfg["paths"]["manifest"], dtype=str)
    man = man[man["file"] != "yellow_tripdata_2024-06.parquet"]          # drop June
    with pytest.raises(acquire.CoverageError) as e:
        acquire.check_months(man, cfg)
    assert "2024-06" in str(e.value)
    # and the zone_hour stage then refuses to zero-fill (unknown coverage)
    (cfg["paths"]["raw"] / "yellow_tripdata_2024-06.parquet").unlink()
    assert store.months_complete(cfg) is False
    record_property("actual", f"CoverageError: {e.value}; months_complete=False")


def test_T1_missing_required_field_fails(cfg, record_property):
    f = cfg["paths"]["raw"] / "yellow_tripdata_2024-04.parquet"
    info = acquire.inspect_file(f)
    info["columns"].pop("PULocationID")
    with pytest.raises(acquire.SchemaError) as e:
        acquire.check_schema(f.name, info["columns"], cfg["required_columns"])
    record_property("actual", f"SchemaError: {e.value}")


def test_T1_full_schema_passes(cfg):
    f = cfg["paths"]["raw"] / "yellow_tripdata_2024-04.parquet"
    cols = {fld.name: str(fld.type) for fld in pq.ParquetFile(f).schema_arrow}
    assert acquire.check_schema(f.name, cols, cfg["required_columns"]) == "matches dictionary"
