#!/usr/bin/env bash
set -euo pipefail
: "${ALICE_TOKEN:?Set ALICE_TOKEN to configured editor bearer token}"
BASE=${BASE:-http://127.0.0.1:${CAPSTONE_PORT:-8000}}
DOC_ID=$(curl -fsS -X POST "$BASE/v1/documents" \
  -H "Authorization: Bearer $ALICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Travel policy","group":"engineering","text":"Employees submit travel receipts within thirty days of purchase. The reimbursement limit is documented by Finance."}' | python -c 'import json,sys;print(json.load(sys.stdin)["document_id"])')
echo "Created document: $DOC_ID"
curl -fsS -X POST "$BASE/v1/rag/query" -H "Authorization: Bearer $ALICE_TOKEN" \
  -H 'Content-Type: application/json' -d '{"question":"When should travel receipts be submitted?"}'
echo
curl -fsS -X POST "$BASE/v1/research/jobs" -H "Authorization: Bearer $ALICE_TOKEN" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-research-001" \
  -d '{"question":"travel receipts","group":"engineering"}'
echo
