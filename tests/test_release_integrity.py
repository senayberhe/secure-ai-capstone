"""Challenge 18 gaps: prompt-version binding and database-level immutability of release evidence."""
import os
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from capstone.db import ModelVersion, database, init_schema

ADMIN = {"Authorization": "Bearer admin-token-abcdefghijklmnopqrstuvwxyz123"}
REVIEWER = {"Authorization": "Bearer reviewer-token-abcdefghijklmnopqrstuvwxyz123"}


@pytest.fixture
def baseline(app):
    with app.state.sessions.begin() as s:
        s.add(ModelVersion(version="baseline-v1", artifact_sha256="a" * 64, registered_by="seed", status="active", traffic_pct=100))


def test_evidence_carries_the_prompt_version(client, release):
    from capstone.llm import prompt_version
    path = release.register("candidate-v2")
    import base64, json
    payload = json.loads(base64.b64decode(release.envelope("candidate-v2", path)["payload"]))
    assert payload["prompt_sha256"] == prompt_version() and len(payload["prompt_sha256"]) == 64


def test_evidence_for_another_prompt_is_rejected(client, release):
    path = release.register("candidate-v2")
    stale = release.envelope("candidate-v2", path, prompt_sha256="0" * 64)
    r = client.post("/v1/models/evidence", headers=ADMIN, json=stale)
    assert r.status_code == 409 and "prompt" in r.json()["detail"]


def test_prompt_change_after_acceptance_blocks_canary(client, baseline, release, monkeypatch):
    evidence_id = release.ready("candidate-v2")
    monkeypatch.setattr("capstone.releases.prompt_version", lambda: "f" * 64)  # a new prompt was deployed
    r = client.post("/v1/releases/canary", headers=REVIEWER, json={"version": "candidate-v2", "evaluation_id": evidence_id})
    assert r.status_code == 409


def engines():
    yield "sqlite"
    if os.getenv("TEST_POSTGRES_URL"):
        yield "postgres"


@pytest.fixture(params=list(engines()))
def store(request, client, release):
    """A database holding one registered version, one evidence row and one verification row."""
    if request.param == "sqlite":
        evidence_id = release.ready("candidate-v2")
        engine = client.app.state.engine
    else:
        from capstone.db import Base
        engine, _ = database(os.environ["TEST_POSTGRES_URL"])
        Base.metadata.drop_all(engine)
        init_schema(engine)
        with engine.begin() as con:
            con.execute(text("INSERT INTO model_versions (version, artifact_sha256, registered_by, status, traffic_pct, observed_requests, observed_errors, observed_critical) VALUES ('candidate-v2', :d, 'sam', 'registered', 0, 0, 0, 0)"), {"d": "b" * 64})
            con.execute(text("INSERT INTO release_evidence (id, model_version, payload_sha256, key_id, attester, artifact_sha256, dataset_sha256, decision, envelope, attested_at, submitted_by, submitted_at) VALUES ('e1', 'candidate-v2', :p, 'k', 'ci', :d, :d, 'eligible', '{}', now(), 'sam', now())"), {"p": "c" * 64, "d": "b" * 64})
        evidence_id = "e1"
    return engine, evidence_id


@pytest.mark.parametrize("statement", ["UPDATE release_evidence SET decision = 'eligible', attester = 'forged' WHERE id = :id",
                                       "DELETE FROM release_evidence WHERE id = :id"])
def test_evidence_rows_are_append_only(store, statement):
    engine, evidence_id = store
    with pytest.raises(DBAPIError):
        with engine.begin() as con:
            con.execute(text(statement), {"id": evidence_id})


def test_registered_digest_cannot_be_rewritten_but_status_can_change(store):
    engine, _ = store
    with pytest.raises(DBAPIError):
        with engine.begin() as con:
            con.execute(text("UPDATE model_versions SET artifact_sha256 = :d WHERE version = 'candidate-v2'"), {"d": "e" * 64})
    with engine.begin() as con:
        con.execute(text("UPDATE model_versions SET status = 'rolled_back' WHERE version = 'candidate-v2'"))
