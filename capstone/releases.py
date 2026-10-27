"""Independent evaluation -> review -> sticky canary -> rollback.

Teaching thresholds, not generalized standards. Operational sample reports in demo are
submitted by admin; a production service must derive them from trusted telemetry.
"""
import hmac
import hashlib
import math
import re
from sqlalchemy import select
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .db import ModelVersion, Evaluation, Incident, Audit, ModelArtifact, ReleaseEvidence, ArtifactVerification, uid, now
from .attestation import AttestationError, verify as verify_signature
from .llm import prompt_version
import base64
from .security import Principal, require_role

VERSION = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,78}$")


ARTIFACT_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,250}$")


def register(session, actor: Principal, version: str, sha256: str, provider_model=None, artifact_path=None):
    require_role(actor, "admin")
    if not VERSION.fullmatch(version) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("Invalid version/digest")
    if provider_model is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{2,100}", provider_model):
        raise ValueError("Invalid provider model")
    if session.get(ModelVersion, version):
        raise ValueError("Model version already registered")
    if artifact_path is not None and (not ARTIFACT_PATH.fullmatch(artifact_path) or ".." in artifact_path.split("/")):
        raise ValueError("Invalid artifact path")
    row = ModelVersion(version=version, artifact_sha256=sha256, provider_model=provider_model,
                       registered_by=actor.user, status="registered", traffic_pct=0)
    session.add(row)
    if artifact_path is not None:
        session.flush()
        session.add(ModelArtifact(version=version, path=artifact_path))
    session.commit()
    return row


def evaluate(session, actor: Principal, version: str, samples: int, accuracy: float,
             unsupported_rate: float, leakage_count: int, p95_ms: float):
    require_role(actor, "admin", "reviewer")
    if session.get(ModelVersion, version) is None:
        raise ValueError("Model not registered")
    if samples < 1 or leakage_count < 0 or not 0 <= accuracy <= 1 or not 0 <= unsupported_rate <= 1 or not math.isfinite(p95_ms) or p95_ms < 0:
        raise ValueError("Invalid evaluation metrics")
    passed = (samples >= 100 and accuracy >= 0.80 and
              unsupported_rate <= 0.05 and leakage_count == 0 and p95_ms <= 1500)
    row = Evaluation(id=uid(), model_version=version, samples=samples,
                     accuracy=accuracy, unsupported_rate=unsupported_rate,
                     leakage_count=leakage_count, p95_ms=p95_ms,
                     decision="eligible" if passed else "hold")
    session.add(row)
    session.commit()
    return row


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)  # SQLite drops tz info


