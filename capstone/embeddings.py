"""Embedding models for retrieval: a deterministic offline baseline and approved Sentence Transformers.

Only models on APPROVED_MODELS may be loaded, so a configuration change cannot pull in an
unreviewed model. Document vectors are cached per chunk and model in `chunk_embeddings`;
only the query is embedded at search time.
"""
from __future__ import annotations
import hashlib
import os
import threading
import numpy as np

# Reviewed for licence (Apache-2.0), size and English retrieval quality.
APPROVED_MODELS = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
}
HASH_MODEL = "hash-128"


class HashEmbedder:
    """Offline hashed bag-of-words baseline: lexical, NOT semantic."""
    name = HASH_MODEL
    dimension = 128

    def encode(self, texts: list[str]) -> np.ndarray:
        from .retrieval import tokens
        out = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in tokens(text):
                digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
                out[i, int.from_bytes(digest, "big") % self.dimension] += 1.0
            norm = float(np.linalg.norm(out[i]))
            if norm > 0:
                out[i] /= norm
        return out


class SentenceTransformerEmbedder:
    """Approved pretrained model; vectors are L2-normalised so dot product = cosine."""

    def __init__(self, name: str, *, local_files_only: bool | None = None):
        if name not in APPROVED_MODELS:
            raise ValueError(f"Embedding model not approved: {name}")
        from sentence_transformers import SentenceTransformer
        if local_files_only is None:
            local_files_only = os.getenv("CAPSTONE_EMBEDDING_OFFLINE", "1") == "1"
        # Offline by default: use the reviewed weights already in the cache, never fetch silently.
        self.model = SentenceTransformer(name, device="cpu", local_files_only=local_files_only)
        self.name = name
        self.dimension = APPROVED_MODELS[name]

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        return np.asarray(self.model.encode(texts, normalize_embeddings=True, batch_size=32), dtype=np.float32)


_cache: dict[str, object] = {}
_lock = threading.Lock()


def get_embedder(name: str | None = None):
    """Process-wide embedder, loaded once. Name from CAPSTONE_EMBEDDING_MODEL (default: hash baseline)."""
    name = name or os.getenv("CAPSTONE_EMBEDDING_MODEL") or HASH_MODEL
    with _lock:
        if name not in _cache:
            _cache[name] = HashEmbedder() if name == HASH_MODEL else SentenceTransformerEmbedder(name)
        return _cache[name]


def to_bytes(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_bytes(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
