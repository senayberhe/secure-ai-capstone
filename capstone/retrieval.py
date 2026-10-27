"""Authorized hybrid retrieval (deterministic hash embeddings + BM25 + RRF).

Offline hash embeddings are NOT semantically trained. Replace with an approved embedding
model and access-scoped ANN index after evaluating that system.
"""
import math
import os
import re
from collections import Counter
import numpy as np
from sqlalchemy import select
from .db import Document, Chunk, ChunkEmbedding
from .embeddings import from_bytes, get_embedder, to_bytes
from .security import Principal

WORD = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")


def tokens(text: str) -> list[str]:
    return WORD.findall(text.casefold())


def identifiers(text: str) -> set[str]:
    """Tokens containing a digit, such as ERR-4031 or rb-404: exact identifiers a user may ask about."""
    return {t for t in tokens(text) if any(ch.isdigit() for ch in t)}


def split_text(text: str, max_words: int = 65, overlap: int = 12) -> list[str]:
    if not 0 <= overlap < max_words:
        raise ValueError("Invalid overlap")
    words = text.split()
    if not words:
        raise ValueError("Empty document")
    stride = max_words - overlap
    chunks = []
    for i in range(0, len(words), stride):
        chunks.append(" ".join(words[i:i + max_words]))
        if i + max_words >= len(words):
            break
    return chunks


def hash_vector(text: str, dims: int = 128) -> np.ndarray:
    """Deprecated alias kept for callers of the original API; see embeddings.HashEmbedder."""
    from .embeddings import HashEmbedder
    return HashEmbedder().encode([text])[0].astype(np.float64)


def bm25_scores(query: str, corpus: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    if not corpus:
        return []
    lists = [tokens(text) for text in corpus]
    doc_count = len(lists)
    avg_len = sum(map(len, lists)) / doc_count or 1
    counts = [Counter(words) for words in lists]
    df = Counter(w for count in counts for w in count)
    q = set(tokens(query))
    result = []
    for words, count in zip(lists, counts):
        score = 0.0
        for term in q:
            f = count[term]
            if not f:
                continue
            idf = math.log(1 + (doc_count - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(words) / avg_len))
        result.append(score)
    return result


def authorized_candidates(session, principal: Principal, group: str | None = None) -> list[tuple[Chunk, str]]:
    """Access-scope SQL rows BEFORE embedding or scoring. No cross-tenant candidates."""
    if group is not None and group not in principal.groups:
        return []
    if not principal.groups:
        return []
    stmt = (select(Chunk, Document.title)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.tenant == principal.tenant,
                   Chunk.access_group.in_(list(principal.groups))))
    if group is not None:
        stmt = stmt.where(Chunk.access_group == group)
    return list(session.execute(stmt).all())


MODES = ("bm25", "dense", "hybrid")
# Minimum cosine similarity for a dense hit to count as evidence. A pretrained model scores every
# chunk above zero, so without a floor it would never abstain. Calibrated on benchmark/ (see
# docs/EVALUATION.md); override with CAPSTONE_MIN_DENSE_SCORE.
DEFAULT_MIN_DENSE = {"hash-128": 1e-9}
SEMANTIC_MIN_DENSE = 0.35


def min_dense_for(embedder) -> float:
    override = os.getenv("CAPSTONE_MIN_DENSE_SCORE")
    if override:
        return float(override)
    return DEFAULT_MIN_DENSE.get(embedder.name, SEMANTIC_MIN_DENSE)


def index_chunks(session, chunks: list[Chunk], embedder) -> None:
    """Embed and cache chunk vectors for one model (call at ingestion)."""
    if not chunks:
        return
    vectors = embedder.encode([c.text for c in chunks])
    for chunk, vector in zip(chunks, vectors):
        session.merge(ChunkEmbedding(chunk_id=chunk.id, model=embedder.name, vector=to_bytes(vector)))


