# Final capstone challenges — with worked solutions

These exercises build on the actual source files. Each challenge has a practical acceptance criterion.

## 1. Calculate cosine similarity

**Question:** q=[0.6,0.8] and d=[1,0].

**Answer:** dot=0.6, ||q||=||d||=1, so cosine similarity=0.6. In the project, hash-vector outputs are normalized before dot-product scoring. This mathematical example does not prove semantic relevance.

## 2. Calculate an RRF score

**Question:** A document ranks first in BM25 and third in dense retrieval; k=60.

**Answer:** 1/(60+1) + 1/(60+3) = 0.0163934 + 0.0158730 = 0.0322664.

## 3. Compute chunk overlap

**Question:** Chunk width 65 words, overlap 12 words. What is stride?

**Answer:** 65−12=53 words. Read `split_text()` and explain why a final redundant chunk should not be created after the remaining words have been covered.

## 4. Fix accidental cross-tenant retrieval

**Question:** A developer filters unauthorized results *after* a global top-k vector search. What's wrong?

**Answer:** Unauthorized chunks could reach an embedding/rerank service or knock permitted chunks out of top-k. Apply authoritative tenant and group restrictions before candidate scoring, or use a separately vetted server-side filtered/partitioned index; verify results at the trust boundary.

## 5. Analyze idempotency

**Question:** Alice repeats an identical question with the same key after a timeout. What should happen?

**Answer:** Return the existing job and its immutable routed model version. Reusing the same key with a different question or requested group must return 409. The uniqueness constraint is (tenant, owner, idempotency key).

## 6. Explain stale-worker fencing

**Question:** Worker A starts attempt 1, lease expires, worker B starts attempt 2, A completes first. Can A update the report?

**Answer:** No. `finish_job` conditionally updates only if both `status='running'` and `attempts=1` match. The actual row is at attempt 2; A's update changes zero rows. An external LLM call might still occur twice, so external actions require idempotency or receipts.

## 7. Explain approval separation

**Question:** A generated report says `"approved":true`. Does that permit publishing?

**Answer:** No. Only an authenticated reviewer with appropriate tenant and group access, distinct from the author, can change the approval record. Model text isn't an authority source.

## 8. Evaluate a binary model

**Question:** 90 correct decisions out of 100, 2 unsupported claims, 0 leaks. Accuracy? Unsupported rate?

**Answer:** Accuracy 90%, unsupported rate 2%. These pass the *illustrative* numeric gates in `releases.py`, assuming p95 and sample requirements also pass. They do not establish real-world fairness or appropriate deployment.

**Update:** `/v1/models/evaluate` still returns `eligible` for these numbers, but an operator-typed evaluation is now informational only. A canary or first baseline requires CI-signed release evidence plus an independent re-hash of the artifact bytes (challenge 18).

## 9. Test fairness gap

**Question:** Group A has 65 positive predictions out of 100. Group B has 40/100.

**Answer:** 0.65−0.40=0.25, or 25 percentage points. The classroom `scorecard()` holds when gap >0.20, pending domain review. Don't infer causation from the gap.

## 10. Calculate canary rollback

**Question:** A candidate receives 12 failures in 100 requests. The policy permits 5% errors.

**Answer:** 12/100=0.12=12%; roll back. The project records the rollback in `ModelVersion` and opens a `release_rollback` incident. These are simulated trusted-operator observations, not proof of live telemetry accuracy.

**Update:** the incident is filed under the operator's own tenant (the release tenant), so those operators can contain and resolve it. It was originally filed under a `platform` tenant no identity belongs to.

## 11. Calculate SLO error budget

**Question:** 99.9% SLO across 200,000 requests.

**Answer:** Allowed failures=200,000×0.001=200. An error budget is not a license to accept critical privacy violations; those should have independent hard gates.

## 12. Big-O retrieval

**Question:** The authorized corpus has N chunks, each with d-dimensional hash vectors. Complexity of naive exact dense scan?

**Answer:** O(Nd) for similarities (plus query/document vectorization if done per request). For production, precompute vectors and consider an appropriate tenant-scoped ANN index.

## 13. Recover from Redis loss

**Question:** Redis loses all pending wake-up notifications, but PostgreSQL still contains queued rows.

**Answer:** The worker polls PostgreSQL and claims eligible rows. A temporary Redis outage may delay wake-ups, and this API currently fails closed for distributed rate limits. Notifications are not authoritative job storage.

