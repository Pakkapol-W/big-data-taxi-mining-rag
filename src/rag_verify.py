"""Brief §06 Verification of the 12 saved LLM outputs (automatic checks + human review).

Automatic checks per output (code, repeatable):
  citations_valid       every cited [EV-...] id exists in the corpus
  citations_retrieved   (with evidence) every cited id was among the retrieved top-3
  has_citation          (answerable, with evidence) at least one citation
  insufficiency_ok      says "INSUFFICIENT EVIDENCE" exactly when expected: unanswerable questions, and
                        every no-evidence run (the prompt allows evidence only)
  numbers_supported     every number in the answer appears (or is a rounding of a number) in the cited
                        evidence or the question; unsupported numbers are listed
Human review (evidence/rag_human_review.json, filled by a person, never by code): retrieval relevance,
numbers vs reference facts, citation support, unsupported inference, verdict, errors, corrected answer.
Original outputs (evidence/llm_outputs/) are never modified; corrected answers live only in the review file.

Full mode replays the saved outputs but recomputes retrieval: if a saved run's evidence ids differ from
the fresh top-3, the run fails (the saved answer would be stale). Text-only prompt drift is reported.

Output: evidence/rag_evaluation.json      Run: python -m src.rag_verify --mode smoke|full
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime

from src import evidence, rag
from src.common import load_config, log
from src.modelling_io import rel, write_json_atomic

CITE_RE = re.compile(r"EV-[A-Z0-9]+(?:-[A-Z0-9]+)*")
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
NUM_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
INSUFF = "INSUFFICIENT EVIDENCE"
REVIEW_FIELDS = {"reviewer": "", "reviewed_at": "", "retrieval_relevant": None, "numbers_match_reference": None,
                 "citations_support_claims": None, "unsupported_inference": None, "verdict": None,
                 "errors": [], "corrected_answer": ""}


def numbers(text: str) -> list[float]:
    """Numbers in text; 'HH:MM' counts as the hour; evidence ids and thousands separators handled."""
    text = CITE_RE.sub(" ", text)
    out = [float(h) for h, _ in TIME_RE.findall(text)]
    text = TIME_RE.sub(" ", text)
    for m in NUM_RE.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _decimals(x: float) -> int:
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0


def supported(n: float, pool: list[float]) -> bool:
    """n is supported if it equals a pool number, is a rounding of one, or is a % of a share."""
    tol = 0.5 * 10 ** -_decimals(n) + 1e-9
    return any(abs(n - m) <= tol or abs(n - m * 100) <= tol for m in pool)


def check(run: dict, q: dict, corpus: dict, fresh_ids: list[str]) -> dict:
    out = run["raw_output"]
    cited = sorted(set(CITE_RE.findall(out)))
    with_ev = run["condition"] == "with_evidence"
    says_insuff = INSUFF in out.upper()
    expect_insuff = (not q["answerable"]) or not with_ev
    pool = numbers(q["question"])
    for i in cited:
        if i in corpus:
            pool += numbers(corpus[i]["text"])
    nums = numbers(out)
    unsupported = sorted({n for n in nums if not supported(n, pool)})
    res = {
        "cited_ids": cited,
        "citations_valid": all(i in corpus for i in cited),
        "invalid_citations": [i for i in cited if i not in corpus],
        "citations_retrieved": all(i in run["evidence_ids"] for i in cited) if with_ev else (not cited),
        "has_citation": bool(cited) if (with_ev and q["answerable"]) else None,
        "says_insufficient": says_insuff, "expected_insufficient": expect_insuff,
        "insufficiency_ok": says_insuff == expect_insuff,
        "numbers_in_answer": nums, "unsupported_numbers": unsupported,
        "numbers_supported": not unsupported,
        "key_numbers_found": [k for k in q["key_numbers"] if any(abs(k - n) < 0.051 for n in nums)],
        "retrieval_matches_saved": run["evidence_ids"] == fresh_ids if with_ev else None,
    }
    checks = ["citations_valid", "citations_retrieved", "insufficiency_ok", "numbers_supported"]
    if res["has_citation"] is not None:
        checks.append("has_citation")
    res["auto_failed"] = [c for c in checks if not res[c]]
    res["auto_pass"] = not res["auto_failed"]
    return res


def load_review(path, run_ids: list[str]) -> dict:
    """Keep every human entry; add blank templates for runs not yet reviewed."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"instructions": (
        "Fill one entry per run by hand: booleans true/false, verdict 'pass' or 'fail', list specific errors, "
        "and write a corrected answer for failed runs. Never edit evidence/llm_outputs/."), "reviews": {}}
    for r in run_ids:
        data["reviews"].setdefault(r, dict(REVIEW_FIELDS))
    return data