def _chunk_vectors(session, chunks: list[Chunk], embedder) -> np.ndarray:
    """Cached vectors for already access-scoped chunks; missing ones are computed and cached."""
    ids = [c.id for c in chunks]
    rows = {r.chunk_id: from_bytes(r.vector) for r in session.scalars(
        select(ChunkEmbedding).where(ChunkEmbedding.model == embedder.name, ChunkEmbedding.chunk_id.in_(ids)))}
    missing = [c for c in chunks if c.id not in rows]
    if missing:
        index_chunks(session, missing, embedder)
        session.commit()
        rows.update({c.id: v for c, v in zip(missing, embedder.encode([c.text for c in missing]))})
    return np.vstack([rows[i] for i in ids])


def retrieve(session, principal: Principal, question: str, top_k: int = 3,
             group: str | None = None, *, mode: str | None = None, embedder=None) -> list[dict]:
    if not 1 <= top_k <= 10 or not question.strip() or len(question) > 6000:
        raise ValueError("Invalid retrieval request")
    mode = mode or os.getenv("CAPSTONE_RETRIEVAL_MODE", "hybrid")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    # Security boundary: access-scope rows BEFORE any scoring or embedding lookup.
    candidates = authorized_candidates(session, principal, group)
    if not candidates:
        return []
    corpus = [c.text for c, title in candidates]
    lexical = bm25_scores(question, corpus) if mode != "dense" else [0.0] * len(corpus)
    dense = [0.0] * len(corpus)
    floor = 0.0
    if mode != "bm25":
        embedder = embedder or get_embedder()
        floor = min_dense_for(embedder)
        q_vec = embedder.encode([question])[0]
        dense = [float(x) for x in _chunk_vectors(session, [c for c, _ in candidates], embedder) @ q_vec]
    # Drop candidates with no evidence signal: prevents unrelated passages leaking as "sources".
    if mode == "hybrid":
        # One shared common word is weak evidence. Hybrid admits a chunk when it is semantically
        # close, or when it contains an exact identifier from the question (e.g. ERR-4031), which
        # embeddings handle poorly. This is what lets hybrid abstain on unanswerable questions.
        ids = identifiers(question)
        eligible = [i for i in range(len(corpus)) if dense[i] >= floor
                    or (lexical[i] > 0 and ids and ids <= set(tokens(corpus[i])))]
    else:
        eligible = [i for i in range(len(corpus)) if lexical[i] > 0 or (mode == "dense" and dense[i] >= floor)]
    if not eligible:
        return []
    if mode == "bm25":
        score = {i: lexical[i] for i in eligible}
    elif mode == "dense":
        score = {i: dense[i] for i in eligible}
    else:
        ranks = [sorted(eligible, key=lambda i: (-metric[i], i)) for metric in (lexical, dense)]
        score = {i: sum(1 / (60 + (ranking.index(i) + 1)) for ranking in ranks) for i in eligible}
    order = sorted(eligible, key=lambda i: (-score[i], -lexical[i], i))[:top_k]
    return [{"citation_id": candidates[i][0].id,
             "document_id": candidates[i][0].document_id,
             "title": candidates[i][1],
             "text": candidates[i][0].text,
             "score": round(score[i], 6)} for i in order]


CITATION_SHAPE = re.compile(r"\[([A-Za-z0-9_-]+:c\d+)\]")


def _unbracket(text: str) -> str:
    """Neutralise citation-shaped text inside an untrusted source, e.g. "[other-doc:c0]".
    Other brackets (such as redaction tokens like [EMAIL]) cannot forge a citation and stay."""
    return CITATION_SHAPE.sub(r"(\1)", text)


def extract_report(question: str, evidence: list[dict]) -> tuple[str, list[str]]:
    if not evidence:
        return "No supporting evidence found in authorized documents.", []
    # Sources are untrusted: a bracketed "[other:c0]" inside a passage must not look like a citation.
    lines = [f"{_unbracket(e['title'])}: {_unbracket(e['text'])} [{e['citation_id']}]" for e in evidence]
    return "Authorized source extracts (not generative synthesis):\n" + "\n".join(lines), [e["citation_id"] for e in evidence]


def check_citations(report: str, ids: list[str], evidence: list[dict]) -> bool:
    permitted = {e["citation_id"] for e in evidence}
    mentions = set(re.findall(r"\[([a-zA-Z0-9_-]+:c\d+)\]", report))
    return bool(ids) and set(ids) <= permitted and mentions == set(ids)
