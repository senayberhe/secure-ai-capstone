"""Challenge 15: local PII redaction before persistence, outbound model calls and logs."""
import logging
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from capstone.privacy import RegexRedactor, get_redactor, reset_redactor

PII = ["212-555-0199", "(212) 555-0199", "212.555.0199", "+1 212 555 0199", "+44 20 7946 0958",
       "+49 30 901820", "person@example.com", "jo.smith+travel@example.co.uk", "A_B-c@sub.example.org",
       "078-05-1120", "4111 1111 1111 1111", "5500-0000-0000-0004"]
NOT_PII = ["version 1.2.3", "released 2026-10-26", "order ORD-2024-000123", "ticket 555-0199",
           "host 192.168.1.10", "costs $1,250.00", "ERR-4031", "call extension 4567", "card 1234 5678 9012 3456",
           "build 20261026.1", "ISO time 2026-10-26T08:12:49Z"]


@pytest.fixture(autouse=True)
def fresh_detector(monkeypatch):
    monkeypatch.delenv("CAPSTONE_PII_DETECTOR", raising=False)
    reset_redactor()
    yield
    reset_redactor()


@pytest.mark.parametrize("value", PII)
def test_regex_redacts_variants(value):
    out = RegexRedactor().redact(f"Contact: {value}, thanks.")
    assert value not in out.text and sum(out.counts.values()) == 1, out


@pytest.mark.parametrize("value", NOT_PII)
def test_regex_leaves_identifiers_alone(value):
    assert RegexRedactor().redact(value).text == value


def test_counts_and_tokens():
    out = RegexRedactor().redact("Mail a@b.io or c@d.io, phone 212-555-0199")
    assert out.counts == {"EMAIL_ADDRESS": 2, "PHONE_NUMBER": 1}
    assert out.text == "Mail [EMAIL] or [EMAIL], phone [PHONE]"


def test_presidio_adds_names_without_flagging_order_ids(monkeypatch):
    pytest.importorskip("presidio_analyzer")
    monkeypatch.setenv("CAPSTONE_PII_DETECTOR", "presidio")
    redactor = get_redactor()
    out = redactor.redact("Ask Maria Garcia about order ORD-2024-000123 or call +44 20 7946 0958.")
    assert "Maria Garcia" not in out.text and "ORD-2024-000123" in out.text and "7946" not in out.text
    assert out.counts == {"PERSON": 1, "PHONE_NUMBER": 1}


def test_misconfigured_detector_fails_at_startup(monkeypatch):
    from capstone.api import create_app
    from capstone.config import Settings
    from tests.conftest import TOKENS
    monkeypatch.setenv("CAPSTONE_PII_DETECTOR", "remote-cloud-api")
    with pytest.raises(RuntimeError):
        create_app(Settings("sqlite://", None, TOKENS, "x" * 40, release_tenant="acme"), init_tables=True)


def test_nothing_raw_is_persisted(app, client, headers):
    from capstone.db import Chunk, Document, Incident, Job
    r = client.post("/v1/documents", headers=headers["alice"], json={
        "title": "Contacts for person@example.com", "group": "engineering",
        "text": "Travel desk: call 212-555-0199 or email travel.desk@example.com."})
    assert r.json()["redactions"] == {"EMAIL_ADDRESS": 2, "PHONE_NUMBER": 1}
    client.post("/v1/research/jobs", headers={**headers["alice"], "Idempotency-Key": "privacy-job-0001"},
                json={"question": "what is travel.desk@example.com for?"})
    client.post("/v1/incidents", headers=headers["admin"], json={"category": "data_exposure", "summary": "Reported by caller 212-555-0199"})
    with app.state.sessions() as s:
        stored = [d.title for d in s.scalars(select(Document))] + [c.text for c in s.scalars(select(Chunk))] \
            + [j.question for j in s.scalars(select(Job))] + [i.summary for i in s.scalars(select(Incident))]
    blob = " ".join(stored)
    for raw in ("person@example.com", "travel.desk@example.com", "212-555-0199"):
        assert raw not in blob
    assert "[EMAIL]" in blob and "[PHONE]" in blob


def test_outbound_model_call_is_redacted():
    from capstone.llm import LLMWriter
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(output_text="Receipts go to the desk [d1:c0].")
    writer = LLMWriter.__new__(LLMWriter)  # skip the real OpenAI client
    writer.model, writer.client = "approved-model", SimpleNamespace(responses=SimpleNamespace(create=create))
    writer.draft("Who is jane@example.com?", [{"citation_id": "d1:c0", "text": "Desk: call 212-555-0199, see [x:c9]"}])
    assert "jane@example.com" not in sent["input"] and "212-555-0199" not in sent["input"]
    assert "[x:c9]" not in sent["input"]  # a source cannot forge a citation in the prompt


def test_logs_from_two_tenants_never_contain_raw_pii(client, headers, caplog):
    caplog.set_level(logging.DEBUG)
    for who, secret in (("alice", "alice.private@example.com"), ("bob", "+44 20 7946 0958")):
        client.post("/v1/documents", headers=headers[who], json={"title": "Note", "group": "engineering", "text": f"Contact {secret} for receipts."})
        client.post("/v1/rag/query", headers=headers[who], json={"question": f"receipts {secret}"})
        client.post("/v1/research/jobs", headers={**headers[who], "Idempotency-Key": f"log-check-{who}-01"}, json={"question": f"receipts {secret}"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "alice.private@example.com" not in text and "7946 0958" not in text
    bob_view = client.post("/v1/rag/query", headers=headers["bob"], json={"question": "contact receipts"}).text
    assert "alice" not in bob_view.lower()
