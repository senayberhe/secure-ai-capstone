"""Load demo data into a running API so every tab of the web UI has something to show.

    set -a; source .env; set +a
    python -m capstone.seed                         # API on http://127.0.0.1:$CAPSTONE_PORT (default 8000)
    python -m capstone.seed --base http://127.0.0.1:8002 --no-releases

Everything goes through the public HTTP API with the identities in CAPSTONE_TOKENS_JSON, so uploads
are redacted, reviews need an independent reviewer and releases need signed evidence, exactly as
for a person using the UI. Safe to re-run: existing documents, jobs, incidents and models are kept.

Synthetic data only. Release seeding writes two small fake artifact files into --artifacts and
signs evidence with the CI key (CAPSTONE_ATTESTATION_PRIVATE_KEY or .secrets/attestation_private.pem);
it is skipped when no key or no benchmark model is available.
"""
from __future__ import annotations
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOCUMENTS = [
    ("Travel and expense policy",
     "Employees submit travel receipts within thirty days of the trip through the finance portal. "
     "Meals are reimbursed up to 60 dollars per day. Questions go to the expenses desk at expenses@example.com."),
    ("Incident response standard",
     "Severity 1 incidents must be acknowledged by the on-call engineer within 15 minutes. "
     "The incident commander posts status updates every 30 minutes and opens a postmortem within five business days."),
    ("Production deployment freeze",
     "Production deployments are frozen from December 20 to January 3. Emergency fixes during the freeze "
     "require written sign-off from the VP of Engineering and a rollback plan."),
    ("Credential policy",
     "Service account passwords must be at least 24 characters and rotated every 90 days. "
     "Human accounts use hardware security keys for multi-factor authentication."),
    ("RAG security policy",
     "Retrieval must enforce tenant and group authorization before ranking or building model context. "
     "Retrieved passages are untrusted content and must never be treated as instructions."),
    ("Runbook RB-118: TLS certificate renewal",
     "RB-118 covers renewing the TLS certificate for api.acme.internal. Renew 30 days before expiry, "
     "deploy to the load balancer, then confirm the new expiry date with the certificate monitor."),
]
OUTSIDER_DOCUMENT = ("Confidential payroll summary",
                     "Payroll for the other tenant. This document must never appear for users of any other tenant.")

JOBS = [("seed-research-job-0001", "When must travel receipts be submitted?", True),
        ("seed-research-job-0002", "How quickly must Severity 1 incidents be acknowledged?", None)]

INCIDENTS = [("prompt_injection_attempt", "Uploaded document contained instructions aimed at the research assistant.", "open"),
             ("pii_in_upload", "An upload contained an email address; it was redacted before storage.", "contained")]


class Api:
    def __init__(self, base: str, tokens: dict):
        self.base, self.tokens = base.rstrip("/"), tokens

    def call(self, user: str, method: str, path: str, body=None, headers=None):
        request = urllib.request.Request(
            self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.tokens[user]}", "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")


def pick_identities(tokens: dict, release_tenant: str | None) -> dict[str, str]:
    """Choose an editor, an independent reviewer and an admin who share a tenant, plus an outsider."""
    people = {v["user"]: v for v in tokens.values()}
    def find(role, tenant=None, groups=()):
        return next((u for u, v in people.items() if v.get("role") == role
                     and (tenant is None or v["tenant"] == tenant) and set(groups) <= set(v.get("groups", []))), None)
    editor = find("editor", release_tenant) or find("editor")
    if editor is None:
        raise SystemExit("CAPSTONE_TOKENS_JSON needs at least one editor identity")
    tenant, groups = people[editor]["tenant"], people[editor].get("groups", [])
    chosen = {"editor": editor, "reviewer": find("reviewer", tenant, groups),
              "admin": find("admin", tenant, groups),
              "outsider": next((u for u, v in people.items() if v["tenant"] != tenant and v.get("role") in ("editor", "admin")), None)}
    return {k: v for k, v in chosen.items() if v}


