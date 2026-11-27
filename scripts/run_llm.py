"""Brief §06 - run the 12 prompts through a LOCAL LLM once and save the genuine outputs.

Not part of run_pipeline.py: it needs Ollama (https://ollama.com) with the model pulled, e.g.
    ollama pull llama3.1:8b
    python scripts/run_llm.py --config config.json            # skips runs already saved
    python scripts/run_llm.py --config config.json --force    # re-query and overwrite (new date/output)

Reads  evidence/rag_prompts.json (built by src.rag). Only aggregate evidence text is sent; no trips.
Writes evidence/llm_outputs/<run_id>.json with: model name + digest, Ollama version, date, options,
       the full prompt exactly as sent, and the raw output exactly as returned (never edited).
Human corrections go to evidence/rag_human_review.json, never into these files.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.common import load_config, log  # noqa: E402
from src.modelling_io import write_json_atomic  # noqa: E402


def model_digest(host: str, name: str) -> str:
    tags = requests.get(f"{host}/api/tags", timeout=30).json().get("models", [])
    for m in tags:
        if m.get("name") == name or m.get("model") == name:
            return m.get("digest", "")
    raise SystemExit(f"model {name!r} not found in Ollama; run: ollama pull {name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--force", action="store_true", help="re-query runs that already have a saved output")
    a = ap.parse_args()
    cfg = load_config(a.config, "full")
    llm = cfg["rag"]["llm"]
    host, name = llm["host"].rstrip("/"), llm["model"]
    try:
        ollama_version = requests.get(f"{host}/api/version", timeout=10).json()["version"]
    except requests.RequestException as e:
        raise SystemExit(f"Ollama not reachable at {host}: {e}")
    digest = model_digest(host, name)
    options = {"temperature": llm["temperature"], "seed": llm["seed"], "num_ctx": llm["num_ctx"]}
    prompts = json.loads((cfg["paths"]["evidence"] / "rag_prompts.json").read_text(encoding="utf-8"))["prompts"]
    out_dir = cfg["paths"]["llm_outputs"]
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in prompts:
        path = out_dir / f"{p['run_id']}.json"
        if path.exists() and not a.force:
            log("llm", f"{p['run_id']}: saved output exists, skipped (use --force to re-query)")
            continue
        started = datetime.now().astimezone()
        r = requests.post(f"{host}/api/generate", timeout=900,
                          json={"model": name, "prompt": p["prompt"], "stream": False, "options": options})
        r.raise_for_status()
        body = r.json()
        rec = {"run_id": p["run_id"], "question_id": p["question_id"], "condition": p["condition"],
               "evidence_ids": p["evidence_ids"],
               "model": name, "model_digest": digest, "provider": "ollama (local)", "ollama_version": ollama_version,
               "date": started.isoformat(timespec="seconds"), "options": options,
               "prompt": p["prompt"], "raw_output": body["response"],
               "stats": {k: body.get(k) for k in ("total_duration", "prompt_eval_count", "eval_count", "done_reason")}}
        write_json_atomic(rec, path)
        log("llm", f"{p['run_id']}: {len(body['response'])} chars in {body.get('total_duration', 0) / 1e9:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
