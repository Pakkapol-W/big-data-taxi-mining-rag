"""T5 — hand-counted fixture matches both aggregation methods; (zone_id, hour) keys are unique."""
import pandas as pd

from src import aggregate
from src.common import connect
from tests.conftest import EXPECTED, HAND_COUNTS


def _as_dict(df: pd.DataFrame) -> dict:
    return {(int(r.zone_id), r.hour.strftime("%Y-%m-%d %H:%M:%S")): int(r.pickup_count)
            for r in df.itertuples()}


def test_T5_both_methods_match_hand_counts(cleaned, record_property):
    cfg, _, res = cleaned
    files = aggregate.clean_files(cfg)
    a, inter_a, _ = aggregate.method_a(connect(cfg), files)
    b, inter_b, tasks_b = aggregate.method_b(files, batch_rows=2)
    assert _as_dict(a) == HAND_COUNTS
    assert _as_dict(b) == HAND_COUNTS
    aggregate.assert_equivalent(a, b, res["retained_rows"])
    assert not a.duplicated(["zone_id", "hour"]).any()
    assert inter_a == EXPECTED["retained"]
    assert inter_b > len(b)          # with 2-row splits the same key appears in several partials
    record_property("actual", f"A == B == hand counts ({len(a)} keys, sum {a.pickup_count.sum()}); "
                              f"B: {tasks_b} map tasks, {inter_b} partial records -> {len(b)} keys")


def test_T5_benchmark_logs_runs_and_medians(cleaned):
    cfg, _, res = cleaned
    aggregate.run(cfg, retained_rows=res["retained_rows"])
    log = pd.read_csv(cfg["paths"]["evidence"] / "experiment_log.csv")
    assert set(log["metric"]) >= {"seconds", "median_seconds", "identical_output_keys"}
    assert (log["metric"] == "median_seconds").sum() == 2