def run(cfg: dict) -> dict:
    ev = cfg["paths"]["evidence"]
    corpus = {c["evidence_id"]: c for c in evidence.load(cfg)}
    questions = {q["id"]: q for q in rag.load_questions()}
    retr = json.loads((ev / "retrieval_log.json").read_text(encoding="utf-8"))
    prompts = {p["run_id"]: p for p in json.loads((ev / "rag_prompts.json").read_text(encoding="utf-8"))["prompts"]}
    fresh = {r["question_id"]: [h["evidence_id"] for h in r["top_k"]] for r in retr["results"]}

    retrieval = [{"question_id": r["question_id"], "top_k": r["top_k"], "expected_evidence": r["expected_evidence"],
                  "expected_hit": r["expected_hit"]} for r in retr["results"]]
    answerable = [r for r in retrieval if r["expected_hit"] is not None]
    summary = {"retrieval_hits": f"{sum(r['expected_hit'] for r in answerable)}/{len(answerable)}"}

    runs, out_dir = [], cfg["paths"]["llm_outputs"]
    saved = sorted(out_dir.glob("*.json")) if cfg["mode"] == "full" else []
    drift = []
    for path in saved:
        r = json.loads(path.read_text(encoding="utf-8"))
        res = check(r, questions[r["question_id"]], corpus, fresh[r["question_id"]])
        if res["retrieval_matches_saved"] is False:
            raise RuntimeError(f"{r['run_id']}: saved evidence {r['evidence_ids']} != fresh top-3 "
                               f"{fresh[r['question_id']]}; rerun scripts/run_llm.py --force")
        if r["prompt"] != prompts[r["run_id"]]["prompt"]:
            drift.append(r["run_id"])
        runs.append({"run_id": r["run_id"], "question_id": r["question_id"], "condition": r["condition"],
                     "model": r["model"], "model_digest": r["model_digest"], "date": r["date"],
                     "output_file": rel(path), "raw_output": r["raw_output"], "auto": res})

    review_path = ev / "rag_human_review.json"
    if runs:
        review = load_review(review_path, [r["run_id"] for r in runs])
        write_json_atomic(review, review_path)
        for r in runs:
            r["human"] = review["reviews"][r["run_id"]]
        done = [r for r in runs if r["human"]["verdict"] in ("pass", "fail")]
        summary.update({
            "llm_runs": len(runs),
            "auto_passed": f"{sum(r['auto']['auto_pass'] for r in runs)}/{len(runs)}",
            "human_reviewed": f"{len(done)}/{len(runs)}",
            "human_passed": f"{sum(r['human']['verdict'] == 'pass' for r in done)}/{len(runs)}" if done else "pending",
            "prompt_text_drift": drift,
            "auto_errors": [f"{r['run_id']}: {', '.join(r['auto']['auto_failed'])}"
                            + (f" (unsupported numbers {r['auto']['unsupported_numbers']})" if r["auto"]["unsupported_numbers"] else "")
                            for r in runs if not r["auto"]["auto_pass"]],
            "human_errors": [f"{r['run_id']}: {e}" for r in runs for e in r["human"]["errors"]],
        })
    else:
        summary["llm_runs"] = 0
        summary["note"] = ("smoke mode: retrieval only, LLM outputs not replayed" if cfg["mode"] == "smoke"
                           else "no saved LLM outputs yet - run scripts/run_llm.py")
    result = {"generated_at": datetime.now().isoformat(timespec="seconds"), "mode": cfg["mode"],
              "embedding_model": retr["embedding_model"], "questions_file": "evidence/rag_questions.json",
              "summary": summary, "retrieval": retrieval, "runs": runs}
    write_json_atomic(result, ev / "rag_evaluation.json")
    log("rag_verify", " ".join(f"{k}={v}" for k, v in summary.items() if k not in ("auto_errors", "human_errors")))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="full")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