**Caveat:** `/health/ready` also pings Redis and returns 503 while it is down. A load balancer or Kubernetes readiness probe would take every API instance out of rotation, which is more than "delayed wake-ups". If the API should stay up, make readiness depend only on PostgreSQL.

## 14. Upgrade the scorecard

**Challenge:** Add precision, recall, F1, and uncertainty intervals to `scorecard.py` using a labeled sample. Require sufficient per-group labeled cases before computing true-positive rate comparisons.

**Acceptance:** Unit tests cover zero denominators, small groups, a case where accuracy passes but recall fails, and a case where the overall score hides a subgroup failure. Hold releases if critical metrics cannot be measured.

**Status: implemented.** `scorecard()` now reports precision, recall and F1 overall and per group, with 95% Wilson intervals. Undefined rates are `None` and add `precision_unmeasurable` / `recall_unmeasurable` gates. Recall is compared only between groups with at least 20 labeled positives (others get `recall_unmeasurable:<group>`), and a weak group fails `group_recall:<group>` even when the overall numbers pass. Tests: `tests/test_scorecard_metrics.py`.

## 15. Upgrade privacy protection

**Challenge:** Add an allowlisted Presidio redaction component or an approved local NER detector. Avoid sending the original text to a remote detection API without consent and review.

**Acceptance:** Test phone/email variants, false positives, multi-tenant logs, and ensure redaction operates before persistence and outbound model calls. Do not claim perfect detection.

**Status: implemented.** `capstone/privacy.py` redacts allowlisted entities locally. The default `regex` detector covers emails, US and international phones, SSNs and Luhn-checked card numbers. `CAPSTONE_PII_DETECTOR=presidio` (the `privacy` extra) adds Presidio with a local spaCy model for names and IBANs; there is no remote detector. Presidio flagged `ORD-2024-000123` as a phone number and scored real international numbers at 0.4, so structured types stay deterministic and digit runs glued to longer tokens are treated as identifiers. Redaction runs before document and job persistence, on incident summaries, and again before outbound LLM calls. Tests: `tests/test_privacy.py` (variants, false positives, persisted rows, outbound prompt, two-tenant logs, fail-closed configuration).

## 16. Replace hash vectors with semantic embeddings

**Challenge:** Use a small approved `sentence-transformers` model and cache document vectors, or integrate tenant-partitioned FAISS/Pinecone/pgvector.

**Acceptance:** A benchmark contains exact identifiers, paraphrases, no-evidence questions and cross-tenant attacks; compare BM25, dense and hybrid Recall@3 and latency. Enforce access scopes before exposing retrieved text.

**Status: implemented.** See `docs/EVALUATION.md`: 61 questions; MiniLM hybrid Recall@3 0.943, MRR 0.929, zero leaks.

## 17. Add real LangGraph persistence

**Challenge:** Add a supported durable LangGraph checkpointer and ensure the same thread is authenticated and can be resumed only by the permitted reviewer. Nodes with external side effects must be idempotent.

**Acceptance:** Restart an application process during an interrupted graph, resume from durable state, and verify the author cannot approve their own request or change thread ownership.

**Status: implemented.** `PostgresSaver` checkpoints keyed `tenant:job_id`; interrupt in one process and resume in another (also run live in Docker after restarting the API and worker). Tests: `tests/test_durable_graph.py`.

## 18. Build a deployment gate

**Challenge:** Wire the evaluated artifact digest, dataset version, prompt version, SLO checks and signed approval into a single immutable `ReleaseEvidence` record. Only allow `begin_canary` if all evidence matches a registered version.

**Acceptance:** Regression tests reject altered artifacts, mismatched evaluation IDs, stale approvals, unauthorized promoters and catastrophic failure overrides.

**Status: implemented.** `ReleaseEvidence` holds a CI-signed (Ed25519) report bound to the version, artifact digest, dataset digest, prompt version (`llm.prompt_version()`, re-checked at canary start) and gate results including p95 latency. Evidence and artifact-verification rows are append-only, and a registered digest cannot be changed, enforced by database triggers on SQLite and PostgreSQL. Tests: `tests/test_release_evidence.py`, `tests/test_release_integrity.py`.

## Interview explanation

A good technical explanation is: "The API derives tenant identity from server-verified credentials. Chunks are filtered by tenant and groups *before* retrieval scoring. A Postgres-backed queue stores durable jobs while Redis only optimizes wake-ups and distributed limits. LangGraph defines a bounded research workflow, whose output requires independently authorized approval. We record model versions per job, gate canary traffic behind version-bound evaluations, and roll back based on operational failures. Offline tests verify security invariants; live-model and infrastructure checks are separate." 
