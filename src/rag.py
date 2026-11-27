"""Brief §06 Retrieval - existing embedding model + cosine top-3 + prompts with / without evidence.

Embedding : fastembed TextEmbedding (ONNX Runtime, CPU only) with config.rag.embedding_model
            (BAAI/bge-small-en-v1.5). Model files cached in data/.models (gitignored); internet only once.
Index     : the corpus texts ("title. text"), L2-normalised; no vector database (29 chunks).
Query     : bge's retrieval instruction + question, L2-normalised. Cosine = dot product.
Top-k     : k = config.rag.top_k (3), sorted by score desc, ties -> evidence_id asc.
Prompts   : one WITH the top-3 evidence and one WITHOUT evidence per question -> 12 prompts.
Outputs   : evidence/retrieval_log.json (model, versions, scores), evidence/rag_prompts.json (12 prompts)
Run       : python -m src.rag --mode smoke|full
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from importlib.metadata import version

import numpy as np

from src import evidence
from src.common import ROOT, load_config, log
from src.modelling_io import rel, write_json_atomic

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # bge-*-v1.5 retrieval instruction

INSTRUCTIONS = """You are answering questions about a data analysis of New York City yellow-taxi pickups (April-June 2024).
Rules:
1. Use ONLY the evidence given below. Do not use outside knowledge and do not guess numbers.
2. After every factual claim, cite the supporting evidence ID in square brackets, e.g. [EV-EXAMPLE].
3. If the evidence does not contain what is needed to answer, start your reply with "INSUFFICIENT EVIDENCE:" and say what is missing.
4. Do not claim that one thing causes another; the evidence shows associations only.
5. Answer in at most 5 sentences."""

NO_EVIDENCE = "(No evidence was retrieved for this question.)"


def load_questions() -> list[dict]:
    return json.loads((ROOT / "evidence" / "rag_questions.json").read_text(encoding="utf-8"))["questions"]


def chunk_text(c: dict) -> str:
    return f"{c['title']}. {c['text']}"


class Embedder:
    def __init__(self, cfg: dict):
        from fastembed import TextEmbedding
        cache = cfg["paths"]["models_cache"]
        cache.mkdir(parents=True, exist_ok=True)
        self.name = cfg["rag"]["embedding_model"]
        self.model = TextEmbedding(model_name=self.name, cache_dir=str(cache), cuda=False, threads=1)

    def embed(self, texts: list[str]) -> np.ndarray:
        v = np.vstack(list(self.model.embed(texts))).astype(np.float64)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def top_k(q_vec: np.ndarray, doc_vecs: np.ndarray, ids: list[str], k: int) -> list[tuple[str, float]]:
    """Cosine top-k (vectors already unit length). Sorted by score desc, ties by evidence_id."""
    scores = doc_vecs @ q_vec
    ranked = sorted(zip(ids, scores.tolist()), key=lambda t: (-round(t[1], 6), t[0]))
    return [(i, round(s, 4)) for i, s in ranked[:k]]


def format_evidence(chunks: list[dict]) -> str:
    return "\n".join(f"[{c['evidence_id']}] (source: {c['source_file']}; metric: {c['metric_definition']})\n"
                     f"{chunk_text(c)}" for c in chunks)


def build_prompt(question: str, chunks: list[dict] | None) -> str:
    ev = format_evidence(chunks) if chunks else NO_EVIDENCE
    return f"{INSTRUCTIONS}\n\nEVIDENCE:\n{ev}\n\nQUESTION: {question}\n\nANSWER:"


def run(cfg: dict) -> dict:
    corpus = evidence.load(cfg)
    by_id = {c["evidence_id"]: c for c in corpus}
    ids = [c["evidence_id"] for c in corpus]
    emb = Embedder(cfg)
    doc_vecs = emb.embed([chunk_text(c) for c in corpus])
    k = cfg["rag"]["top_k"]
    questions = load_questions()
    q_vecs = emb.embed([QUERY_PREFIX + q["question"] for q in questions])

    results, prompts = [], []
    for q, qv in zip(questions, q_vecs):
        hits = top_k(qv, doc_vecs, ids, k)
        hit_ids = [h[0] for h in hits]
        results.append({"question_id": q["id"], "question": q["question"], "answerable": q["answerable"],
                        "top_k": [{"rank": r + 1, "evidence_id": i, "score": s} for r, (i, s) in enumerate(hits)],
                        "expected_evidence": q["expected_evidence"],
                        "expected_hit": (bool(set(hit_ids) & set(q["expected_evidence"]))
                                         if q["expected_evidence"] else None)})
        for cond, ch in (("with_evidence", [by_id[i] for i in hit_ids]), ("without_evidence", None)):
            prompts.append({"run_id": f"{q['id']}_{cond}", "question_id": q["id"], "condition": cond,
                            "evidence_ids": hit_ids if ch else [], "prompt": build_prompt(q["question"], ch)})

    log_ = {"generated_at": datetime.now().isoformat(timespec="seconds"), "mode": cfg["mode"],
            "embedding_model": emb.name, "fastembed": version("fastembed"), "onnxruntime": version("onnxruntime"),
            "dimension": int(doc_vecs.shape[1]), "similarity": "cosine (dot product of L2-normalised vectors)",
            "query_prefix": QUERY_PREFIX, "top_k": k, "corpus": rel(cfg["paths"]["evidence"] / "evidence_corpus.jsonl"),
            "corpus_chunks": len(corpus), "results": results}
    ev = cfg["paths"]["evidence"]
    write_json_atomic(log_, ev / "retrieval_log.json")
    write_json_atomic({"instructions": INSTRUCTIONS, "prompts": prompts}, ev / "rag_prompts.json")
    for r in results:
        log("rag", f"{r['question_id']}: " + ", ".join(f"{h['evidence_id']}={h['score']}" for h in r["top_k"])
                   + ("" if r["expected_hit"] is None else f"  expected hit={r['expected_hit']}"))
    return log_


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["smoke", "full"], default="full")
    a = ap.parse_args()
    run(load_config(a.config, a.mode))