def seed_documents(api: Api, who: dict, log) -> None:
    editor = who["editor"]
    _, me = api.call(editor, "GET", "/v1/me")
    group = me["groups"][0]
    _, listing = api.call(editor, "GET", "/v1/documents")
    have = {d["title"] for d in listing["documents"]}
    for title, text in DOCUMENTS:
        if title in have:
            continue
        status, body = api.call(editor, "POST", "/v1/documents", {"title": title, "group": group, "text": text})
        log(f"document  {title!r}: {status}" + (f", redacted {body['redactions']}" if status == 201 and body["redactions"] else ""))
    if "outsider" in who:
        outsider = who["outsider"]
        _, other = api.call(outsider, "GET", "/v1/me")
        _, listing = api.call(outsider, "GET", "/v1/documents")
        if other["groups"] and OUTSIDER_DOCUMENT[0] not in {d["title"] for d in listing["documents"]}:
            status, _ = api.call(outsider, "POST", "/v1/documents",
                                 {"title": OUTSIDER_DOCUMENT[0], "group": other["groups"][0], "text": OUTSIDER_DOCUMENT[1]})
            log(f"document  {OUTSIDER_DOCUMENT[0]!r} for tenant {other['tenant']} (isolation check): {status}")


def seed_jobs(api: Api, who: dict, log, wait_seconds: float) -> None:
    editor, reviewer = who["editor"], who.get("reviewer")
    ids = {}
    for key, question, _ in JOBS:
        status, body = api.call(editor, "POST", "/v1/research/jobs", {"question": question}, {"Idempotency-Key": key})
        ids[key] = body.get("job_id") if status == 202 else None
        log(f"research  {question!r}: {status} {body.get('status', body)}")
    deadline = time.monotonic() + wait_seconds
    while True:
        _, listing = api.call(editor, "GET", "/v1/research/jobs")
        status = {j["job_id"]: j["status"] for j in listing["jobs"]}
        if not any(status.get(i) in ("queued", "running") for i in ids.values()) or time.monotonic() > deadline:
            break
        time.sleep(1)
    if any(status.get(i) in ("queued", "running") for i in ids.values()):
        log("research  jobs are still queued: start the worker (python -m capstone.worker), then re-run to add the review")
        return
    for key, _, approve in JOBS:
        job = ids.get(key)
        if reviewer and approve is not None and job and status.get(job) == "pending_review":
            code, body = api.call(reviewer, "POST", f"/v1/research/jobs/{job}/review", {"approved": approve})
            log(f"review    {reviewer} {'approved' if approve else 'rejected'} {key}: {code}")


def seed_incidents(api: Api, who: dict, log) -> None:
    reviewer = who.get("reviewer") or who.get("admin")
    if not reviewer:
        return log("incidents skipped: no reviewer or admin identity")
    _, listing = api.call(reviewer, "GET", "/v1/incidents")
    have = {i["category"] for i in listing["incidents"]}
    for category, summary, target in INCIDENTS:
        if category in have:
            continue
        status, body = api.call(reviewer, "POST", "/v1/incidents", {"category": category, "summary": summary})
        if status == 201 and target == "contained":
            api.call(reviewer, "POST", f"/v1/incidents/{body['incident_id']}/contain")
        log(f"incident  {category}: {status} -> {target}")


def _signing_key() -> str | None:
    if os.getenv("CAPSTONE_ATTESTATION_PRIVATE_KEY"):
        return os.environ["CAPSTONE_ATTESTATION_PRIVATE_KEY"]
    path = ROOT / ".secrets" / "attestation_private.pem"
    return path.read_text() if path.exists() else None


