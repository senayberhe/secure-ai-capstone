import pytest
from fastapi.testclient import TestClient
from capstone.api import create_app
from capstone.config import Settings

TOKENS = {
    "alice-token-abcdefghijklmnopqrstuvwxyz123": {"tenant": "acme", "user": "alice", "groups": ["engineering"], "role": "editor"},
    "reviewer-token-abcdefghijklmnopqrstuvwxyz123": {"tenant": "acme", "user": "rachel", "groups": ["engineering"], "role": "reviewer"},
    "admin-token-abcdefghijklmnopqrstuvwxyz123": {"tenant": "acme", "user": "sam", "groups": ["engineering"], "role": "admin"},
    "bob-token-abcdefghijklmnopqrstuvwxyz123": {"tenant": "other", "user": "bob", "groups": ["engineering"], "role": "editor"},
    "guest-token-abcdefghijklmnopqrstuvwxyz123": {"tenant": "acme", "user": "guest", "groups": [], "role": "reader"},
}

from capstone.attestation import generate_keypair, sign

# A test-only "CI" attester. The API trusts its public key; only these fixtures hold the private key.
ATTESTER_PRIVATE, ATTESTER_PUBLIC, ATTESTER_KEY_ID = generate_keypair()

PASSING_BENCHMARK = {"dataset_sha256": "d" * 64, "questions": 61, "results": {"test-model": {"hybrid": {
    "recall_at_3": 0.95, "mrr": 0.93, "abstention_rate": 0.5, "latency_ms_p95": 12.0,
    "cross_tenant_or_group_leaks": 0, "recall_at_3_by_category": {}}}}}


@pytest.fixture
def artifact_root(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    return root


@pytest.fixture
def app(artifact_root):
    settings = Settings(database_url="sqlite://", redis_url=None, api_tokens=TOKENS, routing_secret="x" * 40,
                        release_tenant="acme", attestation_keys={ATTESTER_KEY_ID: ATTESTER_PUBLIC},
                        artifact_root=str(artifact_root))
    return create_app(settings, init_tables=True)


class Release:
    """Drives the attested release flow: artifact bytes -> registration -> CI-signed evidence."""

    def __init__(self, client, artifact_root):
        self.client, self.root = client, artifact_root
        self.admin = {"Authorization": "Bearer admin-token-abcdefghijklmnopqrstuvwxyz123"}

    def artifact(self, version):
        path = self.root / f"{version}.safetensors"
        path.write_bytes(f"synthetic weights for {version}".encode())
        return path

    def envelope(self, version, path, benchmark=None, **overrides):
        from capstone.release_evidence import build_payload
        payload = build_payload(version, path, attester="ci-evaluator", benchmark=benchmark or PASSING_BENCHMARK,
                                embedding_model="test-model")
        return sign({**payload, **overrides}, ATTESTER_PRIVATE)

    def register(self, version, **extra):
        from capstone.release_evidence import artifact_sha256
        path = self.artifact(version)
        r = self.client.post("/v1/models/register", headers=self.admin, json={
            "version": version, "sha256": artifact_sha256(path)[0], "artifact_path": path.name, **extra})
        assert r.status_code == 201, r.text
        return path

    def ready(self, version, **extra):
        """Register a version and return the id of eligible attested evidence for it."""
        path = self.register(version, **extra)
        r = self.client.post("/v1/models/evidence", headers=self.admin, json=self.envelope(version, path))
        assert r.status_code == 201, r.text
        return r.json()["evidence_id"]


@pytest.fixture
def release(client, artifact_root):
    return Release(client, artifact_root)

@pytest.fixture
def client(app):
    return TestClient(app)

@pytest.fixture
def headers():
    return {"alice": {"Authorization": "Bearer alice-token-abcdefghijklmnopqrstuvwxyz123"},
            "reviewer": {"Authorization": "Bearer reviewer-token-abcdefghijklmnopqrstuvwxyz123"},
            "admin": {"Authorization": "Bearer admin-token-abcdefghijklmnopqrstuvwxyz123"},
            "bob": {"Authorization": "Bearer bob-token-abcdefghijklmnopqrstuvwxyz123"},
            "guest": {"Authorization": "Bearer guest-token-abcdefghijklmnopqrstuvwxyz123"}}
