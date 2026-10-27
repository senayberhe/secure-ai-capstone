"""Read-only listing endpoints used by the web UI stay tenant- and role-scoped."""


def _job(app, client, headers, key):
    from capstone.jobs import claim_job, finish_job
    client.post("/v1/documents", headers=headers["alice"], json={"title":"Travel","group":"engineering","text":"Receipts are due within thirty days."})
    job = client.post("/v1/research/jobs", headers={**headers["alice"], "Idempotency-Key": key}, json={"question":"receipts due"}).json()["job_id"]
    with app.state.sessions() as s:
        claimed = claim_job(s)
    with app.state.sessions() as s:
        finish_job(s, claimed)
    return job


def test_me(client, headers):
    me = client.get("/v1/me", headers=headers["admin"]).json()
    assert me == {"user":"sam","tenant":"acme","role":"admin","groups":["engineering"],"release_operator":True}
    assert client.get("/v1/me", headers=headers["bob"]).json()["release_operator"] is False
    assert client.get("/v1/me").status_code == 401


def test_documents_scoped_to_tenant_and_group(client, headers):
    client.post("/v1/documents", headers=headers["alice"], json={"title":"Acme doc","group":"engineering","text":"acme text"})
    client.post("/v1/documents", headers=headers["bob"], json={"title":"Other doc","group":"engineering","text":"other text"})
    assert [d["title"] for d in client.get("/v1/documents", headers=headers["alice"]).json()["documents"]] == ["Acme doc"]
    assert [d["title"] for d in client.get("/v1/documents", headers=headers["bob"]).json()["documents"]] == ["Other doc"]
    assert client.get("/v1/documents", headers=headers["guest"]).json()["documents"] == []


def test_jobs_visible_to_owner_and_in_scope_reviewer(app, client, headers):
    job = _job(app, client, headers, "listing-job-0001")
    mine = client.get("/v1/research/jobs", headers=headers["alice"]).json()["jobs"]
    assert [j["job_id"] for j in mine] == [job] and mine[0]["can_review"] is False
    queue = client.get("/v1/research/jobs", headers=headers["reviewer"]).json()["jobs"]
    assert queue[0]["job_id"] == job and queue[0]["can_review"] is True
    assert client.get("/v1/research/jobs", headers=headers["bob"]).json()["jobs"] == []


def test_queued_jobs_hidden_from_reviewers(client, headers):
    client.post("/v1/research/jobs", headers={**headers["alice"], "Idempotency-Key":"listing-job-0002"}, json={"question":"x"})
    assert client.get("/v1/research/jobs", headers=headers["reviewer"]).json()["jobs"] == []


def test_models_only_for_release_tenant(client, headers):
    client.post("/v1/models/register", headers=headers["admin"], json={"version":"candidate-v2","sha256":"b"*64})
    assert client.get("/v1/models", headers=headers["admin"]).json()["models"][0]["version"] == "candidate-v2"
    assert client.get("/v1/models", headers=headers["bob"]).status_code == 403


def test_incidents_scoped(client, headers):
    client.post("/v1/incidents", headers=headers["admin"], json={"category":"model_failure","summary":"Fallback engaged"})
    assert len(client.get("/v1/incidents", headers=headers["reviewer"]).json()["incidents"]) == 1
    assert client.get("/v1/incidents", headers=headers["alice"]).status_code == 403
