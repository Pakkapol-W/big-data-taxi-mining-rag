# AI log / AI使用记录

Repeat per material use / 每次重要使用填写

Scope: Data + Compute role (brief §04) and its part of the report.
Tool for every entry: Claude Code (Anthropic), model `claude-opus-5-5`, run locally in the project folder.
Original outputs: kept in the local Claude Code session transcripts (session IDs below). They are not submitted
because they contain local file paths. No trip records or personal data were sent to the model. It only saw code,
the brief, aggregate counts and file metadata.

---

## Entry 1: Work plan
Date / 日期：2026-11-23
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 96986c3b)
Purpose / 用途：Break brief §04 into a step-by-step work plan and an implementation plan (files, functions, tests).
Prompt / 输入：Requested a step-by-step plan for the Data + Compute role based on brief §04, then a file-level
implementation plan.
Original output path / 原始输出：DATA_COMPUTE_PLAN.md, IMPLEMENTATION_PLAN.md
Accepted work / 采用内容：Both plans, used as the basis for implementation.
Human verification and corrections / 核验修正：Before implementation the plans were checked again against the brief
and rubric (Entry 2). This found 12 issues, e.g. smoke mode could not run from the submitted ZIP, Method B's combine
step did no work, and config.json had no train/validation/test split. They were fixed in the code.

## Entry 2: Pipeline code
Date / 日期：2026-11-23
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 16394b86)
Purpose / 用途：Implement acquisition, cleaning, storage and the two aggregation methods with a benchmark.
Prompt / 输入：Requested validation of both plans against the brief, then step-by-step implementation. Each step's
code was reviewed and approved before it was written.
Original output path / 原始输出：config.json, requirements.txt, run_pipeline.py, src/common.py, src/acquire.py,
src/clean.py, src/store.py, src/aggregate.py
Accepted work / 采用内容：All listed files (later change: Entry 5).
Human verification and corrections / 核验修正：
- Full pipeline run twice: all outputs byte-identical. Smoke mode runs.
- Counts balance: 10,777,315 input = 10,614,869 retained + 162,446 quarantined.
- Method A and Method B give identical results on 264,748 zone-hours.
- Errors found by these checks and fixed: a DuckDB 2.0 API change; row order changing between runs, which made
  Method B's intermediate count unstable.
- Each file was walked through function by function to confirm understanding before submission.

## Entry 3: Data-quality rule R4b (refund rows)
Date / 日期：2026-11-23
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 16394b86)
Purpose / 用途：While double-checking that there were no exact duplicates, the AI found 123,900 groups of trips
sharing vendor, times and zones, and proposed a rule for them.
Prompt / 输入：Part of the implementation in Entry 2 (verification of rule R4).
Original output path / 原始输出：src/clean.py (rule R4b), config.json (`rules.reversal_pairs`)
Accepted work / 采用内容：Quarantine a negative row that exactly cancels an identical positive trip (122,197 rows) and
keep the original. Pairs with different amounts (1,685) are kept and flagged by R6b.
Human verification and corrections / 核验修正：Evidence reviewed before acceptance: 122,196 exact ± pairs with
identical timestamps, mostly payment type 4 (dispute). The rule can be switched off in config.json, and a test
covers both settings. The assumption that the original trip took place is reported as a limitation.

## Entry 4: Tests T1–T5
Date / 日期：2026-11-23
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 16394b86)
Purpose / 用途：Automated tests for brief T1–T5, and a script that generates the test report from real pytest results.
Prompt / 输入：Part of the implementation in Entry 2.
Original output path / 原始输出：tests/conftest.py, tests/test_acquire.py, tests/test_clean.py,
tests/test_idempotence.py, tests/test_aggregate.py, src/test_report.py, evidence/test_report.md
Accepted work / 采用内容：All listed files. 12 of 12 tests pass.
Human verification and corrections / 核验修正：Expected values come from a 19-trip table counted by hand
(19 = 11 retained + 8 quarantined; 7 zone-hours). They are not taken from the code under test.

## Entry 5: Smoke-mode fix for Method B
Date / 日期：2026-11-24
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 96986c3b)
Purpose / 用途：A code review found that in smoke mode each monthly file fitted in one 500k-row split, so the reduce
step had nothing to merge and did not demonstrate the combine/shuffle stages.
Prompt / 输入：Requested a review of the implemented code, then a fix for this issue.
Original output path / 原始输出：config.json (`benchmark.smoke_batch_rows`), src/aggregate.py
Accepted work / 采用内容：Smoke mode uses 2,000-row splits (12 map tasks, 8 partial rows merged by the reduce) and
checks that the reduce merged at least one row. Full mode is unchanged (500k-row splits, 22 map tasks).
Human verification and corrections / 核验修正：Split sizes from 500k down to 500 rows were compared on the sample
before choosing 2,000. Smoke re-run (A == B, exit code 0) and tests re-run (12/12 pass).

