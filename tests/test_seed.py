"""The demo seed drives the public API only, leaves every tab populated and is safe to re-run."""
import sys
from capstone import seed
from capstone.jobs import claim_job, finish_job
from tests.conftest import PASSING_BENCHMARK, TOKENS


class TestApi(seed.Api):
    """Sends seed requests through the in-process TestClient instead of the network."""
    __test__ = False

    def __init__(self, client):
        super().__init__("", {v["user"]: k for k, v in TOKENS.items()})
        self.client = client

    def call(self, user, method, path, body=None, headers=None):
        r = self.client.request(method, path, json=body,
                                headers={"Authorization": f"Bearer {self.tokens[user]}", **(headers or {})})
        return r.status_code, r.json()


def run_seed(api, artifact_root, monkeypatch, ci_key):
    who = seed.pick_identities(TOKENS, "acme")
    lines = []
    seed.seed_documents(api, who, lines.append)
    seed.seed_jobs(api, who, lines.append, wait_seconds=0)
    seed.seed_incidents(api, who, lines.append)
    # Releases sign the fixtures' benchmark with the test CI key (the API trusts its public half).
    with monkeypatch.context() as m:
        m.setenv("CAPSTONE_ATTESTATION_PRIVATE_KEY", ci_key)
        m.setenv("CAPSTONE_EMBEDDING_MODEL", "test-model")
        m.setattr("capstone.rag_benchmark.run_benchmark", lambda models: PASSING_BENCHMARK)
        seed.seed_releases(api, who, artifact_root, lines.append)
    return who, lines


def test_identities_are_independent_and_share_a_tenant():
    who = seed.pick_identities(TOKENS, "acme")
    assert who == {"editor": "alice", "reviewer": "rachel", "admin": "sam", "outsider": "bob"}


def test_seed_populates_every_tab_and_is_idempotent(app, client, headers, artifact_root, release, monkeypatch):
    # The key the app fixture trusts lives in the conftest module pytest loaded (not a re-import).
    ci_key = sys.modules[type(release).__module__].ATTESTER_PRIVATE
    api = TestApi(client)
    who, lines = run_seed(api, artifact_root, monkeypatch, ci_key)
    assert any("start the worker" in line for line in lines), lines  # no worker ran yet

    docs = client.get("/v1/documents", headers=headers["alice"]).json()["documents"]
    assert len(docs) == len(seed.DOCUMENTS)
    assert seed.OUTSIDER_DOCUMENT[0] not in {d["title"] for d in docs}  # other tenant stays invisible
    incidents = {i["category"]: i["status"] for i in client.get("/v1/incidents", headers=headers["reviewer"]).json()["incidents"]}
    assert incidents == {"prompt_injection_attempt": "open", "pii_in_upload": "contained"}
    models = {m["version"]: m for m in client.get("/v1/models", headers=headers["admin"]).json()["models"]}
    assert models["seed-baseline-v1"]["status"] == "active", lines
    assert models["seed-candidate-v2"]["status"] == "canary" and models["seed-candidate-v2"]["traffic_pct"] == 10

    # Simulate the worker, then re-run: the review is added and nothing is duplicated.
    for _ in seed.JOBS:
        with app.state.sessions() as s:
            claimed = claim_job(s)
        with app.state.sessions() as s:
            assert finish_job(s, claimed)
    run_seed(api, artifact_root, monkeypatch, ci_key)
    jobs = {j["question"]: j["status"] for j in client.get("/v1/research/jobs", headers=headers["alice"]).json()["jobs"]}
    assert jobs == {seed.JOBS[0][1]: "approved", seed.JOBS[1][1]: "pending_review"}
    assert len(client.get("/v1/documents", headers=headers["alice"]).json()["documents"]) == len(seed.DOCUMENTS)
    assert len(client.get("/v1/models", headers=headers["admin"]).json()["models"]) == 2
    assert len(client.get("/v1/incidents", headers=headers["reviewer"]).json()["incidents"]) == 2
