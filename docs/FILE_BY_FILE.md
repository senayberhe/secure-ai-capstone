# File-by-file guided tour

Follow these files in order. Each entry explains what to inspect, how data moves, the security assumption, and a practical test. The tutorial follows the flow **API → identity → database → retrieval → worker/graph → approval → release → monitoring**.

## Core configuration and data access

### 1. `pyproject.toml`

- Contains the package build configuration and four dependency groups: base, `production`, `test`, `documents`, and `llm`.
- Default `uv sync --extra test` deliberately avoids cloud and graph installations so the offline learning suite works anywhere.
- Production installations add `redis`, `psycopg`, `langgraph`, and OpenTelemetry instruments.
- **Exercise:** compare dependency groups using `uv tree` and create a reproducible lock file with `uv lock`. Commit `uv.lock` to source control for a real project.

### 2. `.env.example` and `.gitignore`

- `.env.example` documents required environment variables and local identity roles; *every value must be replaced before deployment*.
- `.gitignore` prevents publishing `.env`, environment files, local databases and private artifacts.
- This is not a secret manager: move production credentials to deployment-managed secrets.

### 3. `capstone/config.py`

- `Settings.from_env()` reads the DB URL, Redis URL, routing secret and JSON identity mapping.
- It refuses to start with no identities, short tokens, or short routing secrets (fail-closed bootstrap).
- `Settings` is immutable after creation, reducing accidental configuration drift.
- **Exercise:** remove the routing secret and verify a startup error is raised.

### 4. `capstone/db.py`

- `Base` is the SQLAlchemy declarative model registry; each ORM class maps to a SQL table.
- `Document` and `Chunk` retain source identifiers and tenant/group scope.
- `Job` stores requests, immutable model version selection, status, attempt generation and lease deadline.
- `Approval` enforces one review record per job. `ModelVersion` stores model artifact identity, canary state, and observed errors. `Evaluation` holds candidate-specific evaluation evidence. `Incident` and `Audit` store operator events.
- `database()` selects SQLite engine options for tests and ordinary pooling for Postgres.
- `init_schema()` uses `create_all` only for quick start; use Alembic in production.
- **Exercise:** inspect SQL constraints and add a migration for an index on `(status, created_at)`.

### 5. `capstone/init_db.py`

- A tiny command module that runs demo schema initialization, separated from the API startup.
- Run with `uv run python -m capstone.init_db`.
- In Docker Compose the one-time `schema` service must finish before the API and worker start.

### 6. `capstone/security.py`

- `Principal` is the authenticated identity: tenant, user, groups, and role.
- `authenticate()` compares the bearer secret with server-held keys rather than trusting JSON identity fields from the client.
- `authorize_document()` returns a generic not-found error for unauthorized resources.
- `redact_pii()` removes some email and telephone patterns from outputs and persisted reports. It cannot detect all private information.
- **Exercise:** try passing `"role":"admin"` in a request and verify Pydantic rejects it.

## RAG and agent execution

### 7. `capstone/retrieval.py`

- `split_text()` splits words into overlapping chunks while avoiding redundant trailing chunks.
- `tokens()` is the deterministic lexical tokenizer; it isn't a linguistic or multilingual tokenizer.
- `hash_vector()` maps terms to a repeatable dense vector using BLAKE2b hashing; these vectors are **not pretrained semantic embeddings**.
- `bm25_scores()` computes keyword scores with term-frequency saturation and document-frequency weighting.
- `authorized_candidates()` applies SQL tenant/group filters *before* vectorization or ranking.
- `retrieve()` combines BM25 and dense ranks using RRF; it drops zero-relevance candidates and returns traceable citation IDs.
- `extract_report()` returns extractive evidence and citation IDs. `check_citations()` verifies cited IDs; it does not prove factual entailment.
- **Exercise:** attempt to query a document from a different tenant and assert no candidate row was considered.

### 8. `capstone/agent.py`

- `ResearchState` is a `TypedDict` containing the question, bounded plan, authorized evidence, draft, citation IDs and verification result.
- Four nodes are deliberately simple: `plan`, `search`, `draft`, `verify`.
- In `offline` mode the same node functions run sequentially without depending on LangGraph.
- In `langgraph` mode, a genuine `StateGraph` uses START → plan → retrieve → draft → verify → END.
- The graph never decides tenant access or publication. Those are enforced independently.
- **Exercise:** after `uv sync --extra production`, change `CAPSTONE_GRAPH=langgraph` and run the graph integration test.

### 9. `capstone/llm.py`