---

Scope (Entries 6–12): Modelling, LLM and Presentation roles (brief §05–§07) and their tests T6–T8.
Tool for every entry: Claude Code (Anthropic), model `claude-opus-5-5`, run locally on the Windows PC
(i9-13900H, 16 GB RAM), session 0d4a34ad. Same transcript policy as above. No trip records were sent to the model.
Separate from AI assistance: the 12 RAG answers are themselves LLM outputs (local Ollama `llama3.1:8b`); they are
experiment data, logged with prompts and model digest in `evidence/llm_outputs/`, not code assistance.

## Entry 6: Architecture plan and decisions
Date / 日期：2026-11-28
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 0d4a34ad)
Purpose / 用途：Read brief §05–§07, the proposal, team_plan.csv and the existing Data + Compute code; propose a
pipeline design (stages 4–13, file contracts, stack) for approval before any code was written.
Prompt / 输入：Asked for architectural choices to be presented for approval first, runnable on both the Windows PC
and the M1 MacBook.
Original output path / 原始输出：Session transcript (design table: stages, modules, inputs/outputs, tests, stack).
Accepted work / 采用内容：Design approved. Choices made by the human from presented options: HistGradientBoosting
regressor; fastembed `BAAI/bge-small-en-v1.5` embeddings; Ollama `llama3.1:8b`; ECharts bundled locally. Also
decided by the human: peak = weekday target hours 07:00–09:00; Modelling owner in team_plan.csv = Pakkapol
(reviewer Anunya); k = 2 kept (Entry 8); the six RAG questions and reference answers (Entry 10).
Human verification and corrections / 核验修正：Each choice was approved before the related code was written. The
AI's first peak proposal (07–09 and 16–19) was narrowed by the human to 07–09 only.

## Entry 7: Portability fixes to the Data + Compute code
Date / 日期：2026-11-28
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 0d4a34ad)
Purpose / 用途：Make the existing pipeline run on Windows; check cross-platform reproducibility.
Prompt / 输入：Part of the requirement that code runs on both machines.
Original output path / 原始输出：src/aggregate.py (`hardware()`), src/common.py (console encoding)
Accepted work / 采用内容：(1) `hardware()` used `os.sysconf`, which does not exist on Windows (T3/T5 failed); it now
reads RAM via the Windows API and keeps the macOS/Linux path unchanged. (2) The `✓` in log messages crashed the
Windows cp1252 console; stdout/stderr are reconfigured to UTF-8 when not already UTF-8 (no change on macOS).
Human verification and corrections / 核验修正：Before the fix 3 of 12 tests failed on Windows; after it 12/12 pass and
smoke mode runs. A full run on Windows gave content-identical clean/aggregate outputs to the committed M1 run
(manifest, quality report, zone_hour.csv, top_zones.csv; A == B on 264,748 keys). Finding reported to the Data owner:
the seeded 20,000-row DuckDB reservoir sample differs between platforms (35 of 20,000 rows shared), so the
committed sample.parquet was restored; Anunya's committed evidence files were restored after the Windows run.

## Entry 8: Exploration and clustering (stages 4–5)
Date / 日期：2026-11-28
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 0d4a34ad)
Purpose / 用途：EDA numbers (temporal, zone, data-quality questions) and K-means on April 24-hour profiles.
Prompt / 输入：Implement stages 4–5 of the approved design.
Original output path / 原始输出：src/modelling_io.py, src/explore.py, src/cluster.py, outputs/eda_summary.json,
outputs/clusters.csv, outputs/cluster_selection.csv, outputs/cluster_profiles.csv, outputs/cluster_memberships.csv
Accepted work / 采用内容：All listed files. Selection rule (max silhouette with every cluster >= 2 zones; May ARI
reported only; June never read) was written into the code before results were seen. Result: k = 2 (silhouette 0.533,
sizes 17/3).
Human verification and corrections / 核验修正：The AI laid out how k = 2 and k = 3 would be interpreted (members, profile
shares); the human kept k = 2 under the pre-set rule and asked for k = 3 to be reported as a sensitivity check
(cluster_memberships.csv). Checked: experiment_log.csv rewrite only added CLU rows (Data owner rows unchanged).
Fixed: pandas 3 deprecation (`dayofweek` → `day_of_week`); a duplicated model fit per k.

