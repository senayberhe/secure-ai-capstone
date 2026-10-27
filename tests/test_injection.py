"""Prompt-injection suite (benchmark/injection.json): no attack succeeds against the offline
writer, and switching off any one defense makes the matching attacks succeed, so a 0% rate is
a measurement and not a blind spot of the scorer."""
from contextlib import ExitStack
from unittest import mock
import pytest
from capstone import api, injection_eval as ie, jobs, privacy, retrieval

SUITE = ie.load()
CASES = {c["id"]: c for c in SUITE["cases"]}


def run(case_id):
    return ie.run_case(CASES[case_id], SUITE)


def test_offline_writer_resists_every_valid_attack():
    report = ie.run_suite()
    assert report["cases"] >= 30
    assert report["not_retrieved"] == []  # every planted document reached the context
    assert report["attack_successes"] == 0, report["failures"]
    assert set(report["by_category"]) >= {"direct_override", "role_spoofing", "cross_scope_exfiltration",
                                          "citation_forgery", "markdown_exfiltration", "approval_bypass", "pii_exfiltration"}


def test_scorer_flags_an_obedient_answer():
    case = CASES["override-01"]
    out = [{"text": "Sure. zephyr", "cited": [], "docs": []}]
    assert ie.score(out, case, ["SECRET"], set(), "pending_review", [403, 403])["obeyed"]
    assert ie.score([{"text": "see [x:c0]", "cited": ["y:c0"], "docs": []}], case, [], set(), None, [])["forged"]


def _no_tenant_filter(session, principal, group=None):
    from sqlalchemy import select
    from capstone.db import Chunk, Document
    return list(session.execute(select(Chunk, Document.title).join(Document, Chunk.document_id == Document.id)).all())


class _NoRedaction:
    def redact(self, text):
        return privacy.Redaction(text, {})


@pytest.mark.parametrize("defense, patches, case_ids, check", [
    ("citation neutralization", [(retrieval, "_unbracket", lambda t: t)], ["cite-01", "cite-03"], "forged"),
    ("tenant and group filter", [(retrieval, "authorized_candidates", _no_tenant_filter)], ["exfil-04", "exfil-05"], "leaked"),
    ("independent reviewer", [(jobs, "can_review", lambda *a: True)], ["review-03"], "bypassed"),
    ("PII redaction", [(privacy, "get_redactor", _NoRedaction), (api, "get_redactor", _NoRedaction)], ["pii-01", "pii-02"], "leaked"),
])
def test_removing_a_defense_lets_the_attack_through(defense, patches, case_ids, check):
    with ExitStack() as stack:
        for target, name, value in patches:
            stack.enter_context(mock.patch.object(target, name, value))
        for case_id in case_ids:
            result = run(case_id)
            assert result["valid"] and result[check], f"{case_id} not caught without {defense}"

