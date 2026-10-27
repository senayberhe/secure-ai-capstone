# Portfolio presentation kit

## Project name

**Secure AI Engineering Platform — RAG, LangGraph, MLOps and Guardrails**

## Technical summary for a resume or portfolio

Designed and implemented an offline-first FastAPI reference platform combining tenant-isolated hybrid retrieval, LangGraph research orchestration, Postgres-backed durable jobs and independent review, Redis-based rate limiting and worker wake-ups, model evaluation scorecards, canary version routing, rollback and incident tracking. Built automated tests for cross-tenant access, redacted errors, idempotency, job claim fencing, release-version binding and approval controls. Docker Compose and GitHub Actions enable repeatable integration testing. The project includes a simulated safety dashboard and an optional restricted LLM writer.

State test counts only after running the suite yourself; don't claim the project has been publicly deployed or that the LLM passed adversarial red-teaming.

## Five-minute demonstration

1. Show `docs/ARCHITECTURE.md`; explain the DB as source of truth, Redis as wake-up accelerator, and Auth → RAG boundary.
2. Run `pytest -q` and call out live/infrastructure skips.
3. Run `python -m capstone.demo` and open `artifacts/dashboard.html`.
4. Show a query from authorized Alice and a query from another tenant that returns zero evidence.
5. Show the persisted `pending_review` job and rejection of self-approval.
6. Show the canary at 10%, then error rate 12% exceeding its 5% threshold and automatic rollback.
7. Explain exactly which code paths are simulated and which would need certified telemetry, managed identity and real provider integration.

## Common interviewer questions

**Why use both Redis and PostgreSQL?** PostgreSQL owns durable state and constraints; Redis makes requests faster and shares throttling. Lost Redis notifications cannot erase jobs.

**How do you prevent cross-tenant RAG leakage?** Filter by server-derived tenant and permissions in the database before candidate scoring, then validate source IDs on output. Isolate indexes and embedding providers as needed.

**Why not let LangGraph handle approvals itself?** Graph interrupts are workflow state, not authorization. Our durable database transitions are enforced by identity and role permissions independent of the model; the graph does bounded research only.

**What are your evaluation limits?** The offline vectors are lexical; citation verification checks IDs, not factual entailment; live attacks and calibration are not measured; synthetic canary telemetry is operator-provided and must be attested before real deployment.

**How do you prevent a crashed worker from corrupting a job?** Lease expiration, a monotonic attempt count and a conditional write fence. External API side effects require a separate idempotency strategy.

**How would you scale?** PostgreSQL indexes and migrations; Redis distributed controls; tenant-aware vector DB; a managed queue for higher scale; persistent LangGraph checkpoints; cloud observability/incident response; horizontal worker replication; a signed CI/CD model registry.

## Suggested screenshots for a README or interview

The README already shows the web UI's Releases tab (`docs/images/web-ui.png`). Other good options:

- The generated `artifacts/dashboard.html` (clearly marked synthetic).
- FastAPI `/docs` showing authenticated routes.
- Local tests passing and integration checks appropriately skipped or passing.
- Grafana/Prometheus dashboard once real metrics have been configured.
- A redacted runbook showing the rollback state machine.