def submit_evidence(session, actor: Principal, envelope: dict, trusted_keys: dict, max_age_hours: int):
    """Store a CI-signed, version-bound evaluation report after verifying it. Idempotent per payload."""
    require_role(actor, "admin", "reviewer")
    try:
        payload, payload_sha = verify_signature(envelope, trusted_keys)
    except AttestationError:
        raise PermissionError("Untrusted or tampered evidence")
    existing = session.scalar(select(ReleaseEvidence).where(ReleaseEvidence.payload_sha256 == payload_sha))
    if existing:
        return existing
    try:
        version, digest = payload["version"], payload["artifact"]["sha256"]
        attested_at = datetime.fromisoformat(payload["created_at"])
        decision, attester, dataset = payload["decision"], payload["attester"], payload["dataset_sha256"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("Malformed evidence payload")
    if payload.get("schema") != "capstone.release-evidence/v1" or decision not in ("eligible", "hold"):
        raise ValueError("Unsupported evidence schema")
    if payload.get("prompt_sha256") != prompt_version():
        raise ValueError("Evidence was produced for a different prompt version")
    candidate = session.get(ModelVersion, version)
    if candidate is None:
        raise ValueError("Evidence names an unregistered version")
    if not hmac.compare_digest(digest, candidate.artifact_sha256):
        raise ValueError("Evidence artifact digest does not match the registered digest")
    if attester == candidate.registered_by:
        raise PermissionError("Evidence must be attested independently of the registrar")
    if now() - _aware(attested_at) > timedelta(hours=max_age_hours):
        raise ValueError("Evidence is too old; re-run the CI evaluation")
    row = ReleaseEvidence(id=uid(), model_version=version, payload_sha256=payload_sha, key_id=envelope["key_id"],
                          attester=attester, artifact_sha256=digest, dataset_sha256=dataset, decision=decision,
                          envelope=json.dumps(envelope), attested_at=_aware(attested_at), submitted_by=actor.user)
    session.add(row)
    session.add(Audit(id=uid(), tenant=actor.tenant, action="evidence_submitted", actor=actor.user))
    session.commit()
    return row


def _verify_artifact_bytes(session, verifier: Principal, candidate: ModelVersion, artifact_root: str | None) -> None:
    """Independently re-hash the stored artifact bytes now and record the result."""
    from .release_evidence import artifact_sha256
    location = session.get(ModelArtifact, candidate.version)
    if not artifact_root or location is None:
        raise ValueError("No approved artifact location to verify")
    root = Path(artifact_root).resolve(strict=True)
    target = (root / location.path).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("Artifact outside the approved root")
    digest, _ = artifact_sha256(target)
    matched = hmac.compare_digest(digest, candidate.artifact_sha256)
    session.add(ArtifactVerification(id=uid(), model_version=candidate.version, sha256=digest,
                                     matched=matched, verified_by=verifier.user))
    session.commit()
    if not matched:
        raise ValueError("Artifact bytes do not match the registered digest")


def _require_release_evidence(session, reviewer: Principal, candidate, evidence_id: str,
                              artifact_root: str | None, max_age_hours: int) -> None:
    """Gate shared by canary and first-baseline activation."""
    evidence = session.get(ReleaseEvidence, evidence_id)
    if (not candidate or candidate.status != "registered" or not evidence
            or evidence.model_version != candidate.version or evidence.decision != "eligible"):
        raise ValueError("Candidate lacks eligible attested evidence")
    if candidate.registered_by == reviewer.user:
        raise PermissionError("Independent review required")
    if not hmac.compare_digest(evidence.artifact_sha256, candidate.artifact_sha256):
        raise ValueError("Evidence is bound to a different artifact")
    if now() - _aware(evidence.attested_at) > timedelta(hours=max_age_hours):
        raise ValueError("Evidence expired")
    # Re-checked here too: the deployed prompt may have changed since the evidence was accepted.
    signed = json.loads(base64.b64decode(json.loads(evidence.envelope)["payload"]))
    if signed.get("prompt_sha256") != prompt_version():
        raise ValueError("Evidence was produced for a different prompt version")
    _verify_artifact_bytes(session, reviewer, candidate, artifact_root)


def begin_canary(session, reviewer: Principal, version: str, evaluation_id: str, percent: int = 10,
                 *, artifact_root: str | None = None, max_age_hours: int = 168):
    """`evaluation_id` is a ReleaseEvidence id: operator-typed evaluations cannot start a canary."""
    require_role(reviewer, "reviewer", "admin")
    if percent not in (5, 10, 25, 50):
        raise ValueError("Unsupported canary percentage")
    candidate = session.get(ModelVersion, version)
    _require_release_evidence(session, reviewer, candidate, evaluation_id, artifact_root, max_age_hours)
    existing = session.scalar(select(ModelVersion).where(ModelVersion.status == "canary"))
    if existing:
        raise ValueError("Only one canary at a time")
    baseline = session.scalar(select(ModelVersion).where(ModelVersion.status == "active"))
    if baseline is None:
        raise ValueError("No active baseline")
    candidate.status = "canary"
    candidate.traffic_pct = percent
    candidate.baseline = baseline.version
    candidate.reviewed_by = reviewer.user
    session.add(Audit(id=uid(), tenant=reviewer.tenant, action="canary_started", actor=reviewer.user))
    session.commit()
    return candidate


def stable_model(session, identity: str, secret: str) -> str | None:
    baseline = session.scalar(select(ModelVersion).where(ModelVersion.status == "active"))
    candidate = session.scalar(select(ModelVersion).where(ModelVersion.status == "canary"))
    if not baseline:
        return None
    if not candidate:
        return baseline.version
    digest = hmac.new(secret.encode(), identity.encode(), hashlib.sha256).digest()
    bucket = int.from_bytes(digest[:8], "big") % 10000
    return candidate.version if bucket < candidate.traffic_pct * 100 else baseline.version


def canary_observation(session, actor: Principal, version: str, requests: int,
                       errors: int, critical_events: int):
    require_role(actor, "admin")
    row = session.get(ModelVersion, version)
    if not row or row.status != "canary":
        raise ValueError("No matching canary")
    if requests < 100 or errors < 0 or errors > requests or critical_events < 0:
        raise ValueError("Insufficient/invalid canary telemetry")
    observed = errors / requests
    row.observed_requests += requests
    row.observed_errors += errors
    row.observed_critical += critical_events
    if observed > 0.05 or critical_events:
        rollback(session, actor, version, reason="canary_gate_failed")
        return {"decision": "rolled_back", "error_rate": round(observed, 4)}
    session.commit()
    return {"decision": "continue", "error_rate": round(observed, 4)}


def rollback(session, actor: Principal, version: str, reason="manual_rollback"):
    require_role(actor, "admin")
    row = session.get(ModelVersion, version)
    if not row or row.status != "canary":
        raise ValueError("Only canary versions may be rolled back")
    row.status = "rolled_back"
    row.traffic_pct = 0
    # Filed under the operator's tenant so the same release operators can contain and resolve it.
    session.add(Incident(id=uid(), tenant=actor.tenant, category="release_rollback",
                         summary=reason, opened_by=actor.user, status="open"))
    session.add(Audit(id=uid(), tenant=actor.tenant, action="rolled_back", actor=actor.user))
    session.commit()
    return row


def promote_canary(session, actor: Principal, version: str):
    """Promotion requires enough healthy canary traffic and independent approval."""
    require_role(actor, "admin", "reviewer")
    candidate = session.get(ModelVersion, version)
    baseline = session.scalar(select(ModelVersion).where(ModelVersion.status == "active"))
    if not candidate or candidate.status != "canary" or not baseline:
        raise ValueError("No eligible canary")
    if actor.user == candidate.registered_by:
        raise PermissionError("Independent promoter required")
    if candidate.observed_requests < 100 or candidate.observed_errors / candidate.observed_requests > .05 or candidate.observed_critical:
        raise ValueError("Canary evidence insufficient or unsafe")
    baseline.status = "retired"
    baseline.traffic_pct = 0
    candidate.status = "active"
    candidate.traffic_pct = 100
    session.add(Audit(id=uid(), tenant=actor.tenant, action="canary_promoted", actor=actor.user))
    session.commit()
    return candidate


def activate_baseline(session, reviewer: Principal, version: str, evaluation_id: str,
                      *, artifact_root: str | None = None, max_age_hours: int = 168):
    """One-time reviewed initial activation, held to the same attested-evidence gate as a canary."""
    require_role(reviewer, "reviewer", "admin")
    candidate = session.get(ModelVersion, version)
    if session.scalar(select(ModelVersion).where(ModelVersion.status == "active")):
        raise ValueError("Active baseline already exists")
    _require_release_evidence(session, reviewer, candidate, evaluation_id, artifact_root, max_age_hours)
    candidate.status = "active"
    candidate.traffic_pct = 100
    candidate.reviewed_by = reviewer.user
    session.add(Audit(id=uid(), tenant=reviewer.tenant, actor=reviewer.user, action="baseline_activated"))
    session.commit()
    return candidate
