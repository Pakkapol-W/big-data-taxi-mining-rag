"""T8: valid evidence ids, sorted top-3, unsupported questions recorded.

Uses tiny hand-made vectors / outputs (no embedding model download), plus the committed corpus and
question files, which are part of the submission.
"""
from __future__ import annotations

import json
import re

import numpy as np

from src import rag, rag_verify
from src.common import ROOT

ID_RE = re.compile(r"^EV-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
CORPUS = {"EV-A": {"text": "Model MAE 19.72, baseline MAE 29.29; peak at 18:00. Share 0.346."},
          "EV-B": {"text": "Trip distance in miles."}, "EV-C": {"text": "Nothing numeric."}}


def test_T8_corpus_ids_valid(record_property):
    path = ROOT / "evidence" / "evidence_corpus.jsonl"
    chunks = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    ids = [c["evidence_id"] for c in chunks]
    assert len(chunks) >= 10 and len(ids) == len(set(ids))
    for c in chunks:
        assert ID_RE.match(c["evidence_id"]), c["evidence_id"]
        assert all(c.get(k) for k in ("source_file", "metric_definition", "timestamp", "text"))
        assert (ROOT / c["source_file"]).exists(), c["source_file"]
        assert "tpep_pickup_datetime," not in c["text"]  # no raw trip rows
    record_property("actual", f"{len(chunks)} chunks, unique ids matching EV-*, each with source_file (exists), "
                              f"metric_definition, timestamp, text")


def test_T8_top3_sorted(record_property):
    docs = np.array([[1, 0, 0], [0.8, 0.6, 0], [0.8, 0.6, 0], [0, 0, 1], [0.6, 0.8, 0]], float)
    ids = ["EV-D1", "EV-D3", "EV-D2", "EV-D4", "EV-D5"]
    hits = rag.top_k(np.array([1.0, 0, 0]), docs, ids, 3)
    assert [h[0] for h in hits] == ["EV-D1", "EV-D2", "EV-D3"]      # 1.0, then tie 0.8 broken by id
    assert [h[1] for h in hits] == sorted([h[1] for h in hits], reverse=True) and len(hits) == 3
    log_path = ROOT / "evidence" / "retrieval_log.json"
    if log_path.exists():
        for r in json.loads(log_path.read_text(encoding="utf-8"))["results"]:
            s = [h["score"] for h in r["top_k"]]
            assert len(s) == 3 and s == sorted(s, reverse=True)
    record_property("actual", "toy query -> EV-D1 (1.0), EV-D2 (0.8), EV-D3 (0.8; tie by id); "
                              "every saved retrieval has 3 hits in descending score")


def test_T8_unsupported_questions_recorded(record_property):
    qs = json.loads((ROOT / "evidence" / "rag_questions.json").read_text(encoding="utf-8"))
    un = [q for q in qs["questions"] if not q["answerable"]]
    assert qs["written_at"] and len(qs["questions"]) == 6 and len(un) == 2
    assert all(q["reference_facts"][0].startswith("INSUFFICIENT EVIDENCE") and not q["expected_evidence"] for q in un)
    q_un = {"question": "How many people could not find a taxi?", "answerable": False, "key_numbers": []}
    q_ok = {"question": "What is the model MAE?", "answerable": True, "key_numbers": [19.72]}
    good = {"condition": "with_evidence", "evidence_ids": ["EV-A", "EV-B", "EV-C"],
            "raw_output": "INSUFFICIENT EVIDENCE: the data has pickups, not unmet demand."}
    bad = {**good, "raw_output": "About 5,000 people [EV-Z]."}
    ans = {**good, "raw_output": "Model MAE is 19.7 vs 29.3 for the baseline, 34.6% share [EV-A]."}
    fab = {**good, "raw_output": "Model MAE is 12.5 [EV-A]."}
    r_good = rag_verify.check(good, q_un, CORPUS, good["evidence_ids"])
    r_bad = rag_verify.check(bad, q_un, CORPUS, good["evidence_ids"])
    r_ans = rag_verify.check(ans, q_ok, CORPUS, good["evidence_ids"])
    r_fab = rag_verify.check(fab, q_ok, CORPUS, good["evidence_ids"])
    assert r_good["auto_pass"] and r_good["says_insufficient"]
    assert not r_bad["auto_pass"] and r_bad["invalid_citations"] == ["EV-Z"] and not r_bad["insufficiency_ok"]
    assert r_ans["auto_pass"] and r_ans["key_numbers_found"] == [19.72]
    assert not r_fab["auto_pass"] and r_fab["unsupported_numbers"] == [12.5]
    record_property("actual", "2 of 6 questions recorded as unanswerable with expected INSUFFICIENT EVIDENCE; "
                              "checker passes a correct refusal and a supported answer, fails invented id EV-Z "
                              "and invented number 12.5")
