"""Labeled retrieval benchmark: BM25 vs dense vs hybrid RRF, per embedding model.

    python -m capstone.rag_benchmark                      # hash baseline + approved MiniLM
    python -m capstone.rag_benchmark --models hash-128    # offline baseline only

Dataset: benchmark/corpus.json and benchmark/cases.json (61 questions over 27 documents in two
tenants; adapted from Lesson 8). Questions with gold chunks measure Recall@3 and MRR; questions
without gold (unauthorized, insufficient, most adversarial) measure abstention.

Isolation is checked against the corpus labels, independently of the retrieval code: any
returned chunk outside the asking identity's tenant and groups counts as a leak.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from .db import Chunk, Document, database, init_schema
from .embeddings import APPROVED_MODELS, HASH_MODEL, get_embedder
from .retrieval import MODES, index_chunks, retrieve
from .security import Principal

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
TOP_K = 3


def dataset_sha256(directory: Path = BENCHMARK) -> str:
    digest = hashlib.sha256()
    for name in ("corpus.json", "cases.json"):
        digest.update(name.encode() + b"\0" + (directory / name).read_bytes())
    return digest.hexdigest()


def load(directory: Path = BENCHMARK):
    corpus = json.loads((directory / "corpus.json").read_text())
    cases = json.loads((directory / "cases.json").read_text())["cases"]
    return corpus, cases


def build_index(corpus: dict, embedder):
    engine, sessions = database("sqlite://")
    init_schema(engine)
    with sessions.begin() as s:
        rows = []
        for d in corpus["documents"]:
            s.add(Document(id=d["doc_id"], tenant=d["tenant_id"], owner="benchmark", access_group=d["group"],
                           title=d["title"], content_sha256=hashlib.sha256(d["text"].encode()).hexdigest()))
            rows.append(Chunk(id=f"{d['doc_id']}:c0", document_id=d["doc_id"], tenant=d["tenant_id"],
                              access_group=d["group"], text=f"{d['title']}\n{d['text']}", position=0))
        s.add_all(rows)
        s.flush()
        if embedder is not None:
            index_chunks(s, rows, embedder)
    return sessions


def principal(corpus: dict, name: str) -> Principal:
    p = corpus["principals"][name]
    return Principal(p["tenant_id"], name, frozenset(p["groups"]), p.get("role", "reader"))


def evaluate_mode(sessions, corpus, cases, mode: str, embedder) -> dict:
    scope = {f"{d['doc_id']}:c0": (d["tenant_id"], d["group"]) for d in corpus["documents"]}
    recall, rr, latency = [], [], []
    by_category: dict[str, list[float]] = defaultdict(list)
    abstained = abstain_total = leaks = 0
    with sessions() as s:
        retrieve(s, principal(corpus, "alice"), "warm up", TOP_K, mode=mode, embedder=embedder)
        for case in cases:
            who = principal(corpus, case["principal"])
            started = time.perf_counter()
            hits = retrieve(s, who, case["question"], TOP_K, mode=mode, embedder=embedder)
            latency.append((time.perf_counter() - started) * 1000)
            ids = [h["citation_id"] for h in hits]
            leaks += sum(1 for i in ids if scope[i][0] != who.tenant or scope[i][1] not in who.groups)
            gold = set(case["relevant"])
            if gold:
                r = len(set(ids[:TOP_K]) & gold) / len(gold)
                recall.append(r)
                by_category[case["category"]].append(r)
                rr.append(next((1 / rank for rank, i in enumerate(ids, 1) if i in gold), 0.0))
            else:
                abstain_total += 1
                abstained += not ids
    return {"recall_at_3": round(float(np.mean(recall)), 4), "mrr": round(float(np.mean(rr)), 4),
            "recall_at_3_by_category": {k: round(float(np.mean(v)), 4) for k, v in sorted(by_category.items())},
            "abstention_rate": round(abstained / abstain_total, 4) if abstain_total else None,
            "latency_ms_p50": round(float(np.percentile(latency, 50)), 3),
            "latency_ms_p95": round(float(np.percentile(latency, 95)), 3),
            "cross_tenant_or_group_leaks": leaks}


def run_benchmark(models=(HASH_MODEL, "sentence-transformers/all-MiniLM-L6-v2"), modes=MODES,
                  directory: Path = BENCHMARK) -> dict:
    corpus, cases = load(directory)
    results = {}
    for name in models:
        embedder = get_embedder(name)
        sessions = build_index(corpus, embedder)
        results[name] = {mode: evaluate_mode(sessions, corpus, cases, mode, embedder) for mode in modes}
    answerable = sum(1 for c in cases if c["relevant"])
    return {"dataset_sha256": dataset_sha256(directory), "questions": len(cases), "answerable": answerable,
            "abstain_expected": len(cases) - answerable, "documents": len(corpus["documents"]),
            "top_k": TOP_K, "results": results,
            "note": "Synthetic corpus; latency is in-process retrieval only (no network), single CPU thread pool."}


def markdown(report: dict) -> str:
    lines = [f"Questions: {report['questions']} ({report['answerable']} answerable, {report['abstain_expected']} should abstain), "
             f"documents: {report['documents']}, dataset sha256 `{report['dataset_sha256'][:12]}…`", "",
             "| Model | Mode | Recall@3 | MRR | Paraphrase R@3 | Exact-ID R@3 | Abstention | p50 ms | p95 ms | Leaks |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for model, modes in report["results"].items():
        for mode, r in modes.items():
            cat = r["recall_at_3_by_category"]
            lines.append(f"| {model.split('/')[-1]} | {mode} | {r['recall_at_3']:.3f} | {r['mrr']:.3f} | "
                         f"{cat.get('paraphrase', 0):.3f} | {cat.get('exact_id', 0):.3f} | {r['abstention_rate']:.3f} | "
                         f"{r['latency_ms_p50']:.2f} | {r['latency_ms_p95']:.2f} | {r['cross_tenant_or_group_leaks']} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare BM25, dense and hybrid retrieval on the labeled benchmark")
    parser.add_argument("--models", nargs="+", default=[HASH_MODEL, "sentence-transformers/all-MiniLM-L6-v2"],
                        choices=[HASH_MODEL, *APPROVED_MODELS])
    parser.add_argument("--out", default=str(ROOT / "artifacts"))
    args = parser.parse_args(argv)
    report = run_benchmark(args.models)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "rag_benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "rag_benchmark.md").write_text(markdown(report))
    print(markdown(report))
    return 0 if all(r["cross_tenant_or_group_leaks"] == 0 for m in report["results"].values() for r in m.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
