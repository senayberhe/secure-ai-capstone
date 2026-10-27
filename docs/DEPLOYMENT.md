# Deployment and operations guide

This guide takes the capstone from offline tests to a development integration environment. Production certification and public deployment require additional security and infrastructure work (see §10).

## 1. Prerequisites

- Python 3.11+; Python 3.13 and [uv](https://docs.astral.sh/uv/) recommended.
- Docker Engine and Docker Compose for the PostgreSQL/Redis path.
- Node.js 22+ only to develop the React UI outside Docker (`web/`).
- For CI-attested releases: an Ed25519 attestation key pair (§5). The private key belongs only to CI.
- For semantic retrieval: the `semantic` extra and the approved model weights (downloaded once, then loaded from the local cache).
- For optional live inference: an approved hosted-model account, an API key stored only in a secret manager or restricted local environment, privacy approval and cost limits.
- No credentials are needed to run the offline unit suite and synthetic demonstration.

## 2. Offline development

```bash
uv sync --extra test --extra production   # production adds LangGraph + checkpointers, psycopg, redis
uv run pytest -q
uv run python -m capstone.demo
```

For a persistent SQLite learning instance:

```bash
cp .env.example .env
# Replace EVERY sample secret, and set CAPSTONE_RELEASE_TENANT (see below).
set -a; source .env; set +a
export DATABASE_URL=sqlite:///./capstone.db
export MODEL_BACKEND=offline
uv run python -m capstone.init_db
uv run uvicorn capstone.asgi:app --reload
# In a separate terminal using the same environment:
CAPSTONE_GRAPH=offline uv run python -m capstone.worker
```

With `CAPSTONE_GRAPH=langgraph` instead, research threads are checkpointed in the same SQLite file and survive restarts (§6).

To develop the web UI with hot reload against that API:

```bash
cd web && npm install
VITE_API_TARGET=http://127.0.0.1:8000 npm run dev   # open http://127.0.0.1:5173
```

SQLite is fine for the offline tutorial and runs with foreign keys enforced so tests match PostgreSQL. Multi-worker concurrency and the real PostgreSQL locking semantics must still be verified with PostgreSQL (§6).

### Required settings

| Variable | Purpose |
|---|---|
| `CAPSTONE_TOKENS_JSON` | Development bearer tokens (24+ characters each) mapped to tenant, user, groups and role |
| `CAPSTONE_ROUTING_SECRET` | 32+ character HMAC key for sticky canary assignment |
| `CAPSTONE_RELEASE_TENANT` | The only tenant allowed to register, release, promote or roll back models. Unset = release operations fail closed (403) |
| `CAPSTONE_ATTESTATION_KEYS` | JSON `{key_id: public PEM}` of trusted CI attesters (§5). Empty = no canary can start |
| `CAPSTONE_ARTIFACT_ROOT` | Approved artifact store; canary start re-hashes registered files here |
| `CAPSTONE_PORT`, `CAPSTONE_WEB_PORT` | Host ports for the Compose API (default 8000) and web UI (default 8090) |
| `CAPSTONE_GRAPH` | `langgraph` (durable checkpoints) or `offline` |
| `CAPSTONE_EMBEDDING_MODEL` | Optional approved Sentence Transformers model; default is the offline hash baseline |
| `CAPSTONE_PII_DETECTOR` | `regex` (default) or `presidio` (local NER; `privacy` extra). An unknown or uninstallable detector fails at startup |

## 3. Docker Compose integration

```bash
cp .env.example .env
# Change passwords, routing secret and all bearer tokens; add attestation keys (§5).
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs --tail=50 api worker
curl -f http://127.0.0.1:${CAPSTONE_PORT:-8000}/health/live
curl -f http://127.0.0.1:${CAPSTONE_PORT:-8000}/health/ready
```

Compose runs PostgreSQL, Redis, a one-shot `schema` initializer, the `api`, a separate LangGraph `worker`, and `web` (the React UI behind nginx, at `http://127.0.0.1:${CAPSTONE_WEB_PORT:-8090}`, proxying `/v1` to the API so the browser uses one origin). PostgreSQL and Redis are internal; only the API and UI are bound to `127.0.0.1`. `./models` is mounted read-only into the API at `/models` as the artifact store.

**Port already in use?** If another app owns the host port, Docker cannot publish the API and leaves its container without a network: `/health/ready` fails inside it and `localhost:8000` shows the *other* app. Set `CAPSTONE_PORT` (and `CAPSTONE_WEB_PORT`) to free ports in `.env`, then `docker compose up -d --force-recreate api web`. Check what owns a port with `lsof -nP -iTCP:8000 -sTCP:LISTEN`.

The schema service uses `Base.metadata.create_all`, which creates new tables but never alters existing ones. Replace it with versioned Alembic migrations before having persistent users or production data. LangGraph's checkpoint tables are created by the API and worker on startup; concurrent startups are serialised with a PostgreSQL advisory lock. The schema step also installs triggers that make `release_evidence` and `artifact_verifications` append-only and freeze each registered artifact digest; it is safe to re-run on an existing database.

Do not run `docker compose down -v` on an installation containing data that must be preserved: it deletes the named volumes, including approvals, release evidence and checkpoints.

## 3a. Accounts and Google sign-in

People can create an account on the sign-in page with an email and password, or with **Continue with Google**. Every new account gets its own private workspace tenant (`ws-…`), is that workspace's admin and sees only its own documents. Sign-up never grants access to a configured tenant such as `acme`, and never to release operations. Research reports still need an independent reviewer, so a one-person workspace can create jobs but not approve them.

How it is protected:

- Passwords are hashed with scrypt (salted, parameters stored per hash). Login answers the same way for unknown emails and wrong passwords, and is rate limited per email and per client IP.
- Sign-in sets an `HttpOnly`, `SameSite=Lax` session cookie; only its SHA-256 is stored in `auth_sessions`, so logout revokes it server-side. Sessions last `CAPSTONE_SESSION_HOURS` (default 24). The cookie is `Secure` when `CAPSTONE_PUBLIC_URL` starts with `https://`.
- Cookie-authenticated writes must send `X-Requested-With: capstone`, which a cross-site page cannot add.
- Google uses the server-side authorization-code flow with PKCE, `state` and `nonce`. The API verifies the ID token's signature, audience, issuer, expiry and `email_verified`. No Google script runs in the page, so the Content-Security-Policy stays strict.
- If someone registered an email with a password and its real owner later signs in with Google, Google's verification wins: the unverified password and its sessions are removed.
- Development bearer tokens from `CAPSTONE_TOKENS_JSON` keep working for CI, scripts and the configured tenants.

To turn on Google sign-in:

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), configure the OAuth consent screen, then **Create credentials → OAuth client ID → Web application**.
2. Add the authorized redirect URI `<CAPSTONE_PUBLIC_URL>/v1/auth/google/callback`, for example `http://127.0.0.1:8090/v1/auth/google/callback` for Compose or `http://127.0.0.1:5173/v1/auth/google/callback` for `npm run dev`. It must match exactly.
3. Put the values in `.env` and restart the API:

```bash
CAPSTONE_PUBLIC_URL=http://127.0.0.1:8090
GOOGLE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...   # keep it out of git, like every value in .env
```

`GET /v1/auth/config` then reports `"google": true` and the button appears. Still missing for a public service: email verification and password reset for password accounts (both need an email provider), and inviting others into a workspace.

## 4. API smoke test

With a configured editor token:

```bash
export ALICE_TOKEN='YOUR_ACTUAL_CONFIGURED_EDITOR_TOKEN'
bash scripts/smoke.sh          # uses CAPSTONE_PORT, or set BASE=http://host:port
```

The script submits a synthetic travel policy and a research job. The worker should claim it, retrieve authorized chunks, return evidence with citations and set the job to `pending_review`. An in-scope reviewer can then read it (`GET /v1/research/jobs/{id}`, or the UI's Research tab) and approve it via `/review`; the author cannot.

## 5. Model registration, evidence and release

A canary, and the first baseline, now require **two independent proofs**. Operator-typed metrics cannot start either.

1. **CI-signed evidence.** CI hashes the artifact bytes, runs the labeled retrieval benchmark, applies the gates and signs a version-bound report with an Ed25519 key only CI holds.
2. **Independent artifact re-hash.** When an independent reviewer starts the canary, the server re-hashes the registered file under `CAPSTONE_ARTIFACT_ROOT` and records the result.

One-time key setup:

```bash
uv run python -m capstone.release_evidence keygen --out .secrets
# Put the printed api_setting into .env as CAPSTONE_ATTESTATION_KEYS='{"<key_id>":"<public PEM>"}'.
# Store .secrets/attestation_private.pem as the CI secret CAPSTONE_ATTESTATION_PRIVATE_KEY. Never give it to the API.
```

Release flow (all release calls need an identity from `CAPSTONE_RELEASE_TENANT`):

```bash
cp model.safetensors models/candidate-v2.safetensors
# 1. Admin registers the digest and location.
#    POST /v1/models/register {"version":"candidate-v2","sha256":"<sha256 of the file>","artifact_path":"candidate-v2.safetensors"}
# 2. CI (or locally, with the CI key) builds and signs evidence:
CAPSTONE_ATTESTATION_PRIVATE_KEY="$(cat .secrets/attestation_private.pem)" \
  uv run python -m capstone.release_evidence build --version candidate-v2 \
  --artifact models/candidate-v2.safetensors --out evidence.json
# 3. Submit it: POST /v1/models/evidence  (body = evidence.json)  -> evidence_id
# 4. An independent reviewer starts the canary with that id:
#    POST /v1/releases/canary {"version":"candidate-v2","evaluation_id":"<evidence_id>","percent":10}
```

The UI's Releases tab does the same: register, paste `evidence.json`, start the canary.

| Endpoint | Responsibility |
|---|---|
| `POST /v1/models/register` | Register a version, its expected artifact hash and its location in the artifact store |
| `POST /v1/models/evidence` | Verify and store CI-signed evidence: trusted key, registered version, digest = registered digest, fresh (`CAPSTONE_EVIDENCE_MAX_AGE_HOURS`, default 168), attester ≠ registrar |
| `GET /v1/models/{version}/evidence` | Evidence and artifact-verification history |
| `POST /v1/models/evaluate` | Informational, operator-provided teaching report; **cannot** start a canary or baseline |
| `POST /v1/releases/bootstrap` | Independent reviewer activates the first baseline (same evidence + re-hash gate) |
| `POST /v1/releases/canary` | Independent reviewer starts a 5/10/25/50% canary (evidence + re-hash gate) |
| `POST /v1/releases/observe` | Admin submits bounded, simulated canary event totals |
| `POST /v1/releases/promote` | Independent reviewer promotes only if the observation gate passed |
| `POST /v1/releases/rollback` | Admin removes the candidate from new routing |
| `GET /v1/releases/assignment` | Sticky assignment for the authenticated caller |
| `GET /v1/models` | Registry listing (release tenant only) |

CI evidence gates (teaching values): Recall@3 ≥ 0.85, MRR ≥ 0.80, abstention on unanswerable questions ≥ 0.40, retrieval p95 ≤ 250 ms, zero cross-tenant or cross-group leaks. A failing run produces a signed `hold` report that cannot start a canary. The operational canary gate holds at least 100 observations and rejects over 5% errors or any critical event. Choose production gates from documented use-case risks and validated evaluation data.

Evidence is also bound to the LLM writer's prompt version: changing the prompt invalidates earlier evidence, both at submission and at canary start.

What is still not verified: canary *telemetry* (`/observe`) is operator-supplied, not from a certified collector. The re-hash proves the bytes at canary start, not that serving infrastructure keeps using them. Keys are file/secret based, not an HSM or transparency log.

### Promotion versus rollback

Successful canary checks alone don't deploy the model fully; the reviewer must explicitly promote. Rollback changes new version selection; it doesn't undo external calls or guarantee no already-in-flight responses use the candidate. Keep idempotency and safe external side-effect controls. Rollback opens an incident under the release tenant, so the same operators can contain and resolve it.

## 6. Durable LangGraph research

```bash
uv sync --extra test --extra production
export CAPSTONE_GRAPH=langgraph
# Optional, against a real server: export TEST_POSTGRES_URL=postgresql+psycopg://user@host:5432/db
uv run pytest -q tests/test_durable_graph.py tests/test_integration.py
```

Research runs as `plan → search → draft → verify → review (interrupt) → finalize`. Each thread is checkpointed by LangGraph's `PostgresSaver` in `DATABASE_URL` (`SqliteSaver` for a SQLite file; override with `CAPSTONE_CHECKPOINT_URL`), keyed `tenant:job_id`. The worker stops each thread at the review interrupt. The database approval (independent, same-tenant, in-scope reviewer; atomic and replay-safe) is recorded first, and only then does the API resume the thread from its checkpoint, in any process and after any restart. Nodes after the interrupt are side-effect free; retried jobs reuse their checkpoint; the worker's reconciler finishes resumes lost in a crash.

### Semantic retrieval

```bash
uv sync --extra semantic
export CAPSTONE_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
uv run python -m capstone.rag_benchmark      # writes artifacts/rag_benchmark.{json,md}
```

Only models on `APPROVED_MODELS` load, from the local cache unless `CAPSTONE_EMBEDDING_OFFLINE=0`. Vectors are cached per chunk and model at upload. The Docker image does not include the `semantic` extra, so containers use the hash baseline unless you add it. Results and their caveats are in `docs/EVALUATION.md`.

## 7. Optional live LLM writer

Only after receiving appropriate permissions and checking data-handling terms:

```bash
export MODEL_BACKEND=openai
export OPENAI_MODEL='YOUR_APPROVED_MODEL_ID'
export OPENAI_API_KEY='READ_FROM_SECRET_MANAGER_NOT_CODE'
```

A version's registered `provider_model` overrides the default model for jobs routed to that version. The writer sends only authorized source passages (with brackets neutralised so a document cannot forge a citation) and the bounded question, uses no model-requested tools, requests short generated output, and rejects unrecognized citation IDs. This is not proof of semantic factuality or prompt-injection resistance.

Use synthetic/redacted documents for initial tests. Enforce token budgets, tenant-level spend limits, output controls, security evaluations and explicit data-retention review before connecting real private content.

## 8. Observability and incident runbook

- `/metrics` is admin-only and exposes aggregate counts and histograms; never label metrics with user emails or free-form questions.
- Set `OTEL_ENABLED=1` only after configuring a compliant collector/exporter. The project contains instrumentation hooks but no preconfigured external trace collector.
- For incidents: detect → disable the affected tool/model or roll back → preserve minimal authorized evidence → assess possible disclosure → restore carefully → add regression tests.
- Incident API state machine: open → contained → resolved, with role and tenant checks. The UI's Incidents tab drives it.

## 9. Database backups and disaster recovery

A live platform needs scheduled and regularly restore-tested PostgreSQL backups, encryption at rest and in transit, least-privilege DB users, audit retention, deletion workflows, an RPO (maximum data-loss tolerance) and an RTO (restoration-time objective). Compose named volumes alone aren't a backup strategy.

The backup scope now includes the LangGraph checkpoint tables (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`), `release_evidence` and `artifact_verifications`. Restoring approvals without their checkpoints leaves paused threads that the reconciler cannot resume. Back up the artifact store with the database, and plan attestation-key rotation: add the new public key to `CAPSTONE_ATTESTATION_KEYS` before CI starts signing with it, and remove the old one only after its evidence has expired.

## 10. CI/CD and staged rollout

`.github/workflows/ci.yml` has three jobs:

- `test`: source checks and tests with PostgreSQL and Redis service containers, then the API image build.
- `web`: frontend tests, production build and the web image build.
- `release-evidence`: manual `workflow_dispatch` with a version and an HTTPS artifact URL. It hashes the artifact, runs the benchmark with the approved model, signs `evidence.json` using the `CAPSTONE_ATTESTATION_PRIVATE_KEY` secret in the `release-evidence` environment (add required reviewers there for a human approval gate), and uploads it. It fails when the gates say `hold`.

GitHub only runs workflows from a repository's root `.github/workflows`. If the capstone lives inside a larger repository, move the file there and set `working-directory: capstone` on its jobs.

Extend it with dependency and secret scanning, container scanning, model-evaluation regression tests, synthetic prompt-injection test suites, staged SLO evaluation and a transparency log for signed evidence.

For public deployment, provision DNS, TLS, load balancing, a real identity provider instead of static bearer tokens, managed secrets, WAF/body-size and request limits, database migrations, monitoring exporters, egress restrictions, per-tenant quotas and an incident contact policy.

## 11. Failure drills

1. Kill the worker after it claims a job; force lease expiry and verify another worker recovers it.
2. Temporarily stop Redis; confirm queued PostgreSQL jobs persist. Shared rate limiting fails closed (503) while Redis is unavailable.
3. Induce a stale worker to submit a result after the claim generation changed; assert the update affects zero rows.
4. Insert a cross-tenant document and query from an unauthorized tenant; verify it never enters ranking candidates.
5. Mark canary results as 12 failures in 100 requests and verify rollback plus incident creation.
6. Trigger a report needing review, then attempt to approve it as the author, a different tenant and a valid reviewer. Only the valid reviewer succeeds, and replay is rejected.
7. With a job in `pending_review`, run `docker compose restart api worker`; approve it as a valid reviewer and verify the response shows `graph_status: approved` (the thread resumed from its checkpoint).
8. Register a candidate, submit valid CI evidence, then replace the file in `./models`; verify the canary is refused and `GET /v1/models/{version}/evidence` records `matched: false`.
9. Start several API/worker replicas against an empty database at once; verify they all come up (no checkpoint-setup race).

Drills 7 and 8, and the author and replay parts of drill 6, were run against the Compose stack during development. Drills 1, 3–6 and 9 are covered by automated tests (9 against a live PostgreSQL). Drill 2 has not been run: Redis was not available on the development machine. Stop and investigate if any invariant fails. Do not remediate by weakening authorization or skipping test gates.
