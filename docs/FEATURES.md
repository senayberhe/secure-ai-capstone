# Feature details

The README summarises these; this page has the full behaviour, settings and file references.

## 1 · Semantic RAG (`embeddings.py`, `retrieval.py`, `rag_benchmark.py`)

Retrieval supports `bm25`, `dense` and `hybrid` (RRF) modes (`CAPSTONE_RETRIEVAL_MODE`, default hybrid). Set `CAPSTONE_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2` (install the `semantic` extra) to use an approved Sentence Transformers model; only models on `APPROVED_MODELS` load, from the local cache unless `CAPSTONE_EMBEDDING_OFFLINE=0`. Document vectors are cached per chunk and model in `chunk_embeddings` at upload, and access scoping still happens before any scoring. The default stays the offline hash baseline. Results on 61 labeled questions are in `docs/EVALUATION.md`: MiniLM hybrid reaches Recall@3 0.943 and MRR 0.929, with zero cross-tenant leaks.

## 2 · Durable LangGraph checkpoints (`checkpoints.py`, `agent.build_durable_graph`)

With `CAPSTONE_GRAPH=langgraph` (the Compose default), research runs as `plan → search → draft → verify → review(interrupt) → finalize`, checkpointed by LangGraph's `PostgresSaver` in the same database (SQLite file locally). Threads are keyed `tenant:job_id`. The worker stops each thread at the review interrupt. After the database approval check (independent, same-tenant, in-scope reviewer; atomic and replay-safe), the API resumes the thread from its checkpoint, in any process, after any restart. The review node re-checks reviewer ≠ author and the tenant. Nodes after the interrupt are side-effect free, retried jobs reuse their checkpoint, and the worker's reconciler finishes resumes lost in a crash. Checkpoint-table setup is serialised across processes with a non-blocking advisory lock.

## 3 · Release evidence integrity (`attestation.py`, `release_evidence.py`)

A canary or first baseline now requires two independent proofs. Operator-typed `/v1/models/evaluate` results are informational only.

1. **CI-signed evidence.** `python -m capstone.release_evidence build --version V --artifact FILE` hashes the artifact bytes, runs the labeled benchmark, applies the gates (Recall@3 ≥ 0.85, MRR ≥ 0.80, abstention ≥ 0.40, p95 ≤ 250 ms, zero leaks) and signs a version-bound report with an Ed25519 key that only CI holds. The `release-evidence` workflow job does this on manual dispatch. `POST /v1/models/evidence` accepts it only if the signature verifies against `CAPSTONE_ATTESTATION_KEYS`, the version is registered, the attested digest equals the registered digest, it is fresh (`CAPSTONE_EVIDENCE_MAX_AGE_HOURS`, default 168) and the attester is not the registrar.
2. **Independent artifact re-hash.** Register with `artifact_path` (relative to `CAPSTONE_ARTIFACT_ROOT`, mounted read-only at `/models` in Compose). When the reviewer, who cannot be the registrar, starts the canary, the server re-hashes those bytes and records the result. A mismatch blocks the canary.

Set up keys once with `uv run python -m capstone.release_evidence keygen --out .secrets`. Put the printed `api_setting` in `.env`, and store the private key as the CI secret `CAPSTONE_ATTESTATION_PRIVATE_KEY`. Never give the private key to the API.

Evidence rows and registered digests are append-only: database triggers on SQLite and PostgreSQL refuse UPDATE and DELETE, and prompt versions are bound to the evidence at submission and canary time (`tests/test_release_integrity.py`).

## 4 · Scorecard and privacy upgrades

`scorecard()` reports precision, recall, F1 and Wilson intervals overall and per group, and holds the release when a critical metric cannot be measured or a subgroup fails behind a passing average. `capstone/privacy.py` redacts allowlisted PII locally before documents, job questions and incident summaries are stored, and again before any outbound LLM call. Set `CAPSTONE_PII_DETECTOR=presidio` (with the `privacy` extra) to add on-machine Presidio NER for names and IBANs.

## 5 · Prompt-injection suite (`injection_eval.py`, `benchmark/injection.json`)

30 attacks in 7 categories (instruction override, role/delimiter spoofing, cross-scope exfiltration, citation forgery, markdown/link exfiltration, approval bypass, PII exfiltration), run through the real upload, RAG, worker and review paths. `uv run python -m capstone.injection_eval` writes `artifacts/injection_eval.{json,md}` and exits non-zero on any successful attack; `--writer openai --model M` measures the optional LLM writer instead. `tests/test_injection.py` also switches off each defense in turn and asserts the matching attacks then succeed. With the default offline writer: 30 valid trials, **0 successful attacks**. That says little about an LLM, which has not been measured yet. Results and limits are in `docs/EVALUATION.md`.
