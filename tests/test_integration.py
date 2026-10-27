"""Optional infrastructure integration checks; CI runs with real Postgres and Redis."""
import os
import pytest
from capstone.db import database, init_schema, Job, uid
from capstone.jobs import claim_job


def test_postgres_concurrent_claim():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Requires TEST_POSTGRES_URL and running PostgreSQL")
    pytest.importorskip("psycopg")
    engine, sessions = database(url)
    init_schema(engine)
    identifier = uid()
    with sessions.begin() as s:
        s.add(Job(id=identifier, tenant="test_tenant", owner="worker_tester", groups_json="[]",
                  question="test", idem_key=identifier, status="queued", citations_json="[]"))
    # Test actual SKIP LOCKED claim path, and uniqueness of a claimed job ID.
    from concurrent.futures import ThreadPoolExecutor
    def claim():
        with sessions() as s:
            return claim_job(s)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), [0, 1]))
    assert sum(c is not None and c["id"] == identifier for c in claims) == 1


def test_redis_atomic_rate_limit():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Requires TEST_REDIS_URL and running Redis")
    pytest.importorskip("redis")
    from capstone.queueing import RedisNotifier
    r = RedisNotifier(url)
    key = "test-rate-" + uid()
    assert r.allow(key, limit=2, window=10)
    assert r.allow(key, limit=2, window=10)
    assert not r.allow(key, limit=2, window=10)


def test_langgraph_rag_with_actual_runtime():
    pytest.importorskip("langgraph")
    from capstone.agent import research
    from capstone.security import Principal
    engine, sessions = database("sqlite://")
    init_schema(engine)
    p = Principal("acme", "alice", frozenset(["engineering"]), "reader")
    with sessions() as s:
        assert research(s,p,"some query",engine="langgraph")["verified"] is True