## Entry 9: Features, forecast and tests T6–T7 (stages 6–7)
Date / 日期：2026-11-28
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 0d4a34ad)
Purpose / 用途：Past-only features, 7-day baseline, tuning on May, frozen specification, June test, error analysis.
Prompt / 输入：Implement stages 6–7 and tests T6–T7 of the approved design.
Original output path / 原始输出：src/features.py, src/forecast.py, tests/test_model.py, outputs/predictions.csv,
outputs/metrics.json, evidence/model_spec_frozen.json, evidence/feature_availability.csv, config.json (`model`)
Accepted work / 采用内容：All listed files. Chosen setting hgb_d (Poisson loss) by May MAE; June MAE 19.72 vs
baseline 29.29, RMSE 30.95 vs 47.00, 14,400 test rows; 3,360 rows without 168 h history dropped and counted.
Human verification and corrections / 核验修正：T6/T7 expected values are independent of the code under test: lags and
24 h mean hand-computed on a synthetic grid; perturbing y(≥h) leaves row-h features unchanged; perturbing June leaves
clusters and May selection unchanged; MAE/RMSE checked on [0,2,4] vs [1,2,1]. 17/17 tests pass. Reruns give the same
metrics (deterministic seed, early stopping off).

## Entry 10: RAG pipeline and test T8 (stages 8–11)
Date / 日期：2026-11-28
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 0d4a34ad)
Purpose / 用途：Evidence corpus from aggregates, embedding retrieval, prompts, a script to run the local LLM once,
automatic verification, and test T8.
Prompt / 输入：Implement stages 8–11 of the approved design; draft the six questions with reference answers.
Original output path / 原始输出：evidence/data_dictionary.csv, evidence/rag_questions.json, src/evidence.py, src/rag.py,
scripts/run_llm.py, src/rag_verify.py, tests/test_rag.py; generated evidence/evidence_corpus.jsonl,
retrieval_log.json, rag_prompts.json, llm_outputs/ (12 files), rag_evaluation.json
Accepted work / 采用内容：All listed files. 29 chunks; retrieval found expected evidence for 4/4 answerable questions;
automatic checks 12/12.
Human verification and corrections / 核验修正：The six questions and reference answers drafted by the AI were approved
by the human before any LLM query (timestamp in rag_questions.json). The human reviewed all 12 original outputs in
evidence/rag_human_review.json (12/12 pass). Original outputs are never edited. Fixed: experiment_log value stored as
"264748.0" broke integer parsing. The AI reported a rounding slip in its own summary (it had written 32.7% where the
evidence says 32.6%).

## Entry 11: Dashboard and number check (stages 12–13)
Date / 日期：2026-11-28
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 0d4a34ad)
Purpose / 用途：Offline HTML dashboard with six charts, zone and date filters, LLM evidence and conclusions; automated
check that shown values match outputs and that the page is offline.
Prompt / 输入：Build the dashboard per brief §07 with the approved stack.
Original output path / 原始输出：src/dashboard.py, src/check_dashboard.py, dashboard/index.html,
dashboard/assets/app.js, dashboard/assets/style.css, dashboard/assets/echarts.min.js (ECharts 5.5.1, Apache-2.0,
downloaded once; SHA-256 in evidence/integration_check.json), evidence/integration_check.json
Accepted work / 采用内容：All listed files. 21/21 automated checks (offline references, 11 traced values, numbers in the
narrative backed by cited evidence). Narrative wording is templated in src/dashboard.py for the human to edit.
Human verification and corrections / 核验修正：The page was rendered in headless Chrome (light and dark mode; all zones,
3 zones × 2 weeks, April only, no zones). Errors found by looking at the renders and fixed: the heatmap colour scale
read the day count instead of the value (single colour); the improvement figure showed 32.7% in one place and 32.6%
in another (JavaScript vs Python rounding of 0.3265; now one label from Python); clipped axis titles. Colour palette
checked with a palette validator (light and dark). Manual T9 steps are listed in integration_check.json.

## Entry 12: Integration
Date / 日期：2026-11-28
Tool/model / 工具模型：Claude Code, claude-opus-5-5 (session 0d4a34ad)
Purpose / 用途：Connect the new stages to run_pipeline.py (smoke and full), extend the test report, pin dependencies.
Prompt / 输入：Part of Entries 8–11.
Original output path / 原始输出：run_pipeline.py, src/test_report.py, requirements.txt (scikit-learn 1.9.1,
fastembed 0.9.1, onnxruntime 1.32.0), .gitignore (data/.models/), team_plan.csv, evidence/test_report.md
Accepted work / 采用内容：All listed files. Smoke mode runs every stage except the one-off LLM script; full mode
replays the saved LLM outputs and fails if fresh retrieval no longer matches them. 20/20 tests pass.
Human verification and corrections / 核验修正：To be completed by Pakkapol after reviewing the code.

