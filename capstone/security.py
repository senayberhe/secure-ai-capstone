"""Verified identities and fail-closed document permissions."""
import hmac
from dataclasses import dataclass
from fastapi import HTTPException


@dataclass(frozen=True)
class Principal:
    tenant: str
    user: str
    groups: frozenset[str]
    role: str


def authenticate(header: str | None, tokens: dict) -> Principal:
    if not header or not header.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    # compare_digest raises TypeError on non-ASCII str, so compare bytes.
    secret = header[7:].encode()
    record = None
    for key, value in tokens.items():
        if hmac.compare_digest(secret, key.encode()):
            record = value
    if record is None:
        raise HTTPException(401, "Authentication required")
    return Principal(
        tenant=record["tenant"], user=record["user"],
        groups=frozenset(record.get("groups", [])), role=record.get("role", "reader")
    )


def require_role(principal: Principal, *roles):
    if principal.role not in roles:
        raise HTTPException(403, "Permission denied")


def require_release_operator(principal: Principal, release_tenant: str | None):
    """Model releases are platform-wide, so only the configured operator tenant may change them."""
    if not release_tenant or principal.tenant != release_tenant:
        raise HTTPException(403, "Release operations not permitted for this tenant")


def can_review(principal: Principal, tenant: str, groups) -> bool:
    return (principal.role in {"reviewer", "admin"} and principal.tenant == tenant
            and set(groups).issubset(principal.groups))


def authorize_document(principal: Principal, tenant: str, group: str):
    if principal.tenant != tenant or group not in principal.groups:
        raise HTTPException(404, "Document not found")


def sanitize_incident_summary(text: str) -> str:
    # Incident summaries are short operator-authored category descriptions, NOT private prompts.
    if not 1 <= len(text.strip()) <= 300 or any(ord(c) < 32 for c in text):
        raise ValueError("Invalid incident summary")
    return redact_pii(text)

def redact_pii(text: str) -> str:
    """Redact allowlisted PII with the configured local detector (see privacy.py)."""
    from .privacy import get_redactor
    return get_redactor().redact(text).text
