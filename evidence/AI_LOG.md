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