- Optional `LLMWriter` uses the OpenAI Responses API, bounded output tokens, short timeouts, and `store=False`.
- It receives only previously authorized passages, not unrestricted database or network tools.
- The returned source IDs must appear in the authorized evidence set; this still isn't a semantic factuality check.
- Mode is off by default. Configure `MODEL_BACKEND=openai`, an API key, and an approved `OPENAI_MODEL` only after privacy and cost review.
- `provider_model` on a registered model version can override the default for a routed canary job.
- **Exercise:** mock `responses.create` to return a fabricated citation and verify the writer fails closed.

### 10. `capstone/jobs.py`

- `submit_job()` checks idempotency against tenant, owner, question, and document group. A duplicate reuses the original job; a changed request conflicts.
- `claim_job()` uses PostgreSQL row locking with `SKIP LOCKED` where supported and an atomic status update. It increments the attempt generation and records a lease deadline.
- `recover_expired()` requeues abandoned leases up to the retry limit.
- `finish_job()` runs graph nodes and saves evidence/report only if the current attempt still owns the job. It redacts simple PII before writing the result.
- `review_job()` requires a distinct authorized reviewer, matching tenant, and permission for all source groups; it rejects repeated reviews.
- **Exercise:** force lease expiration, reclaim with another worker, and verify the old attempt cannot commit.

### 11. `capstone/worker.py`

- Runs in a different process from FastAPI. Each iteration claims a durable database job and invokes the graph.
- Redis wake-ups reduce latency, but even when a notification is lost the loop polls PostgreSQL.
- Optional live inference chooses the provider model for the model version recorded on that job.
- Worker errors are summarized without logging user questions or raw model exceptions.
- **Exercise:** stop Redis temporarily and confirm queued work remains in the database. Then restore Redis and the worker.

### 12. `capstone/queueing.py`

- `MemoryRateLimiter` is a thread-safe local fixed-window counter for tests. It does not coordinate replicas.
- `RedisNotifier` uses a Redis List only as a wake-up channel, not the source of durable job state.
- `allow()` executes an atomic Redis Lua counter with expiry; it enforces a shared rate limit in Compose.
- **Exercise:** use the integration test to verify a third request is denied when the limit is 2.

## Governance and operations

### 13. `capstone/scorecard.py`

- Computes aggregate binary accuracy, unsupported-claim rate, privacy leak counts and group positive-selection rates.
- Returns `HOLD` when minimum sample size, per-group sample size, accuracy, selection-rate gap, evidence support, or zero-leak gate fails.
- These are **course thresholds**, not a normative fairness or safety standard.
- **Exercise:** change just one `leak` flag and confirm the release scorecard returns HOLD even if accuracy remains perfect.

### 14. `capstone/releases.py`

- `register()` stores an immutable version key and expected artifact digest (the real bytes still need independent verification).
- `evaluate()` stores evaluation evidence bound to the version. This example accepts trusted-operator assertions; replace with attested evaluator evidence for production.
- `activate_baseline()` requires an independently reviewed eligible initial model; `begin_canary()` requires an eligible evaluation and review by someone other than the version registrant.
- `stable_model()` assigns traffic via keyed HMAC of verified identity so a user stays in one cohort.
- `canary_observation()` tracks samples, errors and critical events; it automatically rolls back on failure.
- `promote_canary()` requires healthy evidence and a distinct reviewer; `rollback()` records an incident.
- **Exercise:** attempt to reuse another model version's evaluation ID: the API must reject it.

### 15. `capstone/telemetry.py`

- Defines aggregate Prometheus HTTP request counters, latency histograms and job completion counters.
- Never creates labels containing user IDs, emails, job IDs or raw prompt text.
- `metric_bytes()` exposes a Prometheus-formatted payload through an admin-only endpoint.
- **Exercise:** inspect `/metrics` after a request and verify a private search string is absent.

### 16. `capstone/api.py`

- `create_app()` composes middleware, dependencies, validated Pydantic request schemas, and all HTTP endpoints.
- Authentication and rate limiting run through a shared dependency. The server derives tenant identity from authentication.
- `/v1/documents` ingests owned text and chunks it; `/v1/rag/query` retrieves authorized excerpts only.
- `/v1/research/jobs` submits durable work; `/v1/research/jobs/{id}` enforces owner scope; `/review` enforces separate reviewer authorization.
- `/v1/models/*` and `/v1/releases/*` provide version registration, evaluation, canary, observations, rollback, promotion and sticky assignment.
- `/v1/scorecards` returns aggregate metrics to an authorized reviewer. `/v1/incidents/*` implements contained → resolved state transitions.
- Error handlers return generic validation errors rather than echoing private user inputs.
- **Exercise:** replay an approval and confirm the endpoint returns HTTP 409.

### 17. `capstone/asgi.py`

- Production ASGI entry point. It loads validated settings and creates the FastAPI service.
- Optional `OTEL_ENABLED=1` activates FastAPI instrumentation; you still need an SDK exporter and a collector to observe traces.
- **Exercise:** remove `CAPSTONE_TOKENS_JSON` and verify the application refuses to start.

