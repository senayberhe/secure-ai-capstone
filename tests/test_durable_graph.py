"""Challenge 2: LangGraph checkpoints survive process restarts; approval rules still hold."""
import os
import subprocess
import sys
import textwrap
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langgraph.checkpoint.sqlite")
from capstone.agent import resume_review, run_until_review, thread_config
from capstone.api import create_app
from capstone.config import Settings
from tests.conftest import TOKENS

ALICE, RACHEL, SAM, BOB = (k for k in TOKENS if k.split("-")[0] in ("alice", "reviewer", "admin", "bob"))
H = lambda key: {"Authorization": f"Bearer {key}"}

PHASE_ONE = textwrap.dedent('''
    import json, sys
    from fastapi.testclient import TestClient
    from capstone.api import create_app
    from capstone.config import Settings
    from capstone.worker import process_once
    from tests.conftest import TOKENS
    url = sys.argv[1]
    app = create_app(Settings(url, None, TOKENS, "x" * 40, release_tenant="acme", graph="langgraph"), init_tables=True)
    c = TestClient(app)
    alice = {"Authorization": "Bearer alice-token-abcdefghijklmnopqrstuvwxyz123"}
    c.post("/v1/documents", headers=alice, json={"title": "Travel", "group": "engineering", "text": "Receipts are due within thirty days of travel."})
    job = c.post("/v1/research/jobs", headers={**alice, "Idempotency-Key": "durable-job-0001"}, json={"question": "receipts due"}).json()["job_id"]
    assert process_once(app.state.sessions, engine="langgraph", graph=app.state.graph)
    print(json.dumps({"job_id": job, "status": c.get(f"/v1/research/jobs/{job}", headers=alice).json()["status"]}))
''')


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request, tmp_path):
    if request.param == "postgres":
        if not os.getenv("TEST_POSTGRES_URL"):
            pytest.skip("Requires TEST_POSTGRES_URL and running PostgreSQL")
        pytest.importorskip("langgraph.checkpoint.postgres")
        from sqlalchemy import text
        from capstone.db import Base, database
        engine, _ = database(os.environ["TEST_POSTGRES_URL"])
        Base.metadata.drop_all(engine)
        with engine.begin() as con:
            for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"):
                con.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        return os.environ["TEST_POSTGRES_URL"]
    return f"sqlite:///{tmp_path / 'durable.db'}"


def restart(url):
    return create_app(Settings(url, None, TOKENS, "x" * 40, release_tenant="acme", graph="langgraph"), init_tables=True)


def test_interrupt_in_one_process_resume_in_another(database_url):
    out = subprocess.run([sys.executable, "-c", PHASE_ONE, database_url], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(__file__)), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    first = __import__("json").loads(out.stdout.strip().splitlines()[-1])
    job_id = first["job_id"]
    assert first["status"] == "pending_review"

    app = restart(database_url)  # brand-new process state: new engine, new checkpointer connection
    snapshot = app.state.graph.get_state(thread_config("acme", job_id))
    assert snapshot.next == ("review",) and snapshot.values["citations"]  # durable interrupted thread

    c = TestClient(app)
    assert c.post(f"/v1/research/jobs/{job_id}/review", headers=H(ALICE), json={"approved": True}).status_code == 403
    assert c.post(f"/v1/research/jobs/{job_id}/review", headers=H(BOB), json={"approved": True}).status_code == 404
    approved = c.post(f"/v1/research/jobs/{job_id}/review", headers=H(RACHEL), json={"approved": True}).json()
    assert approved == {"job_id": job_id, "status": "approved", "graph_status": "approved"}
    assert c.post(f"/v1/research/jobs/{job_id}/review", headers=H(RACHEL), json={"approved": False}).status_code == 409

    final = restart(database_url).state.graph.get_state(thread_config("acme", job_id))
    assert final.next == () and final.values["final_status"] == "approved" and final.values["reviewed_by"] == "rachel"


def _pending_job(app):
    from capstone.jobs import claim_job, finish_job
    c = TestClient(app)
    c.post("/v1/documents", headers=H(ALICE), json={"title": "Travel", "group": "engineering", "text": "Receipts are due within thirty days."})
    job_id = c.post("/v1/research/jobs", headers={**H(ALICE), "Idempotency-Key": "durable-job-0002"}, json={"question": "receipts due"}).json()["job_id"]
    with app.state.sessions() as s:
        claimed = claim_job(s)
    with app.state.sessions() as s:
        assert finish_job(s, claimed, graph=app.state.graph)
    return job_id, claimed


def test_graph_rejects_self_or_cross_tenant_resume(tmp_path):
    app = restart(f"sqlite:///{tmp_path / 'a.db'}")
    job_id, _ = _pending_job(app)
    for reviewer, tenant in [("alice", "acme"), ("rachel", "other")]:
        with pytest.raises(PermissionError):
            resume_review(app.state.graph, "acme", job_id, approved=True, reviewer=reviewer, reviewer_tenant=tenant)
    assert app.state.graph.get_state(thread_config("acme", job_id)).next == ("review",)


def test_retried_job_reuses_checkpoint(tmp_path):
    app = restart(f"sqlite:///{tmp_path / 'b.db'}")
    job_id, claimed = _pending_job(app)
    config = thread_config("acme", job_id)
    history = len(list(app.state.graph.get_state_history(config)))
    assert run_until_review(app.state.graph, claimed)["interrupted"]
    assert len(list(app.state.graph.get_state_history(config))) == history  # no re-run, no duplicate side effects


def test_reconciler_finishes_resume_lost_in_a_crash(tmp_path):
    from capstone.db import Job
    from capstone.jobs import reconcile_reviews, review_job
    from capstone.security import Principal
    app = restart(f"sqlite:///{tmp_path / 'c.db'}")
    job_id, _ = _pending_job(app)
    with app.state.sessions() as s:  # decision committed, but the API "crashed" before resuming
        review_job(s, s.get(Job, job_id), Principal("acme", "rachel", frozenset(["engineering"]), "reviewer"), False)
    with app.state.sessions() as s:
        assert reconcile_reviews(s, app.state.graph) == 1
        assert reconcile_reviews(s, app.state.graph) == 0
    assert app.state.graph.get_state(thread_config("acme", job_id)).values["final_status"] == "rejected"


def test_concurrent_postgres_checkpointer_setup():
    if not os.getenv("TEST_POSTGRES_URL"):
        pytest.skip("Requires TEST_POSTGRES_URL and running PostgreSQL")
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import text
    from capstone.checkpoints import open_checkpointer
    from capstone.db import database
    engine, _ = database(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as con:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"):
            con.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        opened = list(pool.map(lambda _: open_checkpointer(os.environ["TEST_POSTGRES_URL"]), range(4)))
    for _, close in opened:
        close()
