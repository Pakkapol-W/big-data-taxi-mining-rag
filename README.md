# NYC taxi zone-hour pickups: mining, forecasting and evidence-grounded LLM answers

Introduction to Big Data capstone, Group 14.

For each of the 20 busiest NYC yellow-taxi pickup zones (April–June 2024) we forecast the number of pickups in the
next hour, find zone patterns, and explain the results with evidence-grounded LLM answers. The target is
**recorded pickups** per zone and hour (New York local time). It is not demand.

| Member | Roles | Reviewer of |
|---|---|---|
| 1820269117 陈祝金 Anunya Tangrungruangchai | Data, Compute | Modelling, LLM, Presentation |
| 1820269118 林伟杰 Pakkapol Wongnitirat | Modelling, LLM, Presentation | Data, Compute |

Details: `team_plan.csv`, `contributions.csv`, `PROPOSAL.md`.

## Hardware and software

| | Official run | Development |
|---|---|---|
| Machine | Apple M1, 8 cores, 16 GB RAM, macOS 14 | Intel i9-13900H, 16 GB RAM, Windows 11 |
| GPU | not used | not used |

- Python 3.12. All packages are pinned in `requirements.txt` and have wheels for macOS arm64 and Windows x64.
- No cluster, database server, GPU, paid API or API key is needed.
- The embedding model (`BAAI/bge-small-en-v1.5`, about 70 MB) is downloaded once into `data/.models/`.
- The 12 LLM answers were produced once with a local Ollama model (`llama3.1:8b`). Full mode replays them, so
  Ollama is only needed to re-query.

## Install

macOS / Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```bash
# Smoke: 20,000-row sample shipped in the ZIP; checks every code path (no accuracy claim). Writes to outputs/smoke/.
python run_pipeline.py --config config.json --mode smoke

# Full: downloads the 3 TLC files + zone lookup (~180 MB, skipped if already present and unchanged),
# cleans, aggregates (methods A and B, timed 3x), explores, clusters, forecasts, rebuilds the evidence corpus,
# recomputes retrieval, replays and verifies the saved LLM outputs, builds the dashboard data and checks it.
python run_pipeline.py --config config.json --mode full

# Tests T1-T8 and the test report (evidence/test_report.md)
python -m src.test_report
```

Each stage can also be rerun alone, e.g. `python -m src.forecast --mode full` (stages: `explore`, `cluster`,
`forecast`, `evidence`, `rag`, `rag_verify`, `dashboard`, `check_dashboard`). Do not run `python -m src.aggregate` by
itself: it rewrites `evidence/experiment_log.csv` with only the aggregation rows. Run the full pipeline instead.

Optional: query the LLM again. This creates new outputs with a new date, so the human review must then be redone.

```bash
ollama pull llama3.1:8b
python scripts/run_llm.py --config config.json --force
python -m src.rag_verify --mode full
```

## Dashboard

Open `dashboard/index.html` in a browser by double-clicking it. It works offline with no server. All scripts are
local files (ECharts 5.5.1 is bundled in `dashboard/assets/`). The data is `dashboard/assets/data.js`, generated
by `src/dashboard.py` from the output files. `src/check_dashboard.py` confirms that the values shown match the
outputs (`evidence/integration_check.json`).

## Pipeline and files

```
acquire -> clean -> aggregate (A == B) -> zone_hour -> explore -> cluster -> features/forecast
        -> evidence corpus -> retrieval -> [run_llm, once] -> verify -> dashboard -> dashboard check
```

| Path | Content |
|---|---|
| `config.json` | paths, seed 42, window, rules, splits (train 2024-04 / valid 2024-05 / test 2024-06), cluster, model and RAG settings |
| `src/acquire.py`, `clean.py`, `store.py`, `aggregate.py` | download + manifest, quality rules R1–R7 and R4b, partitions, zone-hour grid, methods A/B |
| `src/explore.py`, `cluster.py`, `features.py`, `forecast.py` | EDA numbers, K-means on April profiles, past-only features, baseline vs HistGradientBoosting |
| `src/evidence.py`, `rag.py`, `rag_verify.py`, `scripts/run_llm.py` | evidence corpus, cosine top-3 retrieval, prompts, local LLM run, checks |
| `src/dashboard.py`, `check_dashboard.py`, `dashboard/` | offline dashboard and its number check |
| `data/data_manifest.csv`, `data/sample.parquet`, `data/dim/zones.parquet` | manifest (URL, bytes, rows, SHA-256), 20,000-row sample, zone dimension |
| `outputs/zone_hour.csv` | 20 zones × 2,184 hours with coverage flag |
| `outputs/clusters.csv`, `predictions.csv`, `metrics.json` | clusters, June predictions (`zone_id, hour, y_true, y_pred, baseline`), metrics |
| `evidence/quality_report.csv`, `experiment_log.csv`, `test_report.md` | rule counts, experiments and timings, test results |
| `evidence/data_dictionary.csv` | field dictionary with units |
| `evidence/rag_questions.json`, `evidence_corpus.jsonl`, `llm_outputs/`, `rag_human_review.json`, `rag_evaluation.json` | questions + reference answers, corpus, 12 original LLM outputs, human review, evaluation |
| `evidence/AI_LOG.md` | AI assistance disclosure |

Raw data (`data/raw/`), clean partitions, the model cache and smoke outputs are not submitted. They are rebuilt by
the commands above.

## Data, privacy and AI use

- Source: NYC TLC yellow-taxi trip records (public), downloaded by script. URL, date, bytes, rows and SHA-256 are in
  `data/data_manifest.csv`.
- Only aggregates and rule descriptions are indexed for the LLM. The LLM runs locally, so no trip records leave the
  machine.
- AI assistance (Claude Code) is disclosed per use in `evidence/AI_LOG.md`.