### 18. `capstone/demo.py`

- Runs a complete deterministic TestClient workflow in memory.
- Tests authorized retrieval, cross-tenant isolation, background completion, independent approval, candidate evaluation, canary start, and rollback.
- Produces `artifacts/scorecard.json` and `artifacts/dashboard.html` with an explicit *synthetic demonstration* label.
- **Exercise:** change the canary errors from 12 to 2 and confirm the synthetic observation does not roll back.

### 18a. `capstone/seed.py`

- Fills a running API with synthetic demo data through HTTP only, using the identities in `CAPSTONE_TOKENS_JSON`: documents, research jobs and a review, incidents, and a signed baseline plus 10% canary.
- Re-runnable: it checks what exists first. Release seeding needs the CI signing key and the benchmark model, and is skipped with a message otherwise.
- **Exercise:** add a third research job that the reviewer rejects, then confirm the Research tab shows all three states.

### 18b. `capstone/injection_eval.py` and `benchmark/injection.json`

- 30 prompt-injection cases run through upload, RAG, the worker and review in a fresh database each; reports successes per category and per check (obeyed, leaked, forged, bypassed).
- Trials whose planted document was not retrieved are excluded rather than counted as defended.
- **Exercise:** write a case whose marker appears in the planted text and see the loader reject it; explain why that case would be meaningless for the extractive writer.

## Tests (read these alongside the implementation)

### 19. `tests/conftest.py`

- Constructs a fresh SQLite-backed app and independent demo identities for each test.
- Exposes TestClient and auth-header fixtures. It never needs a real cloud API key.

### 20. `tests/test_api.py`

- Exercises FastAPI request contracts, redacted validation errors, ownership isolation, approval transitions, model-to-evaluation binding, canary rollback and promotion, and metrics access.

### 21. `tests/test_core.py`

- Tests chunking, vector determinism, BM25, source IDs, rate limits, fairness scorecards, worker retries, stale claim fencing, and defensive handling of malicious document content.

### 22. `tests/test_integration.py`

- When PostgreSQL and Redis URLs are provided, tests concurrent claims and distributed rate limits.
- The LangGraph test is skipped if the package is unavailable; it runs in the Compose-enabled CI workflow.

## Tooling and deployment

### 23. `Dockerfile`

- Installs production dependencies, creates a non-root user and launches Uvicorn. For a hardened release, pin base image digest, scan dependencies, set a read-only filesystem and use a staged build.

### 24. `compose.yaml`

- Runs database, Redis, schema initializer, API and worker with health checks. The API binds only to localhost by default.
- This is a dev Compose architecture; don't expose Redis or Postgres to the Internet.

### 25. `.dockerignore`

- Prevents test caches, local database files, Git internals and `.env` from entering the Docker build context.

### 26. `.github/workflows/ci.yml`

- Creates Python environment and PostgreSQL/Redis service containers, runs tests and syntax checks, then builds the Docker image.
- Improve with signed artifacts, dependency/secret scanning, SBOM generation and deployment approval gates.

### 27. `scripts/smoke.sh`

- Submits a bounded local document and question to an already running instance. Requires `ALICE_TOKEN`, avoiding hardcoded secrets inside the shell script.

### 28. Documentation files

- `README.md`: start and use the repository.
- `docs/ARCHITECTURE.md`: trust boundaries and data-flow diagram.
- `docs/THREAT_MODEL.md`: security threats, assumptions and limitations.
- `docs/DEPLOYMENT.md`: operations, release process, rollback and recovery.
- `docs/EVALUATION.md`: model and agent evaluation framework.
- `docs/EXERCISES.md`: challenge solutions and interview questions.
- `docs/PORTFOLIO.md`: project narrative and portfolio demonstration.
- `docs/FEATURES.md`: full detail on the advanced features summarised in the README.
- `docs/API_WALKTHROUGH.md`: end-to-end `curl` examples for research and releases.
- `docs/WEB_UI.md`: web UI features, sign-in and token handling.
- `LICENSE`: MIT license.

### 29. Generated artifacts

- `artifacts/scorecard.json`: machine-readable synthetic test output.
- `artifacts/dashboard.html`: static, local synthetic dashboard with no third-party assets.
- `TEST_REPORT.md`: verification status and known limitations.

## Suggested learning sequence

First implement authentication and document ownership. Then build tokenization/chunking, BM25 and embeddings, and retrieval tests. Next add the job lifecycle and a bounded graph. Add the reviewer gate before introducing any actual external action. Finally add the registry, canary routing, error gates, incident records and observability. Only enable optional live inference after the complete mock system passes security and reliability tests.
