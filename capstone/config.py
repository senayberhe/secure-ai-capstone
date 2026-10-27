"""Environment configuration. No built-in production credentials."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str | None
    api_tokens: dict
    routing_secret: str
    mode: str = "offline"
    max_chars: int = 6000
    release_tenant: str | None = None
    graph: str = "offline"
    # Challenge 3: public keys of trusted CI attesters ({key_id: PEM}) and the artifact store root.
    attestation_keys: dict = field(default_factory=dict)
    artifact_root: str | None = None
    evidence_max_age_hours: int = 168

    @classmethod
    def from_env(cls):
        raw = os.getenv("CAPSTONE_TOKENS_JSON", "{}")
        tokens = json.loads(raw)
        if not isinstance(tokens, dict) or not tokens:
            raise RuntimeError("CAPSTONE_TOKENS_JSON must contain configured identities")
        if not all(len(k) >= 24 for k in tokens):
            raise RuntimeError("Bearer tokens must have at least 24 characters")
        key = os.getenv("CAPSTONE_ROUTING_SECRET", "")
        if len(key) < 32:
            raise RuntimeError("CAPSTONE_ROUTING_SECRET must have >=32 characters")
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///./capstone.db"),
            redis_url=os.getenv("REDIS_URL") or None,
            api_tokens=tokens,
            routing_secret=key,
            mode=os.getenv("MODEL_BACKEND", "offline"),
            release_tenant=os.getenv("CAPSTONE_RELEASE_TENANT") or None,
            graph=os.getenv("CAPSTONE_GRAPH", "offline"),
            attestation_keys=_attestation_keys(),
            artifact_root=os.getenv("CAPSTONE_ARTIFACT_ROOT") or None,
            evidence_max_age_hours=int(os.getenv("CAPSTONE_EVIDENCE_MAX_AGE_HOURS", "168")),
        )


def _attestation_keys() -> dict:
    raw = os.getenv("CAPSTONE_ATTESTATION_KEYS")
    path = os.getenv("CAPSTONE_ATTESTATION_KEYS_FILE")
    if path:
        raw = Path(path).read_text()
    keys = json.loads(raw) if raw else {}
    if not isinstance(keys, dict):
        raise RuntimeError("CAPSTONE_ATTESTATION_KEYS must map key ids to PEM public keys")
    return keys
