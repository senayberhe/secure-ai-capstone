# Final capstone verification report

**Executed:** October 25, 2026 · Python 3.13 · `uv sync --extra test --extra production`.

## Executed checks

| Verification | Result |
|---|---|
| Automated pytest suite with live PostgreSQL 16 (`TEST_POSTGRES_URL`) | **143 passed, 1 skipped** (Redis) |
| Automated pytest suite without services | 97 passed, 4 skipped |
| Web UI (vitest) | 9 passed |
| Docker Compose: research thread interrupted by the worker container, api+worker restarted, resumed by independent approval | Passed |
| Docker Compose: CI-signed evidence → bootstrap → canary; artifact swapped after attestation → canary refused | Passed |
| Integration tests against a live PostgreSQL 16 server | **Passed** (concurrent `SKIP LOCKED` claim, LangGraph runtime) |
| Live API (uvicorn) + LangGraph worker on PostgreSQL, full API walkthrough (`docs/API_WALKTHROUGH.md`) over HTTP | Passed |
| Local SQLite setup (`init_db` from `.env`) | Passed |
| Synthetic end-to-end demo and artifacts | Passed |
| Python source compilation | Passed |

## Fixed during this review

| Issue | Effect before the fix |
|---|---|
| Documents were flushed after their chunks | `POST /v1/documents` returned 500 on PostgreSQL, so uploads failed under Docker Compose; SQLite hid it by not enforcing foreign keys |
| Release endpoints accepted any tenant's admin/reviewer | Another tenant could register, canary, promote or roll back the platform's models |
| Rollback incidents were filed under tenant `platform` | No real identity could ever contain or resolve them |
| `provider_model` was dropped at registration | Per-version model routing silently fell back to `OPENAI_MODEL` |
| Reviewers could not read a job's report | Approvals were made without seeing the report |
| Bracketed text such as `[x:c0]` in a source document | Citation verification failed and every research job over that document failed |
| Non-ASCII bearer token | 500 instead of 401 |

Each fix has a regression test in `tests/test_api.py` or `tests/test_core.py`.

## Coverage highlights

- Authentication rejects missing/invalid bearer credentials, including non-ASCII tokens.
- Rejects unauthorized group uploads and cross-tenant document retrieval.
- Retrieval filters documents before vector scoring; validates source citation IDs.
- Job submissions are idempotent for the same user, group, question and key.
- Separate workers use attempt numbers and a conditional status update to fence stale results.
- Lost lease recovery, retry limits and denied approval replays.
- Independent, in-scope reviewers read and decide; authors cannot self-approve.
- Release operations limited to `CAPSTONE_RELEASE_TENANT`, failing closed when unset.
- Candidate evaluation evidence cannot be reused for a different model version.
- Canary operational failure triggers rollback and a resolvable incident.
- Validation errors and Prometheus labels do not echo the tested private input string.

## Remaining environment-dependent skips

1. `test_integration.py::test_postgres_concurrent_claim` runs only with `TEST_POSTGRES_URL` (it passed against a local PostgreSQL 16 here).
2. `test_integration.py::test_redis_atomic_rate_limit` runs only with `TEST_REDIS_URL` (Redis is not installed on this machine).

Docker Compose was run locally (see the table above). The GitHub Actions CI workflow has not been executed yet. Do not claim it passed until it does.

## Advanced challenges

- **Semantic RAG:** benchmark of 61 labeled questions; MiniLM hybrid Recall@3 0.943, MRR 0.929, abstention 0.500, 0 leaks (see `docs/EVALUATION.md`). Tests: `tests/test_semantic_rag.py`.
- **Durable checkpoints:** cross-process interrupt/resume on SQLite and PostgreSQL, self-approval and cross-tenant resume refused inside the graph, retried jobs reuse checkpoints, a lost resume is reconciled, and concurrent PostgreSQL setup no longer deadlocks. Tests: `tests/test_durable_graph.py`.
- **Release evidence:** 14 attack tests in `tests/test_release_evidence.py`: typed evaluations, forged signer, tampered verdict, digest mismatch, unregistered version, swapped bytes, hold decision, wrong version, stale evidence, registrar-as-attester, idempotent resubmission, fail-closed without keys, and the CI CLI.

Found and fixed while doing this: concurrent `PostgresSaver.setup()` from the API and worker crashed the worker on first Compose start, and a blocking advisory lock then deadlocked against `CREATE INDEX CONCURRENTLY`. Setup now uses a polled try-lock.

## Scorecard, privacy and release-integrity upgrades (exercises 14, 15, 18)

- `tests/test_scorecard_metrics.py`: zero denominators, small groups, accuracy passing while recall fails, an overall pass hiding a failing subgroup.
- `tests/test_privacy.py`: 12 PII variants, 11 known false positives, persisted rows, outbound LLM prompt, two-tenant logs, local Presidio (names redacted, order IDs kept), fail-closed configuration.
- `tests/test_release_integrity.py`: prompt-version binding at submission and canary time; UPDATE/DELETE of evidence and digest rewrites refused by triggers on SQLite and PostgreSQL.

Found and fixed: PL/pgSQL `%` placeholders were parsed by psycopg as query parameters, which would have broken schema initialisation on PostgreSQL. Bracket neutralisation was narrowed to citation-shaped text so redaction tokens like `[EMAIL]` survive. In Docker Compose the triggers were installed on the existing database, and a direct `psql` UPDATE of an evidence row was refused.
