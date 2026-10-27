"""End-to-end synthetic portfolio demonstration; no live LLM or private records."""
from pathlib import Path
from html import escape
import json
from fastapi.testclient import TestClient
from .api import create_app
from .config import Settings
from .db import ModelVersion
from .jobs import claim_job, finish_job
from .attestation import generate_keypair, sign
from .release_evidence import artifact_sha256, build_payload
import tempfile

DIR = Path(__file__).resolve().parent.parent / "artifacts"


def _benchmark():
    """Real benchmark with the approved MiniLM model when cached; otherwise a labeled synthetic stand-in."""
    from .rag_benchmark import run_benchmark
    model = "sentence-transformers/all-MiniLM-L6-v2"
    try:
        return run_benchmark(models=[model]), model
    except Exception:
        return ({"dataset_sha256": "0" * 64, "questions": 0, "results": {"synthetic": {"hybrid": {
            "recall_at_3": 0.9, "mrr": 0.9, "abstention_rate": 0.5, "latency_ms_p95": 10.0,
            "cross_tenant_or_group_leaks": 0, "recall_at_3_by_category": {}}}}}, "synthetic")


def run_demo():
    tokens = {
        "alice-demo-development-key-aaaaaaaaaaaa": {"tenant":"acme","user":"alice","groups":["engineering"],"role":"editor"},
        "reviewer-demo-development-key-bbbbbbbbbbbb": {"tenant":"acme","user":"rachel","groups":["engineering"],"role":"reviewer"},
        "admin-demo-development-key-cccccccccccc": {"tenant":"acme","user":"sam","groups":["engineering"],"role":"admin"},
        "outsider-demo-development-key-dddddddddd": {"tenant":"other","user":"bob","groups":["engineering"],"role":"editor"},
    }
    # Simulated CI attester: the API trusts only this public key; the private key never reaches it.
    ci_private, ci_public, ci_key = generate_keypair()
    store = tempfile.TemporaryDirectory()
    app = create_app(Settings("sqlite://", None, tokens, "local-demonstration-routing-secret-0123456789",
                              release_tenant="acme", attestation_keys={ci_key: ci_public}, artifact_root=store.name),
                     init_tables=True)
    client = TestClient(app)
    def hdr(identity):
        key = next(k for k,v in tokens.items() if v["user"] == identity)
        return {"Authorization":f"Bearer {key}"}
    upload = client.post("/v1/documents",headers=hdr("alice"),json={
        "title":"Travel policy","group":"engineering",
        "text":"Employees must submit travel receipts within thirty days. "
               "This policy is relevant to research assistants and authorized RAG tools."})
    assert upload.status_code == 201
    rag = client.post("/v1/rag/query",headers=hdr("alice"),json={"question":"travel receipts"}).json()
    denied = client.post("/v1/rag/query",headers=hdr("bob"),json={"question":"travel receipts"}).json()
    job = client.post("/v1/research/jobs",headers={**hdr("alice"),"Idempotency-Key":"final-capstone-job-0001"},json={"question":"travel receipts"}).json()
    with app.state.sessions() as session:
        claimed=claim_job(session)
    with app.state.sessions() as session:
        finished=finish_job(session,claimed)
    reviewed=client.post(f"/v1/research/jobs/{job['job_id']}/review",headers=hdr("rachel"),json={"approved":True}).json()
    with app.state.sessions.begin() as session:
        session.add(ModelVersion(version="baseline-v1",artifact_sha256="a"*64,registered_by="seed",status="active",traffic_pct=100))
    artifact = Path(store.name) / "candidate-v2.safetensors"
    artifact.write_bytes(b"synthetic demonstration weights, not a real model")
    registered=client.post("/v1/models/register",headers=hdr("sam"),json={
        "version":"candidate-v2","sha256":artifact_sha256(artifact)[0],"artifact_path":artifact.name}).json()
    benchmark, model = _benchmark()
    envelope = sign(build_payload("candidate-v2", artifact, attester="demo-ci", benchmark=benchmark,
                                  embedding_model=model), ci_private)
    evaluation=client.post("/v1/models/evidence",headers=hdr("sam"),json=envelope).json()
    canary=client.post("/v1/releases/canary",headers=hdr("rachel"),json={
        "version":"candidate-v2","evaluation_id":evaluation["evidence_id"],"percent":10}).json()
    observed=client.post("/v1/releases/observe",headers=hdr("sam"),json={
        "version":"candidate-v2","requests":100,"errors":12,"critical_events":0}).json()
    score_rows=[{"true":i%2, "pred":i%2, "group":("A" if i%4<2 else "B"),
                 "unsupported":False,"leak":False} for i in range(120)]
    quality=client.post("/v1/scorecards",headers=hdr("rachel"),json={"records":score_rows}).json()
    report={"dataset":"Synthetic classroom test; no real users or model inference",
            "rag_sources":len(rag["citations"]), "cross_tenant_sources":len(denied["citations"]),
            "research_completed":finished,"research_review":reviewed["status"],
            "candidate":registered["version"],"offline_gate":evaluation["decision"],
            "evidence":{"attester":evaluation["attester"],"artifact_sha256":evaluation["artifact_sha256"],
                        "benchmark_model":model,"benchmark":"measured" if model!="synthetic" else "synthetic stand-in"},
            "canary_pct":canary["traffic_pct"],"canary_errors":12,
            "canary_requests":100,"rollout_decision":observed["decision"],
            "scorecard":quality}
    DIR.mkdir(exist_ok=True)
    (DIR/"scorecard.json").write_text(json.dumps(report,indent=2)+"\n")
    cards = [
        ("Authorized RAG sources",str(report["rag_sources"])),
        ("Cross-tenant sources",str(report["cross_tenant_sources"])),
        ("Research review",report["research_review"]),
        ("Canary share",f"{report['canary_pct']}%"),
        ("Observed canary errors",f"{report['canary_errors']} / {report['canary_requests']}"),
        ("Rollback decision",report["rollout_decision"].upper()),
    ]
    cardhtml="\n".join(f'<article><div class="value">{escape(value)}</div><div class="name">{escape(name)}</div></article>' for name,value in cards)
    html=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Secure AI Platform — Synthetic Demonstration</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;line-height:1.55;background:#f8fafc;color:#0f172a;max-width:1040px;padding:44px 25px;margin:auto}}h1{{font-size:2.3rem;letter-spacing:-.04em}}.tag{{font-size:.75rem;letter-spacing:.13em;color:#475569;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin:30px 0}}article{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:22px;box-shadow:0 1px 4px #00000007}}.value{{font-size:1.8rem;font-weight:750}}.name{{color:#64748b;font-size:.88rem}}.alert{{padding:16px;border-left:4px solid #b45309;background:#fff7ed}}.footer{{color:#64748b;font-size:.85rem;margin-top:36px}}pre{{background:#e2e8f0;padding:18px;overflow:auto;border-radius:8px}}</style></head><body>
<div class="tag">AI ENGINEERING · SECURITY AND RELIABILITY</div><h1>Secure AI Platform</h1>
<p>Research, evidence access, independent approval, and model release controls — one integrated service.</p>
<div class="alert"><strong>Synthetic demonstration.</strong> These counts come from a deterministic offline test. They do not establish live-model quality or production security.</div>
<div class="grid">{cardhtml}</div>
<h2>What the example demonstrates</h2>
<p>Tenant-scoped retrieval excludes unauthorized evidence before ranking; a durable research job requires another actor to approve it; a candidate passes offline review but rolls back when 12% of its 100 canary requests fail.</p>
<h2>Release sequence</h2>
<pre>Register digest → CI-signed evidence → Independent review + artifact re-hash → 10% Canary → Monitor → Rollback</pre>
<p class="footer">Open the included scorecard.json for machine-readable details. See README.md and docs/DEPLOYMENT.md before connecting private data.</p></body></html>'''
    (DIR/"dashboard.html").write_text(html)
    return report


if __name__ == "__main__":
    print(json.dumps(run_demo(),indent=2))
