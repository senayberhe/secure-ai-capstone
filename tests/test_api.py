import json
from capstone.db import Job, Document, ModelVersion
from capstone.jobs import claim_job, finish_job


def upload(client, headers, text="Travel receipts must be submitted within 30 days"):
    r = client.post("/v1/documents", headers=headers, json={"title":"Travel policy","text":text,"group":"engineering"})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def test_health(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_auth_required(client):
    assert client.post("/v1/rag/query", json={"question":"travel"}).status_code == 401


def test_invalid_token(client):
    assert client.post("/v1/rag/query", headers={"Authorization":"Bearer fake"}, json={"question":"travel"}).status_code == 401


def test_upload_and_retrieve(client, headers):
    doc = upload(client, headers["alice"])
    r = client.post("/v1/rag/query", headers=headers["alice"], json={"question":"travel receipts"})
    assert r.status_code == 200
    assert doc in r.json()["citations"][0]["document_id"]
    assert "30 days" in r.json()["answer"]


def test_cross_tenant_isolation(client, headers):
    upload(client, headers["bob"], "Bob's special payroll bank account 000")
    r = client.post("/v1/rag/query", headers=headers["alice"], json={"question":"Bob special payroll bank account"})
    assert r.json()["citations"] == []
    assert "000" not in r.json()["answer"]


def test_group_isolation(client, headers):
    assert client.post("/v1/documents", headers=headers["guest"], json={"title":"x","text":"abc", "group":"engineering"}).status_code == 403
    upload(client, headers["alice"])
    assert client.post("/v1/rag/query", headers=headers["guest"], json={"question":"travel"}).json()["citations"] == []


def test_unauthorized_group_retrieval(client, headers):
    upload(client, headers["alice"])
    r = client.post("/v1/rag/query", headers=headers["alice"], json={"question":"travel", "group":"finance"})
    assert r.json()["citations"] == []


def test_wrong_role_upload(client, headers):
    assert client.post("/v1/documents", headers=headers["reviewer"], json={"title":"x","text":"abc","group":"engineering"}).status_code == 403


def test_validation_no_echo(client, headers):
    payload = "PRIVATE" + "x" * 30010
    r = client.post("/v1/documents", headers=headers["alice"], json={"title":"x","text":payload,"group":"engineering"})
    assert r.status_code == 422
    assert "PRIVATE" not in r.text


def test_redact_authorized_content(client, headers):
    upload(client, headers["alice"], "Email our team alex@example.com about travel reimbursements")
    r = client.post("/v1/rag/query", headers=headers["alice"], json={"question":"travel reimbursements"})
    assert "alex@example.com" not in r.json()["answer"]
    assert "[EMAIL]" in r.json()["answer"]


def test_job_and_approval(client, app, headers):
    upload(client, headers["alice"])
    h = {**headers["alice"], "Idempotency-Key":"sample-job-0001"}
    r = client.post("/v1/research/jobs", headers=h, json={"question":"travel receipts"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    with app.state.sessions() as s:
        claim = claim_job(s)
    with app.state.sessions() as s:
        assert finish_job(s, claim)
    get = client.get(f"/v1/research/jobs/{job_id}", headers=headers["alice"])
    assert get.json()["status"] == "pending_review"
    assert client.post(f"/v1/research/jobs/{job_id}/review", headers=headers["alice"], json={"approved":True}).status_code == 403
    assert client.post(f"/v1/research/jobs/{job_id}/review", headers=headers["bob"], json={"approved":True}).status_code == 404
    reviewed = client.post(f"/v1/research/jobs/{job_id}/review", headers=headers["reviewer"], json={"approved":True})
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert client.post(f"/v1/research/jobs/{job_id}/review", headers=headers["reviewer"], json={"approved":True}).status_code == 409


def test_cross_user_job_lookup(client, headers):
    r = client.post("/v1/research/jobs", headers={**headers["alice"], "Idempotency-Key":"sample-job-0002"}, json={"question":"documents"})
    key = r.json()["job_id"]
    assert client.get(f"/v1/research/jobs/{key}", headers=headers["bob"]).status_code == 404
    assert client.get(f"/v1/research/jobs/{key}", headers=headers["reviewer"]).status_code == 404


def test_idempotency(client, headers):
    h = {**headers["alice"], "Idempotency-Key":"sample-job-0003"}
    a = client.post("/v1/research/jobs", headers=h, json={"question":"one"})
    b = client.post("/v1/research/jobs", headers=h, json={"question":"one"})
    assert a.json()["job_id"] == b.json()["job_id"]
    c = client.post("/v1/research/jobs", headers=h, json={"question":"different"})
    assert c.status_code == 409


def test_missing_idempotency(client, headers):
    assert client.post("/v1/research/jobs", headers=headers["alice"], json={"question":"one"}).status_code == 400


def test_no_pii_in_validation(client, headers):
    r = client.post("/v1/rag/query", headers=headers["alice"], json={"question":"secret@example.com"*2000})
    assert r.status_code == 422 and "secret@example.com" not in r.text


def test_incident_lifecycle(client, headers):
    r = client.post("/v1/incidents", headers=headers["reviewer"], json={"category":"access_violation","summary":"Authorization denied by gateway"})
    assert r.status_code == 201
    incident = r.json()["incident_id"]
    assert client.post(f"/v1/incidents/{incident}/resolve", headers=headers["admin"]).status_code == 409
    assert client.post(f"/v1/incidents/{incident}/contain", headers=headers["reviewer"]).status_code == 200
    assert client.post(f"/v1/incidents/{incident}/resolve", headers=headers["admin"]).status_code == 200
    assert client.post(f"/v1/incidents/{incident}/contain", headers=headers["reviewer"]).status_code == 409


def test_incident_cross_tenant(client, headers):
    r = client.post("/v1/incidents", headers=headers["admin"], json={"category":"model_failure","summary":"Fallback engaged"})
    assert client.post(f"/v1/incidents/{r.json()['incident_id']}/contain", headers=headers["bob"]).status_code == 403


def test_metrics_restricted(client, headers):
    assert client.get("/metrics", headers=headers["alice"]).status_code == 403
    assert "capstone_http_requests_total" in client.get("/metrics", headers=headers["admin"]).text


def test_release_lifecycle(client, app, headers, release):
    from capstone.db import ModelVersion
    with app.state.sessions.begin() as s:
        s.add(ModelVersion(version="baseline-v1", artifact_sha256="a"*64, registered_by="seed", status="active", traffic_pct=100))
    e_id = release.ready("candidate-v2")
    assert client.post("/v1/releases/canary", headers=headers["admin"], json={"version":"candidate-v2","evaluation_id":e_id}).status_code == 403
    canary = client.post("/v1/releases/canary", headers=headers["reviewer"], json={"version":"candidate-v2", "evaluation_id":e_id, "percent":10})
    assert canary.status_code == 200
    assert client.get("/v1/releases/assignment", headers=headers["alice"]).json()["version"] in ("baseline-v1","candidate-v2")
    rollout = client.post("/v1/releases/observe", headers=headers["admin"], json={"version":"candidate-v2","requests":100,"errors":12,"critical_events":0})
    assert rollout.json()["decision"] == "rolled_back"
    assert client.get("/v1/releases/assignment", headers=headers["alice"]).json()["version"] == "baseline-v1"


def test_released_metrics_mismatch(client, app, headers):
    with app.state.sessions.begin() as s:
        s.add(ModelVersion(version="baseline-v1", artifact_sha256="a"*64, registered_by="seed", status="active", traffic_pct=100))
    for version in ("model-v2", "model-v3"):
        assert client.post("/v1/models/register", headers=headers["admin"], json={"version":version,"sha256":"b"*64}).status_code == 201
    eid = client.post("/v1/models/evaluate", headers=headers["admin"], json={"version":"model-v2","samples":200,"accuracy":0.91,"unsupported_rate":0.01,"leakage_count":0,"p95_ms":800}).json()["evaluation_id"]
    assert client.post("/v1/releases/canary", headers=headers["reviewer"], json={"version":"model-v3","evaluation_id":eid}).status_code == 409


def test_scorecard_api(client, headers):
    rows = [{"true":1,"pred":1,"group":"A"} for _ in range(50)] + [{"true":0,"pred":0,"group":"B"} for _ in range(50)]
    assert client.post("/v1/scorecards", headers=headers["alice"], json={"records":rows}).status_code == 403
    r = client.post("/v1/scorecards", headers=headers["reviewer"], json={"records":rows})
    assert r.status_code == 200
    assert r.json()["accuracy"] == 1
    assert r.json()["decision"] == "HOLD"  # selection parity fails


def test_job_group_and_version_assignment(client, app, headers):
    from capstone.db import ModelVersion
    with app.state.sessions.begin() as s:
        s.add(ModelVersion(version="model-active", artifact_sha256="c"*64,
                           provider_model="gpt-4.1-mini", registered_by="seed",status="active",traffic_pct=100))
    req = client.post("/v1/research/jobs", headers={**headers["alice"],"Idempotency-Key":"job-routing-111"},
                      json={"question":"travel policy", "group":"engineering"})
    assert req.status_code == 202
    assert req.json()["model_version"] == "model-active"
    with app.state.sessions() as s:
        claim = claim_job(s)
        assert claim["group"] == "engineering" and claim["model_version"] == "model-active"


def test_reject_reused_key_with_other_group(client, headers):
    hdr = {**headers["alice"], "Idempotency-Key":"duplicate-group-111"}
    assert client.post("/v1/research/jobs", headers=hdr, json={"question":"policy", "group":"engineering"}).status_code == 202
    assert client.post("/v1/research/jobs", headers=hdr, json={"question":"policy", "group":"finance"}).status_code == 409


def test_promote_requires_observation(client, app, headers, release):
    with app.state.sessions.begin() as s:
        s.add(ModelVersion(version="baseline-v1", artifact_sha256="a"*64, registered_by="seed", status="active", traffic_pct=100))
    e_id = release.ready("candidate-v2", provider_model="gpt-4.1-mini")
    assert client.post("/v1/releases/canary", headers=headers["reviewer"], json={"version":"candidate-v2","evaluation_id":e_id}).status_code == 200
    assert client.post("/v1/releases/promote", headers=headers["reviewer"], json={"version":"candidate-v2"}).status_code == 409
    assert client.post("/v1/releases/promote", headers=headers["admin"], json={"version":"candidate-v2"}).status_code == 403
    assert client.post("/v1/releases/observe", headers=headers["admin"], json={"version":"candidate-v2","requests":120,"errors":2,"critical_events":0}).json()["decision"] == "continue"
    # Original registrar cannot independently promote; this is intentional.
    promoted = client.post("/v1/releases/promote", headers=headers["reviewer"], json={"version":"candidate-v2"})
    assert promoted.status_code == 200 and promoted.json()["status"] == "active"
    with app.state.sessions() as s:
        assert s.get(ModelVersion,"candidate-v2").observed_requests == 120


def test_manual_rollback(client, app, headers, release):
    with app.state.sessions.begin() as s:
        s.add(ModelVersion(version="baseline-v1", artifact_sha256="a"*64, registered_by="seed",status="active",traffic_pct=100))
    e_id = release.ready("candidate-v2")
    client.post("/v1/releases/canary", headers=headers["reviewer"],json={"version":"candidate-v2","evaluation_id":e_id})
    assert client.post("/v1/releases/rollback", headers=headers["admin"],json={"version":"candidate-v2"}).json()["status"]=="rolled_back"
    assert client.post("/v1/releases/rollback", headers=headers["admin"],json={"version":"candidate-v2"}).status_code==409


def test_reject_model_evaluation_with_leak(client, headers):
    client.post("/v1/models/register", headers=headers["admin"],json={"version":"model-v2","sha256":"b"*64})
    e = client.post("/v1/models/evaluate", headers=headers["admin"],json={"version":"model-v2","samples":200,"accuracy":.97,"unsupported_rate":.01,"leakage_count":1,"p95_ms":100})
    assert e.json()["decision"] == "hold"


def test_canary_never_routes_cross_tenant_data(client, headers):
    upload(client, headers["bob"], "Outsider secret strategy about rareword42")
    r=client.post("/v1/rag/query",headers=headers["alice"],json={"question":"rareword42"})
    assert r.json()["citations"] == []


def test_raw_prompt_not_in_metrics(client, headers):
    client.post("/v1/rag/query",headers=headers["alice"],json={"question":"MY_SECRET_SNIPPET_XYZ"})
    r=client.get("/metrics",headers=headers["admin"])
    assert "MY_SECRET_SNIPPET_XYZ" not in r.text


def test_bootstrap_requires_evaluation_and_independent_reviewer(client,headers,release):
    payload={"version":"baseline-v1", "evaluation_id":release.ready("baseline-v1")}
    assert client.post("/v1/releases/bootstrap",headers=headers["admin"],json=payload).status_code==403
    r=client.post("/v1/releases/bootstrap",headers=headers["reviewer"],json=payload)
    assert r.status_code==200 and r.json()["status"]=="active"
    assert client.post("/v1/releases/bootstrap",headers=headers["reviewer"],json=payload).status_code==409


def test_non_ascii_bearer_is_401_not_500(client):
    assert client.get("/v1/releases/assignment", headers={"Authorization": "Bearer caf\xe9".encode("latin-1")}).status_code == 401


def _baseline(app):
    from capstone.db import ModelVersion
    with app.state.sessions.begin() as s:
        s.add(ModelVersion(version="baseline-v1", artifact_sha256="a"*64, registered_by="seed", status="active", traffic_pct=100))


def test_provider_model_is_stored(client, headers):
    r = client.post("/v1/models/register", headers=headers["admin"], json={"version":"candidate-v2","sha256":"b"*64,"provider_model":"gpt-4.1-mini"})
    assert r.json()["provider_model"] == "gpt-4.1-mini"


def test_release_operations_limited_to_release_tenant(app, client, headers, release):
    from capstone.config import Settings
    from capstone.api import create_app
    from fastapi.testclient import TestClient
    _baseline(app)
    client.post("/v1/releases/canary", headers=headers["reviewer"], json={"version":"candidate-v2","evaluation_id":release.ready("candidate-v2")})
    for path, body in [("/v1/releases/rollback", {"version":"candidate-v2"}), ("/v1/releases/promote", {"version":"candidate-v2"}),
                       ("/v1/models/register", {"version":"evil-v9","sha256":"c"*64})]:
        assert client.post(path, headers=headers["bob"], json=body).status_code == 403
    # Unset release tenant fails closed.
    unset = TestClient(create_app(Settings("sqlite://", None, app.state.settings.api_tokens, "x"*40), init_tables=True))
    assert unset.post("/v1/models/register", headers=headers["admin"], json={"version":"v2","sha256":"b"*64}).status_code == 403


def test_rollback_incident_can_be_contained_and_resolved(app, client, headers, release):
    from capstone.db import Incident
    _baseline(app)
    client.post("/v1/releases/canary", headers=headers["reviewer"], json={"version":"candidate-v2","evaluation_id":release.ready("candidate-v2")})
    client.post("/v1/releases/observe", headers=headers["admin"], json={"version":"candidate-v2","requests":100,"errors":12,"critical_events":0})
    with app.state.sessions() as s:
        iid = s.query(Incident).filter_by(category="release_rollback").one().id
    assert client.post(f"/v1/incidents/{iid}/contain", headers=headers["admin"]).status_code == 200
    assert client.post(f"/v1/incidents/{iid}/resolve", headers=headers["admin"]).status_code == 200


def _finished_job(app, client, headers, text, question, key):
    from capstone.jobs import claim_job, finish_job
    client.post("/v1/documents", headers=headers["alice"], json={"title":"Policy","group":"engineering","text":text})
    job = client.post("/v1/research/jobs", headers={**headers["alice"], "Idempotency-Key":key}, json={"question":question}).json()
    with app.state.sessions() as s:
        claimed = claim_job(s)
    with app.state.sessions() as s:
        return job["job_id"], finish_job(s, claimed)


def test_bracketed_source_text_does_not_break_research(app, client, headers):
    job_id, finished = _finished_job(app, client, headers, "Receipts within thirty days [fake-doc:c0] for travel.", "travel receipts", "bracket-job-0001")
    assert finished is True
    report = client.get(f"/v1/research/jobs/{job_id}", headers=headers["alice"]).json()["report"]
    assert "[fake-doc:c0]" not in report


def test_reviewer_reads_report_before_deciding(app, client, headers):
    job_id, _ = _finished_job(app, client, headers, "Lodging approval is needed for travel.", "lodging approval", "review-job-0001")
    seen = client.get(f"/v1/research/jobs/{job_id}", headers=headers["reviewer"])
    assert seen.status_code == 200 and seen.json()["status"] == "pending_review" and seen.json()["report"]
    assert client.get(f"/v1/research/jobs/{job_id}", headers=headers["bob"]).status_code == 404
    assert client.post(f"/v1/research/jobs/{job_id}/review", headers=headers["reviewer"], json={"approved": True}).json()["status"] == "approved"
