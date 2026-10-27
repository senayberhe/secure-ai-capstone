"""Auth-required FastAPI endpoints for secure RAG, jobs, approvals, releases and incidents."""
import hashlib
import json
import re
import time
from typing import Literal
from fastapi import FastAPI, Header, HTTPException, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from .config import Settings
from .db import (Document, Chunk, Job, ModelVersion, Incident, Audit, ReleaseEvidence, ArtifactVerification,
                 database, init_schema, uid)
from .security import (Principal, authenticate, can_review, require_release_operator, require_role,
                       redact_pii, sanitize_incident_summary)
from .retrieval import split_text, retrieve, extract_report, index_chunks
from .embeddings import get_embedder
from .privacy import get_redactor
from collections import Counter
from .jobs import submit_job, review_job
from .agent import resume_review
from .releases import (register, evaluate, begin_canary, canary_observation, rollback, stable_model, promote_canary,
                       activate_baseline, submit_evidence)
from .attestation import load_public_keys
from .scorecard import scorecard
from .telemetry import REQUESTS, DURATION, metric_bytes
from .queueing import MemoryRateLimiter, RedisNotifier


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Upload(StrictModel):
    title: str = Field(min_length=1, max_length=180)
    text: str = Field(min_length=1, max_length=25000)
    group: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class Query(StrictModel):
    question: str = Field(min_length=1, max_length=3000)
    group: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    top_k: int = Field(default=3, ge=1, le=10)


class Review(StrictModel):
    approved: bool


class Register(StrictModel):
    version: str
    sha256: str
    provider_model: str | None = None
    artifact_path: str | None = Field(default=None, max_length=255)


class EvidenceEnvelope(StrictModel):
    payload: str = Field(min_length=1, max_length=200000)
    signature: str = Field(min_length=1, max_length=200)
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")


class ModelEval(StrictModel):
    version: str
    samples: int = Field(ge=1, le=1000000)
    accuracy: float = Field(ge=0, le=1)
    unsupported_rate: float = Field(ge=0, le=1)
    leakage_count: int = Field(ge=0)
    p95_ms: float = Field(ge=0, le=1e7)


class Canary(StrictModel):
    version: str
    evaluation_id: str
    percent: int = 10


class Observation(StrictModel):
    version: str
    requests: int = Field(ge=0, le=100000000)
    errors: int = Field(ge=0)
    critical_events: int = Field(ge=0)


class Rollback(StrictModel):
    version: str


class IncidentCreate(StrictModel):
    category: str = Field(pattern=r"^[a-z_]{3,60}$")
    summary: str = Field(min_length=1, max_length=300)


class DecisionRows(StrictModel):
    true: Literal[0, 1]
    pred: Literal[0, 1]
    group: str = Field(min_length=1, max_length=40)
    unsupported: bool = False
    leak: bool = False


class ScorecardRequest(StrictModel):
    records: list[DecisionRows] = Field(min_length=20, max_length=100000)


