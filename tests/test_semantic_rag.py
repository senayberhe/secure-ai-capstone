"""Challenge 1: retrieval modes, cached vectors, labeled benchmark and isolation."""
import pytest
from capstone.db import ChunkEmbedding
from capstone.embeddings import APPROVED_MODELS, SentenceTransformerEmbedder, get_embedder
from capstone.rag_benchmark import load, run_benchmark
from capstone.retrieval import MODES, retrieve
from capstone.security import Principal

MINILM = "sentence-transformers/all-MiniLM-L6-v2"


def minilm_available():
    try:
        get_embedder(MINILM)
        return True
    except Exception:
        return False


def test_benchmark_has_enough_labeled_questions():
    corpus, cases = load()
    assert len(cases) >= 50
    assert sum(1 for c in cases if c["relevant"]) >= 30
    assert {c["category"] for c in cases} >= {"paraphrase", "exact_id", "unauthorized", "insufficient"}


def test_baseline_benchmark_never_leaks_across_tenants_or_groups():
    report = run_benchmark(models=["hash-128"])
    for mode, r in report["results"]["hash-128"].items():
        assert r["cross_tenant_or_group_leaks"] == 0, mode
        assert 0 <= r["recall_at_3"] <= 1 and r["latency_ms_p95"] > 0
    assert report["results"]["hash-128"]["bm25"]["recall_at_3"] >= 0.85


@pytest.mark.skipif(not minilm_available(), reason="approved MiniLM weights not cached locally")
def test_semantic_hybrid_beats_lexical_on_paraphrases():
    r = run_benchmark(models=["hash-128", MINILM])["results"]
    semantic, baseline = r[MINILM], r["hash-128"]
    assert all(m["cross_tenant_or_group_leaks"] == 0 for m in semantic.values())
    assert semantic["hybrid"]["recall_at_3"] >= 0.9
    assert semantic["hybrid"]["recall_at_3_by_category"]["paraphrase"] > baseline["hybrid"]["recall_at_3_by_category"]["paraphrase"]
    assert semantic["hybrid"]["recall_at_3_by_category"]["exact_id"] == 1.0
    assert semantic["hybrid"]["abstention_rate"] > semantic["bm25"]["abstention_rate"]


def test_unapproved_model_rejected():
    with pytest.raises(ValueError):
        SentenceTransformerEmbedder("someone/unreviewed-model")
    assert MINILM in APPROVED_MODELS


def test_upload_caches_vectors_and_every_mode_stays_tenant_scoped(app, client, headers):
    client.post("/v1/documents", headers=headers["alice"], json={"title":"Travel","group":"engineering","text":"Receipts are due within thirty days of travel."})
    client.post("/v1/documents", headers=headers["bob"], json={"title":"Other","group":"engineering","text":"Other tenant receipts are due within ninety days."})
    with app.state.sessions() as s:
        assert s.query(ChunkEmbedding).count() == 2
        acme = Principal("acme", "alice", frozenset(["engineering"]), "editor")
        for mode in MODES:
            texts = [h["text"] for h in retrieve(s, acme, "when are receipts due", mode=mode)]
            assert texts and all("Other tenant" not in t for t in texts), mode


def test_missing_vectors_are_computed_and_cached(app):
    from capstone.db import Chunk, Document
    with app.state.sessions.begin() as s:
        s.add(Document(id="legacy", tenant="acme", owner="a", access_group="engineering", title="Legacy", content_sha256="0"*64))
        s.add(Chunk(id="legacy:c0", document_id="legacy", tenant="acme", access_group="engineering", text="legacy lodging rules", position=0))
    with app.state.sessions() as s:
        retrieve(s, Principal("acme", "a", frozenset(["engineering"]), "r"), "lodging rules", mode="dense")
    with app.state.sessions() as s:
        assert s.get(ChunkEmbedding, ("legacy:c0", "hash-128")) is not None