def seed_releases(api: Api, who: dict, artifacts: Path, log) -> None:
    """Register a baseline and a candidate, attest both, activate the baseline and start a 10% canary."""
    admin, reviewer = who.get("admin"), who.get("reviewer")
    if not admin or not reviewer:
        return log("releases  skipped: needs an admin and an independent reviewer in the release tenant")
    status, listing = api.call(admin, "GET", "/v1/models")
    if status != 200:
        return log(f"releases  skipped: {admin} is not a release operator ({status})")
    if listing["models"]:
        return log("releases  skipped: models already registered")
    key = _signing_key()
    if not key:
        return log("releases  skipped: no CI signing key (set CAPSTONE_ATTESTATION_PRIVATE_KEY or run release_evidence keygen)")
    from .attestation import sign
    from .rag_benchmark import run_benchmark
    from .release_evidence import artifact_sha256, build_payload
    model = os.getenv("CAPSTONE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    try:
        benchmark = run_benchmark(models=[model])
    except Exception:
        return log(f"releases  skipped: benchmark model {model} unavailable (uv sync --extra semantic)")
    artifacts.mkdir(parents=True, exist_ok=True)
    evidence = {}
    for version in ("seed-baseline-v1", "seed-candidate-v2"):
        path = artifacts / f"{version}.safetensors"
        path.write_bytes(f"synthetic seed weights for {version}; not a real model".encode())
        status, _ = api.call(admin, "POST", "/v1/models/register",
                             {"version": version, "sha256": artifact_sha256(path)[0], "artifact_path": path.name})
        envelope = sign(build_payload(version, path, attester="seed-ci", benchmark=benchmark, embedding_model=model), key)
        code, body = api.call(admin, "POST", "/v1/models/evidence", envelope)
        evidence[version] = body.get("evidence_id") if code == 201 else None
        log(f"release   {version}: registered {status}, evidence {code} {body.get('decision', body)}")
    if not all(evidence.values()):
        return log("releases  stopped: evidence was rejected (does CAPSTONE_ATTESTATION_KEYS trust this signing key?)")
    code, body = api.call(reviewer, "POST", "/v1/releases/bootstrap",
                          {"version": "seed-baseline-v1", "evaluation_id": evidence["seed-baseline-v1"]})
    log(f"release   baseline activated by {reviewer}: {code} {body}")
    code, body = api.call(reviewer, "POST", "/v1/releases/canary",
                          {"version": "seed-candidate-v2", "evaluation_id": evidence["seed-candidate-v2"], "percent": 10})
    log(f"release   canary started by {reviewer}: {code} {body}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed a running API with synthetic demo data")
    parser.add_argument("--base", default=f"http://127.0.0.1:{os.getenv('CAPSTONE_PORT', '8000')}")
    parser.add_argument("--no-releases", action="store_true", help="skip model registration and canary")
    parser.add_argument("--artifacts", default=str(ROOT / "models"),
                        help="host directory the API serves as CAPSTONE_ARTIFACT_ROOT (Compose mounts ./models)")
    parser.add_argument("--wait", type=float, default=30, help="seconds to wait for the worker to finish research jobs")
    args = parser.parse_args(argv)
    tokens = {v["user"]: k for k, v in json.loads(os.getenv("CAPSTONE_TOKENS_JSON", "{}")).items()}
    if not tokens:
        parser.error("Set CAPSTONE_TOKENS_JSON (set -a; source .env; set +a)")
    api = Api(args.base, tokens)
    who = pick_identities(json.loads(os.environ["CAPSTONE_TOKENS_JSON"]), os.getenv("CAPSTONE_RELEASE_TENANT"))
    print(f"Seeding {args.base} as {who}")
    try:
        urllib.request.urlopen(args.base + "/health/live", timeout=5)
    except Exception:
        parser.error(f"No API at {args.base}. Start it, or pass --base (Compose uses CAPSTONE_PORT).")
    log = lambda line: print("  " + line)
    seed_documents(api, who, log)
    seed_jobs(api, who, log, args.wait)
    seed_incidents(api, who, log)
    if not args.no_releases:
        seed_releases(api, who, Path(args.artifacts), log)
    print("Done. Sign in to the web UI to explore.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
