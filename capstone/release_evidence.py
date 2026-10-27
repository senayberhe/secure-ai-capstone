"""CI side of release evidence: hash the artifact, run the benchmark, apply gates, sign.

    python -m capstone.release_evidence keygen --out .secrets/attestation
    CAPSTONE_ATTESTATION_PRIVATE_KEY="$(cat key.pem)" \\
      python -m capstone.release_evidence build --version candidate-v2 --artifact model.safetensors --out evidence.json

The signed report is bound to one model version, one artifact digest (computed here from the
bytes, not taken from the registrar) and one benchmark dataset digest. The decision is computed
here from measured metrics; the API never accepts a decision it did not see signed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .attestation import generate_keypair, sign
from .llm import prompt_version

SCHEMA = "capstone.release-evidence/v1"
ARTIFACT_SUFFIXES = {".safetensors", ".onnx", ".gguf", ".bin"}
GATES = {"recall_at_3_min": 0.85, "mrr_min": 0.80, "abstention_rate_min": 0.40,
         "latency_ms_p95_max": 250.0, "leaks_max": 0}


def artifact_sha256(path: Path, max_bytes: int = 20 * 1024**3) -> tuple[str, int]:
    """Stream-hash model bytes without deserializing them."""
    path = Path(path)
    if path.suffix.lower() not in ARTIFACT_SUFFIXES:
        raise ValueError("Unapproved artifact extension")
    size = path.stat().st_size
    if not 0 < size <= max_bytes:
        raise ValueError("Invalid artifact size")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest(), size


def apply_gates(metrics: dict) -> list[str]:
    failed = []
    if metrics["recall_at_3"] < GATES["recall_at_3_min"]:
        failed.append("recall_at_3")
    if metrics["mrr"] < GATES["mrr_min"]:
        failed.append("mrr")
    if (metrics["abstention_rate"] or 0) < GATES["abstention_rate_min"]:
        failed.append("abstention_rate")
    if metrics["latency_ms_p95"] > GATES["latency_ms_p95_max"]:
        failed.append("latency_ms_p95")
    if metrics["cross_tenant_or_group_leaks"] > GATES["leaks_max"]:
        failed.append("cross_tenant_or_group_leaks")
    return failed


def _git_commit() -> str:
    if os.getenv("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def build_payload(version: str, artifact: Path, *, attester: str, benchmark: dict,
                  embedding_model: str, retrieval_mode: str = "hybrid") -> dict:
    sha, size = artifact_sha256(artifact)
    metrics = benchmark["results"][embedding_model][retrieval_mode]
    failed = apply_gates(metrics)
    return {"schema": SCHEMA, "version": version,
            "artifact": {"sha256": sha, "bytes": size, "name": Path(artifact).name},
            "dataset_sha256": benchmark["dataset_sha256"], "questions": benchmark["questions"],
            "prompt_sha256": prompt_version(),
            "embedding_model": embedding_model, "retrieval_mode": retrieval_mode,
            "metrics": metrics, "gates": GATES, "failed_gates": failed,
            "decision": "eligible" if not failed else "hold",
            "attester": attester, "git_commit": _git_commit(), "ci_run": os.getenv("GITHUB_RUN_ID", "local"),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build and sign version-bound release evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen")
    keygen.add_argument("--out", required=True)
    build = sub.add_parser("build")
    build.add_argument("--version", required=True)
    build.add_argument("--artifact", required=True)
    build.add_argument("--attester", default=os.getenv("GITHUB_WORKFLOW", "ci-evaluator"))
    build.add_argument("--embedding-model", default=os.getenv("CAPSTONE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"))
    build.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    if args.command == "keygen":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        private_pem, public_pem, kid = generate_keypair()
        private_file = out / "attestation_private.pem"
        private_file.write_text(private_pem)
        os.chmod(private_file, 0o600)
        (out / "attestation_public.pem").write_text(public_pem)
        print(json.dumps({"key_id": kid, "private_key": str(private_file),
                          "api_setting": {"CAPSTONE_ATTESTATION_KEYS": {kid: public_pem}}}, indent=2))
        return 0

    private_pem = os.environ.get("CAPSTONE_ATTESTATION_PRIVATE_KEY")
    if not private_pem:
        parser.error("Set CAPSTONE_ATTESTATION_PRIVATE_KEY (CI secret) to sign evidence")
    from .rag_benchmark import run_benchmark
    benchmark = run_benchmark(models=[args.embedding_model])
    payload = build_payload(args.version, Path(args.artifact), attester=args.attester,
                            benchmark=benchmark, embedding_model=args.embedding_model)
    Path(args.out).write_text(json.dumps(sign(payload, private_pem), indent=2) + "\n")
    print(json.dumps({k: payload[k] for k in ("version", "decision", "failed_gates")} | {"artifact_sha256": payload["artifact"]["sha256"]}))
    return 0 if payload["decision"] == "eligible" else 1


if __name__ == "__main__":
    raise SystemExit(main())
