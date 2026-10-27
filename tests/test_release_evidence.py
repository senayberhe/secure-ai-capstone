"""Challenge 3: only CI-signed, version-bound evidence plus verified artifact bytes can start a canary."""
import json
from datetime import datetime, timedelta, timezone
import pytest
from capstone.attestation import generate_keypair, sign
from capstone.db import ModelVersion
from tests.conftest import ATTESTER_KEY_ID, ATTESTER_PUBLIC

ADMIN = {"Authorization": "Bearer admin-token-abcdefghijklmnopqrstuvwxyz123"}
REVIEWER = {"Authorization": "Bearer reviewer-token-abcdefghijklmnopqrstuvwxyz123"}


@pytest.fixture
def baseline(app):
    with app.state.sessions.begin() as s:
        s.add(ModelVersion(version="baseline-v1", artifact_sha256="a" * 64, registered_by="seed", status="active", traffic_pct=100))


def canary(client, evidence_id, version="candidate-v2"):
    return client.post("/v1/releases/canary", headers=REVIEWER, json={"version": version, "evaluation_id": evidence_id})


def test_attested_evidence_starts_canary_and_records_verification(client, baseline, release):
    evidence_id = release.ready("candidate-v2")
    assert canary(client, evidence_id).status_code == 200
    record = client.get("/v1/models/candidate-v2/evidence", headers=ADMIN).json()
    assert record["evidence"][0]["attester"] == "ci-evaluator"
    assert record["artifact_verifications"][0] == {**record["artifact_verifications"][0], "matched": True, "verified_by": "rachel"}


def test_operator_typed_evaluation_cannot_start_canary(client, baseline, release):
    release.register("candidate-v2")
    typed = client.post("/v1/models/evaluate", headers=ADMIN, json={"version": "candidate-v2", "samples": 999, "accuracy": 1.0,
                                                                   "unsupported_rate": 0, "leakage_count": 0, "p95_ms": 1}).json()
    assert typed["decision"] == "eligible"
    assert canary(client, typed["evaluation_id"]).status_code == 409


def test_untrusted_signer_rejected(client, release):
    path = release.register("candidate-v2")
    forged_private, _, _ = generate_keypair()
    from capstone.release_evidence import build_payload
    from tests.conftest import PASSING_BENCHMARK
    payload = build_payload("candidate-v2", path, attester="ci-evaluator", benchmark=PASSING_BENCHMARK, embedding_model="test-model")
    envelope = {**sign(payload, forged_private), "key_id": ATTESTER_KEY_ID}  # claims to be the trusted key
    assert client.post("/v1/models/evidence", headers=ADMIN, json=envelope).status_code == 403


def test_tampered_payload_rejected(client, release):
    import base64
    path = release.register("candidate-v2")
    envelope = release.envelope("candidate-v2", path, decision="hold")
    body = json.loads(base64.b64decode(envelope["payload"]))
    body["decision"] = "eligible"  # flip the verdict after signing
    tampered = {**envelope, "payload": base64.b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).decode()}
    assert client.post("/v1/models/evidence", headers=ADMIN, json=tampered).status_code == 403


def test_evidence_must_match_registered_digest(client, release, artifact_root):
    release.register("candidate-v2")
    other = artifact_root / "different.safetensors"
    other.write_bytes(b"some other weights")
    assert client.post("/v1/models/evidence", headers=ADMIN, json=release.envelope("candidate-v2", other)).status_code == 409


def test_evidence_for_unregistered_version_rejected(client, release, artifact_root):
    path = release.artifact("ghost-v9")
    assert client.post("/v1/models/evidence", headers=ADMIN, json=release.envelope("ghost-v9", path)).status_code == 409


def test_swapped_artifact_bytes_block_canary(client, app, baseline, release, artifact_root):
    evidence_id = release.ready("candidate-v2")
    (artifact_root / "candidate-v2.safetensors").write_bytes(b"tampered weights after evaluation")
    assert canary(client, evidence_id).status_code == 409
    checks = client.get("/v1/models/candidate-v2/evidence", headers=ADMIN).json()["artifact_verifications"]
    assert checks[0]["matched"] is False
    with app.state.sessions() as s:
        assert s.get(ModelVersion, "candidate-v2").status == "registered"


def test_failing_benchmark_produces_hold_that_cannot_canary(client, baseline, release):
    from tests.conftest import PASSING_BENCHMARK
    failing = json.loads(json.dumps(PASSING_BENCHMARK))
    failing["results"]["test-model"]["hybrid"]["cross_tenant_or_group_leaks"] = 1
    path = release.register("candidate-v2")
    r = client.post("/v1/models/evidence", headers=ADMIN, json=release.envelope("candidate-v2", path, benchmark=failing)).json()
    assert r["decision"] == "hold"
    assert canary(client, r["evidence_id"]).status_code == 409


def test_evidence_bound_to_its_version(client, baseline, release):
    evidence_id = release.ready("candidate-v2")
    release.register("candidate-v3")
    assert canary(client, evidence_id, version="candidate-v3").status_code == 409


def test_stale_evidence_rejected(client, release):
    path = release.register("candidate-v2")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    assert client.post("/v1/models/evidence", headers=ADMIN, json=release.envelope("candidate-v2", path, created_at=old)).status_code == 409


def test_registrar_cannot_be_the_attester(client, release):
    path = release.register("candidate-v2")
    assert client.post("/v1/models/evidence", headers=ADMIN, json=release.envelope("candidate-v2", path, attester="sam")).status_code == 403


def test_resubmission_is_idempotent(client, release):
    path = release.register("candidate-v2")
    envelope = release.envelope("candidate-v2", path)
    first = client.post("/v1/models/evidence", headers=ADMIN, json=envelope).json()["evidence_id"]
    assert client.post("/v1/models/evidence", headers=ADMIN, json=envelope).json()["evidence_id"] == first


def test_canary_fails_closed_without_trusted_keys(tmp_path):
    from fastapi.testclient import TestClient
    from capstone.api import create_app
    from capstone.config import Settings
    from tests.conftest import TOKENS, Release
    app = create_app(Settings("sqlite://", None, TOKENS, "x" * 40, release_tenant="acme", artifact_root=str(tmp_path)), init_tables=True)
    client = TestClient(app)
    path = Release(client, tmp_path).register("candidate-v2")
    assert client.post("/v1/models/evidence", headers=ADMIN, json=Release(client, tmp_path).envelope("candidate-v2", path)).status_code == 403


def test_ci_cli_keygen_build_and_sign(tmp_path, monkeypatch, capsys):
    from capstone import release_evidence
    from capstone.attestation import load_public_keys, verify
    assert release_evidence.main(["keygen", "--out", str(tmp_path / "keys")]) == 0
    keys = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("CAPSTONE_ATTESTATION_PRIVATE_KEY", (tmp_path / "keys" / "attestation_private.pem").read_text())
    artifact = tmp_path / "model.safetensors"
    artifact.write_bytes(b"weights")
    code = release_evidence.main(["build", "--version", "candidate-v2", "--artifact", str(artifact),
                                  "--embedding-model", "hash-128", "--out", str(tmp_path / "evidence.json")])
    payload, _ = verify(json.loads((tmp_path / "evidence.json").read_text()),
                        load_public_keys(keys["api_setting"]["CAPSTONE_ATTESTATION_KEYS"]))
    assert payload["version"] == "candidate-v2" and payload["artifact"]["bytes"] == 7
    # The lexical hash baseline misses the abstention and recall gates, so CI refuses it.
    assert code == 1 and payload["decision"] == "hold" and "abstention_rate" in payload["failed_gates"]
