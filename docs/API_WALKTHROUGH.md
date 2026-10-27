# API walkthrough

Drive the whole workflow with `curl` against a running API (local or Docker Compose). Start the stack first as described in the README.

## Try the complete workflow

Use an editor token from your `.env` (or a newly generated equivalent):

```bash
export BASE=http://127.0.0.1:8000
export ALICE_TOKEN='YOUR_CONFIGURED_EDITOR_TOKEN'
export REVIEWER_TOKEN='YOUR_CONFIGURED_INDEPENDENT_REVIEWER_TOKEN'

curl -sS -X POST "$BASE/v1/documents" \
  -H "Authorization: Bearer $ALICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Travel policy","group":"engineering","text":"Employees must submit receipts within thirty days of travel. Approval is required for expensive lodging."}'

curl -sS -X POST "$BASE/v1/rag/query" \
  -H "Authorization: Bearer $ALICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"When should travel receipts be submitted?"}'

curl -sS -X POST "$BASE/v1/research/jobs" \
  -H "Authorization: Bearer $ALICE_TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: unique-research-001' \
  -d '{"question":"travel receipts","group":"engineering"}'
```

Copy `job_id` from the response; poll until `pending_review`:

```bash
curl -sS "$BASE/v1/research/jobs/JOB_ID" -H "Authorization: Bearer $ALICE_TOKEN"
```

An in-scope reviewer (same tenant, covering the job's document groups) can `GET` the job once it is `pending_review`, to read the report before deciding. Then send the independent review (the author may not approve their own job):

```bash
curl -sS -X POST "$BASE/v1/research/jobs/JOB_ID/review" \
 -H "Authorization: Bearer $REVIEWER_TOKEN" -H 'Content-Type: application/json' \
 -d '{"approved":true}'
```

The approved state means **internal workflow approval only**. This reference application does not send email, publish to the Internet, or grant an agent arbitrary tools.

## Model registry and release lifecycle

Model releases are platform-wide, so release endpoints (`/v1/models/*`, `/v1/releases/*` except `assignment`) only accept identities from the tenant named in **`CAPSTONE_RELEASE_TENANT`**. If it is unset, they fail closed with 403. An admin of any other tenant cannot register, canary, promote or roll back models. Rollback incidents are filed under that tenant, so its admins can contain and resolve them.

Use an `admin` bearer token from the release tenant to register an immutable version reference and submit a synthetic test summary. **The example `sha256` string is metadata, not proof of verified artifact integrity**; in a real release, hash the actual artifact and link it to the registration via a trusted pipeline.

```bash
export ADMIN_TOKEN='YOUR_CONFIGURED_ADMIN_TOKEN'
curl -sS -X POST "$BASE/v1/models/register" \
 -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
 -d '{"version":"candidate-v2","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","provider_model":"gpt-4.1-mini"}'

curl -sS -X POST "$BASE/v1/models/evaluate" \
 -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
 -d '{"version":"candidate-v2","samples":150,"accuracy":0.91,"unsupported_rate":0.01,"leakage_count":0,"p95_ms":800}'
```

Use the returned `evaluation_id` in `POST /v1/releases/canary` with the **independent reviewer** identity. For the first baseline, call `POST /v1/releases/bootstrap` with an eligible evaluation ID and an independent reviewer. Only one baseline may be activated.

After rollout, `POST /v1/releases/observe` accepts **operator-supplied simulated telemetry**; a production service should derive signed/controlled observations from a trusted collector. The canary is rolled back when observed error rate exceeds 5% or any critical event occurs, and cannot be promoted until it has sufficient healthy observations. Rollback affects future routing; it cannot revoke already-completed responses or external side effects.

See `docs/DEPLOYMENT.md` for full setup and rollout details.
