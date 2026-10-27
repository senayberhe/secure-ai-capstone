"""Durable job lifecycle, leases, and stale-worker fencing."""
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from .db import Job, Approval, Audit, now, uid
from .security import Principal, can_review, redact_pii
from .agent import research, resume_review, run_until_review, thread_config


def submit_job(session, principal: Principal, question: str, idem_key: str, *, requested_group=None, model_version=None) -> Job:
    question = redact_pii(question)  # never persist raw PII from a prompt; retries redact identically
    match = session.scalar(select(Job).where(Job.tenant == principal.tenant, Job.owner == principal.user, Job.idem_key == idem_key))
    if match:
        if match.question != question or match.requested_group != requested_group:
            raise ValueError("Idempotency key already used with different question")
        return match
    job = Job(id=uid(), tenant=principal.tenant, owner=principal.user,
              groups_json=json.dumps(sorted(principal.groups)), question=question,
              idem_key=idem_key, requested_group=requested_group, model_version=model_version, citations_json="[]", status="queued")
    session.add(job)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(Job).where(Job.tenant == principal.tenant, Job.owner == principal.user, Job.idem_key == idem_key))
        if not existing or existing.question != question or existing.requested_group != requested_group:
            raise ValueError("Idempotency key conflict")
        return existing
    return job


def claim_job(session, max_attempts=3, lease_seconds=120):
    """PostgreSQL uses SKIP LOCKED. SQLite tests use an atomic compare/update."""
    recover_expired(session, max_attempts)
    stmt = select(Job).where(Job.status == "queued").order_by(Job.created_at, Job.id).limit(1)
    if session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = session.scalar(stmt)
    if job is None:
        session.rollback()
        return None
    attempt = job.attempts + 1
    lease = now() + timedelta(seconds=lease_seconds)
    changed = session.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                              .values(status="running", attempts=attempt, lease_until=lease))
    if changed.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    return {"id": job.id, "tenant": job.tenant, "owner": job.owner,
            "groups": frozenset(json.loads(job.groups_json)),
            "question": job.question, "group": job.requested_group, "model_version": job.model_version, "attempt": attempt}


def recover_expired(session, max_attempts=3):
    expired = list(session.scalars(select(Job).where(Job.status == "running", Job.lease_until < now())))
    for job in expired:
        job.status = "failed" if job.attempts >= max_attempts else "queued"
        job.lease_until = None
    session.commit()
    return len(expired)


def finish_job(session, claimed: dict, engine: str = "offline", writer=None, graph=None) -> bool:
    """Run research for a claimed job. With a durable `graph`, the thread stops at its review
    interrupt and is checkpointed; a retried job reuses that checkpoint instead of re-running."""
    principal = Principal(claimed["tenant"], claimed["owner"], claimed["groups"], "reader")
    try:
        if graph is not None:
            result = run_until_review(graph, claimed)
            if not result["interrupted"]:
                raise ValueError("Thread did not reach review")
        else:
            result = research(session, principal, claimed["question"], engine=engine, group=claimed.get("group"), writer=writer)
        if not result.get("verified"):
            raise ValueError("Invalid citations")
    except Exception:
        # Never copy untrusted exception text into audit or API responses.
        changed = session.execute(update(Job).where(Job.id == claimed["id"],
                               Job.status == "running", Job.attempts == claimed["attempt"])
                               .values(status="queued" if claimed["attempt"] < 3 else "failed", lease_until=None))
        session.commit()
        return False
    changed = session.execute(update(Job).where(Job.id == claimed["id"],
                               Job.status == "running", Job.attempts == claimed["attempt"])
                               .values(status="pending_review", report=redact_pii(result["report"]),
                                       citations_json=json.dumps(result["citations"]), lease_until=None))
    if changed.rowcount == 1:
        session.add(Approval(id=uid(), job_id=claimed["id"], tenant=claimed["tenant"],
                             requested_by=claimed["owner"], decision="pending"))
        session.add(Audit(id=uid(), tenant=claimed["tenant"], action="research_completed",
                          actor="worker"))
    session.commit()
    return changed.rowcount == 1


def review_job(session, job: Job, reviewer: Principal, approved: bool):
    if not can_review(reviewer, job.tenant, json.loads(job.groups_json)):
        raise PermissionError("Reviewer not authorized for this tenant and document scope")
    if reviewer.user == job.owner:
        raise PermissionError("Author may not approve own report")
    approval = session.scalar(select(Approval).where(Approval.job_id == job.id).with_for_update())
    if not approval or approval.decision != "pending" or job.status != "pending_review":
        raise ValueError("Review not pending")
    approval.reviewed_by = reviewer.user
    approval.decision = "approved" if approved else "rejected"
    job.status = "approved" if approved else "rejected"
    session.add(Audit(id=uid(), tenant=job.tenant, action="review_approved" if approved else "review_rejected", actor=reviewer.user))
    session.commit()
    return job


def reconcile_reviews(session, graph, limit: int = 50) -> int:
    """Finish graph threads whose database decision is recorded but whose resume never completed
    (for example, the API crashed between the approval commit and the resume)."""
    rows = session.execute(select(Job, Approval).join(Approval, Approval.job_id == Job.id)
                           .where(Job.status.in_(["approved", "rejected"]), Approval.reviewed_by.is_not(None))
                           .limit(limit)).all()
    finished = 0
    for job, approval in rows:
        try:
            before = graph.get_state(thread_config(job.tenant, job.id)).next
            if before == ("review",):
                resume_review(graph, job.tenant, job.id, approved=approval.decision == "approved",
                              reviewer=approval.reviewed_by, reviewer_tenant=job.tenant)
                finished += 1
        except Exception:
            continue  # Retried on the next pass; never copy untrusted error text into logs.
    return finished
