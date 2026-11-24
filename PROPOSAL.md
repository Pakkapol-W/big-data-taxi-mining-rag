# Proposal / 开题

Group / 组号：14

Members / 成员：
- 1820269117 陈祝金 Anunya Tangrungruangchai: Data + Compute
- 1820269118 林伟杰 Pakkapol Wongnitirat: Modelling, LLM, Presentation

Default or alternative / 默认或自选：Default dataset: NYC TLC yellow taxi trips, April–June 2024, joined to the
Taxi Zone Lookup.

Question and target / 问题目标：
For each of the 20 busiest New York City taxi zones, forecast the number of yellow-taxi pickups in the next hour,
and explain the patterns with traceable evidence.
- Target: `pickup_count(zone, hour)`, the number of retained yellow-taxi trips that started in that zone in that
  hour.
- It measures recorded pickups. It does not measure unmet demand, optimal dispatch or causal effects.
- Hour = pickup time rounded down to the hour, New York local time (no daylight-saving change in April–June).
- Forecast zones = the 20 zones with the most retained pickups in April (ties go to the lower zone_id), fixed for
  the whole project.

Sources and scale / 来源规模：

| File | Rows | Size |
|---|---:|---:|
| yellow_tripdata_2024-04.parquet | 3,514,289 | 59.1 MB |
| yellow_tripdata_2024-05.parquet | 3,723,833 | 62.6 MB |
| yellow_tripdata_2024-06.parquet | 3,539,193 | 59.9 MB |
| taxi_zone_lookup.csv | 265 | 12 KB |

- Source: TLC official site (d37ci6vzurychx.cloudfront.net). The data is public and is downloaded by a script.
  URL, date, bytes, rows, SHA-256 and schema are recorded in `data/data_manifest.csv`.
- After cleaning: 10,614,869 retained trips. Zone-hour grid: 263 zones × 2,184 hours = 574,392 rows. Forecast
  file: 20 zones × 2,184 hours = 43,680 rows.

Fields and joins / 字段关联：
- Trips: 19 columns as in the TLC data dictionary. Key fields are `tpep_pickup_datetime`, `PULocationID`,
  `trip_distance`, `fare_amount` and `total_amount`.
- Join: `PULocationID` → `LocationID` in the zone lookup (one row per zone, so the join cannot add rows; 0 trips
  unmatched).
- Zones 264 (Unknown) and 265 (Outside of NYC) are excluded.

Split and metrics / 切分指标：
- Split by time: April = training, May = validation, June = final test. Stored in `config.json` (`splits`, seed 42).
- Baseline: the count in the same zone and hour seven days earlier.
- Metrics: overall MAE and RMSE, plus MAE per zone, computed on the same test rows for the baseline and the model.
- Model, features and parameter settings: ______ (Modelling owner)

Principle → implementation → evidence / 原理与证据：

| Course chapters | Principle | Implementation | Evidence |
|---|---|---|---|
| 1–2 | Business metric; lifecycle; correlation is not causation | Target = recorded pickups per zone-hour, stated as not demand | Report §1 |
| 3 | Acquisition, ETL, quality and quarantine | Scripted download with hashes; 9 written rules; removed rows kept with reasons; input = retained + quarantined | `data/data_manifest.csv`, `evidence/quality_report.csv`, tests T1, T2, T4 |
| 4 | Access-driven storage, partitioning, metadata | Clean Parquet partitioned by pickup month; separate zone dimension table; unique zone-hour keys | `data/clean/pickup_month=…/`, `data/dim/zones.parquet`, test T3 |
| 5 | Map, local combine, shuffle, reduce | Method A (global GROUP BY) vs Method B (per-split counts, then merge); 3 timed runs each | `evidence/experiment_log.csv`, test T5 |
| 6 | Distributions, trends, honest visualisation | ______ (Modelling / Presentation) | ______ |
| 7 | Clustering, temporal validation, baselines | ______ (Modelling) | ______ |
| 8–9 | RAG, prompts, hallucination checks | ______ (LLM) | ______ |
| 10–11 | Minimisation, lineage, quality, responsibility | Only aggregates leave the pipeline; lineage from source column to chart; AI use logged | Report lineage table, `evidence/AI_LOG.md` |

Resources and model access / 资源：
- Hardware: Apple M1 laptop, 8 cores, 16 GB RAM. No cluster, GPU or paid service.
- Software: Python 3.12, DuckDB 2.0.1, pandas 3.1.1, PyArrow 26.0.1, pytest 9.2.0 (pinned in `requirements.txt`).
- Full pipeline run: about 20 seconds. Smoke run on the 20,000-row sample: about 1 second.
- LLM and embedding model access: ______ (LLM owner)

Roles and milestones / 分工节点：

| Date | Milestone | Owner | Status |
|---|---|---|---|
| 2026-11-23 | Acquisition, cleaning, storage, aggregation, benchmark, tests T1–T5 | <your name> | Done |
| 2026-11-25 | Report Part A (Data + Compute) | <your name> | Done |
| ______ | EDA, clustering, baseline and validation; frozen forecast specification | <friend's name> | ______ |
| ______ | Final June test, RAG evaluation, dashboard | <friend's name> | ______ |
| 2026-11-29 | Full run, offline rehearsal, reviews recorded | Both | ______ |
| 2026-11-30 | Submit ZIP and video; presentation | Both | ______ |

Each member reviews the other's modules (see `team_plan.csv`).

Risks / 风险：
- Data coverage: trips near the window edges that TLC filed in the March or July files are not downloaded, so the
  first and last hours may be slightly low.
- Refund rows (rule R4b): 122,197 negative rows that cancel an identical trip are removed. This assumes the original
  trip took place; the rule can be switched off in `config.json`.
- Representativeness: yellow taxis only (no green taxis or app-based rides). 1,222,683 rows with missing values
  (all payment type 0) are kept unfilled.
- Team size: two members cover five roles. Mitigation: the data interface was fixed early in `config.json` and
  `zone_hour.csv`, and the core pipeline was finished first so modelling could start on final data.
- Privacy: no individual trips are sent to an external LLM. Only aggregates and rule descriptions are indexed.

Alternative mapping and teacher confirmation / 自选对应方案与教师确认：Not applicable (default dataset).