def create_app(settings: Settings, *, init_tables: bool = False):
    engine, sessions = database(settings.database_url)
    if init_tables:
        init_schema(engine)
    app = FastAPI(title="Secure AI Engineering Capstone", version="0.1.0")
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.engine = engine
    app.state.notifier = RedisNotifier(settings.redis_url) if settings.redis_url else None
    app.state.limiter = MemoryRateLimiter() if not settings.redis_url else app.state.notifier
    get_redactor()  # load the PII detector now: a misconfigured detector fails at startup, not mid-request
    # Fails at startup if a configured attester key is malformed.
    trusted_keys = load_public_keys(settings.attestation_keys)
    gate = {"artifact_root": settings.artifact_root, "max_age_hours": settings.evidence_max_age_hours}
    app.state.graph = None
    if settings.graph == "langgraph":
        # The API resumes interrupted research threads from the shared durable checkpoint store.
        from .agent import build_durable_graph
        from .checkpoints import checkpoint_url, open_checkpointer
        saver, _close = open_checkpointer(checkpoint_url(settings.database_url))
        app.state.graph = build_durable_graph(sessions, saver)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.middleware("http")
    async def telemetry(request: Request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # No exception details or user content in client response.
            response = JSONResponse(status_code=500, content={"detail": "Internal error"})
        route = request.scope.get("route")
        route_label = getattr(route, "path", "unmatched")
        REQUESTS.labels(route=route_label, method=request.method, status=str(response.status_code)).inc()
        DURATION.labels(route=route_label, method=request.method).observe(time.perf_counter() - start)
        return response

    def principal(request: Request) -> Principal:
        p = authenticate(request.headers.get("Authorization"), settings.api_tokens)
        # Avoid raw tokens in Redis keys. Rate limit scoped by tenant + subject.
        identity = hashlib.sha256(f"{p.tenant}:{p.user}".encode()).hexdigest()
        try:
            if not app.state.limiter.allow(identity):
                raise HTTPException(429, "Rate limit exceeded")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Rate-limit service unavailable")
        return p

    def release_operator(p: Principal = Depends(principal)) -> Principal:
        require_release_operator(p, settings.release_tenant)
        return p

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        try:
            with engine.connect() as con:
                con.execute(text("SELECT 1"))
            if app.state.notifier:
                app.state.notifier.ping()
        except Exception:
            raise HTTPException(503, "Dependency unavailable")
        return {"status": "ready"}

    @app.post("/v1/documents", status_code=201)
    def upload(data: Upload, p: Principal = Depends(principal)):
        require_role(p, "editor", "admin")
        if data.group not in p.groups:
            raise HTTPException(403, "Group not authorized")
        docid = uid()
        # Redact before anything is persisted, embedded or indexed.
        redactor = get_redactor()
        title, body = redactor.redact(data.title), redactor.redact(data.text)
        blocks = split_text(body.text, max_words=65, overlap=12)
        with sessions.begin() as s:
            s.add(Document(id=docid, tenant=p.tenant, owner=p.user,
                           access_group=data.group, title=title.text,
                           content_sha256=hashlib.sha256(body.text.encode()).hexdigest()))
            rows = [Chunk(id=f"{docid}:c{i}", document_id=docid, tenant=p.tenant,
                          access_group=data.group, text=chunk, position=i) for i, chunk in enumerate(blocks)]
            s.add_all(rows)
            s.flush()
            # Cache vectors at ingestion so queries only embed the question.
            index_chunks(s, rows, get_embedder())
        redacted = dict(Counter(title.counts) + Counter(body.counts))
        return {"document_id": docid, "chunks": len(blocks), "redactions": redacted}

    @app.post("/v1/rag/query")
    def ask(data: Query, p: Principal = Depends(principal)):
        with sessions() as s:
            passages = retrieve(s, p, data.question, data.top_k, data.group)
        report, citations = extract_report(data.question, passages)
        return {"answer": redact_pii(report), "citations":
                [{"citation_id": x["citation_id"], "document_id": x["document_id"],
                  "title": x["title"]} for x in passages], "mode": "offline-extractive"}

    @app.post("/v1/research/jobs", status_code=202)
    def create_job(data: Query, p: Principal = Depends(principal),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        if not idempotency_key or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", idempotency_key):
            raise HTTPException(400, "Valid Idempotency-Key required")
        with sessions() as s:
            try:
                job = submit_job(s, p, data.question, idempotency_key, requested_group=data.group,
                                 model_version=stable_model(s, f"{p.tenant}:{p.user}", settings.routing_secret))
            except ValueError:
                raise HTTPException(409, "Idempotency conflict")
        if app.state.notifier:
            try:
                app.state.notifier.notify(job.id)
            except Exception:
                pass  # Postgres polling guarantees discoverability, not notification delivery.
        return {"job_id": job.id, "status": job.status, "model_version": job.model_version}

    @app.get("/v1/research/jobs/{job_id}")
    def get_job(job_id: str, p: Principal = Depends(principal)):
        with sessions() as s:
            job = s.get(Job, job_id)
            # The owner, or an in-scope reviewer once there is a report to decide on.
            reviewable = job is not None and job.status in {"pending_review", "approved", "rejected"} \
                and can_review(p, job.tenant, json.loads(job.groups_json))
            if not job or job.tenant != p.tenant or (job.owner != p.user and not reviewable):
                raise HTTPException(404, "Job not found")
            return {"job_id": job.id, "status": job.status,
                    "report": redact_pii(job.report) if job.report else None,
                    "citations": json.loads(job.citations_json), "model_version": job.model_version}

    @app.post("/v1/research/jobs/{job_id}/review")
    def review(job_id: str, data: Review, p: Principal = Depends(principal)):
        with sessions() as s:
            job = s.get(Job, job_id)
            if not job or job.tenant != p.tenant:
                raise HTTPException(404, "Job not found")
            try:
                job = review_job(s, job, p, data.approved)
            except PermissionError:
                raise HTTPException(403, "Review not authorized")
            except ValueError:
                raise HTTPException(409, "No pending review")
            graph_status = None
            if app.state.graph is not None:
                # The decision is committed above; resuming the checkpoint only finishes the thread.
                # If this fails, the worker's reconciler resumes it from the same durable state.
                try:
                    graph_status = resume_review(app.state.graph, job.tenant, job.id, approved=data.approved,
                                                 reviewer=p.user, reviewer_tenant=p.tenant)
                except Exception:
                    graph_status = None
            return {"job_id": job.id, "status": job.status,
                    **({"graph_status": graph_status} if app.state.graph is not None else {})}

    @app.post("/v1/models/register", status_code=201)
    def register_model(data: Register, p: Principal = Depends(release_operator)):
        with sessions() as s:
            try:
                row = register(s, p, data.version, data.sha256, data.provider_model, data.artifact_path)
            except ValueError:
                raise HTTPException(400, "Invalid model registration")
            return {"version": row.version, "status": row.status, "provider_model": row.provider_model}

    @app.post("/v1/models/evaluate")
    def evaluate_model(data: ModelEval, p: Principal = Depends(release_operator)):
        with sessions() as s:
            try:
                result = evaluate(s, p, **data.model_dump())
            except ValueError:
                raise HTTPException(400, "Invalid evaluation")
            return {"evaluation_id": result.id, "version": result.model_version, "decision": result.decision}

    @app.post("/v1/models/evidence", status_code=201)
    def submit_release_evidence(data: EvidenceEnvelope, p: Principal = Depends(release_operator)):
        """Accept a CI-signed evaluation report; its id is what starts a canary or baseline."""
        with sessions() as s:
            try:
                row = submit_evidence(s, p, data.model_dump(), trusted_keys, settings.evidence_max_age_hours)
            except PermissionError as error:
                raise HTTPException(403, str(error))
            except ValueError as error:
                raise HTTPException(409, str(error))
            return {"evidence_id": row.id, "version": row.model_version, "decision": row.decision,
                    "attester": row.attester, "artifact_sha256": row.artifact_sha256}

    @app.get("/v1/models/{version}/evidence")
    def list_evidence(version: str, p: Principal = Depends(release_operator)):
        with sessions() as s:
            evidence = s.scalars(select(ReleaseEvidence).where(ReleaseEvidence.model_version == version)
                                 .order_by(ReleaseEvidence.submitted_at.desc()))
            checks = s.scalars(select(ArtifactVerification).where(ArtifactVerification.model_version == version)
                               .order_by(ArtifactVerification.verified_at.desc()))
            return {"evidence": [{"evidence_id": e.id, "decision": e.decision, "attester": e.attester,
                                  "artifact_sha256": e.artifact_sha256, "dataset_sha256": e.dataset_sha256,
                                  "attested_at": e.attested_at.isoformat(), "key_id": e.key_id} for e in evidence],
                    "artifact_verifications": [{"sha256": v.sha256, "matched": v.matched, "verified_by": v.verified_by,
                                                "verified_at": v.verified_at.isoformat()} for v in checks]}

    @app.post("/v1/releases/bootstrap")
    def bootstrap(data: Canary, p: Principal = Depends(release_operator)):
        with sessions() as s:
            try:
                row = activate_baseline(s, p, data.version, data.evaluation_id, **gate)
            except PermissionError:
                raise HTTPException(403, "Independent reviewer required")
            except ValueError:
                raise HTTPException(409, "Baseline gate failed")
            return {"version": row.version, "status": row.status}

    @app.post("/v1/releases/canary")
    def start_canary(data: Canary, p: Principal = Depends(release_operator)):
        with sessions() as s:
            try:
                row = begin_canary(s, p, **data.model_dump(), **gate)
            except PermissionError:
                raise HTTPException(403, "Independent reviewer required")
            except ValueError:
                raise HTTPException(409, "Canary gate failed")
            return {"version": row.version, "traffic_pct": row.traffic_pct, "status": row.status}

    @app.post("/v1/releases/observe")
    def observe(data: Observation, p: Principal = Depends(release_operator)):
        with sessions() as s:
            try:
                return canary_observation(s, p, **data.model_dump())
            except ValueError:
                raise HTTPException(409, "Invalid canary observation")

    @app.post("/v1/releases/promote")
    def promote(data: Rollback, p: Principal = Depends(release_operator)):
        with sessions() as s:
            try:
                return {"status": promote_canary(s, p, data.version).status}
            except PermissionError:
                raise HTTPException(403, "Independent promotion required")
            except ValueError:
                raise HTTPException(409, "Promotion gates not satisfied")

    @app.post("/v1/releases/rollback")
    def manual_rollback(data: Rollback, p: Principal = Depends(release_operator)):
        with sessions() as s:
            try:
                return {"status": rollback(s, p, data.version).status}
            except ValueError:
                raise HTTPException(409, "Cannot roll back")

    @app.get("/v1/releases/assignment")
    def assignment(p: Principal = Depends(principal)):
        with sessions() as s:
            return {"version": stable_model(s, f"{p.tenant}:{p.user}", settings.routing_secret)}

    @app.post("/v1/scorecards")
    def evaluate_rows(data: ScorecardRequest, p: Principal = Depends(principal)):
        require_role(p, "reviewer", "admin")
        return scorecard([r.model_dump() for r in data.records])

    @app.post("/v1/incidents", status_code=201)
    def open_incident(data: IncidentCreate, p: Principal = Depends(principal)):
        require_role(p, "admin", "reviewer")
        with sessions.begin() as s:
            row = Incident(id=uid(), tenant=p.tenant, category=data.category,
                           summary=sanitize_incident_summary(data.summary),
                           opened_by=p.user, status="open")
            s.add(row)
        return {"incident_id": row.id, "status": row.status}

    @app.post("/v1/incidents/{incident_id}/contain")
    def contain(incident_id: str, p: Principal = Depends(principal)):
        require_role(p, "admin", "reviewer")
        with sessions.begin() as s:
            row = s.get(Incident, incident_id)
            if not row or row.tenant != p.tenant:
                raise HTTPException(404, "Incident not found")
            if row.status != "open":
                raise HTTPException(409, "Invalid incident state")
            row.status = "contained"
        return {"status": "contained"}

    @app.post("/v1/incidents/{incident_id}/resolve")
    def resolve(incident_id: str, p: Principal = Depends(principal)):
        require_role(p, "admin")
        with sessions.begin() as s:
            row = s.get(Incident, incident_id)
            if not row or row.tenant != p.tenant:
                raise HTTPException(404, "Incident not found")
            if row.status != "contained":
                raise HTTPException(409, "Contain first")
            row.status = "resolved"
        return {"status": "resolved"}

    # -- read-only listings for the web UI: every query is scoped to the caller's tenant ----------

    @app.get("/v1/me")
    def me(p: Principal = Depends(principal)):
        return {"user": p.user, "tenant": p.tenant, "role": p.role, "groups": sorted(p.groups),
                "release_operator": bool(settings.release_tenant) and p.tenant == settings.release_tenant}

    @app.get("/v1/documents")
    def list_documents(p: Principal = Depends(principal)):
        if not p.groups:
            return {"documents": []}
        with sessions() as s:
            rows = s.scalars(select(Document).where(Document.tenant == p.tenant,
                                                    Document.access_group.in_(sorted(p.groups)))
                             .order_by(Document.created_at.desc()).limit(100))
            return {"documents": [{"document_id": d.id, "title": d.title, "group": d.access_group,
                                   "owner": d.owner, "created_at": d.created_at.isoformat()} for d in rows]}

    @app.get("/v1/research/jobs")
    def list_jobs(p: Principal = Depends(principal)):
        with sessions() as s:
            rows = s.scalars(select(Job).where(Job.tenant == p.tenant).order_by(Job.created_at.desc()).limit(200))
            visible = [j for j in rows if j.owner == p.user or (
                j.status in {"pending_review", "approved", "rejected"}
                and can_review(p, j.tenant, json.loads(j.groups_json)))]
            return {"jobs": [{"job_id": j.id, "owner": j.owner, "question": j.question, "status": j.status,
                              "model_version": j.model_version, "created_at": j.created_at.isoformat(),
                              "can_review": j.status == "pending_review" and j.owner != p.user
                              and can_review(p, j.tenant, json.loads(j.groups_json))}
                             for j in visible[:100]]}

    @app.get("/v1/models")
    def list_models(p: Principal = Depends(release_operator)):
        with sessions() as s:
            rows = s.scalars(select(ModelVersion).order_by(ModelVersion.version))
            return {"models": [{"version": m.version, "status": m.status, "traffic_pct": m.traffic_pct,
                                "provider_model": m.provider_model, "registered_by": m.registered_by,
                                "reviewed_by": m.reviewed_by, "observed_requests": m.observed_requests,
                                "observed_errors": m.observed_errors, "observed_critical": m.observed_critical}
                               for m in rows]}

    @app.get("/v1/incidents")
    def list_incidents(p: Principal = Depends(principal)):
        require_role(p, "admin", "reviewer")
        with sessions() as s:
            rows = s.scalars(select(Incident).where(Incident.tenant == p.tenant).limit(200))
            return {"incidents": [{"incident_id": i.id, "category": i.category, "summary": i.summary,
                                   "status": i.status, "opened_by": i.opened_by} for i in rows]}

    @app.get("/metrics")
    def metrics(p: Principal = Depends(principal)):
        require_role(p, "admin")
        return Response(metric_bytes(), media_type="text/plain; version=0.0.4")

    return app
